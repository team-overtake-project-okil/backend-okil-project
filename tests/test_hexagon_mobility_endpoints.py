from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.hexagons as hexagons
from app.database import get_async_db
from app.scoring import SYSTEM_WEIGHTS


def _client(monkeypatch):
    monkeypatch.setenv("DB_URL", "postgresql://test:test@localhost/test")
    app = FastAPI()
    app.include_router(hexagons.router)
    db = Mock()
    db.execute = AsyncMock()

    async def override_db():
        yield db

    app.dependency_overrides[get_async_db] = override_db
    return TestClient(app), db


def test_hexagon_list_returns_only_rating_and_includes_mobility(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.setattr(hexagons, "fetch_hexagons", AsyncMock(return_value=[
        {"h3_id": "hex-a", "health": 100, "study": 100, "daily": 100,
         "sport": 100, "enjoyment": 100, "food": 100, "security": 100}
    ]))
    monkeypatch.setattr(hexagons, "get_mobility_scores", AsyncMock(return_value={"hex-a": 72.0}))
    params = {f"weight_{category}": 0 for category in SYSTEM_WEIGHTS if category != "mobility"}
    params["weight_mobility"] = 1

    response = client.get("/hexagons", params=params)

    assert response.status_code == 200
    assert response.json() == [{"h3_id": "hex-a", "overall_rating": 72}]


def test_detail_keeps_existing_fields_and_adds_routes_from_hex_center(monkeypatch):
    client, db = _client(monkeypatch)
    result = Mock()
    result.mappings.return_value.first.return_value = {"lat": 49.84, "lon": 24.03}
    db.execute.return_value = result
    monkeypatch.setattr(hexagons.settings, "HERE_ROUTING_API_KEY", "test-key")
    hexagon = {
        "h3_id": "hex-a", "health": 100, "study": 100, "daily": 100,
        "sport": 100, "enjoyment": 100, "food": 100, "security": 100,
    }
    monkeypatch.setattr(hexagons, "fetch_hexagon", AsyncMock(return_value=hexagon))
    monkeypatch.setattr(hexagons, "get_mobility_scores", AsyncMock(return_value={"hex-a": 72.0}))
    monkeypatch.setattr(
        hexagons,
        "fetch_hexagon_explanations",
        AsyncMock(return_value={category: {"explanation": "ok"} for category in SYSTEM_WEIGHTS}),
    )
    monkeypatch.setattr(
        hexagons,
        "get_here_routes",
        AsyncMock(return_value={"h3_id": "hex-a", "routes": [{"transport_mode": "bus"}]}),
    )
    params = {f"weight_{category}": 0 for category in SYSTEM_WEIGHTS if category != "mobility"}
    params["weight_mobility"] = 1

    response = client.get("/hexagons/hex-a", params=params)

    assert response.status_code == 200
    body = response.json()
    assert body["overall_rating"] == 72
    assert body["routes"] == [{"transport_mode": "bus"}]
    assert next(item for item in body["categories"] if item["category"] == "mobility")["score"] == 72
    assert {"h3_id", "overall_rating", "categories", "routes"} == set(body)
    hexagons.get_here_routes.assert_awaited_once_with(
        "hex-a", (49.84, 24.03), "bus", hexagons.LVIV_CENTER
    )
