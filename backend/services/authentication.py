"""Authentication primitives for vendor OTP and NMC officer sessions.

Phone numbers, OTPs, passwords, and session tokens are never persisted in
plaintext. This module intentionally does not depend on recommendation logic.
"""
from __future__ import annotations
from backend.core.data_paths import dataset_path

import csv
import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy import text

from backend.core.config import get_auth_settings
from backend.repositories.database import DATABASE_PATH, connect, transaction, utc_now


ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_VENDOR_SOURCE = dataset_path("historical_vendors")
SESSION_COOKIE_NAME = "nmc_session"
OTP_EXPIRY_MINUTES = 5
OTP_MAX_ATTEMPTS = 5
OTP_COOLDOWN_SECONDS = 45
SESSION_EXPIRY_HOURS = 12


class AuthError(ValueError):
    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


def _secret() -> bytes:
    return get_auth_settings().auth_hmac_secret.encode("utf-8")


def normalize_indian_mobile(value: str) -> str:
    """Normalize conservative Indian mobile formats to ten digits."""
    raw = (value or "").strip()
    if not raw:
        raise AuthError("Enter a valid Indian mobile number.", 422)
    if re.search(r"[A-Za-z]", raw):
        raise AuthError("Enter a valid Indian mobile number.", 422)
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not re.fullmatch(r"[6-9][0-9]{9}", digits):
        raise AuthError("Enter a valid Indian mobile number.", 422)
    return digits


def phone_hmac(normalized_mobile: str) -> str:
    return hmac.new(_secret(), f"phone:{normalized_mobile}".encode(), hashlib.sha256).hexdigest()


def _otp_hash(challenge_id: str, otp: str) -> str:
    return hmac.new(_secret(), f"otp:{challenge_id}:{otp}".encode(), hashlib.sha256).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def seed_historical_vendor_identities(
    database_path: str | Path = DATABASE_PATH,
    source_path: str | Path = HISTORICAL_VENDOR_SOURCE,
) -> dict[str, int | str | bool]:
    """Idempotently seed only unambiguous HMAC-to-vendor mappings."""
    source_path = Path(source_path)
    if not source_path.is_file():
        connection = connect(database_path)
        try:
            identity_rows = connection.execute(
                "SELECT COUNT(*) AS identity_count FROM vendor_auth_identities"
            ).fetchone()["identity_count"]
        finally:
            connection.close()
        return {
            "source": str(source_path),
            "source_available": False,
            "rows": 0,
            "valid_rows": 0,
            "missing_rows": 0,
            "invalid_nonempty_values": 0,
            "distinct_valid_numbers": 0,
            "ambiguous_numbers": 0,
            "ambiguous_vendor_ids": 0,
            "seedable_numbers": 0,
            "inserted": 0,
            "already_existing": 0,
            "identity_rows": identity_rows,
        }
    with source_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        required = {"vendor_id", "phone_number", "alternate_phone"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"Historical vendor source must contain {sorted(required)}")
        rows = list(reader)

    number_to_vendors: dict[str, set[str]] = {}
    valid_rows = 0
    missing_rows = 0
    invalid_nonempty = 0
    for row in rows:
        row_numbers: set[str] = set()
        for column in ("phone_number", "alternate_phone"):
            raw = (row.get(column) or "").strip()
            if not raw:
                continue
            try:
                row_numbers.add(normalize_indian_mobile(raw))
            except AuthError:
                invalid_nonempty += 1
        if row_numbers:
            valid_rows += 1
            for number in row_numbers:
                number_to_vendors.setdefault(number, set()).add(row["vendor_id"])
        else:
            missing_rows += 1

    ambiguous = {number for number, vendors in number_to_vendors.items() if len(vendors) > 1}
    now = utc_now()
    seedable_numbers = len(number_to_vendors) - len(ambiguous)
    with transaction(database_path, immediate=True) as connection:
        existing_hashes = {
            row["phone_hash"]
            for row in connection.execute(
                "SELECT phone_hash FROM vendor_auth_identities"
            ).fetchall()
        }
        pending_rows = []
        for number, vendors in number_to_vendors.items():
            if number in ambiguous:
                continue
            vendor_id = next(iter(vendors))
            hashed_phone = phone_hmac(number)
            if hashed_phone not in existing_hashes:
                pending_rows.append({
                    "identity_id": f"VID_{uuid.uuid4().hex}",
                    "vendor_id": vendor_id,
                    "phone_hash": hashed_phone,
                    "created_at": now,
                    "updated_at": now,
                })
        inserted = 0
        if pending_rows:
            cursor = connection.execute(
                text(
                    """INSERT INTO vendor_auth_identities
                       (identity_id, vendor_id, phone_hash, vendor_source, is_verified, created_at, updated_at)
                       VALUES (:identity_id, :vendor_id, :phone_hash, 'HISTORICAL', 0,
                               :created_at, :updated_at)
                       ON CONFLICT DO NOTHING"""
                ),
                pending_rows,
            )
            inserted = max(cursor.rowcount, 0)
        already_existing = seedable_numbers - inserted
        identity_rows = connection.execute(
            "SELECT COUNT(*) AS identity_count FROM vendor_auth_identities"
        ).fetchone()["identity_count"]
    return {
        "source": str(source_path),
        "source_available": True,
        "rows": len(rows),
        "valid_rows": valid_rows,
        "missing_rows": missing_rows,
        "invalid_nonempty_values": invalid_nonempty,
        "distinct_valid_numbers": len(number_to_vendors),
        "ambiguous_numbers": len(ambiguous),
        "ambiguous_vendor_ids": sum(len(number_to_vendors[number]) for number in ambiguous),
        "seedable_numbers": seedable_numbers,
        "inserted": inserted,
        "already_existing": already_existing,
        "identity_rows": identity_rows,
    }


class OtpProvider(Protocol):
    def send_otp(self, normalized_mobile: str, otp_code: str) -> None: ...


class DevelopmentOtpProvider:
    """Development-only provider; never includes the OTP in API responses."""

    def send_otp(self, normalized_mobile: str, otp_code: str) -> None:
        settings = get_auth_settings()
        if settings.app_env == "development" and settings.otp_debug:
            print(f"DEVELOPMENT OTP: {otp_code}")


def get_otp_provider() -> OtpProvider:
    settings = get_auth_settings()
    provider = settings.otp_provider
    if provider == "development":
        if settings.app_env == "production":
            raise RuntimeError("Development OTP provider cannot deliver production OTPs")
        return DevelopmentOtpProvider()
    raise RuntimeError(f"Unsupported OTP_PROVIDER: {provider}")


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def create_otp_challenge(
    mobile: str,
    database_path: str | Path = DATABASE_PATH,
    provider: OtpProvider | None = None,
) -> dict[str, str]:
    normalized = normalize_indian_mobile(mobile)
    delivery = provider or get_otp_provider()
    hashed_phone = phone_hmac(normalized)
    now_dt = datetime.now(timezone.utc)
    now = _timestamp(now_dt)
    with transaction(database_path, immediate=True) as connection:
        recent = connection.execute(
            """SELECT created_at FROM auth_otp_challenges
               WHERE phone_hash = ? AND consumed_at IS NULL
               ORDER BY created_at DESC LIMIT 1""",
            (hashed_phone,),
        ).fetchone()
        if recent and datetime.fromisoformat(recent["created_at"]) > now_dt - timedelta(seconds=OTP_COOLDOWN_SECONDS):
            raise AuthError("Please wait before requesting another OTP.", 429)
        identity = connection.execute(
            "SELECT vendor_id FROM vendor_auth_identities WHERE phone_hash = ?",
            (hashed_phone,),
        ).fetchone()
        purpose = "VENDOR_LOGIN" if identity else "NEW_VENDOR_REGISTRATION"
        challenge_id = f"OTP_{uuid.uuid4().hex}"
        otp = generate_otp()
        connection.execute(
            """INSERT INTO auth_otp_challenges
               (challenge_id, phone_hash, mobile_last4, purpose, otp_hash, expires_at,
                attempt_count, max_attempts, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (
                challenge_id,
                hashed_phone,
                normalized[-4:],
                purpose,
                _otp_hash(challenge_id, otp),
                _timestamp(now_dt + timedelta(minutes=OTP_EXPIRY_MINUTES)),
                OTP_MAX_ATTEMPTS,
                now,
            ),
        )
    delivery.send_otp(normalized, otp)
    return {"challenge_id": challenge_id, "next_step": "VERIFY_OTP"}


def _create_session(
    connection,
    role: str,
    *,
    vendor_id: str | None = None,
    admin_id: str | None = None,
    new_vendor_phone_hash: str | None = None,
    mobile_last4: str | None = None,
) -> tuple[str, dict]:
    token = secrets.token_urlsafe(32)
    session_id = f"SES_{uuid.uuid4().hex}"
    now_dt = datetime.now(timezone.utc)
    connection.execute(
        """INSERT INTO auth_sessions
           (session_id, session_token_hash, role, vendor_id, admin_id,
            new_vendor_phone_hash, mobile_last4, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            _token_hash(token),
            role,
            vendor_id,
            admin_id,
            new_vendor_phone_hash,
            mobile_last4,
            _timestamp(now_dt),
            _timestamp(now_dt + timedelta(hours=SESSION_EXPIRY_HOURS)),
        ),
    )
    row = connection.execute("SELECT * FROM auth_sessions WHERE session_id = ?", (session_id,)).fetchone()
    return token, dict(row)


def verify_otp_challenge(
    challenge_id: str,
    otp: str,
    database_path: str | Path = DATABASE_PATH,
) -> tuple[str, dict]:
    now_dt = datetime.now(timezone.utc)
    now = _timestamp(now_dt)
    with transaction(database_path, immediate=True) as connection:
        challenge = connection.execute(
            "SELECT * FROM auth_otp_challenges WHERE challenge_id = ?", (challenge_id,)
        ).fetchone()
        if challenge is None:
            raise AuthError("Invalid or expired OTP challenge.")
        if challenge["consumed_at"] is not None:
            raise AuthError("This OTP has already been used.")
        if datetime.fromisoformat(challenge["expires_at"]) <= now_dt:
            raise AuthError("This OTP has expired.")
        if challenge["attempt_count"] >= challenge["max_attempts"]:
            raise AuthError("Maximum OTP attempts exceeded.", 429)
        if not hmac.compare_digest(challenge["otp_hash"], _otp_hash(challenge_id, otp.strip())):
            connection.execute(
                "UPDATE auth_otp_challenges SET attempt_count = attempt_count + 1 WHERE challenge_id = ?",
                (challenge_id,),
            )
            # Failed attempts are security state and must survive the raised error.
            connection.commit()
            if challenge["attempt_count"] + 1 >= challenge["max_attempts"]:
                raise AuthError("Maximum OTP attempts exceeded.", 429)
            raise AuthError("Incorrect OTP.")
        connection.execute(
            "UPDATE auth_otp_challenges SET consumed_at = ? WHERE challenge_id = ?", (now, challenge_id)
        )
        identity = connection.execute(
            "SELECT * FROM vendor_auth_identities WHERE phone_hash = ?", (challenge["phone_hash"],)
        ).fetchone()
        if identity:
            connection.execute(
                """UPDATE vendor_auth_identities
                   SET is_verified = 1, mobile_last4 = ?, updated_at = ?
                   WHERE identity_id = ?""",
                (challenge["mobile_last4"], now, identity["identity_id"]),
            )
            return _create_session(connection, "VENDOR", vendor_id=identity["vendor_id"],
                                   mobile_last4=challenge["mobile_last4"])
        return _create_session(
            connection,
            "NEW_VENDOR_ONBOARDING",
            new_vendor_phone_hash=challenge["phone_hash"],
            mobile_last4=challenge["mobile_last4"],
        )


def create_admin_session(
    username: str,
    password: str,
    database_path: str | Path = DATABASE_PATH,
) -> tuple[str, dict]:
    settings = get_auth_settings()
    expected_user = settings.nmc_admin_username
    expected_password = settings.nmc_admin_password
    valid = hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_password)
    if not valid:
        raise AuthError("Invalid officer credentials.")
    with transaction(database_path, immediate=True) as connection:
        return _create_session(connection, "ADMIN", admin_id=username)


def get_session(token: str | None, database_path: str | Path = DATABASE_PATH) -> dict | None:
    if not token:
        return None
    connection = connect(database_path)
    try:
        row = connection.execute(
            """SELECT * FROM auth_sessions
               WHERE session_token_hash = ? AND revoked_at IS NULL""",
            (_token_hash(token),),
        ).fetchone()
        if row is None or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            return None
        return dict(row)
    finally:
        connection.close()


def revoke_session(token: str | None, database_path: str | Path = DATABASE_PATH) -> None:
    if not token:
        return
    with transaction(database_path, immediate=True) as connection:
        connection.execute(
            "UPDATE auth_sessions SET revoked_at = ? WHERE session_token_hash = ? AND revoked_at IS NULL",
            (utc_now(), _token_hash(token)),
        )


def bind_onboarding_session(
    session_id: str,
    vendor_id: str,
    database_path: str | Path = DATABASE_PATH,
) -> None:
    now = utc_now()
    with transaction(database_path, immediate=True) as connection:
        session = connection.execute(
            "SELECT * FROM auth_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if session is None or session["role"] != "NEW_VENDOR_ONBOARDING":
            raise AuthError("A verified onboarding session is required.", 403)
        connection.execute(
            """INSERT INTO vendor_auth_identities
               (identity_id, vendor_id, phone_hash, mobile_last4, vendor_source, is_verified, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'OPERATIONAL', 1, ?, ?)""",
            (f"VID_{uuid.uuid4().hex}", vendor_id, session["new_vendor_phone_hash"],
             session.get("mobile_last4"), now, now),
        )
        connection.execute(
            """UPDATE auth_sessions
               SET role = 'VENDOR', vendor_id = ?, new_vendor_phone_hash = NULL
               WHERE session_id = ?""",
            (vendor_id, session_id),
        )


def cookie_secure() -> bool:
    return get_auth_settings().session_cookie_secure


def find_vendor_by_phone_hash(
    hashed_phone: str,
    database_path: str | Path = DATABASE_PATH,
) -> str | None:
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT vendor_id FROM vendor_auth_identities WHERE phone_hash = ?",
            (hashed_phone,),
        ).fetchone()
        return row["vendor_id"] if row is not None else None
    finally:
        connection.close()


def bind_verified_phone_hash(
    hashed_phone: str,
    vendor_id: str,
    database_path: str | Path = DATABASE_PATH,
) -> None:
    """Bind a WhatsApp-verified sender without ever persisting the plaintext phone."""
    now = utc_now()
    with transaction(database_path, immediate=True) as connection:
        existing = connection.execute(
            "SELECT vendor_id FROM vendor_auth_identities WHERE phone_hash = ?",
            (hashed_phone,),
        ).fetchone()
        if existing is not None:
            if existing["vendor_id"] != vendor_id:
                raise AuthError("This verified mobile identity is already linked.", 409)
            return
        connection.execute(
            """INSERT INTO vendor_auth_identities
               (identity_id, vendor_id, phone_hash, vendor_source, is_verified, created_at, updated_at)
               VALUES (?, ?, ?, 'OPERATIONAL', 1, ?, ?)""",
            (f"VID_{uuid.uuid4().hex}", vendor_id, hashed_phone, now, now),
        )
