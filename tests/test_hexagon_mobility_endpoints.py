from unittest.mock import AsyncMock, Mock

import pytest

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
    monkeypatch.setattr(hexagons.settings, "TRAVELTIME_API_KEY", "test-key")
    monkeypatch.setattr(hexagons.settings, "TRAVELTIME_APP_ID", "test-app")
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
        "get_traveltime_routes",
        AsyncMock(return_value={
            "h3_id": "hex-a",
            "routes": {
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"transport_mode": "bus"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[24.0, 49.8], [24.1, 49.9]],
                    },
                }],
            },
        }),
    )
    params = {f"weight_{category}": 0 for category in SYSTEM_WEIGHTS if category != "mobility"}
    params["weight_mobility"] = 1

    response = client.get("/hexagons/hex-a", params=params)

    assert response.status_code == 200
    body = response.json()
    assert body["overall_rating"] == 72
    assert body["routes"]["type"] == "FeatureCollection"
    assert body["routes"]["features"][0]["properties"]["transport_mode"] == "bus"
    assert next(item for item in body["categories"] if item["category"] == "mobility")["score"] == 72
    assert {"h3_id", "overall_rating", "categories", "routes"} == set(body)
    hexagons.get_traveltime_routes.assert_awaited_once_with(
        "hex-a", (49.84, 24.03), "bus", hexagons.LVIV_CENTER
    )


@pytest.mark.parametrize("path", ["/hexagons", "/hexagons/hex-a"])
def test_local_mode_excludes_mobility_and_skips_both_providers(monkeypatch, path):
    client, _ = _client(monkeypatch)
    monkeypatch.setattr(hexagons.settings, "EXTERNAL_TRANSPORT_ENABLED", False)
    monkeypatch.setattr(hexagons.settings, "TRAVELTIME_APP_ID", "")
    monkeypatch.setattr(hexagons.settings, "TRAVELTIME_API_KEY", "")
    row = {"h3_id": "hex-a", **{category: 100 for category in SYSTEM_WEIGHTS}}
    monkeypatch.setattr(hexagons, "fetch_hexagons", AsyncMock(return_value=[row]))
    monkeypatch.setattr(hexagons, "fetch_hexagon", AsyncMock(return_value=row))
    mobility = AsyncMock(side_effect=AssertionError("TravelTime must not be called"))
    routes = AsyncMock(side_effect=AssertionError("TravelTime must not be called"))
    monkeypatch.setattr(hexagons, "get_mobility_scores", mobility)
    monkeypatch.setattr(hexagons, "get_traveltime_routes", routes)
    explanations = AsyncMock(return_value={
        category: {"explanation": "ok"} for category in SYSTEM_WEIGHTS
    })
    monkeypatch.setattr(hexagons, "fetch_hexagon_explanations", explanations)

    # Even a positive explicit mobility weight cannot call disabled providers.
    response = client.get(path, params={"weight_mobility": 5})

    assert response.status_code == 200
    body = response.json()
    if isinstance(body, list):
        assert body == [{"h3_id": "hex-a", "overall_rating": 100}]
    else:
        assert body["overall_rating"] == 100
        assert body["routes"] == {"type": "FeatureCollection", "features": []}
        rating = next(item for item in body["categories"] if item["category"] == "mobility")
        assert rating["enabled"] is False
        assert rating["weight"] == 0
        assert rating["adjusted_score"] is None
        assert explanations.await_args.args[2]["mobility"] == 0
    mobility.assert_not_awaited()
    routes.assert_not_awaited()


def test_compare_scores_all_anchors_and_draws_selected_in_outline_colors(monkeypatch):
    client, db = _client(monkeypatch)
    ids = ["hex-a", "hex-b"]
    colors = ["#b26782", "#827eae"]
    result = Mock()
    result.mappings.return_value = [
        dict.fromkeys(SYSTEM_WEIGHTS, 90) | {"h3_id": i, "lat": 49.8, "lon": 24.0}
        for i in reversed(ids)
    ]
    db.execute.return_value = result
    for key in ("TRAVELTIME_API_KEY", "TRAVELTIME_APP_ID"):
        monkeypatch.setattr(hexagons.settings, key, "test")
    monkeypatch.setattr(hexagons.settings, "EXTERNAL_TRANSPORT_ENABLED", True)
    monkeypatch.setattr(hexagons, "get_mobility_scores", AsyncMock(side_effect=[
        dict.fromkeys(ids, 80), dict.fromkeys(ids, 45),
    ]))
    monkeypatch.setattr(hexagons, "fetch_hexagon_explanations", AsyncMock(
        return_value={category: {"explanation": None} for category in SYSTEM_WEIGHTS},
    ))
    route_lookup = AsyncMock(return_value={"routes": {"features": [{
        "type": "Feature", "properties": {"line_style": "solid", "transport_mode": "bus"},
        "geometry": {"type": "LineString", "coordinates": [[24, 49.8], [24.01, 49.8]]},
    }]}})
    monkeypatch.setattr(hexagons, "get_traveltime_routes", route_lookup)
    response = client.get("/hexagons/compare", params={
        "h3_id": ids,
        "color": colors,
        "anchor_lat": [49.8, 49.9],
        "anchor_lon": [24, 24.1],
        "selected_anchor_index": 1,
    })
    assert response.status_code == 200, response.text
    items = response.json()
    assert [item["h3_id"] for item in items] == ids
    assert all(item["mobility_scores"] == [80, 45] for item in items)
    assert all({c["category"] for c in item["categories"]} == set(SYSTEM_WEIGHTS) for item in items)
    assert [call.args[3] for call in route_lookup.await_args_list] == [(49.9, 24.1)] * 2
    features = [f["properties"] for item in items for f in item["routes"]["features"]]
    assert {p["anchor_index"] for p in features} == {1}
    assert {p["color"] for p in features} == set(colors)
