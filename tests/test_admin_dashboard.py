import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.api as api
from backend.database import connect, initialize_database
from operational_fixtures import copy_saved_geometry


class AdminDashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "operational" / "admin.db"
        initialize_database(self.database_path)
        copy_saved_geometry(self.database_path)
        self.database_patch = patch.object(api, "DATABASE_PATH", self.database_path)
        self.database_patch.start()
        self.auth_patch = patch.object(api, "AUTH_TEST_BYPASS", True)
        self.auth_patch.start()
        self.client = TestClient(api.app)

    def tearDown(self):
        self.client.close()
        self.auth_patch.stop()
        self.database_patch.stop()
        self.temp_directory.cleanup()

    def zone_with_status(self, status):
        with closing(connect(self.database_path)) as connection:
            return connection.execute(
                "SELECT zone_id FROM zone_live_status WHERE status = ? ORDER BY zone_id LIMIT 1",
                (status,),
            ).fetchone()[0]

    def test_overview_and_zone_catalog_are_operational(self):
        overview = self.client.get("/api/admin/overview")
        self.assertEqual(overview.status_code, 200, overview.text)
        body = overview.json()
        self.assertEqual(body["total_official_zones"], sum(body["zone_status_counts"].values()))
        self.assertEqual(body["request_status_counts"]["PENDING"], 0)
        self.assertEqual(body["allocation_status_counts"]["ACTIVE"], 0)

        catalog = self.client.get("/api/admin/zones?zone_type=FREE&live_status=OPEN")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        zones = catalog.json()["zones"]
        self.assertGreater(len(zones), 0)
        self.assertTrue(all(zone["zone_type"] == "FREE" for zone in zones))
        self.assertTrue(all(zone["live_status"] == "OPEN" for zone in zones))
        self.assertTrue(all(zone["current_vendor_count"] == 0 for zone in zones))
        exact = self.client.get(f"/api/admin/zones?q={zones[0]['zone_id']}").json()["zones"]
        self.assertTrue(any(zone["zone_id"] == zones[0]["zone_id"] for zone in exact))

    def test_manual_status_is_audited_and_filters_recommendations(self):
        recommendation = self.client.post(
            "/api/recommendations", json={"business": "food", "top_n": 1}
        )
        self.assertEqual(recommendation.status_code, 200, recommendation.text)
        zone_id = recommendation.json()["recommendations"][0]["zone_id"]

        closed = self.client.patch(
            f"/api/zones/{zone_id}/live-status",
            json={"status": "CLOSED", "verified_by": "admin_test"},
        )
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["status"], "CLOSED")
        after_close = self.client.post(
            "/api/recommendations", json={"business": "food", "top_n": 20}
        ).json()["recommendations"]
        self.assertNotIn(zone_id, {item["zone_id"] for item in after_close})

        suspended = self.client.patch(
            f"/api/zones/{zone_id}/live-status",
            json={"status": "SUSPENDED", "verified_by": "admin_test"},
        )
        self.assertEqual(suspended.status_code, 200, suspended.text)
        after_suspend = self.client.post(
            "/api/recommendations", json={"business": "food", "top_n": 20}
        ).json()["recommendations"]
        self.assertNotIn(zone_id, {item["zone_id"] for item in after_suspend})

        with closing(connect(self.database_path)) as connection:
            audits = connection.execute(
                """SELECT performed_by, details_json FROM admin_audit_log
                   WHERE action = 'ZONE_LIVE_STATUS_UPDATED' AND entity_id = ?
                   ORDER BY created_at""",
                (zone_id,),
            ).fetchall()
        self.assertEqual(len(audits), 2)
        self.assertEqual(audits[-1]["performed_by"], "admin_test")
        self.assertEqual(json.loads(audits[-1]["details_json"])["effective_status"], "SUSPENDED")

    def test_policy_statuses_cannot_be_overridden(self):
        for status in ("RESTRICTED", "NO_VENDING"):
            zone_id = self.zone_with_status(status)
            response = self.client.patch(
                f"/api/zones/{zone_id}/live-status",
                json={"status": "OPEN", "verified_by": "admin_test"},
            )
            self.assertEqual(response.status_code, 409, response.text)
            current = self.client.get(f"/api/zones/{zone_id}/live-status").json()
            self.assertEqual(current["status"], status)

    def test_full_is_capacity_derived_and_not_a_manual_option(self):
        zone_id = self.zone_with_status("OPEN")
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE zone_live_status
                   SET current_vendor_count = official_capacity, available_capacity = 0,
                       status = 'FULL'
                   WHERE zone_id = ?""",
                (zone_id,),
            )
            connection.commit()
        reopened = self.client.patch(
            f"/api/zones/{zone_id}/live-status",
            json={"status": "OPEN", "verified_by": "admin_test"},
        )
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertEqual(reopened.json()["status"], "FULL")
        manual_full = self.client.patch(
            f"/api/zones/{zone_id}/live-status",
            json={"status": "FULL", "verified_by": "admin_test"},
        )
        self.assertEqual(manual_full.status_code, 422)

    def test_admin_identity_is_required_for_status_change(self):
        zone_id = self.zone_with_status("OPEN")
        response = self.client.patch(
            f"/api/zones/{zone_id}/live-status",
            json={"status": "CLOSED", "verified_by": "  "},
        )
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
