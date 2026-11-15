from collections import defaultdict
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from app.config import settings
from app.category_calculation import aggregate, components
from routers.debug import router, Experiment, evaluate


@pytest.fixture(autouse=True)
def isolated_auth_state(monkeypatch):
    import routers.debug as debug
    monkeypatch.setattr(settings, "DEBUG_REQUIRE_HTTPS", False)
    monkeypatch.setattr(debug, "sessions", {})
    monkeypatch.setattr(debug, "login_attempts", {})


def test_missing_type_and_remote_transit_explain_baseline_and_experiment():
    cafe = {"id": 4, "lat": 49.84, "lon": 24.03}
    stop = {"id": 8, "lat": 49.85, "lon": 24.03}
    types = {"cafe": SimpleNamespace(radius_of_influence_1=100, radius_of_influence_2=200,
                                     radius_of_influence_3=300)}
    inputs = types, {"food": ["cafe", "missing"], "mobility": ["gtfs_stop"]}, [], defaultdict(list,cafe=[cafe]), [stop], {8:[1]}, {1:{8,9}}
    cell = {"lat":49.84,"lon":24.03,"h3_id":"test","food":75}
    steps = components(cell, inputs)
    assert steps[-1]["penalty"] == 0.3
    assert set(steps[-1]["destinations"]) == {9}
    assert aggregate(steps,{})[0]["food"] == 50
    assert aggregate(steps,{"cafe":0,"missing":0})[0]["food"] == 0
    result = evaluate(cell, inputs, Experiment(ids=["test"],types={"cafe":3}), True)
    food = next(r for r in result["categories"]["experiment"] if r["category"]=="food")
    assert food["score"] == 75


def test_api_rejects_missing_or_wrong_password(monkeypatch):
    monkeypatch.setattr(settings, "DEBUG_ENABLED", True)
    monkeypatch.setattr(settings, "DEBUG_USER", "team")
    monkeypatch.setattr(settings, "DEBUG_PASSWORD", "example")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    for auth in (None, ("team","incorrect")):
        assert client.get("/debug/config", auth=auth).status_code == 401
    assert client.post("/debug/recalculate", auth=("team", "example"), json={"ids": []}).status_code == 401
    token = client.post("/debug/session", auth=("team", "example")).json()["token"]
    assert client.post("/debug/recalculate", headers={"Authorization": "Bearer " + token}, json={"ids": []}).status_code == 422


def test_debug_session_expires_after_fifteen_minutes(monkeypatch):
    from fastapi import Depends
    import routers.debug as debug
    monkeypatch.setattr(settings, "DEBUG_ENABLED", True)
    monkeypatch.setattr(settings, "DEBUG_USER", "team")
    monkeypatch.setattr(settings, "DEBUG_PASSWORD", "example")
    monkeypatch.setattr(debug, "sessions", {})
    now = [1000.0]
    monkeypatch.setattr(debug.time, "time", lambda: now[0])
    app = FastAPI()
    app.include_router(router)

    @app.get("/probe", dependencies=[Depends(debug.authorize)])
    def probe():
        return {"ok": True}

    client = TestClient(app)
    assert client.post("/debug/session", auth=("team", "wrong")).status_code == 401
    response = client.post("/debug/session", auth=("team", "example"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["expires_at"] == 1900.0
    headers = {"Authorization": "Bearer " + response.json()["token"]}
    now[0] = 1899.0
    assert client.get("/probe", headers=headers).status_code == 200
    now[0] = 1900.0
    assert client.get("/probe", headers=headers).status_code == 401
    assert client.get("/probe", headers={"Authorization": "Bearer invalid"}).status_code == 401


def login_test_client(monkeypatch):
    import routers.debug as debug
    monkeypatch.setattr(settings, "DEBUG_ENABLED", True)
    monkeypatch.setattr(settings, "DEBUG_USER", "team")
    monkeypatch.setattr(settings, "DEBUG_PASSWORD", "example")
    app = FastAPI()
    app.include_router(router)

    @app.get("/probe", dependencies=[Depends(debug.authorize)])
    def probe():
        return {"ok": True}

    return TestClient(app)


def test_login_blocks_after_five_failures_and_recovers(monkeypatch):
    import routers.debug as debug
    now = [1000.0]
    monkeypatch.setattr(debug.time, "time", lambda: now[0])
    client = login_test_client(monkeypatch)
    for _ in range(4):
        assert client.post("/debug/session", auth=("team", "wrong")).status_code == 401
    blocked = client.post("/debug/session", auth=("different-user", "wrong"))
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "900"
    assert client.post("/debug/session", auth=("team", "example")).status_code == 429
    assert client.post("/debug/session", auth=("team", "example"), headers={"X-Forwarded-For": "203.0.113.123"}).status_code == 429
    now[0] += 900
    assert client.post("/debug/session", auth=("team", "example")).status_code == 200


def test_successful_login_resets_failures_and_tokens_are_hashed(monkeypatch):
    import routers.debug as debug
    client = login_test_client(monkeypatch)
    for _ in range(4):
        assert client.post("/debug/session", auth=("team", "wrong")).status_code == 401
    result = client.post("/debug/session", auth=("team", "example"))
    token = result.json()["token"]
    assert token not in debug.sessions
    assert client.get("/probe", headers={"Authorization": "Bearer " + token}).status_code == 200
    assert client.get("/probe", auth=("team", "example")).status_code == 401
    for _ in range(4):
        assert client.post("/debug/session", auth=("team", "wrong")).status_code == 401


def test_failures_outside_the_window_do_not_block_and_disabled_login_is_hidden(monkeypatch):
    import routers.debug as debug
    now = [1000.0]
    monkeypatch.setattr(debug.time, "time", lambda: now[0])
    client = login_test_client(monkeypatch)
    for _ in range(8):
        assert client.post("/debug/session", auth=("team", "wrong")).status_code == 401
        now[0] += debug.LOGIN_WINDOW_SECONDS + 1
    monkeypatch.setattr(settings, "DEBUG_ENABLED", False)
    assert client.post("/debug/session", auth=("team", "example")).status_code == 404


def test_remote_login_requires_https(monkeypatch):
    client = login_test_client(monkeypatch)
    monkeypatch.setattr(settings, "DEBUG_REQUIRE_HTTPS", True)
    assert client.post("/debug/session", auth=("team", "example")).status_code == 403
    assert client.post("/debug/session", auth=("team", "example"), headers={"Host": "localhost"}).status_code == 403
    assert client.post("/debug/session", auth=("team", "example"), headers={"X-Forwarded-Proto": "https"}).status_code == 403
    secure = TestClient(client.app, base_url="https://testserver")
    assert secure.post("/debug/session", auth=("team", "example")).status_code == 200


def test_local_http_exception_is_opt_in(monkeypatch):
    client = login_test_client(monkeypatch)
    monkeypatch.setattr(settings, "DEBUG_REQUIRE_HTTPS", True)
    monkeypatch.setattr(settings, "DEBUG_ALLOW_LOCAL_HTTP", False)
    local = TestClient(client.app, base_url="http://localhost", client=("127.0.0.1", 50000))
    assert local.post("/debug/session", auth=("team", "example")).status_code == 403
    monkeypatch.setattr(settings, "DEBUG_ALLOW_LOCAL_HTTP", True)
    assert local.post("/debug/session", auth=("team", "example")).status_code == 200
