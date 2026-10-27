import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from scripts.migrate_sqlite_to_postgres import (
    build_diagnostics,
    normalize_value,
    open_source_read_only,
    parse_args,
    plan_table,
    values_equal,
    target_engine,
)


class SQLiteToPostgresMigrationTests(unittest.TestCase):
    def test_render_postgres_alias_uses_existing_driver_without_connecting(self):
        with patch("scripts.migrate_sqlite_to_postgres.create_engine") as create:
            target_engine("postgres://user:password@example.invalid/database")
        create.assert_called_once_with("postgresql+psycopg://user:password@example.invalid/database", pool_pre_ping=True)

    def test_explicit_mode_is_required(self):
        with self.assertRaises(SystemExit):
            parse_args([])
        self.assertTrue(parse_args(["--dry-run"]).dry_run)
        self.assertTrue(parse_args(["--apply"]).apply)
        self.assertEqual(parse_args(["--diagnose", "pilot_slots"]).diagnose, "pilot_slots")

    def test_reconciliation_counts_insert_update_and_unchanged(self):
        source = [
            {"zone_id": "A", "status": "OPEN"},
            {"zone_id": "B", "status": "FULL"},
            {"zone_id": "C", "status": "CLOSED"},
        ]
        target = [
            {"zone_id": "A", "status": "OPEN"},
            {"zone_id": "B", "status": "OPEN"},
        ]
        plan = plan_table("zone_live_status", source, target)
        self.assertEqual((plan.inserted, plan.updated, plan.unchanged), (1, 1, 1))

    def test_counter_never_plans_to_lower_target(self):
        source = [{"year": 2026, "last_sequence": 4}]
        target = [{"year": 2026, "last_sequence": 8}]
        plan = plan_table("application_vendor_id_counters", source, target)
        self.assertEqual((plan.updated, plan.unchanged), (0, 1))

    def test_json_comparison_ignores_serialization_formatting(self):
        compact = normalize_value("state_json", '{"step":"MAIN","values":[1,2]}')
        formatted = normalize_value("state_json", '{ "values": [1, 2], "step": "MAIN" }')
        self.assertEqual(compact, formatted)

    def test_semantic_cross_database_normalization(self):
        self.assertTrue(values_equal("width_m", 12.5, Decimal("12.5")))
        self.assertTrue(values_equal("is_deleted", 0, False))
        self.assertTrue(values_equal(
            "updated_at",
            "2026-10-14T10:00:00Z",
            datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc),
        ))
        self.assertTrue(values_equal(
            "y_m", 2208984.064878301, 2208984.0648783
        ))
        self.assertFalse(values_equal("x_m", 12.5, 12.5001))
        self.assertFalse(values_equal("parent_boundary_id", "", None))

    def test_diagnostics_are_column_level_and_redact_vendor_assignment(self):
        source = [{
            "zone_id": "NN_F_013", "slot_id": "S001", "x_m": 12.5,
            "status": "AVAILABLE", "assigned_vendor_id": "vendor-one",
        }]
        target = [{
            "zone_id": "NN_F_013", "slot_id": "S001", "x_m": Decimal("12.5"),
            "status": "BLOCKED", "assigned_vendor_id": "vendor-two",
        }]
        diagnostics = build_diagnostics(
            "pilot_slots", source, target,
            primary_key=("zone_id", "slot_id"),
            source_schema_info=[], target_schema_info=[],
        )
        self.assertEqual(diagnostics["column_mismatch_counts"], {
            "assigned_vendor_id": 1, "status": 1,
        })
        self.assertEqual(diagnostics["column_representation_difference_counts"], {"x_m": 1})
        assigned = next(
            item for item in diagnostics["mismatches"]
            if item["column"] == "assigned_vendor_id"
        )
        self.assertEqual(assigned["sqlite_value"], "<redacted>")
        self.assertEqual(assigned["postgres_value"], "<redacted>")
        self.assertEqual(assigned["key"], {"zone_id": "NN_F_013", "slot_id": "S001"})

    def test_source_connection_is_read_only(self):
        connection = open_source_read_only()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE migration_write_probe(value INTEGER)")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
