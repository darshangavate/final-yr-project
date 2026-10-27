from sqlalchemy.exc import IntegrityError
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.api as api
from backend.database import (
    OperationalConflictError,
    approve_allocation_request,
    connect,
    create_allocation_request,
    initialize_database,
    utc_now,
)


class AdminAllocationWorkflowTests(unittest.TestCase):
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
                       WHERE status = 'OPEN' AND available_capacity > 1
                       ORDER BY zone_id LIMIT 8"""
                )
            ]
            self.restricted_zone = connection.execute(
                """SELECT zone_id FROM zone_live_status
                   WHERE status = 'RESTRICTED' AND official_capacity > 1 LIMIT 1"""
            ).fetchone()[0]

    def tearDown(self):
        self.client.close()
        self.auth_patch.stop()
        self.database_patch.stop()
        self.temp_directory.cleanup()

    def create_request(self, vendor_id, zone_id, request_type="NEW_ALLOCATION"):
        response = self.client.post(
            "/api/allocation-requests",
            json={
                "vendor_id": vendor_id,
                "zone_id": zone_id,
                "request_type": request_type,
                # Category comes from the selected vendor's actual profile.
                "preferred_division": "Nashik East",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def insert_active_allocation(self, vendor_id, zone_id, allocation_id="ALLOC_OLD"):
        now = utc_now()
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """INSERT INTO allocations
                   (allocation_id, vendor_id, zone_id, status, allocated_at,
                    approved_by, created_at, updated_at)
                   VALUES (?, ?, ?, 'ACTIVE', ?, 'admin_seed', ?, ?)""",
                (allocation_id, vendor_id, zone_id, now, now, now),
            )
            connection.commit()

    def test_new_approval_updates_request_allocation_capacity_and_audits(self):
        zone_id = self.open_zones[0]
        with closing(connect(self.database_path)) as connection:
            before = connection.execute(
                "SELECT current_vendor_count, available_capacity FROM zone_live_status WHERE zone_id = ?",
                (zone_id,),
            ).fetchone()
        request = self.create_request("V2024_00101", zone_id)
        response = self.client.post(
            f"/api/allocation-requests/{request['request_id']}/approve",
            json={"approved_by": "admin_01", "notes": "Documents verified"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["request"]["status"], "APPROVED")
        self.assertEqual(body["request"]["reviewed_by"], "admin_01")
        self.assertEqual(body["allocation"]["status"], "ACTIVE")
        self.assertEqual(body["allocation"]["vendor_id"], "V2024_00101")

        with closing(connect(self.database_path)) as connection:
            after = connection.execute(
                "SELECT current_vendor_count, available_capacity FROM zone_live_status WHERE zone_id = ?",
                (zone_id,),
            ).fetchone()
            actions = {
                row[0]
                for row in connection.execute(
                    "SELECT action FROM admin_audit_log WHERE details_json LIKE ?",
                    (f'%"request_id": "{request["request_id"]}"%',),
                )
            }
        self.assertEqual(after["current_vendor_count"], before["current_vendor_count"] + 1)
        self.assertEqual(after["available_capacity"], before["available_capacity"] - 1)
        self.assertTrue(
            {
                "ALLOCATION_REQUEST_APPROVED",
                "ALLOCATION_CREATED",
                "ZONE_OCCUPANCY_INCREMENTED",
            }.issubset(actions)
        )

        repeated = self.client.post(
            f"/api/allocation-requests/{request['request_id']}/approve",
            json={"approved_by": "admin_01"},
        )
        self.assertEqual(repeated.status_code, 409)

    def test_last_slot_becomes_full_and_full_zone_cannot_be_approved(self):
        zone_id = self.open_zones[1]
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE zone_live_status
                   SET official_capacity = 1, current_vendor_count = 0,
                       available_capacity = 1, status = 'OPEN'
                   WHERE zone_id = ?""",
                (zone_id,),
            )
            connection.commit()
        first = self.create_request("V2024_00102", zone_id)
        second = self.create_request("V2024_00103", zone_id)
        approved = self.client.post(
            f"/api/allocation-requests/{first['request_id']}/approve",
            json={"approved_by": "admin_01"},
        )
        self.assertEqual(approved.status_code, 200)
        live = self.client.get(f"/api/zones/{zone_id}/live-status")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["current_vendor_count"], 1)
        self.assertEqual(live.json()["available_capacity"], 0)
        self.assertEqual(live.json()["status"], "FULL")

        rejected = self.client.post(
            f"/api/allocation-requests/{second['request_id']}/approve",
            json={"approved_by": "admin_01"},
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertIn("ZONE_FULL", rejected.json()["detail"])

    def test_rejection_changes_no_occupancy(self):
        zone_id = self.open_zones[2]
        request = self.create_request("V2024_00104", zone_id)
        with closing(connect(self.database_path)) as connection:
            before = tuple(
                connection.execute(
                    """SELECT current_vendor_count, available_capacity
                       FROM zone_live_status WHERE zone_id = ?""",
                    (zone_id,),
                ).fetchone()
            )
        response = self.client.post(
            f"/api/allocation-requests/{request['request_id']}/reject",
            json={
                "reviewed_by": "admin_02",
                "rejection_reason": "Capacity reserved for another approved allocation",
                "notes": "Review completed",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "REJECTED")
        self.assertEqual(response.json()["reviewed_by"], "admin_02")
        self.assertEqual(
            response.json()["rejection_reason"],
            "Capacity reserved for another approved allocation",
        )
        with closing(connect(self.database_path)) as connection:
            after = tuple(
                connection.execute(
                    """SELECT current_vendor_count, available_capacity
                       FROM zone_live_status WHERE zone_id = ?""",
                    (zone_id,),
                ).fetchone()
            )
            allocation_count = connection.execute(
                "SELECT COUNT(allocation_id) FROM allocations"
            ).fetchone()[0]
            audit = connection.execute(
                """SELECT action FROM admin_audit_log
                   WHERE entity_id = ? AND action = 'ALLOCATION_REQUEST_REJECTED'""",
                (request["request_id"],),
            ).fetchone()
        self.assertEqual(after, before)
        self.assertEqual(allocation_count, 0)
        self.assertIsNotNone(audit)

    def test_pending_relocation_lists_vendor_current_zone(self):
        old_zone, requested_zone = self.open_zones[3:5]
        vendor_id = "V2024_00105"
        self.insert_active_allocation(vendor_id, old_zone)
        request = create_allocation_request(
            vendor_id,
            requested_zone,
            "RELOCATION",
            business_category="food",
            database_path=self.database_path,
        )

        response = self.client.get(
            "/api/allocation-requests",
            params={"status": "PENDING"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["requests"][0]["current_zone_id"], old_zone)

    def test_relocation_releases_old_reopens_full_and_moves_occupancy(self):
        old_zone, new_zone = self.open_zones[3:5]
        vendor_id = "V2024_00105"
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE zone_live_status
                   SET official_capacity = 1, current_vendor_count = 1,
                       available_capacity = 0, status = 'FULL'
                   WHERE zone_id = ?""",
                (old_zone,),
            )
            new_before = connection.execute(
                "SELECT current_vendor_count, available_capacity FROM zone_live_status WHERE zone_id = ?",
                (new_zone,),
            ).fetchone()
            connection.commit()
        self.insert_active_allocation(vendor_id, old_zone)
        request = self.create_request(vendor_id, new_zone, "RELOCATION")
        response = self.client.post(
            f"/api/allocation-requests/{request['request_id']}/approve",
            json={"approved_by": "admin_03"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["released_allocation"]["status"], "RELEASED")
        self.assertEqual(result["allocation"]["status"], "ACTIVE")

        with closing(connect(self.database_path)) as connection:
            old_live = connection.execute(
                "SELECT * FROM zone_live_status WHERE zone_id = ?", (old_zone,)
            ).fetchone()
            new_live = connection.execute(
                "SELECT * FROM zone_live_status WHERE zone_id = ?", (new_zone,)
            ).fetchone()
            active_count = connection.execute(
                """SELECT COUNT(allocation_id) FROM allocations
                   WHERE vendor_id = ? AND status = 'ACTIVE'""",
                (vendor_id,),
            ).fetchone()[0]
            actions = {
                row[0]
                for row in connection.execute(
                    "SELECT action FROM admin_audit_log WHERE details_json LIKE ?",
                    (f'%"request_id": "{request["request_id"]}"%',),
                )
            }
        self.assertEqual(old_live["current_vendor_count"], 0)
        self.assertEqual(old_live["available_capacity"], 1)
        self.assertEqual(old_live["status"], "OPEN")
        self.assertEqual(new_live["current_vendor_count"], new_before["current_vendor_count"] + 1)
        self.assertEqual(new_live["available_capacity"], new_before["available_capacity"] - 1)
        self.assertEqual(active_count, 1)
        self.assertTrue(
            {"ALLOCATION_RELEASED", "ZONE_OCCUPANCY_DECREMENTED"}.issubset(actions)
        )

    def test_allocation_list_read_and_live_status_endpoints(self):
        zone_id = self.open_zones[5]
        request = self.create_request("V2024_00106", zone_id)
        approved = self.client.post(
            f"/api/allocation-requests/{request['request_id']}/approve",
            json={"approved_by": "admin_01"},
        ).json()
        allocation_id = approved["allocation"]["allocation_id"]
        listed = self.client.get("/api/allocations", params={"status": "ACTIVE"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(listed.json()["allocations"][0], approved["allocation"])
        paged = self.client.get(
            "/api/allocations",
            params={"status": "ACTIVE", "limit": 1, "offset": 0},
        )
        self.assertEqual(paged.status_code, 200, paged.text)
        self.assertEqual(paged.json()["count"], 1)
        self.assertEqual(paged.json()["total_count"], 1)
        self.assertEqual(paged.json()["allocations"][0], approved["allocation"])
        fetched = self.client.get(f"/api/allocations/{allocation_id}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json(), approved["allocation"])
        live = self.client.get(f"/api/zones/{zone_id}/live-status")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["zone_id"], zone_id)
        self.assertEqual(self.client.get("/api/allocations/ALLOC_missing").status_code, 404)
        self.assertEqual(self.client.get("/api/zones/ZONE_missing/live-status").status_code, 404)

    def test_relocation_does_not_reopen_policy_restricted_old_zone(self):
        vendor_id = "V2024_00109"
        new_zone = self.open_zones[6]
        with closing(connect(self.database_path)) as connection:
            capacity = connection.execute(
                "SELECT official_capacity FROM zone_live_status WHERE zone_id = ?",
                (self.restricted_zone,),
            ).fetchone()[0]
            connection.execute(
                """UPDATE zone_live_status
                   SET current_vendor_count = 1, available_capacity = ?, status = 'RESTRICTED'
                   WHERE zone_id = ?""",
                (capacity - 1, self.restricted_zone),
            )
            connection.commit()
        self.insert_active_allocation(vendor_id, self.restricted_zone, "ALLOC_RESTRICTED_OLD")
        request = self.create_request(vendor_id, new_zone, "RELOCATION")
        response = self.client.post(
            f"/api/allocation-requests/{request['request_id']}/approve",
            json={"approved_by": "admin_policy"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        with closing(connect(self.database_path)) as connection:
            old_status = connection.execute(
                "SELECT status FROM zone_live_status WHERE zone_id = ?",
                (self.restricted_zone,),
            ).fetchone()[0]
        self.assertEqual(old_status, "RESTRICTED")

    def test_capacity_and_count_constraints_prevent_negative_values(self):
        zone_id = self.open_zones[6]
        with closing(connect(self.database_path)) as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    "UPDATE zone_live_status SET current_vendor_count = -1 WHERE zone_id = ?",
                    (zone_id,),
                )
            connection.rollback()
            with self.assertRaises(IntegrityError):
                connection.execute(
                    "UPDATE zone_live_status SET available_capacity = -1 WHERE zone_id = ?",
                    (zone_id,),
                )

    def test_two_concurrent_approvals_cannot_take_one_final_slot(self):
        zone_id = self.open_zones[7]
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE zone_live_status
                   SET official_capacity = 1, current_vendor_count = 0,
                       available_capacity = 1, status = 'OPEN'
                   WHERE zone_id = ?""",
                (zone_id,),
            )
            connection.commit()
        first = self.create_request("V2024_00107", zone_id)
        second = self.create_request("V2024_00108", zone_id)

        def approve(request_id):
            try:
                approve_allocation_request(
                    request_id, "admin_concurrent", database_path=self.database_path
                )
                return "approved"
            except OperationalConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(approve, (first["request_id"], second["request_id"]))
            )
        self.assertCountEqual(outcomes, ["approved", "conflict"])
        with closing(connect(self.database_path)) as connection:
            live = connection.execute(
                "SELECT * FROM zone_live_status WHERE zone_id = ?", (zone_id,)
            ).fetchone()
            active_count = connection.execute(
                "SELECT COUNT(allocation_id) FROM allocations WHERE status = 'ACTIVE'"
            ).fetchone()[0]
        self.assertEqual(live["current_vendor_count"], 1)
        self.assertEqual(live["available_capacity"], 0)
        self.assertEqual(active_count, 1)


if __name__ == "__main__":
    unittest.main()
