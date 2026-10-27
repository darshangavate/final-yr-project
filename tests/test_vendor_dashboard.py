import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.api as api
import auth_service
from backend.database import (
    approve_allocation_request,
    cancel_allocation_request,
    connect,
    create_allocation_request,
    initialize_database,
    reject_allocation_request,
    transaction,
)
from backend.vendor_service import register_vendor
from operational_fixtures import copy_saved_geometry


class VendorDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "dashboard.db"
        initialize_database(self.database_path)
        copy_saved_geometry(self.database_path)
        self.database_patch = patch.object(api, "DATABASE_PATH", self.database_path)
        self.database_patch.start()
        self.client = TestClient(api.app)

    def tearDown(self):
        self.client.close()
        self.database_patch.stop()
        self.temp_directory.cleanup()

    def sign_in(self, vendor_id):
        with transaction(self.database_path, immediate=True) as connection:
            token, _ = auth_service._create_session(connection, "VENDOR", vendor_id=vendor_id)
        self.client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)

    def open_zones(self, count):
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                """SELECT zone_id FROM zone_live_status
                   WHERE status = 'OPEN'
                     AND available_capacity > 0
                   ORDER BY zone_id LIMIT ?""",
                (count,),
            ).fetchall()
        return [row["zone_id"] for row in rows]

    def test_own_dashboard_contains_all_statuses_active_allocation_and_counts(self):
        vendor_id = "V2024_00001"
        zones = self.open_zones(5)
        requests = [
            create_allocation_request(vendor_id, zone, "NEW_ALLOCATION", database_path=self.database_path)
            for zone in zones[:4]
        ]
        cancel_allocation_request(requests[0]["request_id"], self.database_path)
        reject_allocation_request(
            requests[1]["request_id"], "officer", "Location unavailable",
            database_path=self.database_path,
        )
        approved = approve_allocation_request(
            requests[2]["request_id"], "officer", database_path=self.database_path
        )
        create_allocation_request(
            "V2024_00002", zones[4], "NEW_ALLOCATION", database_path=self.database_path
        )

        self.sign_in(vendor_id)
        response = self.client.get("/api/vendors/me/dashboard")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        self.assertEqual(body["profile"]["vendor_id"], vendor_id)
        self.assertEqual(body["profile"]["vendor_type"], "EXISTING_IMPORTED")
        self.assertEqual(body["request_summary"], {
            "total_requests": 4,
            "pending_count": 1,
            "approved_count": 1,
            "rejected_count": 1,
            "cancelled_count": 1,
        })
        history = {item["request_id"]: item for item in body["requests"]}
        self.assertEqual(set(history), {row["request_id"] for row in requests})
        self.assertEqual(history[requests[0]["request_id"]]["status"], "CANCELLED")
        self.assertEqual(history[requests[1]["request_id"]]["status"], "REJECTED")
        self.assertEqual(
            history[requests[1]["request_id"]]["rejection_reason"], "Location unavailable"
        )
        self.assertEqual(history[requests[2]["request_id"]]["status"], "APPROVED")
        self.assertEqual(history[requests[3]["request_id"]]["status"], "PENDING")
        self.assertTrue(all(item["zone_description"] for item in body["requests"]))
        self.assertEqual(body["active_allocation"]["allocation_id"], approved["allocation"]["allocation_id"])
        self.assertEqual(body["active_allocation"]["status"], "ACTIVE")
        self.assertIsNotNone(body["active_allocation"]["live_zone_status"])
        self.assertTrue(body["action_state"]["has_active_allocation"])
        self.assertTrue(body["action_state"]["has_pending_request"])
        self.assertFalse(body["action_state"]["can_submit_request"])
        self.assertEqual(body["action_state"]["blocked_reason"], "Pending request already exists")

    def test_new_vendor_empty_dashboard_and_admin_role_separation(self):
        new_vendor = register_vendor({
            "full_name": "Kalyani Ugale",
            "business_category": "food",
            "preferred_division": "Nashik West",
            "preferred_locality": "College Road",
            "priority_profile": "ACCESSIBILITY",
            "parking_preference": True,
            "transport_preference": True,
            "market_preference": False,
            "preferred_language": "mr",
        }, self.database_path)
        self.sign_in(new_vendor["vendor_id"])
        response = self.client.get("/api/vendors/me/dashboard")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile"]["vendor_type"], "NEW")
        self.assertEqual(body["profile"]["full_name"], "Kalyani Ugale")
        self.assertTrue(body["profile"]["vendor_id"].startswith("NV_"))
        self.assertEqual(body["requests"], [])
        self.assertIsNone(body["active_allocation"])
        self.assertTrue(body["action_state"]["can_submit_request"])

        explorer = self.client.get("/api/vendors/me/eligible-zones")
        self.assertEqual(explorer.status_code, 200, explorer.text)
        self.assertGreater(explorer.json()["count"], 0)
        self.assertTrue(all(zone["live_status"] == "OPEN" for zone in explorer.json()["zones"]))

        with transaction(self.database_path, immediate=True) as connection:
            token, _ = auth_service._create_session(connection, "ADMIN", admin_id="officer")
        self.client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)
        forbidden = self.client.get("/api/vendors/me/dashboard")
        self.assertEqual(forbidden.status_code, 403)


if __name__ == "__main__":
    unittest.main()
