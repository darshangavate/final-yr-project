import hashlib
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.api as api
from backend.database import connect, initialize_database, utc_now
from backend.mcda_engine import CATEGORIES, derive_mcda_weights, recommend
from backend.vendor_service import HISTORICAL_VENDOR_PATH
from backend.services import vendors as vendor_service


class OperationalVendorRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "operational" / "test.db"
        initialize_database(self.database_path)
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

    @staticmethod
    def registration_payload():
        return {
            "full_name": "Kalyani Ugale",
            "business_name": "Kalyani Snacks",
            "business_category": "food",
            "preferred_division": "Nashik West",
            "preferred_locality": "College Road",
            "priority_profile": "ACCESSIBILITY",
            "parking_preference": False,
            "transport_preference": True,
            "market_preference": True,
            "preferred_language": "mr",
        }

    def register(self):
        response = self.client.post("/api/vendors", json=self.registration_payload())
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_register_unique_ids_retrieve_update_and_audit(self):
        first = self.register()
        second = self.register()
        self.assertTrue(first["vendor_id"].startswith("NV_"))
        self.assertNotEqual(first["vendor_id"], second["vendor_id"])
        self.assertRegex(first["application_vendor_id"], r"^NV-\d{4}-\d{6}$")
        self.assertNotEqual(first["application_vendor_id"], second["application_vendor_id"])
        self.assertEqual(first["vendor_type"], "NEW")
        self.assertEqual(first["full_name"], "Kalyani Ugale")
        self.assertEqual(first["business_name"], "Kalyani Snacks")
        self.assertEqual(first["status"], "ACTIVE")
        self.assertNotIn("name", first)
        self.assertNotIn("phone", first)

        fetched = self.client.get(f"/api/vendors/{first['vendor_id']}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json(), first)

        updated = self.client.patch(
            f"/api/vendors/{first['vendor_id']}",
            json={
                "preferred_locality": "Gangapur Road",
                "priority_profile": "COMMERCIAL",
                "parking_preference": True,
                "preferred_language": "hi",
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        profile = updated.json()
        self.assertEqual(profile["preferred_locality"], "Gangapur Road")
        self.assertEqual(profile["priority_profile"], "COMMERCIAL")
        self.assertTrue(profile["parking_preference"])
        self.assertEqual(profile["preferred_language"], "hi")

        with closing(connect(self.database_path)) as connection:
            actions = [
                row[0]
                for row in connection.execute(
                    "SELECT action FROM admin_audit_log WHERE entity_id = ? ORDER BY rowid",
                    (first["vendor_id"],),
                )
            ]
        self.assertEqual(actions, ["VENDOR_REGISTERED", "VENDOR_PROFILE_UPDATED"])

    def test_invalid_category_division_profile_language_and_boolean_are_rejected(self):
        cases = (
            ("business_category", "unsupported"),
            ("preferred_division", "Invented Division"),
            ("priority_profile", "FASTEST"),
            ("preferred_language", "fr"),
            ("transport_preference", "yes"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                payload = self.registration_payload()
                payload[field] = value
                response = self.client.post("/api/vendors", json=payload)
                self.assertEqual(response.status_code, 422, response.text)

    def test_historical_vendor_resolves_and_receives_overlay_without_csv_change(self):
        before_hash = hashlib.sha256(HISTORICAL_VENDOR_PATH.read_bytes()).hexdigest()
        # Test the optional identity overlay using synthetic metadata, rather
        # than pinning a private person's name from a mutable source CSV.
        identity_patch = patch.object(vendor_service, "_historical_identity_map", return_value={
            "V2024_00001": {"full_name":"Synthetic Historical Vendor", "business_name":None},
        })
        identity_patch.start()
        self.addCleanup(identity_patch.stop)
        historical = self.client.get("/api/vendors/V2024_00001")
        self.assertEqual(historical.status_code, 200)
        original = historical.json()
        self.assertEqual(original["vendor_type"], "EXISTING_IMPORTED")
        self.assertIsNone(original["application_vendor_id"])
        self.assertEqual(original["full_name"], "Synthetic Historical Vendor")
        self.assertIsNone(original["business_name"])
        self.assertEqual(original["priority_profile"], "BALANCED")
        self.assertIsNone(original["created_at"])

        overlay = self.client.patch(
            "/api/vendors/V2024_00001",
            json={
                "priority_profile": "FACILITIES",
                "market_preference": True,
                "preferred_language": "mr",
            },
        )
        self.assertEqual(overlay.status_code, 200, overlay.text)
        result = overlay.json()
        self.assertEqual(result["vendor_type"], "EXISTING_IMPORTED")
        self.assertEqual(result["business_category"], original["business_category"])
        self.assertEqual(result["priority_profile"], "FACILITIES")
        self.assertTrue(result["market_preference"])
        self.assertIsNotNone(result["created_at"])
        self.assertEqual(hashlib.sha256(HISTORICAL_VENDOR_PATH.read_bytes()).hexdigest(), before_hash)

        with closing(connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT vendor_type FROM operational_vendors WHERE vendor_id = 'V2024_00001'"
            ).fetchone()
        self.assertEqual(row["vendor_type"], "EXISTING_IMPORTED")

    def test_new_vendor_can_request_allocation_and_active_rule_still_applies(self):
        vendor = self.register()
        no_active = self.client.get(
            f"/api/vendors/{vendor['vendor_id']}/active-allocation"
        )
        self.assertEqual(no_active.status_code, 200)
        self.assertIsNone(no_active.json()["allocation"])
        with closing(connect(self.database_path)) as connection:
            zones = [
                row[0]
                for row in connection.execute(
                    """SELECT zone_id FROM zone_live_status
                       WHERE status = 'OPEN' AND available_capacity > 0
                       ORDER BY zone_id LIMIT 2"""
                )
            ]
        request = self.client.post(
            "/api/allocation-requests",
            json={
                "vendor_id": vendor["vendor_id"],
                "zone_id": zones[0],
                "request_type": "NEW_ALLOCATION",
                "business_category": "food",
            },
        )
        self.assertEqual(request.status_code, 201, request.text)

        now = utc_now()
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """INSERT INTO allocations
                   (allocation_id, vendor_id, zone_id, status, allocated_at, created_at, updated_at)
                   VALUES ('ALLOC_NEW_VENDOR', ?, ?, 'ACTIVE', ?, ?, ?)""",
                (vendor["vendor_id"], zones[0], now, now, now),
            )
            connection.commit()
        active = self.client.get(
            f"/api/vendors/{vendor['vendor_id']}/active-allocation"
        )
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json()["allocation"]["allocation_id"], "ALLOC_NEW_VENDOR")
        conflict = self.client.post(
            "/api/allocation-requests",
            json={
                "vendor_id": vendor["vendor_id"],
                "zone_id": zones[1],
                "request_type": "NEW_ALLOCATION",
                "business_category": "food",
            },
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("active allocation", conflict.json()["detail"])

    def test_operational_vendor_recommendations_keep_scores_and_live_filtering(self):
        vendor = self.register()
        response = self.client.post(
            "/api/recommendations",
            json={"vendor_id": vendor["vendor_id"], "top_n": 3},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["count"], 3)
        self.assertTrue(body["live_filter_applied"])

        baseline = recommend(
            "food",
            division="Nashik West",
            top_n=3,
            use_live_status=True,
            database_path=self.database_path,
            factor_weights=derive_mcda_weights(vendor)["final_weights"],
        )
        self.assertEqual(
            [item["zone_id"] for item in body["recommendations"]],
            baseline.zone_id.tolist(),
        )
        for item, (_, expected) in zip(body["recommendations"], baseline.iterrows()):
            self.assertAlmostEqual(item["score"], float(expected.score), places=12)

        full_zone = body["recommendations"][0]["zone_id"]
        with closing(connect(self.database_path)) as connection:
            capacity = connection.execute(
                "SELECT official_capacity FROM zone_live_status WHERE zone_id = ?",
                (full_zone,),
            ).fetchone()[0]
            connection.execute(
                """UPDATE zone_live_status
                   SET current_vendor_count = ?, available_capacity = 0, status = 'FULL'
                   WHERE zone_id = ?""",
                (capacity, full_zone),
            )
            connection.commit()
        filtered = self.client.post(
            "/api/recommendations",
            json={"vendor_id": vendor["vendor_id"], "top_n": 3},
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertNotIn(
            full_zone,
            [item["zone_id"] for item in filtered.json()["recommendations"]],
        )

    def test_explicitly_selected_division_overrides_saved_profile_for_recommendations(self):
        vendor = self.register()
        response = self.client.post(
            "/api/recommendations",
            json={
                "vendor_id": vendor["vendor_id"],
                "business": vendor["business_category"],
                "division": "Nashik East",
                "top_n": 3,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        recommendations = response.json()["recommendations"]
        self.assertEqual(len(recommendations), 3)
        self.assertTrue(all(item["zone_division"] == "Nashik East" for item in recommendations))

    def test_preferences_change_weights_deterministically(self):
        vendor = self.register()
        before = self.client.post(
            "/api/recommendations", json={"vendor_id": vendor["vendor_id"], "top_n": 3}
        ).json()
        updated = self.client.patch(
            f"/api/vendors/{vendor['vendor_id']}",
            json={
                "priority_profile": "FACILITIES",
                "parking_preference": True,
                "transport_preference": False,
                "market_preference": False,
            },
        )
        self.assertEqual(updated.status_code, 200)
        after = self.client.post(
            "/api/recommendations", json={"vendor_id": vendor["vendor_id"], "top_n": 3}
        ).json()
        self.assertNotEqual(before["effective_factor_weights"], after["effective_factor_weights"])
        self.assertNotEqual(
            [row["score"] for row in before["recommendations"]],
            [row["score"] for row in after["recommendations"]],
        )
        repeated = self.client.post(
            "/api/recommendations", json={"vendor_id": vendor["vendor_id"], "top_n": 3}
        ).json()
        self.assertEqual(after["effective_factor_weights"], repeated["effective_factor_weights"])
        self.assertEqual(after["recommendations"], repeated["recommendations"])

    def test_initialization_is_idempotent_and_preserves_operational_vendor(self):
        vendor = self.register()
        first = initialize_database(self.database_path)
        second = initialize_database(self.database_path)
        self.assertEqual(first["inserted"], 0)
        self.assertEqual(second["inserted"], 0)
        fetched = self.client.get(f"/api/vendors/{vendor['vendor_id']}")
        self.assertEqual(fetched.status_code, 200)
        with closing(connect(self.database_path)) as connection:
            count = connection.execute(
                "SELECT COUNT(vendor_id) FROM operational_vendors"
            ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(CATEGORIES, [
            "vegetable_fruit", "food", "clothing", "flower", "general_goods", "other"
        ])

    def test_existing_database_receives_nullable_identity_columns(self):
        legacy_path = Path(self.temp_directory.name) / "legacy.db"
        with closing(connect(legacy_path)) as connection:
            connection.execute("""CREATE TABLE operational_vendors (
                vendor_id TEXT PRIMARY KEY, vendor_type TEXT NOT NULL,
                business_category TEXT NOT NULL, preferred_division TEXT,
                preferred_locality TEXT, priority_profile TEXT NOT NULL,
                parking_preference INTEGER NOT NULL, transport_preference INTEGER NOT NULL,
                market_preference INTEGER NOT NULL, preferred_language TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            connection.commit()
        initialize_database(legacy_path)
        with closing(connect(legacy_path)) as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(operational_vendors)")}
        self.assertTrue({"full_name", "business_name"}.issubset(columns))


if __name__ == "__main__":
    unittest.main()
