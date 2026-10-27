"""Focused v2 acceptance checks; all write operations use disposable SQLite."""
import copy
import importlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.exc import IntegrityError
from fastapi.testclient import TestClient
from sklearn.metrics import adjusted_rand_score

from backend.core.data_paths import CATALOG, ROOT, dataset_path
from backend.decision_policy import FACTOR_ORDER, RESEARCH_BASELINE_WEIGHTS, derive_mcda_weights, neutralize_constrained_division_weight
from backend.geo.zone_locations import resolve_zone_locations, saved_pilot_areas, PILOT_COUNTS
from backend.repositories import database as db
from backend import mcda_engine as engine
from backend.application import app
from backend.core import context
from backend.services import vendors
from backend.routers import recommendations as recommendation_router, zones as zone_router, vendors as vendor_router
from operational_fixtures import copy_saved_geometry

VERSION = ROOT / "data/model/versions/manual-142-v2"


def v2_datasets():
    datasets = copy.deepcopy(CATALOG["datasets"])
    for name, value in list(datasets.items()):
        if "manual-142-v1" in value:
            datasets[name] = value.replace("manual-142-v1", "manual-142-v2")
    datasets["mcda_configuration"] = "data/model/versions/manual-142-v2/metadata/mcda_configuration.json"
    datasets["recommendation_output"] = "data/model/versions/manual-142-v2/recommendations"
    return datasets


@pytest.fixture
def v2(monkeypatch):
    monkeypatch.setitem(CATALOG, "datasets", v2_datasets())
    monkeypatch.setitem(CATALOG, "active_model_version", "manual-142-v2")
    for name, module in list(sys.modules.items()):
        if name.startswith("backend") and hasattr(module, "DATA"):
            monkeypatch.setattr(module, "DATA", VERSION / "inputs")
    yield


@pytest.fixture
def workspace(v2, tmp_path, monkeypatch):
    path = tmp_path / "operational.db"
    db.initialize_database(path)
    copy_saved_geometry(path)
    vendor = db.create_operational_vendor({"business_category":"food", "preferred_division":None}, path)
    monkeypatch.setattr(context, "DATABASE_PATH", path)
    monkeypatch.setattr(context, "get_session", lambda *args, **kwargs:{"role":"VENDOR", "vendor_id":vendor["vendor_id"], "session_id":"DISPOSABLE"})
    monkeypatch.setattr(context, "AUTH_TEST_BYPASS", False)
    client = TestClient(app)
    yield path, vendor["vendor_id"], client
    client.close()


def test_locations_and_preserved_candidate_coverage(v2):
    resolved = resolve_zone_locations()
    areas = saved_pilot_areas()
    features = pd.read_csv(VERSION / "features/zone_features.csv").set_index("zone_id")
    candidates = pd.read_csv(VERSION / "inputs/candidate_zone_snapshot.csv")
    assert len(resolved) == len(features) == 308
    assert len(candidates) == 166
    assert candidates.geometry_usable_for_reference_model.sum() == 146
    assert features.reference_location_source.eq("PILOT_SAVED_PROVISIONAL").sum() == 4
    assert features.reference_location_source.eq("MANUAL_PROVISIONAL").sum() == 138
    assert sum(PILOT_COUNTS.values()) == 85
    for zone_id, (area, _) in areas.items():
        point = area.representative_point()
        assert resolved[zone_id]["location_source"] == "PILOT_SAVED_PROVISIONAL"
        assert area.covers(point)
        assert np.allclose([features.loc[zone_id].reference_point_easting, features.loc[zone_id].reference_point_northing], [point.x, point.y], atol=1e-6)
    for zone_id in ["NE_F_001","NE_F_003","NR_F_002","NR_F_025"]:
        assert resolved[zone_id]["location_source"] == "ORIGINAL_ANALYTICAL_FALLBACK_UNRESOLVED"


def test_balanced_and_missing_criteria(v2):
    baseline = derive_mcda_weights({"priority_profile":"BALANCED"})
    assert baseline["final_weights"] == RESEARCH_BASELINE_WEIGHTS
    scored = engine.score_candidates("food", factor_weights=baseline["final_weights"])
    old_method = engine.score_candidates("food", factor_weights=RESEARCH_BASELINE_WEIGHTS)
    pd.testing.assert_frame_equal(scored, old_method)
    components = {key:pd.Series([.4 if key != "business" else np.nan]) for key in FACTOR_ORDER}
    result = engine._calculate_mcda(pd.DataFrame({"zone_id":["EXAMPLE"]}), components, baseline["final_weights"])
    assert result.score.iloc[0] == pytest.approx(40)
    assert result.score_coverage.iloc[0] == pytest.approx(.75)
    assert sum(result[f"{key}_contribution"].iloc[0] for key in FACTOR_ORDER) == pytest.approx(40)


@pytest.mark.parametrize("profile,factor", [("COMMERCIAL","commercial"),("ACCESSIBILITY","access"),("FACILITIES","facilities")])
def test_personalization_formula(v2, profile, factor):
    weights = derive_mcda_weights({"priority_profile":profile})
    b = RESEARCH_BASELINE_WEIGHTS
    q = {key:3.0 if key == factor else 1.0 for key in FACTOR_ORDER}
    total = sum(b[key]*q[key] for key in FACTOR_ORDER)
    expected = {key:.4*b[key]+.6*b[key]*q[key]/total for key in FACTOR_ORDER}
    assert weights["final_weights"] == pytest.approx(expected)
    assert weights["final_weights"][factor] > b[factor]
    assert sum(weights["final_weights"].values()) == pytest.approx(1)
    scored = engine.score_candidates("food", factor_weights=weights["final_weights"])
    assert scored.score.between(0,100).all()
    pd.testing.assert_frame_equal(scored, engine.score_candidates("food", factor_weights=weights["final_weights"]))
    contribution = scored[[f"{key}_contribution" for key in FACTOR_ORDER]].sum(axis=1)
    np.testing.assert_allclose(contribution, scored.score)
    assert not scored.score.equals(engine.score_candidates("food").score)


def test_checkbox_meaning_and_validation(v2):
    for control,factor in [("market_preference","commercial"),("transport_preference","access"),("parking_preference","facilities")]:
        w = derive_mcda_weights({control:True})
        assert w["multipliers"][factor] == 3
        assert derive_mcda_weights({control:False})["final_weights"] == RESEARCH_BASELINE_WEIGHTS
        with pytest.raises(ValueError): derive_mcda_weights({control:"false"})
    with pytest.raises(ValueError): derive_mcda_weights({"priority_profile":"LOW_COMPETITION"})
    with pytest.raises(ValueError): derive_mcda_weights({"priority_profile":3})
    assert derive_mcda_weights({"priority_profile":"COMMERCIAL","market_preference":True})["multipliers"]["commercial"] == 3


def test_top_three_all_zones_locality_and_own_map(workspace, monkeypatch):
    path, vendor_id, client = workspace
    response = client.post("/api/recommendations",json={"vendor_id":vendor_id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["official_free_zone_count"] == 166
    # NE_F_003 has unknown official capacity: it remains model-scoreable but
    # cannot be offered as an operationally available location.
    assert body["eligible_scoreable_count"] == 145
    assert any(row["zone_id"] == "NE_F_003" for row in body["ineligible_zones"])
    assert body["eligible_unscorable_count"] == 20
    assert len(body["recommendations"]) == 3
    assert body["remaining_recommendations"][0]["rank"] == 4
    scores = [row["score"] for row in body["recommendations"] + body["remaining_recommendations"]]
    assert scores == sorted(scores, reverse=True)
    assert all(row["score"] is None for row in body["eligible_unscorable_zones"])
    assert all(row["title"] == row["zone_id"] for row in body["recommendations"])
    assert body["locality_scoring"]["reason"] == "NO_LOCALITY_PREFERENCE"
    update = client.patch(f"/api/vendors/{vendor_id}",json={"preferred_locality":"Unreviewed test locality","priority_profile":"COMMERCIAL"})
    assert update.status_code == 200, update.text
    changed = client.post("/api/recommendations",json={"vendor_id":vendor_id}).json()
    assert changed["locality_scoring"] == {"applied":False,"reason":"REVIEWED_LOCALITY_REFERENCE_UNAVAILABLE"}
    assert changed["effective_factor_weights"]["commercial"] == pytest.approx(.33714285714285713)
    context_body = client.get("/api/vendors/me/eligible-zones").json()
    assert {r["zone_id"] for r in context_body["zones"]} == {r["zone_id"] for r in body["recommendations"] + body["remaining_recommendations"] + body["eligible_unscorable_zones"]}
    display = client.get("/api/zones/display-geometry").json()
    locations = resolve_zone_locations(path)
    assert len(display["features"]) == 308
    for feature in display["features"]:
        zone_id = feature["properties"]["zone_id"]
        assert (feature["geometry"]["coordinates"] if feature["geometry"] else None) == locations[zone_id]["coordinates"]
    assert client.get("/api/admin/zones").status_code == 403
    # Authorization remains enforced, independently of recommendation scoreability.
    assert client.post("/api/allocation-requests",json={"vendor_id":"OTHER_VENDOR","zone_id":body["remaining_recommendations"][0]["zone_id"],"request_type":"NEW_ALLOCATION"}).status_code == 403
    monkeypatch.setattr(context, "get_session", lambda *args, **kwargs:{"role":"ADMIN", "admin_id":"DISPOSABLE_ADMIN", "session_id":"DISPOSABLE"})
    admin = client.get("/api/admin/zones")
    assert admin.status_code == 200, admin.text
    assert len(admin.json()["zones"]) == 308
    for row in admin.json()["zones"]:
        assert row["resolved_location"]["coordinates"] == locations[row["zone_id"]]["coordinates"]


def test_non_top_three_and_unscorable_applications_and_guards(workspace):
    path,vendor_id,client = workspace
    body = client.post("/api/recommendations",json={"vendor_id":vendor_id}).json()
    target = body["remaining_recommendations"][0]["zone_id"]
    payload = {"vendor_id":vendor_id,"zone_id":target,"request_type":"NEW_ALLOCATION","business_category":"food"}
    response = client.post("/api/allocation-requests",json=payload)
    assert response.status_code == 201, response.text
    request_id = response.json()["request_id"]
    assert client.post("/api/allocation-requests",json=payload).status_code == 409
    db.approve_allocation_request(request_id,"DISPOSABLE_ADMIN",database_path=path)
    assert client.post("/api/allocation-requests",json={**payload,"zone_id":body["remaining_recommendations"][1]["zone_id"]}).status_code == 409
    unscorable = body["eligible_unscorable_zones"][0]["zone_id"]
    relocation = client.post("/api/allocation-requests",json={**payload,"zone_id":unscorable,"request_type":"RELOCATION"})
    assert relocation.status_code == 201, relocation.text
    assert db.approve_allocation_request(relocation.json()["request_id"],"DISPOSABLE_ADMIN",database_path=path)["allocation"]["zone_id"] == unscorable
    with db.connect(path) as connection:
        full = body["remaining_recommendations"][2]["zone_id"]
        connection.execute("UPDATE zone_live_status SET current_vendor_count=official_capacity,available_capacity=0,status='FULL' WHERE zone_id=?",(full,))
        illegal = connection.execute("SELECT zone_id FROM zone_live_status WHERE status='NO_VENDING' LIMIT 1").fetchone()[0]
    for zone in [full,illegal]:
        assert client.post("/api/allocation-requests",json={**payload,"zone_id":zone,"request_type":"RELOCATION"}).status_code == 409
    assert client.post("/api/allocation-requests",json={**payload,"zone_id":target,"business_category":"clothing","request_type":"RELOCATION"}).status_code == 422


def test_v1_catalog_rollback_restores_policy_and_model(monkeypatch):
    # Runs with the actual v1 catalog before activation, or its saved manifest afterwards.
    previous = CATALOG.get("model_versions",{}).get("manual-142-v1", CATALOG["datasets"])
    monkeypatch.setitem(CATALOG,"datasets",copy.deepcopy(previous))
    monkeypatch.setattr(engine,"DATA",ROOT/previous["mcda_inputs"])
    weights = derive_mcda_weights({"priority_profile":"COMMERCIAL"})
    assert weights["multipliers"]["commercial"] == 1.25
    assert len(engine.load()[0]) == 146
    assert engine.recommend("food",division="Nashik East").zone_division.eq("Nashik East").all()


def test_cluster_selection_and_artifact(v2):
    import joblib
    trainer = importlib.import_module("scripts.04_train_kmeans")
    evaluation = pd.read_csv(VERSION / "kmeans/k_evaluation.csv")
    assert evaluation.k.tolist() == list(range(2,9))
    metadata = json.loads((VERSION/"metadata/kmeans/kmeans_run.json").read_text())
    assert metadata["seeds"] == [42,43,44,45,46]
    assert metadata["minimum_cluster_size_required"] == 8
    eligible = evaluation.loc[evaluation.min_cluster_size.ge(8) & evaluation.stability_ari_median.ge(.8)]
    expected = int(eligible.loc[eligible.silhouette_mean.ge(eligible.silhouette_mean.max()-.01-1e-12)].k.min()) if not eligible.empty else 2
    assert metadata["selected_k"] == expected
    artifact = joblib.load(VERSION/"artifacts/kmeans/zone_kmeans.joblib")
    assert artifact["model"].random_state == 42 and artifact["model"].n_init == 20
    features = pd.read_csv(VERSION/"features/zone_features.csv").set_index("zone_id")
    inputs = features.loc[artifact["training_zone_ids"],artifact["features"]]
    transformed = trainer.apply_preprocess(inputs, artifact["preprocessing"])[:,artifact["active_indices"]]
    labels = artifact["model"].predict(transformed)
    assignments = pd.read_csv(VERSION/"kmeans/training_assignments.csv").set_index("zone_id").loc[artifact["training_zone_ids"],"cluster"]
    np.testing.assert_array_equal(labels, assignments)


def test_empty_and_verified_filters(workspace):
    path,vendor_id,client = workspace
    with db.connect(path) as connection:
        connection.execute("UPDATE zone_live_status SET status='CLOSED' WHERE status='OPEN'")
    body = client.post("/api/recommendations",json={"vendor_id":vendor_id}).json()
    assert body["recommendations"] == body["remaining_recommendations"] == body["eligible_unscorable_zones"] == []
    assert len(body["ineligible_zones"]) == 308


def test_approval_rechecks_capacity_and_relocation_rollback(workspace):
    path,vendor_id,client = workspace
    first = db.create_allocation_request(vendor_id,"NN_F_013","NEW_ALLOCATION",business_category="food",database_path=path)
    db.approve_allocation_request(first["request_id"],"DISPOSABLE_ADMIN",database_path=path)
    with db.connect(path) as connection:
        connection.execute("UPDATE pilot_slots SET assigned_vendor_id=?,status='OCCUPIED' WHERE zone_id='NN_F_013' AND slot_id='S001'",(vendor_id,))
    target = "PAN_F_019"
    request = db.create_allocation_request(vendor_id,target,"RELOCATION",business_category="food",database_path=path)
    with db.connect(path) as connection:
        connection.execute("CREATE TRIGGER fail_relocation BEFORE INSERT ON allocations BEGIN SELECT RAISE(ABORT, 'disposable failure'); END")
    with pytest.raises(IntegrityError):
        db.approve_allocation_request(request["request_id"],"DISPOSABLE_ADMIN",database_path=path)
    with db.connect(path) as connection:
        assert connection.execute("SELECT assigned_vendor_id FROM pilot_slots WHERE zone_id='NN_F_013' AND slot_id='S001'").fetchone()[0] == vendor_id
        assert connection.execute("SELECT zone_id FROM allocations WHERE vendor_id=? AND status='ACTIVE'",(vendor_id,)).fetchone()[0] == "NN_F_013"
        connection.execute("DROP TRIGGER fail_relocation")
        connection.execute("UPDATE zone_live_status SET current_vendor_count=official_capacity,available_capacity=0,status='FULL' WHERE zone_id=?",(target,))
    with pytest.raises(db.OperationalConflictError):
        db.approve_allocation_request(request["request_id"],"DISPOSABLE_ADMIN",database_path=path)
    with db.connect(path) as connection:
        connection.execute("UPDATE zone_live_status SET current_vendor_count=0,available_capacity=official_capacity,status='OPEN' WHERE zone_id=?",(target,))
    db.approve_allocation_request(request["request_id"],"DISPOSABLE_ADMIN",database_path=path)
    with db.connect(path) as connection:
        slot = connection.execute("SELECT assigned_vendor_id,status FROM pilot_slots WHERE zone_id='NN_F_013' AND slot_id='S001'").fetchone()
        assert slot[0] is None and slot[1] == "AVAILABLE"
        assert connection.execute("SELECT COUNT(*) FROM pilot_slot_events WHERE zone_id='NN_F_013' AND slot_id='S001'").fetchone()[0] == 1


def test_approval_rechecks_business_profile(workspace):
    path,vendor_id,client = workspace
    request = db.create_allocation_request(vendor_id,"NN_F_013","NEW_ALLOCATION",business_category="food",database_path=path)
    with db.connect(path) as connection:
        connection.execute("UPDATE operational_vendors SET business_category='clothing' WHERE vendor_id=?",(vendor_id,))
    with pytest.raises(db.OperationalConflictError,match="business category changed"):
        db.approve_allocation_request(request["request_id"],"DISPOSABLE_ADMIN",database_path=path)
    assert db.get_allocation_request(request["request_id"],path)["status"] == "PENDING"


def test_atomic_catalog_rollback_route(tmp_path, monkeypatch):
    from backend.core import data_paths
    catalog = json.loads((ROOT/"data/catalog.json").read_text())
    if "model_versions" not in catalog:
        pytest.skip("Version switch manifests are registered only after acceptance")
    temporary_root = tmp_path/"data"
    temporary_root.mkdir()
    (temporary_root/"catalog.json").write_text(json.dumps(catalog))
    acceptance = temporary_root/"model/versions/manual-142-v2/metadata/acceptance.json"
    acceptance.parent.mkdir(parents=True)
    acceptance.write_text((VERSION/"metadata/acceptance.json").read_text())
    monkeypatch.setattr(data_paths,"DATA_ROOT",temporary_root)
    data_paths.activate_model_version("manual-142-v1")
    previous = json.loads((temporary_root/"catalog.json").read_text())
    assert previous["datasets"] == catalog["model_versions"]["manual-142-v1"]
    assert "mcda_configuration" not in previous["datasets"]
    data_paths.activate_model_version("manual-142-v2")
    restored = json.loads((temporary_root/"catalog.json").read_text())
    assert restored["datasets"] == catalog["model_versions"]["manual-142-v2"]


def test_saved_preferences_refresh_rankings_and_contributions(workspace):
    path,vendor_id,client = workspace
    baseline = None
    for priority,division in [("BALANCED",None),("COMMERCIAL",None),("ACCESSIBILITY",None),("FACILITIES",None),("BALANCED","Nashik East")]:
        saved = client.patch(f"/api/vendors/{vendor_id}",json={"priority_profile":priority,"preferred_division":division,"market_preference":False,"transport_preference":False,"parking_preference":False})
        assert saved.status_code == 200, saved.text
        assert client.get(f"/api/vendors/{vendor_id}").json()["priority_profile"] == priority
        response = client.post("/api/recommendations",json={"vendor_id":vendor_id})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["priority_profile"] == priority
        assert not any(body["preference_flags"].values())
        weights = derive_mcda_weights(saved.json())["final_weights"]
        if division:
            weights = neutralize_constrained_division_weight(weights)
        assert body["effective_factor_weights"] == pytest.approx(weights)
        ranked = body["recommendations"] + body["remaining_recommendations"]
        if division:
            assert ranked
            assert all(row["zone_division"] == division for row in ranked)
            assert body["strict_division_filter_applied"] is True
        else:
            assert len(ranked) == 145 and len(body["eligible_unscorable_zones"]) == 20
            assert "NE_F_003" not in {row["zone_id"] for row in ranked}
        for row in ranked:
            available = [factor for factor in row["factors"] if factor["score"] is not None]
            denominator = sum(weights[factor["key"]] for factor in available)
            for factor in row["factors"]:
                expected = 0 if factor["score"] is None else 100*weights[factor["key"]]*factor["score"]/denominator
                assert factor["contribution"] == pytest.approx(expected)
            assert sum(factor["contribution"] for factor in row["factors"]) == pytest.approx(row["score"])
        if baseline is None:
            baseline = {row["zone_id"]:(index+1,row["score"]) for index,row in enumerate(ranked)}
        else:
            assert any(abs(row["score"]-baseline[row["zone_id"]][1]) > 1e-8 for row in ranked)
        differences = [(index+1)-baseline[row["zone_id"]][0] for index,row in enumerate(ranked)]
        print(priority,division or "Any division","top10",[(row["zone_id"],round(row["score"],2)) for row in ranked[:10]],"rank_changes",sum(delta != 0 for delta in differences),"max_shift",max(abs(delta) for delta in differences))
        repeated = client.post("/api/recommendations",json={"vendor_id":vendor_id}).json()
        assert [(row["zone_id"],row["score"]) for row in repeated["recommendations"]+repeated["remaining_recommendations"]] == [(row["zone_id"],row["score"]) for row in ranked]
