from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from routers.mobility_places import cache_key, router


@pytest.fixture
def client(redis_server):
    from fakeredis import FakeAsyncRedis

    app = FastAPI()
    app.state.redis = FakeAsyncRedis(server=redis_server, decode_responses=True)
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def test_places_round_trip_isolation_and_stale_write(client, redis_server):
    from fakeredis import FakeRedis

    url = "/mobility/places"
    token = str(uuid4())
    headers = {"Authorization": f"Bearer {token}"}
    other = {"Authorization": f"Bearer {uuid4()}"}
    snapshot = {
        "revision": 1,
        "selectedPresetId": "ucu",
        "personalRoutesEnabled": True,
        "anchors": [
            {
                "id": "custom",
                "lat": 49.84,
                "lng": 24.02,
                "label": "Work",
                "source": "map",
            },
        ],
    }

    assert client.get(url).status_code == 401
    assert client.get(url, headers=headers).json() is None
    saved = client.put(url, json=snapshot, headers=headers)
    assert saved.status_code == 200
    assert saved.headers["Cache-Control"] == "no-store"
    assert saved.json() == snapshot
    assert client.get(url, headers=headers).json() == snapshot
    assert client.get(url, headers=other).json() is None

    invalid = {**snapshot, "anchors": snapshot["anchors"] * 6}
    assert client.put(url, json=invalid, headers=headers).status_code == 422
    assert client.get(url, headers=headers).json() == snapshot
    key = cache_key(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
    with FakeRedis(server=redis_server) as redis:
        four_hours = 4 * 60 * 60
        assert four_hours - 60 < redis.ttl(key) <= four_hours

    cleared = {**snapshot, "anchors": [], "selectedPresetId": None, "revision": 2}
    assert client.put(url, json=cleared, headers=headers).json() == cleared
    assert client.put(url, json=snapshot, headers=headers).json() == cleared
    assert client.get(url, headers=headers).json() == cleared
