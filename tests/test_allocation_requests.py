import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.api as api
from backend.database import connect, initialize_database, utc_now


class AllocationRequestApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "operational" / "test.db"
        initialize_database(self.database_path)
        self.database_patch = patch.object(api, "DATABASE_PATH", self.database_path)
        self.database_patch.start()
        self.auth_patch = patch.object(api, "AUTH_TEST_BYPASS", True)
        self.auth_patch.start()
        self.client = TestClient(api.app)
        with closing(connect(self.database_path)) as connection:
            self.open_zones = [
                row[0]
                for row in connection.execute(
                    """SELECT zone_id FROM zone_live_status
                       WHERE status = 'OPEN' AND available_capacity > 0
                       ORDER BY zone_id LIMIT 6"""
                )
            ]
            self.restricted_zone = connection.execute(
                "SELECT zone_id FROM zone_live_status WHERE status = 'RESTRICTED' LIMIT 1"
            ).fetchone()[0]
            self.no_vending_zone = connection.execute(
                "SELECT zone_id FROM zone_live_status WHERE status = 'NO_VENDING' LIMIT 1"
            ).fetchone()[0]

    def tearDown(self):
        self.client.close()
        self.auth_patch.stop()
        self.database_patch.stop()
        self.temp_directory.cleanup()

    def payload(self, vendor_id="V2024_00001", zone_id=None, request_type="NEW_ALLOCATION"):
        return {
            "vendor_id": vendor_id,
            "zone_id": zone_id or self.open_zones[0],
            "request_type": request_type,
            # These tests exercise capacity/lifecycle with several historical
            # profiles; the API derives their actual category server-side.
            "preferred_division": "Nashik West",
            "notes": "Test request",
        }

    def insert_active_allocation(self, vendor_id, zone_id):
        now = utc_now()
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """INSERT INTO allocations
                   (allocation_id, vendor_id, zone_id, status, allocated_at, created_at, updated_at)
                   VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?)""",
                (f"ALLOC_{vendor_id}", vendor_id, zone_id, now, now, now),
            )
            connection.commit()

    def test_create_fetch_filter_duplicate_and_audit(self):
        response = self.client.post("/api/allocation-requests", json=self.payload())
        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        self.assertEqual(created["status"], "PENDING")
        self.assertTrue(created["request_id"].startswith("REQ_"))

        fetched = self.client.get(f"/api/allocation-requests/{created['request_id']}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json(), created)

        listed = self.client.get("/api/allocation-requests", params={"status": "PENDING"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(listed.json()["requests"][0]["request_id"], created["request_id"])

        duplicate = self.client.post("/api/allocation-requests", json=self.payload())
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn("pending request", duplicate.json()["detail"])

        with closing(connect(self.database_path)) as connection:
            audit = connection.execute(
                "SELECT * FROM admin_audit_log WHERE entity_id = ?", (created["request_id"],)
            ).fetchone()
            count = connection.execute(
                "SELECT current_vendor_count FROM zone_live_status WHERE zone_id = ?",
                (created["zone_id"],),
            ).fetchone()[0]
        self.assertEqual(audit["action"], "ALLOCATION_REQUEST_CREATED")
        self.assertEqual(json.loads(audit["details_json"])["vendor_id"], created["vendor_id"])
        self.assertEqual(count, 0)

    def test_restricted_and_no_vending_zones_are_rejected(self):
        for index, zone_id in enumerate((self.restricted_zone, self.no_vending_zone), start=2):
            response = self.client.post(
                "/api/allocation-requests",
                json=self.payload(vendor_id=f"V2024_{index:05d}", zone_id=zone_id),
            )
            self.assertEqual(response.status_code, 409)
            self.assertIn("OFFICIAL_ZONE_NOT_FREE", response.json()["detail"])

    def test_closed_suspended_and_full_zones_are_rejected(self):
        for index, status in enumerate(("CLOSED", "SUSPENDED", "FULL"), start=10):
            zone_id = self.open_zones[index - 10]
            with closing(connect(self.database_path)) as connection:
                connection.execute(
                    "UPDATE zone_live_status SET status = ? WHERE zone_id = ?", (status, zone_id)
                )
                connection.commit()
            response = self.client.post(
                "/api/allocation-requests",
                json=self.payload(vendor_id=f"V2024_{index:05d}", zone_id=zone_id),
            )
            self.assertEqual(response.status_code, 409)
            self.assertIn(f"ZONE_{status}", response.json()["detail"])

    def test_zero_available_capacity_is_rejected(self):
        zone_id = self.open_zones[3]
        with closing(connect(self.database_path)) as connection:
            capacity = connection.execute(
                "SELECT official_capacity FROM zone_live_status WHERE zone_id = ?", (zone_id,)
            ).fetchone()[0]
            connection.execute(
                """UPDATE zone_live_status
                   SET current_vendor_count = ?, available_capacity = 0, status = 'FULL'
                   WHERE zone_id = ?""",
                (capacity, zone_id),
            )
            connection.commit()
        response = self.client.post(
            "/api/allocation-requests",
            json=self.payload(vendor_id="V2024_00020", zone_id=zone_id),
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("ZONE_FULL", response.json()["detail"])

    def test_active_allocation_rules_for_new_and_relocation(self):
        self.insert_active_allocation("V2024_00030", self.open_zones[0])
        new_request = self.client.post(
            "/api/allocation-requests",
            json=self.payload(vendor_id="V2024_00030", zone_id=self.open_zones[1]),
        )
        self.assertEqual(new_request.status_code, 409)
        self.assertIn("already has an active allocation", new_request.json()["detail"])

        relocation_without_allocation = self.client.post(
            "/api/allocation-requests",
            json=self.payload(
                vendor_id="V2024_00031", zone_id=self.open_zones[1], request_type="RELOCATION"
            ),
        )
        self.assertEqual(relocation_without_allocation.status_code, 409)
        self.assertIn("requires an active allocation", relocation_without_allocation.json()["detail"])

        valid_relocation = self.client.post(
            "/api/allocation-requests",
            json=self.payload(
                vendor_id="V2024_00030", zone_id=self.open_zones[1], request_type="RELOCATION"
            ),
        )
        self.assertEqual(valid_relocation.status_code, 201, valid_relocation.text)

    def test_cancel_pending_and_reject_non_pending_cancellation(self):
        created = self.client.post("/api/allocation-requests", json=self.payload()).json()
        cancelled = self.client.post(
            f"/api/allocation-requests/{created['request_id']}/cancel"
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "CANCELLED")
        with closing(connect(self.database_path)) as connection:
            audit_actions = [
                row[0]
                for row in connection.execute(
                    "SELECT action FROM admin_audit_log WHERE entity_id = ? ORDER BY created_at",
                    (created["request_id"],),
                )
            ]
        self.assertIn("ALLOCATION_REQUEST_CANCELLED", audit_actions)

        repeated = self.client.post(f"/api/allocation-requests/{created['request_id']}/cancel")
        self.assertEqual(repeated.status_code, 409)
        self.assertIn("Only PENDING", repeated.json()["detail"])

    def test_unknown_vendor_and_request_are_not_found(self):
        unknown_vendor = self.client.post(
            "/api/allocation-requests", json=self.payload(vendor_id="UNKNOWN_VENDOR")
        )
        self.assertEqual(unknown_vendor.status_code, 404)
        self.assertEqual(
            self.client.get("/api/allocation-requests/REQ_missing").status_code, 404
        )

    def test_invalid_request_type_and_status_filter_are_unprocessable(self):
        payload = self.payload()
        payload["request_type"] = "INVALID"
        self.assertEqual(
            self.client.post("/api/allocation-requests", json=payload).status_code, 422
        )
        self.assertEqual(
            self.client.get("/api/allocation-requests", params={"status": "INVALID"}).status_code,
            422,
        )

    def test_existing_recommendation_endpoint_still_works(self):
        response = self.client.post(
            "/api/recommendations",
            json={"business": "food", "division": "", "top_n": 1},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["count"], 1)


if __name__ == "__main__":
    unittest.main()
