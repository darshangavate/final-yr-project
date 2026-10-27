"""Project-root environment loading and validated authentication settings."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

# Explicit process environment values win over values in the project .env file.
load_dotenv(dotenv_path=ENV_FILE, override=False)


class ConfigurationError(RuntimeError):
    """Raised when required application configuration is absent or invalid."""


@dataclass(frozen=True)
class GeminiSettings:
    """Server-only settings for optional recommendation explanations."""

    enabled: bool
    api_key: str | None
    model: str
    timeout_seconds: float
    cache_size: int
    requests_per_minute: int


def _optional_boolean(name: str, default: str = "false") -> bool:
    value = os.environ.get(name, default).strip().lower()
    if value not in {"true", "false"}:
        raise ConfigurationError(f"Configuration variable {name} must be true or false")
    return value == "true"


def get_gemini_settings() -> GeminiSettings:
    """Validate optional Gemini configuration without revealing credentials."""
    enabled = _optional_boolean("GEMINI_EXPLANATIONS_ENABLED")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip() or None
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
    if not model or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise ConfigurationError("GEMINI_MODEL is invalid")
    try:
        timeout_seconds = float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "15"))
        cache_size = int(os.environ.get("GEMINI_EXPLANATION_CACHE_SIZE", "128"))
        requests_per_minute = int(os.environ.get("GEMINI_EXPLANATION_RATE_LIMIT", "6"))
    except ValueError as exc:
        raise ConfigurationError("Gemini numeric configuration is invalid") from exc
    if not 1 <= timeout_seconds <= 60:
        raise ConfigurationError("GEMINI_TIMEOUT_SECONDS must be between 1 and 60")
    if not 1 <= cache_size <= 1000:
        raise ConfigurationError("GEMINI_EXPLANATION_CACHE_SIZE must be between 1 and 1000")
    if not 1 <= requests_per_minute <= 60:
        raise ConfigurationError("GEMINI_EXPLANATION_RATE_LIMIT must be between 1 and 60")
    return GeminiSettings(
        enabled=enabled,
        api_key=api_key,
        model=model,
        # Gemini rejects manually supplied deadlines below ten seconds. Keep a
        # safe provider-compatible floor while retaining a bounded timeout.
        timeout_seconds=max(10.0, timeout_seconds),
        cache_size=cache_size,
        requests_per_minute=requests_per_minute,
    )


def get_database_url() -> str | None:
    """Return an optional operational database URL without logging it."""
    value = os.environ.get("DATABASE_URL", "").strip()
    return value or None


def get_database_backend() -> str:
    """Describe the configured backend using a non-sensitive value."""
    url = get_database_url()
    if not url:
        return "sqlite"
    if url.startswith("sqlite:"):
        return "sqlite"
    if url.startswith(("postgres:", "postgresql:", "postgresql+psycopg:")):
        return "postgresql"
    raise ConfigurationError("DATABASE_URL must use SQLite or PostgreSQL")


def allow_destructive_db_reset() -> bool:
    """Destructive database operations are denied unless explicitly enabled."""
    value = os.environ.get("ALLOW_DESTRUCTIVE_DB_RESET", "false").strip().lower()
    if value not in {"true", "false"}:
        raise ConfigurationError(
            "Configuration variable ALLOW_DESTRUCTIVE_DB_RESET must be true or false"
        )
    return value == "true"


@dataclass(frozen=True)
class AuthSettings:
    app_env: str
    auth_hmac_secret: str
    session_cookie_secure: bool
    otp_provider: str
    otp_debug: bool
    nmc_admin_username: str
    nmc_admin_password: str


_REQUIRED_AUTH_VARIABLES = (
    "APP_ENV",
    "AUTH_HMAC_SECRET",
    "SESSION_COOKIE_SECURE",
    "OTP_PROVIDER",
    "OTP_DEBUG",
    "NMC_ADMIN_USERNAME",
    "NMC_ADMIN_PASSWORD",
)


def _required_value(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise ConfigurationError(f"Required configuration variable is missing: {name}")
    return value


def _boolean_value(name: str) -> bool:
    value = _required_value(name).strip().lower()
    if value not in {"true", "false"}:
        raise ConfigurationError(f"Configuration variable {name} must be true or false")
    return value == "true"


def get_auth_settings() -> AuthSettings:
    """Read current environment values and fail without disclosing their contents."""
    missing = [name for name in _REQUIRED_AUTH_VARIABLES if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigurationError(
            "Missing required authentication configuration: " + ", ".join(missing)
        )
    settings = AuthSettings(
        app_env=_required_value("APP_ENV").strip().lower(),
        auth_hmac_secret=_required_value("AUTH_HMAC_SECRET"),
        session_cookie_secure=_boolean_value("SESSION_COOKIE_SECURE"),
        otp_provider=_required_value("OTP_PROVIDER").strip().lower(),
        otp_debug=_boolean_value("OTP_DEBUG"),
        nmc_admin_username=_required_value("NMC_ADMIN_USERNAME"),
        nmc_admin_password=_required_value("NMC_ADMIN_PASSWORD"),
    )
    if settings.app_env == "production":
        if not settings.session_cookie_secure:
            raise ConfigurationError("Production requires SESSION_COOKIE_SECURE=true")
        if settings.otp_debug:
            raise ConfigurationError("Production requires OTP_DEBUG=false")
        if allow_destructive_db_reset():
            raise ConfigurationError("Production requires ALLOW_DESTRUCTIVE_DB_RESET=false")
    return settings


def validate_production_database(database_path: str | Path) -> None:
    """Refuse an implicit/temporary production DB before bootstrap writes."""
    if os.environ.get("APP_ENV", "").strip().lower() != "production":
        return
    if not get_database_url():
        raise ConfigurationError("Production requires an explicit persistent DATABASE_URL")
    from sqlalchemy.engine import make_url
    value = str(database_path)
    if isinstance(database_path, Path) or "://" not in value:
        path = Path(value)
    else:
        url = make_url(value)
        if not url.drivername.startswith("sqlite"):
            return
        path = Path(url.database or "")
        if not url.database or url.database == ":memory:":
            raise ConfigurationError("Production SQLite must use a persistent file")
    if not path.is_absolute():
        raise ConfigurationError("Production SQLite DATABASE_URL must use an absolute path on a persistent disk")
    # Do not turn a wrong mount/path into a seemingly healthy empty database.
    if not path.is_file():
        raise ConfigurationError("Production SQLite database must already exist; verify its persistent disk and restore it before startup")


def get_cors_origins() -> list[str]:
    """Explicit frontend origins; local defaults preserve existing behavior."""
    from urllib.parse import urlsplit

    raw = os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    origins = [value.strip() for value in raw.split(",") if value.strip()]
    if not origins:
        raise ConfigurationError("CORS_ORIGINS must contain at least one frontend origin")
    for origin in origins:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or "*" in origin):
            raise ConfigurationError("CORS_ORIGINS must contain explicit HTTP(S) origins")
    return [origin.rstrip("/") for origin in origins]


@dataclass(frozen=True)
class WhatsAppSettings:
    access_token: str
    phone_number_id: str
    verify_token: str
    meta_app_secret: str
    graph_version: str


def get_whatsapp_settings() -> WhatsAppSettings:
    """Validate direct Meta Cloud API configuration without exposing values."""
    names = (
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_VERIFY_TOKEN",
        "META_APP_SECRET",
        "WHATSAPP_GRAPH_VERSION",
    )
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigurationError(
            "Missing required WhatsApp configuration: " + ", ".join(missing)
        )
    graph_version = _required_value("WHATSAPP_GRAPH_VERSION").strip()
    if not re.fullmatch(r"v\d+\.\d+", graph_version):
        raise ConfigurationError("WHATSAPP_GRAPH_VERSION must use the form vNN.N")
    return WhatsAppSettings(
        access_token=_required_value("WHATSAPP_ACCESS_TOKEN"),
        phone_number_id=_required_value("WHATSAPP_PHONE_NUMBER_ID").strip(),
        verify_token=_required_value("WHATSAPP_VERIFY_TOKEN"),
        meta_app_secret=_required_value("META_APP_SECRET"),
        graph_version=graph_version,
    )
