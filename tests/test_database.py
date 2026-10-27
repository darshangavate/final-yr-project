import csv
import importlib
from sqlalchemy.exc import IntegrityError
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.database import (
    OFFICIAL_ZONES_PATH,
    connect,
    database_backend,
    get_engine,
    initialize_database,
)


class OperationalDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "operational" / "test.db"
        self.first_summary = initialize_database(self.database_path)

    def tearDown(self):
        self.temp_directory.cleanup()

    def test_engine_selection_supports_sqlite_and_postgresql_urls(self):
        self.assertEqual(database_backend(self.database_path), "sqlite")
        self.assertEqual(get_engine(self.database_path).dialect.name, "sqlite")
        postgres_url = "postgresql+psycopg://user:password@example.invalid/database"
        self.assertEqual(database_backend(postgres_url), "postgresql")
        self.assertEqual(get_engine(postgres_url).dialect.name, "postgresql")

    def test_connection_context_commits_and_closes_on_success(self):
        with connect(self.database_path) as connection:
            connection.execute("UPDATE zone_live_status SET verified_by = 'context-test'")
        self.assertTrue(connection.raw.closed)
        with closing(connect(self.database_path)) as restored:
            self.assertEqual(restored.execute("SELECT verified_by FROM zone_live_status LIMIT 1").fetchone()[0], "context-test")

    def test_connection_context_rolls_back_and_closes_on_error(self):
        with self.assertRaisesRegex(RuntimeError, "abort workflow"):
            with connect(self.database_path) as connection:
                connection.execute("UPDATE zone_live_status SET verified_by = 'must-rollback'")
                raise RuntimeError("abort workflow")
        self.assertTrue(connection.raw.closed)
        with closing(connect(self.database_path)) as restored:
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM zone_live_status WHERE verified_by = 'must-rollback'").fetchone()[0], 0)

    def test_all_official_zones_are_seeded_once(self):
        with OFFICIAL_ZONES_PATH.open(newline="", encoding="utf-8-sig") as source:
            official_count = sum(1 for _ in csv.DictReader(source))
        self.assertEqual(self.first_summary["official_zones"], official_count)
        self.assertEqual(self.first_summary["zone_live_status_rows"], official_count)
        self.assertEqual(self.first_summary["inserted"], official_count)

        second_summary = initialize_database(self.database_path)
        self.assertEqual(second_summary["inserted"], 0)
        self.assertEqual(second_summary["already_existing"], official_count)
        self.assertEqual(second_summary["zone_live_status_rows"], official_count)

    def test_zone_type_status_capacity_and_zero_occupancy_mapping(self):
        # Compare seeded rows with the canonical source outside SQLite.
        with OFFICIAL_ZONES_PATH.open(newline="", encoding="utf-8-sig") as source:
            official = list(csv.DictReader(source))
        with closing(connect(self.database_path)) as connection:
            seeded = {row["zone_id"]: row for row in connection.execute("SELECT * FROM zone_live_status")}
        self.assertTrue(official)
        for source_row in official:
            row = seeded[source_row["zone_id"]]
            expected_status = {"FREE": "OPEN", "RESTRICTED": "RESTRICTED", "NO_VENDING": "NO_VENDING"}[
                source_row["zone_type"]
            ]
            self.assertEqual(row["status"], expected_status)
            self.assertEqual(row["current_vendor_count"], 0)
            if source_row["capacity"]:
                capacity = int(float(source_row["capacity"]))
                self.assertEqual(row["official_capacity"], capacity)
                self.assertEqual(row["available_capacity"], capacity)
            else:
                self.assertIsNone(row["official_capacity"])
                self.assertIsNone(row["available_capacity"])

    def test_constraints_and_partial_unique_index(self):
        with closing(connect(self.database_path)) as connection:
            zone_id = connection.execute("SELECT zone_id FROM zone_live_status LIMIT 1").fetchone()[0]
            with self.assertRaises(IntegrityError):
                connection.execute(
                    "UPDATE zone_live_status SET current_vendor_count = -1 WHERE zone_id = ?", (zone_id,)
                )
            now = "2026-01-01T00:00:00+00:00"
            connection.execute(
                """INSERT INTO allocations
                   (allocation_id, vendor_id, zone_id, status, allocated_at, created_at, updated_at)
                   VALUES ('a1', 'v1', ?, 'ACTIVE', ?, ?, ?)""",
                (zone_id, now, now, now),
            )
            with self.assertRaises(IntegrityError):
                connection.execute(
                    """INSERT INTO allocations
                       (allocation_id, vendor_id, zone_id, status, allocated_at, created_at, updated_at)
                       VALUES ('a2', 'v1', ?, 'ACTIVE', ?, ?, ?)""",
                    (zone_id, now, now, now),
                )

    def test_existing_fastapi_app_imports(self):
        module = importlib.import_module("backend.api")
        self.assertIsNotNone(module.app)

    def test_existing_database_gains_auth_schema_without_losing_zone_state(self):
        with closing(connect(self.database_path)) as connection:
            zone_id = connection.execute(
                "SELECT zone_id FROM zone_live_status ORDER BY zone_id LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                "UPDATE zone_live_status SET verified_by = 'migration-test' WHERE zone_id = ?",
                (zone_id,),
            )
            connection.execute("DROP TABLE auth_sessions")
            connection.execute("DROP TABLE auth_otp_challenges")
            connection.execute("DROP TABLE vendor_auth_identities")
            connection.commit()

        initialize_database(self.database_path)

        with closing(connect(self.database_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            verified_by = connection.execute(
                "SELECT verified_by FROM zone_live_status WHERE zone_id = ?", (zone_id,)
            ).fetchone()[0]
        self.assertTrue(
            {
                "vendor_auth_identities", "auth_otp_challenges", "auth_sessions",
                "whatsapp_conversations", "whatsapp_processed_messages",
            }.issubset(tables)
        )
        self.assertEqual(verified_by, "migration-test")


if __name__ == "__main__":
    unittest.main()
