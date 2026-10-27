import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.api as api
from backend.database import initialize_database
from backend.mcda_engine import WEIGHTS, derive_mcda_weights
from backend.decision_policy import neutralize_constrained_division_weight


class RecommendationPersonalizationTests(unittest.TestCase):
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

    def register(self, priority="BALANCED", parking=False, transport=False, market=False):
        response = self.client.post(
            "/api/vendors",
            json={
                "business_category": "food",
                "preferred_division": "Nashik West",
                "priority_profile": priority,
                "parking_preference": parking,
                "transport_preference": transport,
                "market_preference": market,
                "preferred_language": "en",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def recommend_for(self, vendor_id=None):
        payload = {"top_n": 5}
        if vendor_id:
            payload["vendor_id"] = vendor_id
        else:
            payload.update({"business": "food", "division": "Nashik West"})
        response = self.client.post("/api/recommendations", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_anonymous_and_balanced_vendor_match_baseline(self):
        anonymous = self.recommend_for()
        balanced = self.register()
        personalized = self.recommend_for(balanced["vendor_id"])
        self.assertFalse(anonymous["personalization_applied"])
        self.assertTrue(personalized["personalization_applied"])
        self.assertEqual(anonymous["base_factor_weights"], WEIGHTS)
        expected = neutralize_constrained_division_weight(WEIGHTS)
        self.assertEqual(anonymous["effective_factor_weights"], expected)
        self.assertEqual(personalized["effective_factor_weights"], expected)
        self.assertEqual(anonymous["effective_factor_weights"]["preference"], 0)
        self.assertEqual(
            [(row["zone_id"], row["score"]) for row in anonymous["recommendations"]],
            [(row["zone_id"], row["score"]) for row in personalized["recommendations"]],
        )

    def test_priority_and_flag_multipliers_and_normalization(self):
        cases = (
            ({"priority_profile": "COMMERCIAL"}, "commercial", 1.25),
            ({"priority_profile": "ACCESSIBILITY"}, "access", 1.25),
            ({"priority_profile": "FACILITIES"}, "facilities", 1.25),
            ({"priority_profile": "BALANCED", "market_preference": True}, "commercial", 1.10),
            ({"priority_profile": "BALANCED", "transport_preference": True}, "access", 1.10),
            ({"priority_profile": "BALANCED", "parking_preference": True}, "facilities", 1.10),
            (
                {"priority_profile": "COMMERCIAL", "market_preference": True},
                "commercial",
                1.375,
            ),
            (
                {"priority_profile": "ACCESSIBILITY", "transport_preference": True},
                "access",
                1.375,
            ),
            (
                {"priority_profile": "FACILITIES", "parking_preference": True},
                "facilities",
                1.375,
            ),
        )
        for profile, factor, expected_multiplier in cases:
            with self.subTest(profile=profile):
                # This is the original v1 policy contract. Active v2 blending
                # and non-compounding controls have separate acceptance tests.
                with patch("backend.decision_policy.active_mcda_configuration", return_value=None):
                    result = derive_mcda_weights(profile)
                self.assertAlmostEqual(result["multipliers"][factor], expected_multiplier, places=12)
                self.assertGreater(result["final_weights"][factor], WEIGHTS[factor])
                self.assertAlmostEqual(sum(result["final_weights"].values()), 1.0, places=15)

    def test_profiles_have_different_weights_contributions_and_are_deterministic(self):
        commercial = self.register("COMMERCIAL", market=True)
        access = self.register("ACCESSIBILITY", transport=True)
        facilities = self.register("FACILITIES", parking=True)
        commercial_result = self.recommend_for(commercial["vendor_id"])
        access_result = self.recommend_for(access["vendor_id"])
        facilities_result = self.recommend_for(facilities["vendor_id"])

        weight_sets = {
            tuple(result["effective_factor_weights"].items())
            for result in (commercial_result, access_result, facilities_result)
        }
        self.assertEqual(len(weight_sets), 3)
        repeated = self.recommend_for(commercial["vendor_id"])
        self.assertEqual(commercial_result["recommendations"], repeated["recommendations"])
        self.assertEqual(
            commercial_result["effective_factor_weights"], repeated["effective_factor_weights"]
        )
        first_commercial = {
            factor["key"]: factor["contribution"]
            for factor in commercial_result["recommendations"][0]["factors"]
        }
        first_access = {
            factor["key"]: factor["contribution"]
            for factor in access_result["recommendations"][0]["factors"]
        }
        self.assertNotEqual(first_commercial, first_access)

    def test_explanation_weights_and_contributions_match_final_score(self):
        vendor = self.register("COMMERCIAL", market=True)
        result = self.recommend_for(vendor["vendor_id"])
        for recommendation in result["recommendations"]:
            contributions = sum(factor["contribution"] for factor in recommendation["factors"])
            self.assertAlmostEqual(contributions, recommendation["score"], places=10)
            for factor in recommendation["factors"]:
                self.assertAlmostEqual(
                    factor["weight"] / 100,
                    result["effective_factor_weights"][factor["key"]],
                    places=12,
                )

    def test_historical_self_exclusion_explicit_zone_exclusion_and_live_filter_remain(self):
        initial = self.client.post(
            "/api/recommendations",
            json={"vendor_id": "V2024_00001", "top_n": 3},
        )
        self.assertEqual(initial.status_code, 200, initial.text)
        body = initial.json()
        self.assertTrue(body["live_filter_applied"])
        self.assertTrue(body["personalization_applied"])
        self.assertTrue(
            all(
                row["evidence_method"] == "TEXT_ASSOCIATIONS_LEAVE_LOCATION_GROUP_OUT"
                for row in body["recommendations"]
            )
        )
        excluded_zone = body["recommendations"][0]["zone_id"]
        excluded = self.client.post(
            "/api/recommendations",
            json={
                "vendor_id": "V2024_00001",
                "exclude_zone": excluded_zone,
                "top_n": 3,
            },
        )
        self.assertEqual(excluded.status_code, 200, excluded.text)
        self.assertNotIn(
            excluded_zone,
            [row["zone_id"] for row in excluded.json()["recommendations"]],
        )


if __name__ == "__main__":
    unittest.main()
