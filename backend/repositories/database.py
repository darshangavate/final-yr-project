"""Portable SQLAlchemy Core operational storage for the vendor application.

This module is deliberately independent of the research CSV/ML pipeline.  The
official zone master is used only to seed missing operational status rows.
SQLite-specific connection setup and locking are intentionally isolated here.
"""
from __future__ import annotations
from backend.core.data_paths import dataset_path

import csv
import json
import os
import re
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import (
    CheckConstraint, Column, ForeignKey, ForeignKeyConstraint, Index, Integer,
    Float, MetaData, String, Table, Text, UniqueConstraint, create_engine, event,
    inspect, text,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from backend.core.config import ConfigurationError, get_database_backend, get_database_url


ROOT = Path(__file__).resolve().parents[2]
LOCAL_DATABASE_PATH = dataset_path("database")
# Compatibility name retained for callers/tests. With DATABASE_URL configured,
# it carries the URL; otherwise it remains the historical Path object.
DATABASE_PATH = get_database_url() or LOCAL_DATABASE_PATH
DATABASE_URL = get_database_url()
DATABASE_BACKEND = get_database_backend()
OFFICIAL_ZONES_PATH = dataset_path("official_zones")

ZONE_STATUSES = ("OPEN", "FULL", "CLOSED", "SUSPENDED", "RESTRICTED", "NO_VENDING")
REQUEST_TYPES = ("NEW_ALLOCATION", "RELOCATION")
REQUEST_STATUSES = ("PENDING", "APPROVED", "REJECTED", "CANCELLED")
ALLOCATION_STATUSES = ("ACTIVE", "RELEASED")
ALLOCATION_SOURCES = ("ADMIN_APPROVED", "LEGACY_IMPORT")
HISTORICAL_ASSOCIATION_STATUSES = ("AUTO_MATCHED", "REVIEW_REQUIRED", "UNMATCHED")
HISTORICAL_REVIEW_STATUSES = ("UNREVIEWED", "CONFIRMED", "CHANGED", "REJECTED")
VENDOR_TYPES = ("NEW", "EXISTING_IMPORTED")
PRIORITY_PROFILES = ("BALANCED", "COMMERCIAL", "ACCESSIBILITY", "FACILITIES")
PREFERRED_LANGUAGES = ("en", "mr", "hi")
VENDOR_STATUSES = ("ACTIVE", "INACTIVE")


# Historical pre-SQLAlchemy DDL: docs/archive/pre-sqlalchemy-schema.sql.
# METADATA below is the authoritative schema; the archive is never executed.


def utc_now() -> str:
    """Return a sortable UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OperationalNotFoundError(ValueError):
    """Raised when an operational record does not exist."""


class OperationalConflictError(ValueError):
    """Raised when an operation conflicts with current operational state."""


class OperationalValidationError(ValueError):
    """Raised when an operational command contains an invalid value."""


def _build_metadata() -> MetaData:
    """Describe the operational schema without binding it to one SQL dialect."""
    metadata = MetaData()
    operational_vendors = Table(
        "operational_vendors", metadata,
        Column("vendor_id", Text, primary_key=True),
        Column("application_vendor_id", Text),
        Column("vendor_type", Text, nullable=False),
        Column("full_name", Text), Column("business_name", Text),
        Column("business_category", Text, nullable=False),
        Column("preferred_division", Text), Column("preferred_locality", Text),
        Column("priority_profile", Text, nullable=False, server_default="BALANCED"),
        Column("parking_preference", Integer, nullable=False, server_default="0"),
        Column("transport_preference", Integer, nullable=False, server_default="0"),
        Column("market_preference", Integer, nullable=False, server_default="0"),
        Column("preferred_language", Text, nullable=False, server_default="en"),
        Column("status", Text, nullable=False, server_default="ACTIVE"),
        Column("created_at", Text, nullable=False), Column("updated_at", Text, nullable=False),
        CheckConstraint("vendor_type IN ('NEW', 'EXISTING_IMPORTED')"),
        CheckConstraint("priority_profile IN ('BALANCED', 'COMMERCIAL', 'ACCESSIBILITY', 'FACILITIES')"),
        CheckConstraint("parking_preference IN (0, 1)"),
        CheckConstraint("transport_preference IN (0, 1)"),
        CheckConstraint("market_preference IN (0, 1)"),
        CheckConstraint("preferred_language IN ('en', 'mr', 'hi')"),
        CheckConstraint("status IN ('ACTIVE', 'INACTIVE')"),
    )
    Index(
        "idx_operational_vendors_application_vendor_id",
        operational_vendors.c.application_vendor_id,
        unique=True,
        sqlite_where=text("application_vendor_id IS NOT NULL"),
        postgresql_where=text("application_vendor_id IS NOT NULL"),
    )
    Table("application_vendor_id_counters", metadata,
          Column("year", Integer, primary_key=True),
          Column("last_sequence", Integer, nullable=False),
          CheckConstraint("last_sequence >= 0"))
    zones = Table(
        "zone_live_status", metadata,
        Column("zone_id", Text, primary_key=True),
        Column("official_capacity", Integer),
        Column("current_vendor_count", Integer, nullable=False, server_default="0"),
        Column("available_capacity", Integer),
        Column("status", Text, nullable=False, server_default="OPEN"),
        Column("verified_at", Text), Column("verified_by", Text),
        Column("updated_at", Text, nullable=False),
        CheckConstraint("official_capacity IS NULL OR official_capacity >= 0"),
        CheckConstraint("current_vendor_count >= 0"),
        CheckConstraint("available_capacity IS NULL OR available_capacity >= 0"),
        CheckConstraint("status IN ('OPEN','FULL','CLOSED','SUSPENDED','RESTRICTED','NO_VENDING')"),
        CheckConstraint("(official_capacity IS NULL AND available_capacity IS NULL) OR "
                        "(official_capacity IS NOT NULL AND available_capacity = CASE "
                        "WHEN current_vendor_count >= official_capacity THEN 0 "
                        "ELSE official_capacity - current_vendor_count END)",
                        name="ck_zone_live_status_capacity_consistency_v2"),
        CheckConstraint("official_capacity IS NULL OR current_vendor_count < official_capacity OR "
                        "status IN ('FULL','CLOSED','SUSPENDED','RESTRICTED','NO_VENDING')",
                        name="ck_zone_live_status_full_at_or_over_capacity_v2"),
    )
    requests = Table(
        "allocation_requests", metadata,
        Column("request_id", Text, primary_key=True), Column("vendor_id", Text, nullable=False),
        Column("zone_id", Text, ForeignKey(zones.c.zone_id), nullable=False),
        Column("slot_id", Text),
        Column("request_type", Text, nullable=False), Column("status", Text, nullable=False),
        Column("business_category", Text), Column("preferred_division", Text),
        Column("created_at", Text, nullable=False), Column("updated_at", Text, nullable=False),
        Column("reviewed_at", Text), Column("reviewed_by", Text),
        Column("rejection_reason", Text), Column("notes", Text),
        CheckConstraint("request_type IN ('NEW_ALLOCATION', 'RELOCATION')"),
        CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')"),
    )
    Index("one_pending_request_per_vendor_zone", requests.c.vendor_id, requests.c.zone_id,
          unique=True, sqlite_where=text("status = 'PENDING'"),
          postgresql_where=text("status = 'PENDING'"))
    Index("one_pending_request_per_slot", requests.c.zone_id, requests.c.slot_id, unique=True,
          sqlite_where=text("status = 'PENDING' AND slot_id IS NOT NULL"),
          postgresql_where=text("status = 'PENDING' AND slot_id IS NOT NULL"))
    allocations = Table(
        "allocations", metadata,
        Column("allocation_id", Text, primary_key=True), Column("vendor_id", Text, nullable=False),
        Column("zone_id", Text, ForeignKey(zones.c.zone_id), nullable=False),
        Column("slot_id", Text),
        Column("request_id", Text, ForeignKey(requests.c.request_id)),
        Column("status", Text, nullable=False), Column("allocated_at", Text, nullable=False),
        Column("released_at", Text), Column("approved_by", Text),
        Column("allocation_source", Text, nullable=False, server_default="ADMIN_APPROVED"),
        Column("created_at", Text, nullable=False), Column("updated_at", Text, nullable=False),
        CheckConstraint("status IN ('ACTIVE', 'RELEASED')"),
        CheckConstraint("allocation_source IN ('ADMIN_APPROVED', 'LEGACY_IMPORT')"),
    )
    Index("one_active_allocation_per_vendor", allocations.c.vendor_id, unique=True,
          sqlite_where=text("status = 'ACTIVE'"), postgresql_where=text("status = 'ACTIVE'"))
    Index("one_active_allocation_per_slot", allocations.c.zone_id, allocations.c.slot_id, unique=True,
          sqlite_where=text("status = 'ACTIVE' AND slot_id IS NOT NULL"),
          postgresql_where=text("status = 'ACTIVE' AND slot_id IS NOT NULL"))
    Table("admin_audit_log", metadata,
          Column("audit_id", Text, primary_key=True), Column("action", Text, nullable=False),
          Column("entity_type", Text, nullable=False), Column("entity_id", Text, nullable=False),
          Column("performed_by", Text), Column("details_json", Text),
          Column("created_at", Text, nullable=False))
    historical_associations = Table(
        "historical_vendor_zone_associations", metadata,
        Column("vendor_id", Text, primary_key=True),
        Column("matched_zone_id", Text, ForeignKey(zones.c.zone_id)),
        Column("candidate_zone_ids_json", Text, nullable=False, server_default="[]"),
        Column("original_location_text", Text),
        Column("location_text_english", Text),
        Column("recorded_division", Text),
        Column("business_category", Text),
        Column("match_status", Text, nullable=False),
        Column("evidence_types_json", Text, nullable=False, server_default="[]"),
        Column("matched_landmark", Text),
        Column("source_provenance", Text, nullable=False),
        Column("algorithm_version", Text, nullable=False),
        Column("review_status", Text, nullable=False, server_default="UNREVIEWED"),
        Column("reviewed_by", Text), Column("reviewed_at", Text), Column("review_notes", Text),
        Column("created_at", Text, nullable=False), Column("updated_at", Text, nullable=False),
        CheckConstraint("match_status IN ('AUTO_MATCHED','REVIEW_REQUIRED','UNMATCHED')"),
        CheckConstraint("review_status IN ('UNREVIEWED','CONFIRMED','CHANGED','REJECTED')"),
        CheckConstraint("(review_status = 'UNREVIEWED' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
                        "(review_status != 'UNREVIEWED' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)"),
    )
    Index("idx_historical_association_zone", historical_associations.c.matched_zone_id)
    Index("idx_historical_association_status", historical_associations.c.match_status,
          historical_associations.c.review_status)
    Index("idx_historical_association_division", historical_associations.c.recorded_division)
    identities = Table(
        "vendor_auth_identities", metadata,
        Column("identity_id", Text, primary_key=True), Column("vendor_id", Text, nullable=False),
        Column("phone_hash", Text, nullable=False, unique=True),
        Column("mobile_last4", Text),
        Column("vendor_source", Text, nullable=False),
        Column("is_verified", Integer, nullable=False, server_default="0"),
        Column("created_at", Text, nullable=False), Column("updated_at", Text, nullable=False),
        CheckConstraint("vendor_source IN ('HISTORICAL', 'OPERATIONAL')"),
        CheckConstraint("is_verified IN (0, 1)"),
    )
    Index("idx_vendor_auth_identities_vendor_id", identities.c.vendor_id)
    otp = Table(
        "auth_otp_challenges", metadata,
        Column("challenge_id", Text, primary_key=True), Column("phone_hash", Text, nullable=False),
        Column("mobile_last4", Text),
        Column("purpose", Text, nullable=False), Column("otp_hash", Text, nullable=False),
        Column("expires_at", Text, nullable=False),
        Column("attempt_count", Integer, nullable=False, server_default="0"),
        Column("max_attempts", Integer, nullable=False), Column("consumed_at", Text),
        Column("created_at", Text, nullable=False),
        CheckConstraint("purpose IN ('VENDOR_LOGIN', 'NEW_VENDOR_REGISTRATION')"),
        CheckConstraint("attempt_count >= 0"), CheckConstraint("max_attempts > 0"),
    )
    Index("idx_auth_otp_phone_created", otp.c.phone_hash, otp.c.created_at)
    sessions = Table(
        "auth_sessions", metadata,
        Column("session_id", Text, primary_key=True),
        Column("session_token_hash", Text, nullable=False, unique=True),
        Column("role", Text, nullable=False), Column("vendor_id", Text), Column("admin_id", Text),
        Column("new_vendor_phone_hash", Text), Column("mobile_last4", Text),
        Column("created_at", Text, nullable=False),
        Column("expires_at", Text, nullable=False), Column("revoked_at", Text),
        CheckConstraint("role IN ('VENDOR', 'ADMIN', 'NEW_VENDOR_ONBOARDING')"),
        CheckConstraint("(role = 'VENDOR' AND vendor_id IS NOT NULL AND admin_id IS NULL AND new_vendor_phone_hash IS NULL) OR "
                        "(role = 'ADMIN' AND admin_id IS NOT NULL AND vendor_id IS NULL AND new_vendor_phone_hash IS NULL) OR "
                        "(role = 'NEW_VENDOR_ONBOARDING' AND new_vendor_phone_hash IS NOT NULL AND vendor_id IS NULL AND admin_id IS NULL)"),
    )
    Index("idx_auth_sessions_token_active", sessions.c.session_token_hash,
          sessions.c.revoked_at, sessions.c.expires_at)
    Table(
        "vendor_profile_photos", metadata,
        Column("vendor_id", Text, primary_key=True), Column("mime_type", Text, nullable=False),
        Column("image_base64", Text, nullable=False), Column("updated_at", Text, nullable=False),
    )
    conversations = Table(
        "whatsapp_conversations", metadata,
        Column("conversation_key", Text, primary_key=True),
        Column("phone_hash", Text, nullable=False, unique=True), Column("vendor_id", Text),
        Column("language", Text, nullable=False, server_default="en"),
        Column("flow", Text, nullable=False, server_default="START"),
        Column("step", Text, nullable=False, server_default="START"),
        Column("state_json", Text, nullable=False, server_default="{}"),
        Column("invalid_count", Integer, nullable=False, server_default="0"),
        Column("updated_at", Text, nullable=False), Column("expires_at", Text, nullable=False),
        CheckConstraint("language IN ('en', 'mr', 'hi')"), CheckConstraint("invalid_count >= 0"),
    )
    Index("idx_whatsapp_conversations_vendor", conversations.c.vendor_id)
    messages = Table(
        "whatsapp_processed_messages", metadata,
        Column("message_id", Text, primary_key=True), Column("phone_hash", Text, nullable=False),
        Column("status", Text, nullable=False, server_default="PROCESSING"),
        Column("received_at", Text, nullable=False), Column("processed_at", Text),
        Column("error_code", Text),
        CheckConstraint("status IN ('PROCESSING', 'PROCESSED', 'IGNORED', 'FAILED')"),
    )
    Index("idx_whatsapp_processed_phone_received", messages.c.phone_hash, messages.c.received_at)
    layouts = Table("pilot_slot_layouts", metadata,
                    Column("zone_id", Text, primary_key=True),
                    Column("metadata_json", Text, nullable=False),
                    Column("created_at", Text, nullable=False))
    slots = Table(
        "pilot_slots", metadata,
        Column("zone_id", Text, ForeignKey(layouts.c.zone_id), primary_key=True),
        Column("slot_id", Text, primary_key=True),
        Column("width_m", Float, nullable=False), Column("depth_m", Float, nullable=False),
        Column("x_m", Float, nullable=False), Column("y_m", Float, nullable=False),
        Column("geometry_json", Text, nullable=False), Column("status", Text, nullable=False),
        Column("assigned_vendor_id", Text), Column("validation_state", Text, nullable=False),
        Column("pending_request_id", Text), Column("active_allocation_id", Text),
        Column("created_by", Text), Column("created_at", Text),
        Column("updated_at", Text, nullable=False), Column("parent_boundary_id", Text),
        Column("geometry_crs", Text, nullable=False, server_default="LOCAL_SCHEMATIC_METRES"),
        Column("is_deleted", Integer, nullable=False, server_default="0"),
        CheckConstraint("width_m > 0"), CheckConstraint("depth_m > 0"),
        CheckConstraint("status IN ('AVAILABLE','RESERVED','OCCUPIED','BLOCKED')"),
        CheckConstraint("validation_state = 'ILLUSTRATIVE_UNVERIFIED'"),
        CheckConstraint("assigned_vendor_id IS NULL OR status IN ('RESERVED','OCCUPIED')"),
        CheckConstraint("is_deleted IN (0, 1)"),
    )
    Index("one_active_demo_slot_per_vendor", slots.c.assigned_vendor_id, unique=True,
          sqlite_where=text("assigned_vendor_id IS NOT NULL"),
          postgresql_where=text("assigned_vendor_id IS NOT NULL"))
    Table(
        "pilot_slot_events", metadata,
        Column("event_id", Integer, primary_key=True, autoincrement=True),
        Column("zone_id", Text, nullable=False), Column("slot_id", Text, nullable=False),
        Column("actor", Text, nullable=False), Column("before_json", Text, nullable=False),
        Column("after_json", Text, nullable=False), Column("created_at", Text, nullable=False),
        ForeignKeyConstraint(["zone_id", "slot_id"], ["pilot_slots.zone_id", "pilot_slots.slot_id"]),
    )
    return metadata


METADATA = _build_metadata()


def _database_url(database: str | Path | None = None) -> str:
    target = DATABASE_PATH if database is None else database
    if isinstance(target, Path) or "://" not in str(target):
        path = Path(target).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path.as_posix()}"
    url = str(target)
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    return "postgresql+psycopg://" + url.removeprefix("postgresql://") if url.startswith("postgresql://") else url


def database_backend(database: str | Path | None = None) -> str:
    return "postgresql" if _database_url(database).startswith("postgresql") else "sqlite"


@lru_cache(maxsize=32)
def _cached_engine(url: str) -> Engine:
    if url.startswith("sqlite:"):
        engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False}, poolclass=NullPool)

        @event.listens_for(engine, "connect")
        def _sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()
        return engine
    return create_engine(url, pool_pre_ping=True)


def get_engine(database: str | Path | None = None) -> Engine:
    """Return the shared synchronous engine for the configured operational DB."""
    return _cached_engine(_database_url(database))


class DatabaseRow(Mapping[str, Any]):
    """Mapping-first row with temporary positional-read compatibility."""
    def __init__(self, row: RowMapping) -> None:
        self._row = row

    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self._row.values())[key]
        return self._row[key]

    def __iter__(self):
        # sqlite3.Row iterated values; retain that narrow compatibility while
        # keys()/items() and string subscripting provide mapping semantics.
        return iter(self._row.values())

    def __len__(self) -> int:
        return len(self._row)

    def keys(self):
        return self._row.keys()

    def items(self):
        return self._row.items()


class DatabaseResult:
    """Small mapping-result facade preserving the repository's historical API."""
    def __init__(self, result) -> None:
        self._result = result.mappings()
        self.rowcount = result.rowcount

    def fetchone(self):
        row = self._result.fetchone()
        return DatabaseRow(row) if row is not None else None

    def fetchall(self):
        return [DatabaseRow(row) for row in self._result.fetchall()]

    def __iter__(self):
        return (DatabaseRow(row) for row in self._result)


def _named_statement(statement: str, parameters: Any) -> tuple[Any, Mapping[str, Any]]:
    """Normalize legacy positional calls at the repository boundary.

    New code uses named parameters. This narrow compatibility path keeps older
    service calls portable while they are progressively converted.
    """
    if parameters is None:
        return text(statement), {}
    if isinstance(parameters, Mapping):
        return text(statement), parameters
    values: Sequence[Any] = tuple(parameters)
    pieces = statement.split("?")
    if len(pieces) - 1 != len(values):
        raise ValueError("SQL placeholder count does not match parameter count")
    rebuilt = pieces[0]
    params: dict[str, Any] = {}
    for index, value in enumerate(values):
        name = f"p{index}"
        rebuilt += f":{name}" + pieces[index + 1]
        params[name] = value
    return text(rebuilt), params


class DatabaseConnection:
    """SQLAlchemy connection exposing mapping rows and portable execution."""
    def __init__(self, connection: Connection) -> None:
        self.raw = connection

    @property
    def backend(self) -> str:
        return self.raw.dialect.name

    def execute(self, statement: str | Any, parameters: Any = None) -> DatabaseResult:
        if isinstance(statement, str):
            statement, parameters = _named_statement(statement, parameters)
        result = self.raw.execute(statement, parameters or {})
        return DatabaseResult(result)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self) -> DatabaseConnection:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()


def get_connection(database_path: str | Path = DATABASE_PATH) -> DatabaseConnection:
    return DatabaseConnection(get_engine(database_path).connect())


def connect(database_path: str | Path = DATABASE_PATH) -> DatabaseConnection:
    """Compatibility alias for callers using the historical repository API."""
    return get_connection(database_path)


@contextmanager
def transaction(
    database_path: str | Path = DATABASE_PATH,
    *,
    immediate: bool = False,
) -> Iterator[DatabaseConnection]:
    """Yield a connection and atomically commit or roll back its work."""
    connection = connect(database_path)
    try:
        if immediate and connection.backend == "sqlite":
            connection.raw.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.raw.begin()
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_schema(database_path: str | Path = DATABASE_PATH) -> None:
    """Create every missing operational table and index without deleting data."""
    METADATA.create_all(get_engine(database_path), checkfirst=True)


def validate_existing_production_database(database_path: str | Path) -> None:
    """Inspect the configured DB before bootstrap can create tables or seed it."""
    if os.environ.get("APP_ENV", "").strip().lower() != "production":
        return
    try:
        with get_engine(database_path).connect() as connection:
            required = {"operational_vendors", "zone_live_status", "allocations", "allocation_requests"}
            if not required <= set(inspect(connection).get_table_names()):
                raise ConfigurationError("Production database lacks the existing operational schema; verify DATABASE_URL and restore or provision it explicitly")
            if not connection.execute(text("SELECT COUNT(*) FROM zone_live_status")).scalar_one():
                raise ConfigurationError("Production database has no operational zone records; verify the existing database before startup")
    except SQLAlchemyError:
        raise ConfigurationError("Configured production database is unavailable; verify DATABASE_URL, connectivity and permissions") from None


def inspect_schema(database_path: str | Path = DATABASE_PATH) -> dict[str, dict[str, Any]]:
    """Return dialect-independent table, column, and index information."""
    inspector = inspect(get_engine(database_path))
    return {
        table_name: {
            "columns": [column["name"] for column in inspector.get_columns(table_name)],
            "indexes": [index["name"] for index in inspector.get_indexes(table_name)],
        }
        for table_name in inspector.get_table_names()
    }


def for_update(connection: DatabaseConnection) -> str:
    """Lock selected rows on PostgreSQL; SQLite is protected by BEGIN IMMEDIATE."""
    return " FOR UPDATE" if connection.backend == "postgresql" else ""


def _read_official_zones(zones_path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    with Path(zones_path).open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        required = {"zone_id", "zone_type", "capacity"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"Official zone file must contain {sorted(required)}")
        for line_number, row in enumerate(reader, start=2):
            zone_id = (row.get("zone_id") or "").strip()
            zone_type = (row.get("zone_type") or "").strip().upper()
            if not zone_id or zone_id in seen:
                raise ValueError(f"Missing or duplicate zone_id at line {line_number}")
            if zone_type not in {"FREE", "RESTRICTED", "NO_VENDING"}:
                raise ValueError(f"Unsupported zone_type {zone_type!r} for {zone_id}")
            raw_capacity = (row.get("capacity") or "").strip()
            capacity = None
            if raw_capacity:
                numeric_capacity = float(raw_capacity)
                if numeric_capacity < 0 or not numeric_capacity.is_integer():
                    raise ValueError(f"Invalid capacity {raw_capacity!r} for {zone_id}")
                capacity = int(numeric_capacity)
            rows.append({"zone_id": zone_id, "zone_type": zone_type, "capacity": capacity})
            seen.add(zone_id)
    return rows


APPLICATION_VENDOR_ID_PATTERN = re.compile(r"^NV-(\d{4})-(\d{6})$")


def _application_id_year(created_at: str | None, fallback_year: int) -> int:
    if created_at:
        try:
            year = datetime.fromisoformat(created_at.replace("Z", "+00:00")).year
            if 2000 <= year <= 9999:
                return year
        except (TypeError, ValueError):
            pass
    return fallback_year


def _sync_application_id_counters(connection: DatabaseConnection) -> None:
    maximum_by_year: dict[int, int] = {}
    rows = connection.execute(
        "SELECT application_vendor_id FROM operational_vendors "
        "WHERE application_vendor_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        match = APPLICATION_VENDOR_ID_PATTERN.fullmatch(row["application_vendor_id"] or "")
        if match:
            year, sequence = map(int, match.groups())
            maximum_by_year[year] = max(maximum_by_year.get(year, 0), sequence)
    for year, sequence in maximum_by_year.items():
        connection.execute(
            """INSERT INTO application_vendor_id_counters (year, last_sequence)
               VALUES (?, ?)
               ON CONFLICT(year) DO UPDATE SET
                   last_sequence = CASE
                       WHEN application_vendor_id_counters.last_sequence > excluded.last_sequence
                       THEN application_vendor_id_counters.last_sequence
                       ELSE excluded.last_sequence
                   END""",
            (year, sequence),
        )


def _next_application_vendor_id(connection: DatabaseConnection, year: int) -> str:
    """Allocate a public ID while the caller holds a BEGIN IMMEDIATE transaction."""
    connection.execute(
        "INSERT INTO application_vendor_id_counters (year, last_sequence) VALUES (?, 0) ON CONFLICT DO NOTHING",
        (year,),
    )
    connection.execute(
        "UPDATE application_vendor_id_counters SET last_sequence = last_sequence + 1 WHERE year = ?",
        (year,),
    )
    sequence = connection.execute(
        "SELECT last_sequence FROM application_vendor_id_counters WHERE year = ?", (year,)
    ).fetchone()["last_sequence"]
    return f"NV-{year:04d}-{sequence:06d}"


def initialize_database(
    database_path: str | Path = DATABASE_PATH,
    zones_path: str | Path = OFFICIAL_ZONES_PATH,
) -> dict[str, int]:
    """Create the schema and seed any official zones that are not yet present."""
    zones = _read_official_zones(zones_path)
    inserted = 0
    now = utc_now()
    backfilled_application_ids = 0
    initialize_schema(database_path)
    schema = inspect_schema(database_path)
    with transaction(database_path, immediate=True) as connection:
        pilot_columns = set(schema.get("pilot_slots", {}).get("columns", []))
        for name, definition in {
            "parent_boundary_id": "TEXT NULL",
            "geometry_crs": "TEXT NOT NULL DEFAULT 'LOCAL_SCHEMATIC_METRES'",
            "is_deleted": "INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1))",
            "pending_request_id": "TEXT NULL",
            "active_allocation_id": "TEXT NULL",
            "created_by": "TEXT NULL",
            "created_at": "TEXT NULL",
        }.items():
            if name not in pilot_columns:
                connection.execute(f"ALTER TABLE pilot_slots ADD COLUMN {name} {definition}")
        # SQLite has no portable ADD COLUMN IF NOT EXISTS. Inspect first so this
        # remains safe for databases created before vendor identity was added.
        vendor_columns = set(schema.get("operational_vendors", {}).get("columns", []))
        if "full_name" not in vendor_columns:
            connection.execute("ALTER TABLE operational_vendors ADD COLUMN full_name TEXT NULL")
        if "business_name" not in vendor_columns:
            connection.execute("ALTER TABLE operational_vendors ADD COLUMN business_name TEXT NULL")
        if "application_vendor_id" not in vendor_columns:
            connection.execute(
                "ALTER TABLE operational_vendors ADD COLUMN application_vendor_id TEXT NULL"
            )
        allocation_columns = set(schema.get("allocations", {}).get("columns", []))
        if "allocation_source" not in allocation_columns:
            connection.execute(
                "ALTER TABLE allocations ADD COLUMN allocation_source "
                "TEXT NOT NULL DEFAULT 'ADMIN_APPROVED'"
            )
        if "slot_id" not in allocation_columns:
            connection.execute("ALTER TABLE allocations ADD COLUMN slot_id TEXT NULL")
        request_columns = set(schema.get("allocation_requests", {}).get("columns", []))
        if "slot_id" not in request_columns:
            connection.execute("ALTER TABLE allocation_requests ADD COLUMN slot_id TEXT NULL")
        for table, columns in {
            "vendor_auth_identities": set(schema.get("vendor_auth_identities", {}).get("columns", [])),
            "auth_otp_challenges": set(schema.get("auth_otp_challenges", {}).get("columns", [])),
            "auth_sessions": set(schema.get("auth_sessions", {}).get("columns", [])),
        }.items():
            if "mobile_last4" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN mobile_last4 TEXT NULL")
        connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_pending_request_per_slot
            ON allocation_requests(zone_id, slot_id)
            WHERE status = 'PENDING' AND slot_id IS NOT NULL""")
        connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_active_allocation_per_slot
            ON allocations(zone_id, slot_id)
            WHERE status = 'ACTIVE' AND slot_id IS NOT NULL""")
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_operational_vendors_application_vendor_id
               ON operational_vendors(application_vendor_id)
               WHERE application_vendor_id IS NOT NULL"""
        )
        _sync_application_id_counters(connection)
        fallback_year = datetime.now(timezone.utc).year
        missing_ids = connection.execute(
            """SELECT vendor_id, created_at FROM operational_vendors
               WHERE vendor_type = 'NEW'
                 AND (application_vendor_id IS NULL OR TRIM(application_vendor_id) = '')
               ORDER BY created_at, vendor_id"""
        ).fetchall()
        for vendor in missing_ids:
            year = _application_id_year(vendor["created_at"], fallback_year)
            application_vendor_id = _next_application_vendor_id(connection, year)
            connection.execute(
                "UPDATE operational_vendors SET application_vendor_id = ? WHERE vendor_id = ?",
                (application_vendor_id, vendor["vendor_id"]),
            )
            backfilled_application_ids += 1
        for zone in zones:
            initial_status = {
                "FREE": "OPEN",
                "RESTRICTED": "RESTRICTED",
                "NO_VENDING": "NO_VENDING",
            }[zone["zone_type"]]
            capacity = zone["capacity"]
            # A zero-capacity zone has already reached its known capacity.
            if capacity == 0:
                initial_status = "FULL"
            cursor = connection.execute(
                """
                INSERT INTO zone_live_status (
                    zone_id, official_capacity, current_vendor_count,
                    available_capacity, status, updated_at
                ) VALUES (?, ?, 0, ?, ?, ?) ON CONFLICT DO NOTHING
                """,
                (zone["zone_id"], capacity, capacity, initial_status, now),
            )
            inserted += cursor.rowcount
        total_rows = connection.execute(
            "SELECT COUNT(*) AS row_count FROM zone_live_status"
        ).fetchone()["row_count"]
    return {
        "official_zones": len(zones),
        "zone_live_status_rows": total_rows,
        "inserted": inserted,
        "already_existing": len(zones) - inserted,
        "application_vendor_ids_backfilled": backfilled_application_ids,
    }


def _fetch_one(query: str, parameters: tuple[Any, ...], database_path: str | Path):
    connection = connect(database_path)
    try:
        return connection.execute(query, parameters).fetchone()
    finally:
        connection.close()


def _derived_live_rows(connection: DatabaseConnection) -> list[dict[str, Any]]:
    """Derive occupancy and availability from unique ACTIVE allocations."""
    rows = connection.execute(
        """WITH active AS (
               SELECT zone_id, COUNT(DISTINCT vendor_id) AS vendor_count
               FROM allocations WHERE status = 'ACTIVE' GROUP BY zone_id
           )
           SELECT z.zone_id, z.official_capacity,
                  COALESCE(a.vendor_count, 0) AS current_vendor_count,
                  CASE WHEN z.official_capacity IS NULL THEN NULL
                       WHEN COALESCE(a.vendor_count, 0) >= z.official_capacity THEN 0
                       ELSE z.official_capacity - COALESCE(a.vendor_count, 0) END
                       AS available_capacity,
                  CASE WHEN z.status IN ('FULL','CLOSED','SUSPENDED','RESTRICTED','NO_VENDING')
                       THEN z.status
                       WHEN z.official_capacity IS NOT NULL
                            AND COALESCE(a.vendor_count, 0) >= z.official_capacity THEN 'FULL'
                       ELSE 'OPEN' END AS status,
                  z.verified_at, z.verified_by, z.updated_at
           FROM zone_live_status AS z
           LEFT JOIN active AS a ON a.zone_id = z.zone_id
           ORDER BY z.zone_id"""
    ).fetchall()
    return [dict(row) for row in rows]


def _zone_with_derived_occupancy(
    connection: DatabaseConnection,
    zone: RowMapping,
) -> dict[str, Any]:
    """Overlay a locked zone row with authoritative ACTIVE-allocation totals.

    Approval and request validation use this inside their existing transaction,
    so a stale materialized counter can never create an extra allocation.
    Manual CLOSED/SUSPENDED and policy statuses remain authoritative.
    """
    derived = dict(zone)
    active_count = int(connection.execute(
        "SELECT COUNT(DISTINCT vendor_id) AS vendor_count FROM allocations "
        "WHERE zone_id = ? AND status = 'ACTIVE'",
        (zone["zone_id"],),
    ).fetchone()["vendor_count"])
    capacity = zone["official_capacity"]
    available = None if capacity is None else max(0, int(capacity) - active_count)
    protected_statuses = {"FULL", "CLOSED", "SUSPENDED", "RESTRICTED", "NO_VENDING"}
    if zone["status"] in protected_statuses:
        effective_status = zone["status"]
    elif capacity is not None and available == 0:
        effective_status = "FULL"
    else:
        effective_status = "OPEN"
    derived.update(
        current_vendor_count=active_count,
        available_capacity=available,
        status=effective_status,
    )
    return derived


def get_zone_live_status(zone_id: str, database_path: str | Path = DATABASE_PATH):
    return get_live_zone_status_map(database_path).get(zone_id)


def list_zone_live_status(
    status: str | None = None,
    database_path: str | Path = DATABASE_PATH,
) -> list[RowMapping]:
    connection = connect(database_path)
    try:
        rows = _derived_live_rows(connection)
        if status is not None:
            rows = [row for row in rows if row["status"] == status.upper()]
        return rows
    finally:
        connection.close()


def get_live_zone_status_map(
    database_path: str | Path = DATABASE_PATH,
) -> dict[str, dict[str, Any]]:
    """Return every zone's live state with one database query."""
    connection = connect(database_path)
    try:
        rows = _derived_live_rows(connection)
        return {row["zone_id"]: row for row in rows}
    finally:
        connection.close()


def get_admin_overview(
    database_path: str | Path = DATABASE_PATH,
    *,
    zone_statuses: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Aggregate operational counts only; historical associations are excluded."""
    connection = connect(database_path)
    try:
        zone_rows = None if zone_statuses is not None else _derived_live_rows(connection)
        statuses = zone_statuses if zone_statuses is not None else [row["status"] for row in zone_rows]
        zone_counts = {status: 0 for status in ZONE_STATUSES}
        zone_counts.update(Counter(statuses))
        request_counts = {status: 0 for status in REQUEST_STATUSES}
        allocation_counts = {status: 0 for status in ALLOCATION_STATUSES}
        for row in connection.execute(
            "SELECT 'request' AS kind, status, COUNT(request_id) AS count "
            "FROM allocation_requests GROUP BY status UNION ALL "
            "SELECT 'allocation' AS kind, status, COUNT(allocation_id) AS count "
            "FROM allocations GROUP BY status"
        ):
            target = request_counts if row["kind"] == "request" else allocation_counts
            target[row["status"]] = row["count"]
        return {
            "total_official_zones": len(statuses),
            "zone_status_counts": zone_counts,
            "request_status_counts": request_counts,
            "allocation_status_counts": allocation_counts,
        }
    finally:
        connection.close()


def get_allocation_request(request_id: str, database_path: str | Path = DATABASE_PATH):
    return _fetch_one("SELECT * FROM allocation_requests WHERE request_id = ?", (request_id,), database_path)


def list_allocation_requests(
    status: str | None = None,
    database_path: str | Path = DATABASE_PATH,
) -> list[RowMapping]:
    query = "SELECT * FROM allocation_requests"
    parameters: tuple[Any, ...] = ()
    if status is not None:
        query += " WHERE status = ?"
        parameters = (status.upper(),)
    query += " ORDER BY created_at DESC, request_id DESC"
    connection = connect(database_path)
    try:
        return connection.execute(query, parameters).fetchall()
    finally:
        connection.close()


def get_active_allocation_for_vendor(
    vendor_id: str,
    database_path: str | Path = DATABASE_PATH,
):
    return _fetch_one(
        "SELECT * FROM allocations WHERE vendor_id = ? AND status = 'ACTIVE'",
        (vendor_id,),
        database_path,
    )


def get_vendor_dashboard_activity(
    vendor_id: str,
    database_path: str | Path = DATABASE_PATH,
) -> dict[str, Any]:
    """Return only one vendor's allocation and request rows with live zone state."""
    connection = connect(database_path)
    try:
        active = connection.execute(
            """SELECT a.*, r.request_type AS allocation_type,
                      z.status AS live_zone_status, z.official_capacity,
                      z.current_vendor_count, z.available_capacity
               FROM allocations AS a
               LEFT JOIN allocation_requests AS r ON r.request_id = a.request_id
               LEFT JOIN zone_live_status AS z ON z.zone_id = a.zone_id
               WHERE a.vendor_id = ? AND a.status = 'ACTIVE'
               ORDER BY a.allocated_at DESC, a.allocation_id DESC
               LIMIT 1""",
            (vendor_id,),
        ).fetchone()
        requests = connection.execute(
            """SELECT r.*
               FROM allocation_requests AS r
               WHERE r.vendor_id = ?
               ORDER BY r.created_at DESC, r.request_id DESC""",
            (vendor_id,),
        ).fetchall()
        request_ids = [row["request_id"] for row in requests]
        previous_zone_by_request: dict[str, str] = {}
        if request_ids:
            placeholders = ",".join("?" for _ in request_ids)
            audits = connection.execute(
                f"""SELECT entity_id, details_json
                    FROM admin_audit_log
                    WHERE action = 'ALLOCATION_REQUEST_APPROVED'
                      AND entity_id IN ({placeholders})""",
                tuple(request_ids),
            ).fetchall()
            for audit in audits:
                try:
                    details = json.loads(audit["details_json"] or "{}")
                except json.JSONDecodeError:
                    continue
                old_zone_id = details.get("old_zone_id")
                if old_zone_id:
                    previous_zone_by_request[audit["entity_id"]] = old_zone_id
        return {
            "active_allocation": dict(active) if active is not None else None,
            "requests": [dict(row) for row in requests],
            "previous_zone_by_request": previous_zone_by_request,
        }
    finally:
        connection.close()


def get_allocation(allocation_id: str, database_path: str | Path = DATABASE_PATH):
    return _fetch_one("SELECT * FROM allocations WHERE allocation_id = ?", (allocation_id,), database_path)


def list_allocations(
    status: str | None = None,
    database_path: str | Path = DATABASE_PATH,
    limit: int | None = None,
    offset: int = 0,
) -> list[RowMapping]:
    query = "SELECT * FROM allocations"
    parameters: tuple[Any, ...] = ()
    if status is not None:
        normalized_status = status.upper()
        if normalized_status not in ALLOCATION_STATUSES:
            raise OperationalValidationError(
                f"status must be one of: {', '.join(ALLOCATION_STATUSES)}"
            )
        query += " WHERE status = ?"
        parameters = (normalized_status,)
    query += " ORDER BY allocated_at DESC, allocation_id DESC"
    if limit is not None:
        if isinstance(limit, bool) or limit < 1:
            raise OperationalValidationError("limit must be a positive integer")
        if isinstance(offset, bool) or offset < 0:
            raise OperationalValidationError("offset must be a non-negative integer")
        query += " LIMIT ? OFFSET ?"
        parameters += (limit, offset)
    connection = connect(database_path)
    try:
        return connection.execute(query, parameters).fetchall()
    finally:
        connection.close()


def count_allocations(
    status: str | None = None,
    database_path: str | Path = DATABASE_PATH,
) -> int:
    query = "SELECT COUNT(*) FROM allocations"
    parameters: tuple[Any, ...] = ()
    if status is not None:
        normalized_status = status.upper()
        if normalized_status not in ALLOCATION_STATUSES:
            raise OperationalValidationError(
                f"status must be one of: {', '.join(ALLOCATION_STATUSES)}"
            )
        query += " WHERE status = ?"
        parameters = (normalized_status,)
    connection = connect(database_path)
    try:
        return connection.execute(query, parameters).fetchone()[0]
    finally:
        connection.close()


def create_allocation_request(
    vendor_id: str,
    zone_id: str,
    request_type: str,
    business_category: str | None = None,
    preferred_division: str | None = None,
    notes: str | None = None,
    database_path: str | Path = DATABASE_PATH,
    slot_id: str | None = None,
) -> RowMapping:
    """Create a pending request after atomically checking operational state."""
    vendor_id = vendor_id.strip()
    zone_id = zone_id.strip()
    request_type = request_type.strip().upper()
    slot_id = slot_id.strip().upper() if slot_id else None
    if not vendor_id:
        raise OperationalValidationError("vendor_id must not be empty")
    if request_type not in REQUEST_TYPES:
        raise OperationalValidationError(
            f"request_type must be one of: {', '.join(REQUEST_TYPES)}"
        )

    request_id = f"REQ_{uuid.uuid4().hex}"
    audit_id = f"AUD_{uuid.uuid4().hex}"
    now = utc_now()
    policy_types = _official_zone_type_map(str(OFFICIAL_ZONES_PATH.resolve()))
    with transaction(database_path, immediate=True) as connection:
        zone = connection.execute(
            "SELECT * FROM zone_live_status WHERE zone_id = ?" + for_update(connection),
            (zone_id,),
        ).fetchone()
        if zone is None:
            raise OperationalNotFoundError(f"Unknown zone_id: {zone_id}")
        zone = _zone_with_derived_occupancy(connection, zone)
        from backend.services.eligibility import zone_ineligibility_reason
        reason = zone_ineligibility_reason(policy_types.get(zone_id), zone)
        if reason:
            raise OperationalConflictError(f"Zone {zone_id} is ineligible ({reason})")
        if zone["official_capacity"] is not None and zone["available_capacity"] <= 0:
            raise OperationalConflictError(f"Zone {zone_id} has no available capacity")
        if zone["status"] != "OPEN":
            raise OperationalConflictError(
                f"Zone {zone_id} is not open for allocation (status: {zone['status']})"
            )

        active_allocation = connection.execute(
            "SELECT allocation_id, zone_id FROM allocations WHERE vendor_id = ? AND status = 'ACTIVE'",
            (vendor_id,),
        ).fetchone()
        if request_type == "NEW_ALLOCATION" and active_allocation is not None:
            raise OperationalConflictError("Vendor already has an active allocation")
        if request_type == "RELOCATION" and active_allocation is None:
            raise OperationalConflictError("Relocation requires an active allocation")
        if request_type == "RELOCATION" and active_allocation["zone_id"] == zone_id:
            raise OperationalConflictError("Relocation target must differ from the active allocation zone")

        duplicate = connection.execute(
            """SELECT request_id FROM allocation_requests
               WHERE vendor_id = ? AND zone_id = ? AND status = 'PENDING'""",
            (vendor_id, zone_id),
        ).fetchone()
        if duplicate is not None:
            raise OperationalConflictError(
                f"Vendor already has a pending request for zone {zone_id}"
            )

        requested_slot = None
        if slot_id:
            requested_slot = connection.execute(
                "SELECT * FROM pilot_slots WHERE zone_id=? AND slot_id=?" + for_update(connection),
                (zone_id, slot_id),
            ).fetchone()
            if requested_slot is None or requested_slot["is_deleted"]:
                raise OperationalNotFoundError(f"Unknown slot {slot_id} for zone {zone_id}")
            if requested_slot["geometry_crs"] != "EPSG:4326":
                raise OperationalConflictError("Only a reviewed geographic slot can be requested")
            if requested_slot["status"] != "AVAILABLE" or requested_slot["assigned_vendor_id"] or requested_slot["pending_request_id"]:
                raise OperationalConflictError(f"Slot {slot_id} is not available")

        try:
            connection.execute(
                """
                INSERT INTO allocation_requests (
                    request_id, vendor_id, zone_id, slot_id, request_type, status,
                    business_category, preferred_division, created_at,
                    updated_at, notes
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    vendor_id,
                    zone_id,
                    slot_id,
                    request_type,
                    business_category,
                    preferred_division,
                    now,
                    now,
                    notes,
                ),
            )
        except IntegrityError as exc:
            # The partial unique index closes the race after the explicit,
            # user-friendly duplicate check above.
            raise OperationalConflictError(
                f"Vendor already has a pending request for zone {zone_id}"
            ) from exc
        if requested_slot is not None:
            connection.execute(
                """UPDATE pilot_slots SET status='RESERVED', pending_request_id=?, updated_at=?
                   WHERE zone_id=? AND slot_id=?""",
                (request_id, now, zone_id, slot_id),
            )
            connection.execute(
                """INSERT INTO pilot_slot_events(zone_id,slot_id,actor,before_json,after_json,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (zone_id, slot_id, vendor_id, json.dumps(dict(requested_slot)),
                 json.dumps({"status": "RESERVED", "pending_request_id": request_id}), now),
            )
        details = json.dumps(
            {"vendor_id": vendor_id, "zone_id": zone_id, "slot_id": slot_id, "request_type": request_type},
            sort_keys=True,
        )
        connection.execute(
            """INSERT INTO admin_audit_log
               (audit_id, action, entity_type, entity_id, performed_by, details_json, created_at)
               VALUES (?, 'ALLOCATION_REQUEST_CREATED', 'allocation_request', ?, NULL, ?, ?)""",
            (audit_id, request_id, details, now),
        )
        return connection.execute(
            "SELECT * FROM allocation_requests WHERE request_id = ?", (request_id,)
        ).fetchone()


def cancel_allocation_request(
    request_id: str,
    database_path: str | Path = DATABASE_PATH,
) -> RowMapping:
    """Cancel a pending request and record the action in the same transaction."""
    now = utc_now()
    audit_id = f"AUD_{uuid.uuid4().hex}"
    with transaction(database_path) as connection:
        request = connection.execute(
            "SELECT * FROM allocation_requests WHERE request_id = ?" + for_update(connection),
            (request_id,),
        ).fetchone()
        if request is None:
            raise OperationalNotFoundError(f"Unknown allocation request: {request_id}")
        if request["status"] != "PENDING":
            raise OperationalConflictError(
                f"Only PENDING requests can be cancelled (current status: {request['status']})"
            )
        connection.execute(
            """UPDATE allocation_requests
               SET status = 'CANCELLED', updated_at = ?
               WHERE request_id = ?""",
            (now, request_id),
        )
        if request.get("slot_id"):
            connection.execute(
                """UPDATE pilot_slots SET status='AVAILABLE',pending_request_id=NULL,updated_at=?
                   WHERE zone_id=? AND slot_id=? AND pending_request_id=?""",
                (now, request["zone_id"], request["slot_id"], request_id),
            )
        details = json.dumps(
            {
                "vendor_id": request["vendor_id"],
                "zone_id": request["zone_id"],
                "request_type": request["request_type"],
            },
            sort_keys=True,
        )
        connection.execute(
            """INSERT INTO admin_audit_log
               (audit_id, action, entity_type, entity_id, performed_by, details_json, created_at)
               VALUES (?, 'ALLOCATION_REQUEST_CANCELLED', 'allocation_request', ?, NULL, ?, ?)""",
            (audit_id, request_id, details, now),
        )
        return connection.execute(
            "SELECT * FROM allocation_requests WHERE request_id = ?", (request_id,)
        ).fetchone()


@lru_cache(maxsize=4)
def _official_zone_type_map(zones_path: str = str(OFFICIAL_ZONES_PATH)) -> dict[str, str]:
    """Cache immutable policy zone types from the canonical zone master."""
    return {row["zone_id"]: row["zone_type"] for row in _read_official_zones(zones_path)}


def update_zone_live_status(
    zone_id: str,
    requested_status: str,
    verified_by: str,
    database_path: str | Path = DATABASE_PATH,
    zones_path: str | Path = OFFICIAL_ZONES_PATH,
) -> RowMapping:
    """Apply a guarded manual operational status transition for a FREE zone."""
    requested_status = requested_status.strip().upper()
    verified_by = verified_by.strip()
    if requested_status not in {"OPEN", "CLOSED", "SUSPENDED"}:
        raise OperationalValidationError("status must be OPEN, CLOSED, or SUSPENDED")
    if not verified_by:
        raise OperationalValidationError("verified_by must not be empty")
    policy_types = _official_zone_type_map(str(Path(zones_path).resolve()))
    now = utc_now()
    with transaction(database_path, immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM zone_live_status WHERE zone_id = ?" + for_update(connection),
            (zone_id,),
        ).fetchone()
        if row is None:
            raise OperationalNotFoundError(f"Unknown zone_id: {zone_id}")
        policy_type = policy_types.get(zone_id)
        if policy_type != "FREE":
            raise OperationalConflictError(
                f"Policy zone {zone_id} ({policy_type or 'UNKNOWN'}) cannot be changed operationally"
            )
        effective_status = requested_status
        if requested_status == "OPEN" and row["official_capacity"] is not None:
            if row["available_capacity"] <= 0:
                effective_status = "FULL"
        connection.execute(
            """UPDATE zone_live_status
               SET status = ?, verified_at = ?, verified_by = ?, updated_at = ?
               WHERE zone_id = ?""",
            (effective_status, now, verified_by, now, zone_id),
        )
        details = {
            "zone_id": zone_id,
            "policy_zone_type": policy_type,
            "previous_status": row["status"],
            "requested_status": requested_status,
            "effective_status": effective_status,
            "current_vendor_count": row["current_vendor_count"],
            "available_capacity": row["available_capacity"],
        }
        _audit(
            connection,
            "ZONE_LIVE_STATUS_UPDATED",
            "zone",
            zone_id,
            verified_by,
            details,
            now,
        )
        return connection.execute(
            "SELECT * FROM zone_live_status WHERE zone_id = ?", (zone_id,)
        ).fetchone()


def _audit(
    connection: DatabaseConnection,
    action: str,
    entity_type: str,
    entity_id: str,
    performed_by: str | None,
    details: dict[str, Any],
    created_at: str,
) -> None:
    connection.execute(
        """INSERT INTO admin_audit_log
           (audit_id, action, entity_type, entity_id, performed_by, details_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            f"AUD_{uuid.uuid4().hex}",
            action,
            entity_type,
            entity_id,
            performed_by,
            json.dumps(details, sort_keys=True),
            created_at,
        ),
    )


def _increment_zone_occupancy(
    connection: DatabaseConnection,
    zone: RowMapping,
    now: str,
) -> tuple[int, int | None, str]:
    old_count = zone["current_vendor_count"]
    capacity = zone["official_capacity"]
    new_count = old_count + 1
    if capacity is None:
        new_available = None
        new_status = zone["status"]
    else:
        if zone["available_capacity"] <= 0 or new_count > capacity:
            raise OperationalConflictError(f"Zone {zone['zone_id']} has no available capacity")
        new_available = capacity - new_count
        new_status = "FULL" if new_available == 0 else zone["status"]
    connection.execute(
        """UPDATE zone_live_status
           SET current_vendor_count = ?, available_capacity = ?, status = ?, updated_at = ?
           WHERE zone_id = ?""",
        (new_count, new_available, new_status, now, zone["zone_id"]),
    )
    return new_count, new_available, new_status


def _decrement_zone_occupancy(
    connection: DatabaseConnection,
    zone: RowMapping,
    policy_zone_type: str,
    now: str,
) -> tuple[int, int | None, str]:
    old_count = zone["current_vendor_count"]
    if old_count <= 0:
        raise OperationalConflictError(
            f"Cannot release allocation: zone {zone['zone_id']} occupancy is already zero"
        )
    capacity = zone["official_capacity"]
    new_count = old_count - 1
    new_available = None if capacity is None else max(0, capacity - new_count)
    new_status = zone["status"]
    if capacity is not None and new_count >= capacity:
        new_status = "FULL"
    if (
        new_status == "FULL"
        and policy_zone_type == "FREE"
        and (capacity is None or new_count < capacity)
    ):
        new_status = "OPEN"
    connection.execute(
        """UPDATE zone_live_status
           SET current_vendor_count = ?, available_capacity = ?, status = ?, updated_at = ?
           WHERE zone_id = ?""",
        (new_count, new_available, new_status, now, zone["zone_id"]),
    )
    return new_count, new_available, new_status


def release_allocation_in_transaction(
    connection: DatabaseConnection,
    allocation_id: str,
    released_by: str,
    now: str,
    zones_path: str | Path = OFFICIAL_ZONES_PATH,
) -> RowMapping | None:
    """Release an allocation and update authoritative zone occupancy atomically."""
    allocation = connection.execute(
        "SELECT * FROM allocations WHERE allocation_id = ? AND status = 'ACTIVE'" + for_update(connection),
        (allocation_id,),
    ).fetchone()
    if allocation is None:
        return None
    zone = connection.execute(
        "SELECT * FROM zone_live_status WHERE zone_id = ?" + for_update(connection),
        (allocation["zone_id"],),
    ).fetchone()
    if zone is None:
        raise OperationalNotFoundError(f"Unknown zone_id: {allocation['zone_id']}")
    zone = _zone_with_derived_occupancy(connection, zone)
    connection.execute(
        """UPDATE allocations
           SET status = 'RELEASED', released_at = ?, updated_at = ?
           WHERE allocation_id = ? AND status = 'ACTIVE'""",
        (now, now, allocation_id),
    )
    new_count, available_capacity, zone_status = _decrement_zone_occupancy(
        connection,
        zone,
        _official_zone_type_map(str(Path(zones_path).resolve())).get(allocation["zone_id"], ""),
        now,
    )
    details = {
        "allocation_id": allocation_id,
        "vendor_id": allocation["vendor_id"],
        "zone_id": allocation["zone_id"],
        "slot_id": allocation["slot_id"],
        "previous_count": zone["current_vendor_count"],
        "current_count": new_count,
        "available_capacity": available_capacity,
        "zone_status": zone_status,
    }
    _audit(connection, "ALLOCATION_RELEASED", "allocation", allocation_id, released_by, details, now)
    _audit(connection, "ZONE_OCCUPANCY_DECREMENTED", "zone", allocation["zone_id"], released_by, details, now)
    return connection.execute(
        "SELECT * FROM allocations WHERE allocation_id = ?", (allocation_id,)
    ).fetchone()


def approve_allocation_request(
    request_id: str,
    approved_by: str,
    notes: str | None = None,
    database_path: str | Path = DATABASE_PATH,
    zones_path: str | Path = OFFICIAL_ZONES_PATH,
) -> dict[str, RowMapping | None]:
    """Atomically approve a new allocation or complete a relocation."""
    approved_by = approved_by.strip()
    if not approved_by:
        raise OperationalValidationError("approved_by must not be empty")
    policy_types = _official_zone_type_map(str(Path(zones_path).resolve()))
    now = utc_now()
    allocation_id = f"ALLOC_{uuid.uuid4().hex}"

    # BEGIN IMMEDIATE obtains the SQLite write reservation before any capacity
    # read, serializing competing approvals for a final available slot.
    with transaction(database_path, immediate=True) as connection:
        request = connection.execute(
            "SELECT * FROM allocation_requests WHERE request_id = ?" + for_update(connection),
            (request_id,),
        ).fetchone()
        if request is None:
            raise OperationalNotFoundError(f"Unknown allocation request: {request_id}")
        if request["status"] != "PENDING":
            raise OperationalConflictError(
                f"Only PENDING requests can be approved (current status: {request['status']})"
            )
        requested_slot = None
        if request.get("slot_id"):
            requested_slot = connection.execute(
                "SELECT * FROM pilot_slots WHERE zone_id=? AND slot_id=?" + for_update(connection),
                (request["zone_id"], request["slot_id"]),
            ).fetchone()
            if (not requested_slot or requested_slot["is_deleted"] or
                    requested_slot["status"] != "RESERVED" or
                    requested_slot["pending_request_id"] != request_id):
                raise OperationalConflictError("Requested slot is no longer reserved for this application")
        profile = connection.execute("SELECT business_category FROM operational_vendors WHERE vendor_id=?", (request["vendor_id"],)).fetchone()
        if profile is not None and request["business_category"] and request["business_category"] != profile["business_category"]:
            raise OperationalConflictError("Vendor business category changed; submit an updated application before approval")

        target_zone = connection.execute(
            "SELECT * FROM zone_live_status WHERE zone_id = ?" + for_update(connection),
            (request["zone_id"],),
        ).fetchone()
        if target_zone is None:
            raise OperationalNotFoundError(f"Unknown zone_id: {request['zone_id']}")
        target_zone = _zone_with_derived_occupancy(connection, target_zone)
        from backend.services.eligibility import zone_ineligibility_reason
        reason = zone_ineligibility_reason(policy_types.get(request["zone_id"]), target_zone)
        if reason:
            raise OperationalConflictError(f"Zone {request['zone_id']} is ineligible ({reason})")
        if target_zone["official_capacity"] is not None and target_zone["available_capacity"] <= 0:
            raise OperationalConflictError(f"Zone {request['zone_id']} has no available capacity")
        if target_zone["status"] != "OPEN":
            raise OperationalConflictError(
                f"Zone {request['zone_id']} is not open for allocation (status: {target_zone['status']})"
            )

        active = connection.execute(
            "SELECT * FROM allocations WHERE vendor_id = ? AND status = 'ACTIVE'" + for_update(connection),
            (request["vendor_id"],),
        ).fetchone()
        released_allocation = None
        old_zone_id = None
        if request["request_type"] == "NEW_ALLOCATION":
            if active is not None:
                raise OperationalConflictError("Vendor already has an active allocation")
        elif request["request_type"] == "RELOCATION":
            if active is None:
                raise OperationalConflictError("Relocation requires an active allocation")
            if active["zone_id"] == request["zone_id"]:
                raise OperationalConflictError("Relocation target must differ from the active allocation zone")
            old_zone_id = active["zone_id"]
            old_zone = connection.execute(
                "SELECT * FROM zone_live_status WHERE zone_id = ?" + for_update(connection),
                (old_zone_id,),
            ).fetchone()
            if old_zone is None:
                raise OperationalNotFoundError(f"Unknown old allocation zone: {old_zone_id}")
            old_zone = _zone_with_derived_occupancy(connection, old_zone)
            connection.execute(
                """UPDATE allocations
                   SET status = 'RELEASED', released_at = ?, updated_at = ?
                   WHERE allocation_id = ? AND status = 'ACTIVE'""",
                (now, now, active["allocation_id"]),
            )
            old_count, old_available, old_status = _decrement_zone_occupancy(
                connection,
                old_zone,
                policy_types.get(old_zone_id, ""),
                now,
            )
            released_allocation = connection.execute(
                "SELECT * FROM allocations WHERE allocation_id = ?", (active["allocation_id"],)
            ).fetchone()
            # Use the same write transaction: any later approval failure also
            # rolls back this release and both audit histories. Do not call the
            # public slot service here, which opens a separate transaction.
            old_slots = connection.execute(
                "SELECT * FROM pilot_slots WHERE zone_id = ? AND assigned_vendor_id = ?",
                (old_zone_id, request["vendor_id"]),
            ).fetchall()
            slot_timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            for slot in old_slots:
                connection.execute(
                    """UPDATE pilot_slots
                       SET status = 'AVAILABLE', assigned_vendor_id = NULL,
                           pending_request_id = NULL, active_allocation_id = NULL, updated_at = ?
                       WHERE zone_id = ? AND slot_id = ?""",
                    (slot_timestamp, old_zone_id, slot["slot_id"]),
                )
                release_details = {
                    "status": "AVAILABLE", "assigned_vendor_id": None,
                    "reason": "RELOCATION", "request_id": request_id,
                    "released_allocation_id": active["allocation_id"],
                    "new_zone_id": request["zone_id"],
                }
                connection.execute(
                    """INSERT INTO pilot_slot_events
                       (zone_id, slot_id, actor, before_json, after_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (old_zone_id, slot["slot_id"], approved_by,
                     json.dumps(dict(slot)), json.dumps(release_details), slot_timestamp),
                )
                _audit(
                    connection, "SLOT_ASSIGNMENT_RELEASED", "pilot_slot",
                    f"{old_zone_id}/{slot['slot_id']}", approved_by,
                    {**release_details, "vendor_id": request["vendor_id"],
                     "zone_id": old_zone_id, "slot_id": slot["slot_id"]}, slot_timestamp,
                )
            old_details = {
                "request_id": request_id,
                "vendor_id": request["vendor_id"],
                "old_zone_id": old_zone_id,
                "new_zone_id": request["zone_id"],
                "old_count": old_zone["current_vendor_count"],
                "new_count": old_count,
                "available_capacity": old_available,
                "zone_status": old_status,
                "request_type": request["request_type"],
            }
            _audit(
                connection, "ALLOCATION_RELEASED", "allocation", active["allocation_id"],
                approved_by, old_details, now,
            )
            _audit(
                connection, "ZONE_OCCUPANCY_DECREMENTED", "zone", old_zone_id,
                approved_by, old_details, now,
            )
        else:
            raise OperationalValidationError(f"Unsupported request_type: {request['request_type']}")

        connection.execute(
            """INSERT INTO allocations
               (allocation_id, vendor_id, zone_id, slot_id, request_id, status, allocated_at,
                released_at, approved_by, allocation_source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, NULL, ?, 'ADMIN_APPROVED', ?, ?)""",
            (
                allocation_id,
                request["vendor_id"],
                request["zone_id"],
                request.get("slot_id"),
                request_id,
                now,
                approved_by,
                now,
                now,
            ),
        )
        new_count, new_available, new_status = _increment_zone_occupancy(
            connection, target_zone, now
        )
        if requested_slot is not None:
            connection.execute(
                """UPDATE pilot_slots SET status='OCCUPIED',assigned_vendor_id=?,
                   pending_request_id=NULL,active_allocation_id=?,updated_at=?
                   WHERE zone_id=? AND slot_id=? AND pending_request_id=?""",
                (request["vendor_id"], allocation_id, now, request["zone_id"], request["slot_id"], request_id),
            )
            connection.execute(
                """INSERT INTO pilot_slot_events(zone_id,slot_id,actor,before_json,after_json,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (request["zone_id"], request["slot_id"], approved_by, json.dumps(dict(requested_slot)),
                 json.dumps({"status": "OCCUPIED", "assigned_vendor_id": request["vendor_id"],
                             "active_allocation_id": allocation_id}), now),
            )
        connection.execute(
            """UPDATE allocation_requests
               SET status = 'APPROVED', reviewed_at = ?, reviewed_by = ?,
                   updated_at = ?, notes = COALESCE(?, notes)
               WHERE request_id = ?""",
            (now, approved_by, now, notes, request_id),
        )

        details = {
            "request_id": request_id,
            "vendor_id": request["vendor_id"],
            "zone_id": request["zone_id"],
            "slot_id": request.get("slot_id"),
            "old_zone_id": old_zone_id,
            "old_count": target_zone["current_vendor_count"],
            "new_count": new_count,
            "available_capacity": new_available,
            "zone_status": new_status,
            "request_type": request["request_type"],
        }
        _audit(
            connection, "ALLOCATION_CREATED", "allocation", allocation_id,
            approved_by, details, now,
        )
        _audit(
            connection, "ZONE_OCCUPANCY_INCREMENTED", "zone", request["zone_id"],
            approved_by, details, now,
        )
        _audit(
            connection, "ALLOCATION_REQUEST_APPROVED", "allocation_request", request_id,
            approved_by, details, now,
        )
        approved_request = connection.execute(
            "SELECT * FROM allocation_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        allocation = connection.execute(
            "SELECT * FROM allocations WHERE allocation_id = ?", (allocation_id,)
        ).fetchone()
        return {
            "request": approved_request,
            "allocation": allocation,
            "released_allocation": released_allocation,
        }


def reject_allocation_request(
    request_id: str,
    reviewed_by: str,
    rejection_reason: str,
    notes: str | None = None,
    database_path: str | Path = DATABASE_PATH,
) -> RowMapping:
    """Atomically reject a pending allocation request without changing occupancy."""
    reviewed_by = reviewed_by.strip()
    rejection_reason = rejection_reason.strip()
    if not reviewed_by:
        raise OperationalValidationError("reviewed_by must not be empty")
    if not rejection_reason:
        raise OperationalValidationError("rejection_reason must not be empty")
    now = utc_now()
    with transaction(database_path, immediate=True) as connection:
        request = connection.execute(
            "SELECT * FROM allocation_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if request is None:
            raise OperationalNotFoundError(f"Unknown allocation request: {request_id}")
        if request["status"] != "PENDING":
            raise OperationalConflictError(
                f"Only PENDING requests can be rejected (current status: {request['status']})"
            )
        connection.execute(
            """UPDATE allocation_requests
               SET status = 'REJECTED', reviewed_at = ?, reviewed_by = ?,
                   rejection_reason = ?, updated_at = ?, notes = COALESCE(?, notes)
               WHERE request_id = ?""",
            (now, reviewed_by, rejection_reason, now, notes, request_id),
        )
        if request.get("slot_id"):
            connection.execute(
                """UPDATE pilot_slots SET status='AVAILABLE',pending_request_id=NULL,updated_at=?
                   WHERE zone_id=? AND slot_id=? AND pending_request_id=?""",
                (now, request["zone_id"], request["slot_id"], request_id),
            )
        details = {
            "request_id": request_id,
            "vendor_id": request["vendor_id"],
            "zone_id": request["zone_id"],
            "request_type": request["request_type"],
        }
        _audit(
            connection, "ALLOCATION_REQUEST_REJECTED", "allocation_request", request_id,
            reviewed_by, details, now,
        )
        return connection.execute(
            "SELECT * FROM allocation_requests WHERE request_id = ?", (request_id,)
        ).fetchone()


def get_operational_vendor(vendor_id: str, database_path: str | Path = DATABASE_PATH):
    return _fetch_one(
        "SELECT * FROM operational_vendors WHERE vendor_id = ?",
        (vendor_id,),
        database_path,
    )


def create_operational_vendor(
    profile: dict[str, Any],
    database_path: str | Path = DATABASE_PATH,
) -> RowMapping:
    """Register a non-PII operational vendor and write its audit row atomically."""
    now = utc_now()
    vendor_id = f"NV_{uuid.uuid4().hex}"
    with transaction(database_path, immediate=True) as connection:
        application_vendor_id = _next_application_vendor_id(
            connection, _application_id_year(now, datetime.now(timezone.utc).year)
        )
        connection.execute(
            """INSERT INTO operational_vendors (
                   vendor_id, application_vendor_id, vendor_type, full_name, business_name, business_category, preferred_division,
                   preferred_locality, priority_profile, parking_preference,
                   transport_preference, market_preference, preferred_language,
                   status, created_at, updated_at
               ) VALUES (?, ?, 'NEW', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)""",
            (
                vendor_id,
                application_vendor_id,
                profile.get("full_name"),
                profile.get("business_name"),
                profile["business_category"],
                profile.get("preferred_division"),
                profile.get("preferred_locality"),
                profile.get("priority_profile", "BALANCED"),
                int(profile.get("parking_preference", False)),
                int(profile.get("transport_preference", False)),
                int(profile.get("market_preference", False)),
                profile.get("preferred_language", "en"),
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM operational_vendors WHERE vendor_id = ?", (vendor_id,)
        ).fetchone()
        details = {
            key: row[key]
            for key in (
                "vendor_type", "full_name", "business_name", "business_category", "preferred_division",
                "preferred_locality", "priority_profile", "parking_preference",
                "transport_preference", "market_preference", "preferred_language", "status",
            )
        }
        _audit(connection, "VENDOR_REGISTERED", "vendor", vendor_id, None, details, now)
        return row


def save_operational_vendor_profile(
    vendor_id: str,
    vendor_type: str,
    profile: dict[str, Any],
    database_path: str | Path = DATABASE_PATH,
) -> RowMapping:
    """Update an operational row or create an overlay for a historical vendor."""
    now = utc_now()
    with transaction(database_path, immediate=True) as connection:
        existing = connection.execute(
            "SELECT created_at, application_vendor_id FROM operational_vendors WHERE vendor_id = ?", (vendor_id,)
        ).fetchone()
        created_at = existing["created_at"] if existing is not None else now
        application_vendor_id = existing["application_vendor_id"] if existing is not None else None
        connection.execute(
            """INSERT INTO operational_vendors (
                   vendor_id, application_vendor_id, vendor_type, full_name, business_name, business_category, preferred_division,
                   preferred_locality, priority_profile, parking_preference,
                   transport_preference, market_preference, preferred_language,
                   status, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(vendor_id) DO UPDATE SET
                   full_name = excluded.full_name,
                   business_name = excluded.business_name,
                   business_category = excluded.business_category,
                   preferred_division = excluded.preferred_division,
                   preferred_locality = excluded.preferred_locality,
                   priority_profile = excluded.priority_profile,
                   parking_preference = excluded.parking_preference,
                   transport_preference = excluded.transport_preference,
                   market_preference = excluded.market_preference,
                   preferred_language = excluded.preferred_language,
                   status = excluded.status,
                   updated_at = excluded.updated_at""",
            (
                vendor_id,
                application_vendor_id,
                vendor_type,
                profile.get("full_name"),
                profile.get("business_name"),
                profile["business_category"],
                profile.get("preferred_division"),
                profile.get("preferred_locality"),
                profile.get("priority_profile", "BALANCED"),
                int(profile.get("parking_preference", False)),
                int(profile.get("transport_preference", False)),
                int(profile.get("market_preference", False)),
                profile.get("preferred_language", "en"),
                profile.get("status", "ACTIVE"),
                created_at,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM operational_vendors WHERE vendor_id = ?", (vendor_id,)
        ).fetchone()
        details = {
            key: row[key]
            for key in (
                "vendor_type", "full_name", "business_name", "business_category", "preferred_division",
                "preferred_locality", "priority_profile", "parking_preference",
                "transport_preference", "market_preference", "preferred_language", "status",
            )
        }
        _audit(connection, "VENDOR_PROFILE_UPDATED", "vendor", vendor_id, None, details, now)
        return row
