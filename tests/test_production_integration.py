"""HTTPS/first-party session regressions, using only a disposable database."""
import os
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import backend.api as api
import backend.bootstrap as bootstrap
from backend.core.config import ConfigurationError, get_auth_settings, validate_production_database
from backend.repositories import database as db
from backend.services import authentication
from backend.geo import pilot_geometry as gis
from operational_fixtures import copy_saved_geometry

ORIGIN = "https://vendormap-eta.vercel.app"


@pytest.fixture
def production(tmp_path, monkeypatch):
    path = tmp_path / "production-test.db"
    db.initialize_database(path)
    copy_saved_geometry(path)
    monkeypatch.setattr(bootstrap, "seed_historical_vendor_identities", lambda *_: {"inserted":0})
    values = {
        "APP_ENV": "production", "SESSION_COOKIE_SECURE": "true", "OTP_DEBUG": "false",
        "AUTH_HMAC_SECRET": "isolated-production-test-secret-1234567890",
        "NMC_ADMIN_USERNAME": "test-officer", "NMC_ADMIN_PASSWORD": "test-only-password",
        "OTP_PROVIDER": "development", "DATABASE_URL": f"sqlite:///{path.as_posix()}",
        "CORS_ORIGINS": ORIGIN,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(api, "DATABASE_PATH", path)
    monkeypatch.setattr(api, "AUTH_TEST_BYPASS", False)
    validate_production_database(path)
    with TestClient(api.app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        yield client, path


def login_admin(client):
    response = client.post("/api/auth/admin/login", json={"username": "test-officer", "password": "test-only-password"})
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "ADMIN"
    cookie = response.headers["set-cookie"]
    assert all(value in cookie for value in ["HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=43200"])
    assert "Domain=" not in cookie
    assert "token" not in response.json()
    return response


def test_admin_https_session_refresh_expiration_logout_and_origin_protection(production):
    client, path = production
    assert client.get("/api/admin/overview").status_code == 401
    assert client.post("/api/auth/admin/login", json={"username":"test-officer","password":"test-only-password"}, headers={"Origin":"https://attacker.invalid"}).status_code == 403
    login_admin(client)
    for endpoint in ["/api/auth/me", "/api/admin/overview", "/api/allocation-requests?status=PENDING", "/api/admin/vendors", "/api/admin/zones"]:
        response = client.get(endpoint)
        assert response.status_code == 200, (endpoint, response.text)
        assert response.headers["cache-control"] == "private, no-store"
    # A fresh browser connection/server application reuses DB-backed sessions.
    with TestClient(api.app, base_url=ORIGIN, cookies=client.cookies) as refreshed:
        assert refreshed.get("/api/auth/me").json()["role"] == "ADMIN"
    token = client.cookies.get("nmc_session")
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.set("nmc_session", token)
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/admin/overview").status_code == 401
    login_admin(client)
    with db.transaction(path) as connection:
        connection.execute("UPDATE auth_sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
    assert client.get("/api/auth/me").status_code == 401


def test_vendor_https_profile_recommendations_tracking_and_admin_slot_approval(production, monkeypatch):
    client, path = production
    # Only delivery is mocked. OTP hashing/verification and every authorization
    # check, recommendation, request and allocation run through the real API.
    monkeypatch.setattr(authentication, "get_otp_provider", lambda: Mock())
    monkeypatch.setattr(authentication, "generate_otp", lambda: "123456")
    challenge = client.post("/api/auth/vendor/request-otp", json={"mobile":"9000000001"})
    assert challenge.status_code == 200
    verified = client.post("/api/auth/vendor/verify-otp", json={"challenge_id":challenge.json()["challenge_id"],"otp":"123456"})
    assert verified.status_code == 200
    assert verified.json()["role"] == "NEW_VENDOR_ONBOARDING"
    created = client.post("/api/vendors", json={"business_category":"food", "preferred_division":"Panchavati", "preferred_language":"en"})
    assert created.status_code == 201, created.text
    vendor_id = created.json()["vendor_id"]
    vendor_cookies = dict(client.cookies)
    assert client.get("/api/auth/me").json()["vendor_id"] == vendor_id
    assert client.get(f"/api/vendors/{vendor_id}").status_code == 200
    assert client.get("/api/vendors/me/eligible-zones").status_code == 200
    recommendations = client.post("/api/recommendations", json={"vendor_id":vendor_id,"top_n":3})
    assert recommendations.status_code == 200, recommendations.text
    assert recommendations.json()["recommendations"]
    assert client.get("/api/admin/overview").status_code == 403
    assert client.get("/api/admin/vendors").status_code == 403

    # Admin and vendor use separate cookie jars with server-enforced roles.
    with TestClient(api.app, base_url=ORIGIN, headers={"Origin":ORIGIN}) as admin:
        login_admin(admin)
        other_zone = next(row["zone_id"] for row in recommendations.json()["recommendations"] if row["zone_id"] != "PAN_F_031")
        rejected_request = client.post("/api/allocation-requests", json={"vendor_id":vendor_id,"zone_id":other_zone,"request_type":"NEW_ALLOCATION"})
        assert rejected_request.status_code == 201, rejected_request.text
        rejected_id = rejected_request.json()["request_id"]
        rejected = admin.post(f"/api/allocation-requests/{rejected_id}/reject",json={"rejection_reason":"Disposable rejection workflow test"})
        assert rejected.status_code == 200, rejected.text
        assert client.get(f"/api/allocation-requests/{rejected_id}").json()["status"] == "REJECTED"
        assert admin.post("/api/pilot/initialize?zone_id=PAN_F_031").status_code == 200
        slot = admin.post("/api/pilot/slots?zone_id=PAN_F_031", json={
            "geometry":gis.pose_geometry(73.79481222,20.00988107,0,width_m=2,depth_m=2),
            "review_confirmed":True,"width_m":2,"depth_m":2,
        })
        assert slot.status_code == 201, slot.text
        request = client.post("/api/allocation-requests", json={"vendor_id":vendor_id,"zone_id":"PAN_F_031","slot_id":slot.json()["slot_id"],"request_type":"NEW_ALLOCATION"})
        assert request.status_code == 201, request.text
        request_id = request.json()["request_id"]
        assert client.get(f"/api/allocation-requests/{request_id}").json()["status"] == "PENDING"
        assert client.post(f"/api/allocation-requests/{request_id}/approve",json={}).status_code == 403
        approved = admin.post(f"/api/allocation-requests/{request_id}/approve",json={"notes":"Disposable integration test"})
        assert approved.status_code == 200, approved.text
        assert client.get(f"/api/allocation-requests/{request_id}").json()["status"] == "APPROVED"
        allocation = client.get(f"/api/vendors/{vendor_id}/active-allocation").json()["allocation"]
        assert allocation["slot_id"] == slot.json()["slot_id"]
    with TestClient(api.app,base_url=ORIGIN,cookies=vendor_cookies) as refreshed:
        assert refreshed.get("/api/auth/me").json()["vendor_id"] == vendor_id
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get(f"/api/vendors/{vendor_id}").status_code == 401


def test_development_provider_does_not_pretend_to_deliver_production_otp(production):
    client, path = production
    response = client.post("/api/auth/vendor/request-otp",json={"mobile":"9000000002"})
    assert response.status_code == 503
    with closing(db.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_otp_challenges").fetchone()[0] == 0


def test_production_refuses_insecure_cookie_debug_otp_or_implicit_database(production, monkeypatch):
    _, path = production
    monkeypatch.setenv("SESSION_COOKIE_SECURE","false")
    with pytest.raises(ConfigurationError,match="SESSION_COOKIE_SECURE"):
        get_auth_settings()
    monkeypatch.setenv("SESSION_COOKIE_SECURE","true")
    monkeypatch.setenv("OTP_DEBUG","true")
    with pytest.raises(ConfigurationError,match="OTP_DEBUG"):
        get_auth_settings()
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(ConfigurationError,match="DATABASE_URL"):
        validate_production_database(path)
    for value in ["sqlite:///:memory:","sqlite:///relative.db",f"sqlite:///{(path.parent / 'missing.db').as_posix()}"]:
        monkeypatch.setenv("DATABASE_URL",value)
        with pytest.raises(ConfigurationError):
            validate_production_database(value)
    assert not (path.parent / "missing.db").exists()


def test_render_postgres_url_alias_is_supported_without_changing_credentials(monkeypatch):
    monkeypatch.setenv("DATABASE_URL","postgres://user:password@localhost/test")
    assert db.get_database_backend() == "postgresql"
    assert db._database_url(os.environ["DATABASE_URL"]) == "postgresql+psycopg://user:password@localhost/test"


def test_missing_saved_gis_data_returns_503_without_invalidating_the_session(production):
    client, path = production
    login_admin(client)
    with db.transaction(path) as connection:
        connection.execute("DELETE FROM pilot_slots WHERE zone_id = 'NN_F_013'")
    response = client.get("/api/admin/zones")
    assert response.status_code == 503
    assert "Saved zone reference data is unavailable" in response.json()["detail"]
    assert str(path) not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert client.get("/api/auth/me").status_code == 200


def test_production_startup_does_not_initialize_an_existing_empty_database(production, monkeypatch):
    _, path = production
    wrong = path.parent / "wrong-empty.db"
    wrong.touch()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{wrong.as_posix()}")
    monkeypatch.setattr(api, "DATABASE_PATH", wrong)
    with pytest.raises(ConfigurationError, match="existing operational schema"):
        with TestClient(api.app, base_url=ORIGIN):
            pass
    with closing(sqlite3.connect(wrong)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0


def test_production_refuses_destructive_reset_flag(production, monkeypatch):
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_DB_RESET", "true")
    with pytest.raises(ConfigurationError, match="ALLOW_DESTRUCTIVE_DB_RESET"):
        get_auth_settings()


def test_vendor_cannot_spoof_application_category(production):
    client, path = production
    vendor = db.create_operational_vendor({"business_category":"food"}, path)
    with db.transaction(path) as connection:
        token, _ = authentication._create_session(connection, "VENDOR", vendor_id=vendor["vendor_id"])
    client.cookies.set("nmc_session", token)
    response = client.post("/api/allocation-requests", json={
        "vendor_id":vendor["vendor_id"], "zone_id":"PAN_F_031",
        "request_type":"NEW_ALLOCATION", "business_category":"clothing",
    })
    assert response.status_code == 422
    assert "must match" in response.json()["detail"]


def test_unknown_capacity_is_unavailable_for_exploration_submission_and_approval(production):
    client, path = production
    vendor = db.create_operational_vendor({"business_category":"food"}, path)
    with db.transaction(path) as connection:
        token, _ = authentication._create_session(connection, "VENDOR", vendor_id=vendor["vendor_id"])
    client.cookies.set("nmc_session", token)
    zones = client.get("/api/vendors/me/eligible-zones")
    assert zones.status_code == 200
    assert "NE_F_003" not in {row["zone_id"] for row in zones.json()["zones"]}
    payload = {"vendor_id":vendor["vendor_id"], "zone_id":"NE_F_003", "request_type":"NEW_ALLOCATION"}
    response = client.post("/api/allocation-requests", json=payload)
    assert response.status_code == 409
    assert "CAPACITY_UNKNOWN" in response.json()["detail"]
    pending = client.post("/api/allocation-requests", json={**payload,"zone_id":"PAN_F_031"})
    assert pending.status_code == 201, pending.text
    with db.transaction(path) as connection:
        connection.execute("UPDATE zone_live_status SET official_capacity=NULL, available_capacity=NULL WHERE zone_id='PAN_F_031'")
    login_admin(client)
    request_id = pending.json()["request_id"]
    approval = client.post(f"/api/allocation-requests/{request_id}/approve", json={})
    assert approval.status_code == 409
    assert "CAPACITY_UNKNOWN" in approval.json()["detail"]
    assert db.get_allocation_request(request_id, path)["status"] == "PENDING"
    assert db.get_active_allocation_for_vendor(vendor["vendor_id"], path) is None
