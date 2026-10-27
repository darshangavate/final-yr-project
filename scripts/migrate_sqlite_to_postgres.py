"""One-time, reconciliatory SQLite-to-PostgreSQL operational data migration.

This utility never initializes schemas, deletes target rows, or changes the
source database. Run with --dry-run to inspect and with --apply to write.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import Connection, Engine, URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as postgresql_insert


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.config import get_database_backend, get_database_url  # noqa: E402


SOURCE_PATH = ROOT / "data" / "operational" / "vendor_management.db"
REPORT_PATH = ROOT / "reports" / "migrations" / "sqlite_to_postgres_migration.json"

TABLE_ORDER = (
    "zone_live_status",
    "operational_vendors",
    "application_vendor_id_counters",
    "vendor_auth_identities",
    "auth_otp_challenges",
    "auth_sessions",
    "allocation_requests",
    "allocations",
    "pilot_slot_layouts",
    "pilot_slots",
    "pilot_slot_events",
    "whatsapp_conversations",
    "whatsapp_processed_messages",
    "admin_audit_log",
)

CONFLICT_COLUMNS = {
    "zone_live_status": ("zone_id",),
    "operational_vendors": ("vendor_id",),
    "application_vendor_id_counters": ("year",),
    # Bootstrap identity UUIDs need not match the source UUID. phone_hash is
    # the actual stable unique identity, but it is never printed or reported.
    "vendor_auth_identities": ("phone_hash",),
    "auth_otp_challenges": ("challenge_id",),
    "auth_sessions": ("session_id",),
    "allocation_requests": ("request_id",),
    "allocations": ("allocation_id",),
    "pilot_slot_layouts": ("zone_id",),
    "pilot_slots": ("zone_id", "slot_id"),
    "pilot_slot_events": ("event_id",),
    "whatsapp_conversations": ("phone_hash",),
    "whatsapp_processed_messages": ("message_id",),
    "admin_audit_log": ("audit_id",),
}

JSON_COLUMNS = {
    "metadata_json", "geometry_json", "before_json", "after_json",
    "details_json", "state_json",
}

CRITICAL_TABLES = {
    "zone_live_status",
    "operational_vendors",
    "allocation_requests",
    "allocations",
    "vendor_auth_identities",
    "pilot_slots",
}

BOOLEAN_COLUMNS = {
    "parking_preference", "transport_preference", "market_preference",
    "is_verified", "is_deleted",
}

SENSITIVE_COLUMNS = {
    "phone_hash", "otp_hash", "session_token_hash", "new_vendor_phone_hash",
    "assigned_vendor_id",
}

FLOAT_TOLERANCE = Decimal("1e-12")
FLOAT_RELATIVE_TOLERANCE = Decimal("1e-14")
CONSOLE_DIAGNOSTIC_LIMIT = 20


class MigrationSafetyError(RuntimeError):
    """Raised when migration preconditions are unsafe or inconsistent."""


@dataclass(frozen=True)
class TablePlan:
    table: str
    source_count: int
    target_count_before: int
    inserted: int
    updated: int
    unchanged: int
    target_only: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile the local operational SQLite database into configured PostgreSQL."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Inspect and compare; write nothing.")
    mode.add_argument("--apply", action="store_true", help="Apply the migration transaction.")
    mode.add_argument(
        "--diagnose",
        choices=("pilot_slots",),
        metavar="TABLE",
        help="Stage one supported table, compare it, and always roll back.",
    )
    return parser.parse_args(argv)


def validate_configuration() -> str:
    if not SOURCE_PATH.is_file():
        raise MigrationSafetyError("Source SQLite database does not exist")
    database_url = get_database_url()
    if not database_url:
        raise MigrationSafetyError("DATABASE_URL is required for the PostgreSQL target")
    if get_database_backend() != "postgresql":
        raise MigrationSafetyError("DATABASE_URL must select PostgreSQL")
    parsed = URL.create("sqlite", database=str(SOURCE_PATH.resolve()))
    if str(database_url).startswith("sqlite:") or str(parsed) == str(database_url):
        raise MigrationSafetyError("Source and target databases must be different")
    return database_url


def open_source_read_only() -> sqlite3.Connection:
    uri = f"file:{SOURCE_PATH.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def target_engine(database_url: str) -> Engine:
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url.removeprefix("postgres://")
    normalized = (
        "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
        if database_url.startswith("postgresql://")
        else database_url
    )
    return create_engine(normalized, pool_pre_ping=True)


def source_schema(connection: sqlite3.Connection) -> dict[str, list[str]]:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = sorted(set(TABLE_ORDER) - tables)
    if missing:
        raise MigrationSafetyError("Source schema is missing required tables: " + ", ".join(missing))
    return {
        table: [row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        for table in TABLE_ORDER
    }


def source_schema_details(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [
        {
            "column": row["name"],
            "declared_type": row["type"] or "",
            "nullable": not bool(row["notnull"]) and not bool(row["pk"]),
            "default": row["dflt_value"],
            "primary_key_position": int(row["pk"]),
        }
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    ]


def target_schema_details(connection: Connection, table: str) -> list[dict[str, Any]]:
    inspector = inspect(connection)
    primary_key_columns = set(
        inspector.get_pk_constraint(table).get("constrained_columns") or []
    )
    return [
        {
            "column": column["name"],
            "declared_type": str(column["type"]),
            "nullable": bool(column["nullable"]),
            "default": column.get("default"),
            "primary_key": column["name"] in primary_key_columns,
        }
        for column in inspector.get_columns(table)
    ]


def reflect_target(connection: Connection) -> dict[str, Table]:
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    missing = sorted(set(TABLE_ORDER) - existing)
    if missing:
        raise MigrationSafetyError(
            "Target schema is not initialized; missing tables: " + ", ".join(missing)
        )
    metadata = MetaData()
    metadata.reflect(bind=connection, only=list(TABLE_ORDER))
    return {table: metadata.tables[table] for table in TABLE_ORDER}


def validate_columns(
    sqlite_columns: Mapping[str, list[str]], target_tables: Mapping[str, Table]
) -> None:
    for table in TABLE_ORDER:
        source = set(sqlite_columns[table])
        target = set(target_tables[table].c.keys())
        missing_target = sorted(source - target)
        missing_source = sorted(target - source)
        if missing_target or missing_source:
            details = []
            if missing_target:
                details.append("target missing " + ", ".join(missing_target))
            if missing_source:
                details.append("source missing " + ", ".join(missing_source))
            raise MigrationSafetyError(f"Column mismatch for {table}: {'; '.join(details)}")


def read_source_rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()]


def read_target_rows(connection: Connection, table: Table) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(select(table)).mappings().all()]


def row_key(row: Mapping[str, Any], key_columns: Iterable[str]) -> tuple[Any, ...]:
    return tuple(row[column] for column in key_columns)


def _canonical_number(value: int | float | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _canonical_datetime(value: Any) -> str | None:
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return str(value)


def normalize_value(column: str, value: Any) -> Any:
    if value is None:
        return None
    if column in JSON_COLUMNS:
        return normalize_json(value)
    if column in BOOLEAN_COLUMNS and isinstance(value, (bool, int, Decimal)):
        if value in (0, 1, Decimal(0), Decimal(1)):
            return bool(value)
    if column.endswith("_at"):
        canonical_datetime = _canonical_datetime(value)
        if canonical_datetime is not None:
            return canonical_datetime
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return _canonical_number(value)
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def normalized_row(row: Mapping[str, Any], columns: Iterable[str]) -> tuple[Any, ...]:
    return tuple(normalize_value(column, row.get(column)) for column in columns)


def values_equal(column: str, source: Any, target: Any) -> bool:
    left = normalize_value(column, source)
    right = normalize_value(column, target)
    if isinstance(left, Decimal) and isinstance(right, Decimal):
        if isinstance(source, (float, Decimal)) or isinstance(target, (float, Decimal)):
            tolerance = max(
                FLOAT_TOLERANCE,
                max(abs(left), abs(right)) * FLOAT_RELATIVE_TOLERANCE,
            )
            return abs(left - right) <= tolerance
    return left == right


def rows_equal(table: str, source: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    columns = tuple(source.keys())
    if table == "application_vendor_id_counters":
        return int(target["last_sequence"]) >= int(source["last_sequence"])
    return all(values_equal(column, source.get(column), target.get(column)) for column in columns)


def plan_table(
    table: str,
    source_rows: list[dict[str, Any]],
    target_rows: list[dict[str, Any]],
) -> TablePlan:
    keys = CONFLICT_COLUMNS[table]
    source_by_key = {row_key(row, keys): row for row in source_rows}
    target_by_key = {row_key(row, keys): row for row in target_rows}
    if len(source_by_key) != len(source_rows):
        raise MigrationSafetyError(f"Source contains duplicate reconciliation keys in {table}")
    inserted = updated = unchanged = 0
    for key, source in source_by_key.items():
        target = target_by_key.get(key)
        if target is None:
            inserted += 1
        elif rows_equal(table, source, target):
            unchanged += 1
        else:
            updated += 1
    return TablePlan(
        table=table,
        source_count=len(source_rows),
        target_count_before=len(target_rows),
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        target_only=len(set(target_by_key) - set(source_by_key)),
    )


def upsert_table(connection: Connection, table: Table, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    key_columns = CONFLICT_COLUMNS[table.name]
    statement = postgresql_insert(table)
    if table.name == "application_vendor_id_counters":
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.year],
            set_={
                "last_sequence": text(
                    "GREATEST(application_vendor_id_counters.last_sequence, excluded.last_sequence)"
                )
            },
        )
    else:
        updates = {
            column.name: getattr(statement.excluded, column.name)
            for column in table.columns
            if column.name not in key_columns
        }
        statement = statement.on_conflict_do_update(
            index_elements=[table.c[column] for column in key_columns],
            set_=updates,
        )
    connection.execute(statement, rows)


def advance_pilot_event_sequence(connection: Connection, source_max: int | None) -> dict[str, Any]:
    sequence_name = connection.execute(
        text("SELECT pg_get_serial_sequence('pilot_slot_events', 'event_id')")
    ).scalar_one_or_none()
    if not sequence_name:
        raise MigrationSafetyError("PostgreSQL pilot_slot_events sequence was not found")
    current = connection.execute(
        text("SELECT pg_sequence_last_value(CAST(:sequence_name AS regclass))"),
        {"sequence_name": sequence_name},
    ).scalar_one_or_none()
    table_max = connection.execute(text("SELECT MAX(event_id) FROM pilot_slot_events")).scalar_one()
    required = max(int(current or 0), int(source_max or 0), int(table_max or 0), 1)
    connection.execute(
        text("SELECT setval(CAST(:sequence_name AS regclass), :required, true)"),
        {"sequence_name": sequence_name, "required": required},
    )
    return {"advanced_to_at_least": required, "source_max_event_id": source_max or 0}


def verify_table(
    table: str,
    source_rows: list[dict[str, Any]],
    target_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    keys = CONFLICT_COLUMNS[table]
    source_by_key = {row_key(row, keys): row for row in source_rows}
    target_by_key = {row_key(row, keys): row for row in target_rows}
    missing = set(source_by_key) - set(target_by_key)
    extra = set(target_by_key) - set(source_by_key)
    mismatched = {
        key for key in set(source_by_key) & set(target_by_key)
        if not rows_equal(table, source_by_key[key], target_by_key[key])
    }
    status = "PASS" if not missing and not extra and not mismatched else "MISMATCH"
    return {
        "status": status,
        "missing_source_rows_on_target": len(missing),
        "extra_target_rows": len(extra),
        "value_mismatches": len(mismatched),
        "value_level_checked": table in CRITICAL_TABLES,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return "<binary>"
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _diagnostic_value(table: str, column: str, value: Any) -> Any:
    if table != "pilot_slots" or column in SENSITIVE_COLUMNS:
        return None if value is None else "<redacted>"
    return _json_safe(value)


def _diagnostic_key(
    table: str, key_columns: Iterable[str], key: tuple[Any, ...]
) -> dict[str, Any]:
    return {
        column: _diagnostic_value(table, column, value)
        for column, value in zip(key_columns, key)
    }


def build_diagnostics(
    table: str,
    source_rows: list[dict[str, Any]],
    target_rows: list[dict[str, Any]],
    *,
    primary_key: Iterable[str],
    source_schema_info: list[dict[str, Any]],
    target_schema_info: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build safe column-level mismatch and representation diagnostics."""
    key_columns = CONFLICT_COLUMNS[table]
    source_by_key = {row_key(row, key_columns): row for row in source_rows}
    target_by_key = {row_key(row, key_columns): row for row in target_rows}
    mismatches: list[dict[str, Any]] = []
    representation_differences: list[dict[str, Any]] = []
    column_counts: dict[str, int] = {}
    representation_counts: dict[str, int] = {}

    def record(
        collection: list[dict[str, Any]],
        key: tuple[Any, ...],
        column: str,
        sqlite_value: Any,
        postgres_value: Any,
    ) -> None:
        collection.append({
            "table": table,
            "primary_key": list(primary_key),
            "key": _diagnostic_key(table, key_columns, key),
            "column": column,
            "sqlite_value": _diagnostic_value(
                table, column, normalize_value(column, sqlite_value)
            ),
            "postgres_value": _diagnostic_value(
                table, column, normalize_value(column, postgres_value)
            ),
            "sqlite_type": type(sqlite_value).__name__,
            "postgres_type": type(postgres_value).__name__,
        })

    for key in sorted(set(source_by_key) - set(target_by_key), key=repr):
        record(mismatches, key, "__missing_row__", "present", None)
        column_counts["__missing_row__"] = column_counts.get("__missing_row__", 0) + 1
    for key in sorted(set(target_by_key) - set(source_by_key), key=repr):
        record(mismatches, key, "__extra_row__", None, "present")
        column_counts["__extra_row__"] = column_counts.get("__extra_row__", 0) + 1
    for key in sorted(set(source_by_key) & set(target_by_key), key=repr):
        source = source_by_key[key]
        target = target_by_key[key]
        for column in source:
            sqlite_value = source.get(column)
            postgres_value = target.get(column)
            if not values_equal(column, sqlite_value, postgres_value):
                record(mismatches, key, column, sqlite_value, postgres_value)
                column_counts[column] = column_counts.get(column, 0) + 1
            elif type(sqlite_value) is not type(postgres_value) or sqlite_value != postgres_value:
                record(
                    representation_differences, key, column, sqlite_value, postgres_value
                )
                representation_counts[column] = representation_counts.get(column, 0) + 1

    return {
        "mismatch_count": len(mismatches),
        "mismatched_row_count": len({json.dumps(item["key"], sort_keys=True) for item in mismatches}),
        "sample_mismatches": mismatches[:CONSOLE_DIAGNOSTIC_LIMIT],
        "mismatches": mismatches,
        "column_mismatch_counts": dict(sorted(column_counts.items())),
        "representation_difference_count": len(representation_differences),
        "column_representation_difference_counts": dict(sorted(representation_counts.items())),
        "sample_representation_differences": representation_differences[:CONSOLE_DIAGNOSTIC_LIMIT],
        "representation_differences": representation_differences,
        "source_schema": source_schema_info,
        "target_schema": target_schema_info,
    }


def scalar_count(connection: Connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def global_validation(
    source: sqlite3.Connection,
    target: Connection,
    source_rows: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    source_active = int(source.execute(
        "SELECT COUNT(*) FROM allocations WHERE status = 'ACTIVE'"
    ).fetchone()[0])
    source_pending = int(source.execute(
        "SELECT COUNT(*) FROM allocation_requests WHERE status = 'PENDING'"
    ).fetchone()[0])
    source_event_max = source.execute("SELECT MAX(event_id) FROM pilot_slot_events").fetchone()[0]
    target_event_max = target.execute(text("SELECT MAX(event_id) FROM pilot_slot_events")).scalar_one()
    zone_keys_match = {
        row["zone_id"] for row in source_rows["zone_live_status"]
    } == {
        row["zone_id"] for row in target.execute(text("SELECT zone_id FROM zone_live_status")).mappings()
    }
    return {
        "zone_live_status_key_count": len(source_rows["zone_live_status"]),
        "zone_live_status_keys_match": zone_keys_match,
        "expected_zone_count_is_308": len(source_rows["zone_live_status"]) == 308,
        "active_allocation_count": {
            "sqlite": source_active,
            "postgresql": scalar_count(target, "SELECT COUNT(*) FROM allocations WHERE status = 'ACTIVE'"),
        },
        "pending_request_count": {
            "sqlite": source_pending,
            "postgresql": scalar_count(target, "SELECT COUNT(*) FROM allocation_requests WHERE status = 'PENDING'"),
        },
        "operational_vendor_ids_match": verify_table(
            "operational_vendors", source_rows["operational_vendors"],
            [dict(row) for row in target.execute(text("SELECT * FROM operational_vendors")).mappings()],
        )["status"] == "PASS",
        "phone_identity_key_set_matches": verify_table(
            "vendor_auth_identities", source_rows["vendor_auth_identities"],
            [dict(row) for row in target.execute(text("SELECT * FROM vendor_auth_identities")).mappings()],
        )["status"] == "PASS",
        "pilot_slot_key_set_matches": verify_table(
            "pilot_slots", source_rows["pilot_slots"],
            [dict(row) for row in target.execute(text("SELECT * FROM pilot_slots")).mappings()],
        )["status"] == "PASS",
        "pilot_slot_event_max": {
            "sqlite": int(source_event_max or 0),
            "postgresql": int(target_event_max or 0),
            "matches": int(source_event_max or 0) == int(target_event_max or 0),
        },
    }


def safe_error(error: Exception) -> str:
    if isinstance(error, SQLAlchemyError):
        return f"Database operation failed ({type(error).__name__}); connection details hidden"
    return str(error)


def write_report(report: Mapping[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(REPORT_PATH)


def print_header(mode: str) -> None:
    print(f"Mode: {mode}")
    print("Source backend: SQLite")
    print("Target backend: PostgreSQL")
    print("Source path: data/operational/vendor_management.db")
    print("Target: configured PostgreSQL database (URL hidden)")


def print_table_summary(table_reports: Mapping[str, Mapping[str, Any]]) -> None:
    print("\nTable reconciliation summary")
    print("table | SQLite | PostgreSQL | inserted | updated | unchanged | verification")
    for table in TABLE_ORDER:
        item = table_reports[table]
        print(
            f"{table} | {item['sqlite_rows']} | {item['postgresql_rows']} | "
            f"{item['inserted']} | {item['updated']} | {item['unchanged']} | "
            f"{item['verification']['status']}"
        )


def print_diagnostics(diagnostics: Mapping[str, Mapping[str, Any]]) -> None:
    for table, details in diagnostics.items():
        print(f"\nDiagnostics: {table}")
        print(
            f"mismatches={details['mismatch_count']} "
            f"representation_differences={details['representation_difference_count']}"
        )
        visible = list(details["sample_mismatches"])
        if not visible:
            visible = list(details["sample_representation_differences"])
        for item in visible[:CONSOLE_DIAGNOSTIC_LIMIT]:
            print(
                f"key={item['key']} column={item['column']} "
                f"SQLite={item['sqlite_value']!r} ({item['sqlite_type']}) "
                f"PostgreSQL={item['postgres_value']!r} ({item['postgres_type']})"
            )


def _store_table_reports(
    report: dict[str, Any],
    plans: Mapping[str, TablePlan],
    rows_after: Mapping[str, list[dict[str, Any]]],
    verification: Mapping[str, Mapping[str, Any]],
) -> None:
    for table in TABLE_ORDER:
        plan = plans[table]
        report["tables"][table] = {
            "sqlite_rows": plan.source_count,
            "postgresql_rows": len(rows_after[table]),
            "target_rows_before": plan.target_count_before,
            "inserted": plan.inserted,
            "updated": plan.updated,
            "unchanged": plan.unchanged,
            "target_only_before": plan.target_only,
            "verification": verification[table],
        }


def migrate(*, apply: bool, diagnose_table: str | None = None) -> dict[str, Any]:
    database_url = validate_configuration()
    engine = target_engine(database_url)
    report: dict[str, Any] = {
        "timestamp_utc": utc_now(),
        "mode": "diagnose" if diagnose_table else ("apply" if apply else "dry-run"),
        "source": "data/operational/vendor_management.db",
        "target_backend": "postgresql",
        "status": "started",
        "tables": {},
        "warnings": [],
        "errors": [],
        "failed_tables": [],
        "diagnostics": {},
    }
    source = open_source_read_only()
    try:
        sqlite_columns = source_schema(source)
        source_rows = {table: read_source_rows(source, table) for table in TABLE_ORDER}
        with engine.connect() as target:
            target_tables = reflect_target(target)
            validate_columns(sqlite_columns, target_tables)
            before = {
                table: read_target_rows(target, target_tables[table]) for table in TABLE_ORDER
            }
            plans = {
                table: plan_table(table, source_rows[table], before[table])
                for table in TABLE_ORDER
            }
            target_extra_tables = [table for table, plan in plans.items() if plan.target_only]
            if target_extra_tables:
                report["warnings"].append(
                    "Target-only rows exist in: " + ", ".join(target_extra_tables)
                )
            if apply and target_extra_tables:
                raise MigrationSafetyError(
                    "Apply refused because target-only rows would prevent exact reconciliation: "
                    + ", ".join(target_extra_tables)
                )

            if diagnose_table:
                target.rollback()
                transaction = target.begin()
                try:
                    # pilot_slots depends on its layout row. Both are staged
                    # exactly as migration would do, then always rolled back.
                    upsert_table(
                        target,
                        target_tables["pilot_slot_layouts"],
                        source_rows["pilot_slot_layouts"],
                    )
                    upsert_table(
                        target,
                        target_tables[diagnose_table],
                        source_rows[diagnose_table],
                    )
                    staged_rows = read_target_rows(target, target_tables[diagnose_table])
                    after = dict(before)
                    after[diagnose_table] = staged_rows
                    verification = {
                        table: verify_table(table, source_rows[table], after[table])
                        for table in TABLE_ORDER
                    }
                    report["diagnostics"][diagnose_table] = build_diagnostics(
                        diagnose_table,
                        source_rows[diagnose_table],
                        staged_rows,
                        primary_key=[
                            column.name
                            for column in target_tables[diagnose_table].primary_key.columns
                        ],
                        source_schema_info=source_schema_details(source, diagnose_table),
                        target_schema_info=target_schema_details(target, diagnose_table),
                    )
                    if verification[diagnose_table]["status"] != "PASS":
                        report["failed_tables"] = [diagnose_table]
                    report["status"] = "diagnose-completed"
                    report["persistent_writes"] = False
                finally:
                    transaction.rollback()
                _store_table_reports(report, plans, after, verification)
            elif apply:
                # Reflection and preflight SELECTs autobegin a read transaction.
                # End it before opening the single atomic write transaction.
                target.rollback()
                transaction = target.begin()
                try:
                    for table in TABLE_ORDER:
                        upsert_table(target, target_tables[table], source_rows[table])
                    event_ids = [int(row["event_id"]) for row in source_rows["pilot_slot_events"]]
                    report["pilot_slot_event_sequence"] = advance_pilot_event_sequence(
                        target, max(event_ids) if event_ids else None
                    )
                    after = {
                        table: read_target_rows(target, target_tables[table])
                        for table in TABLE_ORDER
                    }
                    verification = {
                        table: verify_table(table, source_rows[table], after[table])
                        for table in TABLE_ORDER
                    }
                    failures = [table for table, result in verification.items()
                                if result["status"] != "PASS"]
                    if failures:
                        report["failed_tables"] = failures
                        for table in failures:
                            report["diagnostics"][table] = build_diagnostics(
                                table,
                                source_rows[table],
                                after[table],
                                primary_key=[
                                    column.name for column in target_tables[table].primary_key.columns
                                ],
                                source_schema_info=source_schema_details(source, table),
                                target_schema_info=target_schema_details(target, table),
                            )
                        _store_table_reports(report, plans, after, verification)
                        report["status"] = "failed"
                        report["errors"].append(
                            "Post-write verification failed; transaction rolled back for: "
                            + ", ".join(failures)
                        )
                        transaction.rollback()
                        return report
                    report["global_validation"] = global_validation(source, target, source_rows)
                    transaction.commit()
                except Exception:
                    transaction.rollback()
                    raise
            else:
                after = before
                verification = {
                    table: verify_table(table, source_rows[table], before[table])
                    for table in TABLE_ORDER
                }
                report["global_validation"] = global_validation(source, target, source_rows)
            if not diagnose_table:
                _store_table_reports(report, plans, after, verification)
                report["status"] = "completed" if apply else "dry-run-completed"
    finally:
        source.close()
        engine.dispose()
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = (
        f"DIAGNOSE {args.diagnose} (always rollback)"
        if args.diagnose
        else ("APPLY" if args.apply else "DRY RUN (zero writes)")
    )
    print_header(mode)
    report: dict[str, Any]
    try:
        report = migrate(apply=args.apply, diagnose_table=args.diagnose)
        write_report(report)
        print_table_summary(report["tables"])
        if report.get("diagnostics"):
            print_diagnostics(report["diagnostics"])
        print(f"\nSafe report: {REPORT_PATH.relative_to(ROOT)}")
        if report["status"] == "failed":
            print("Migration verification failed. PostgreSQL transaction was rolled back.", file=sys.stderr)
            return 1
        if args.diagnose:
            print("Diagnostic staging complete. PostgreSQL transaction was rolled back.")
        elif not args.apply:
            print("Dry run complete. PostgreSQL was not modified.")
        else:
            print("Migration committed after value-level verification.")
        return 0
    except Exception as error:
        report = {
            "timestamp_utc": utc_now(),
            "mode": "diagnose" if args.diagnose else ("apply" if args.apply else "dry-run"),
            "source": "data/operational/vendor_management.db",
            "target_backend": "postgresql",
            "status": "failed",
            "tables": {},
            "warnings": [],
            "errors": [safe_error(error)],
            "failed_tables": [],
            "diagnostics": {},
        }
        write_report(report)
        print(f"Migration refused: {safe_error(error)}", file=sys.stderr)
        print(f"Safe report: {REPORT_PATH.relative_to(ROOT)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
