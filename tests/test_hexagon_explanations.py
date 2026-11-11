import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from app.category_narratives.service import generate_explanations, prepare_category_data


def accessibility(category, type_name, distance=100, count=1, radius=100):
    return dict(category=category, type=type_name, nearest_point_dist_m=distance,
                num_objects_in_radius=count, r1=radius)


def result(rows=(), first=None):
    value = Mock()
    value.mappings.return_value.all.return_value = rows
    value.mappings.return_value.first.return_value = first
    return value


def snapshot():
    return dict(h3_id="891e7688377ffff", health=100, study=100, daily=88,
                mobility=0, sport=100, enjoyment=100, food=100, security=100)


def rows():
    return [
        accessibility("health", "hospital"), accessibility("study", "school"),
        accessibility("daily", "pharmacy", distance=150, count=0),
        accessibility("daily", "grocery"), accessibility("sport", "gym"),
        accessibility("enjoyment", "cinema"), accessibility("food", "cafe"),
        accessibility("security", "shelter"),
        accessibility("mobility", "gtfs_stop", distance=250, count=0, radius=200),
    ]


def test_preparation_uses_stored_scores_and_distance_facts_only():
    data = prepare_category_data(snapshot(), rows(), {})
    pharmacy = data["daily"]["types_data"][0]
    assert pharmacy["type_name"] == "Аптеки"
    assert pharmacy["closest_radius_m"] == 100
    assert pharmacy["nearest_point_dist_m"] == 150
    assert data["daily"]["score"] == 88
    assert "type_score" not in pharmacy
    assert "type_weight" not in pharmacy
    output = generate_explanations(data)["daily"]
    assert set(output) == {"explanation"}
    assert "у межах 100 м" in output["explanation"]
    assert "за 150 м" in output["explanation"]


def test_stored_score_does_not_change_distance_narrative():
    original = snapshot()
    first = generate_explanations(prepare_category_data(original, rows(), {}))
    original["daily"] = 0
    second = generate_explanations(prepare_category_data(original, rows(), {}))
    assert first == second
    assert "stale" not in str(second)


def test_missing_poi_and_unconfigured_category():
    data = prepare_category_data(snapshot(), [accessibility("health", "hospital", None, 0)], {})
    output = generate_explanations(data)
    assert "відстань до найближчого об’єкта невідома" in output["health"]["explanation"]
    assert "Недостатньо даних" in output["study"]["explanation"]
    assert "Недостатньо даних" in output["mobility"]["explanation"]


def test_transport_description_does_not_promise_maximum_network_score():
    data = prepare_category_data(snapshot(), rows(), {})
    text = generate_explanations(data)["mobility"]["explanation"]
    assert "у межах 200 м" in text
    assert "за 250 м" in text
    assert "транспортні зв’язки" in text
    assert "максимального бала" not in text


async def request(app, path, query=b""):
    """Exercise ASGI routing/serialization without an HTTP client dependency."""
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
               "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
               "query_string": query, "root_path": "", "headers": [],
               "server": ("test", 80), "client": ("test", 123)}, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(body)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("DB_URL", "postgresql+asyncpg://test:test@127.0.0.1/test")
    from fastapi import FastAPI
    from app.database import get_async_db
    from routers.hexagons import router

    db = Mock()
    db.execute = AsyncMock()
    app = FastAPI()
    app.include_router(router)

    async def override():
        yield db

    app.dependency_overrides[get_async_db] = override
    return app, db




def test_detail_endpoint_returns_only_explanation_and_stored_rating(api, monkeypatch):
    app, db = api
    from app.jobs import count_category_ratings as job

    def unexpected_calculation(*args, **kwargs):
        raise AssertionError("Request must not recalculate category scores")

    monkeypatch.setattr(job, "calculate_distance_score", unexpected_calculation)
    monkeypatch.setattr(job, "get_distance_meters", unexpected_calculation)
    db.execute.side_effect = [result(first=snapshot()), result(rows())]
    status, body = asyncio.run(request(app, "/hexagons/891e7688377ffff", b"weight_daily=0&weight_mobility=0"))
    assert status == 200
    assert len(body["categories"]) == 8
    daily = next(item for item in body["categories"] if item["category"] == "daily")
    assert daily["score"] == 88
    assert daily["enabled"] is False
    assert set(daily) == {"category", "score", "enabled", "weight", "adjusted_score", "explanation"}
    assert "за 150 м" in daily["explanation"]
    assert all(item["explanation"] for item in body["categories"])
    assert db.execute.await_count == 2
    sql_call = db.execute.await_args_list[1]
    assert sql_call.args[1]["h3_id"] == snapshot()["h3_id"]
    sql = str(sql_call.args[0])
    assert "stop_route_connectable" not in sql
    assert "radius_of_influence_2" not in sql
    assert "radius_of_influence_1 AS r1" in sql
    assert "COALESCE" not in sql
    assert "count(*) FILTER" in sql
    assert "min(d.distance_m)" in sql


def test_missing_hexagon_skips_explanation_query(api):
    app, db = api
    db.execute.return_value = result(first=None)
    status, body = asyncio.run(request(app, "/hexagons/missing"))
    assert status == 404
    db.execute.assert_awaited_once()


def test_list_endpoint_does_not_query_explanation_data(api):
    app, db = api
    db.execute.return_value = result([snapshot()])
    status, body = asyncio.run(request(app, "/hexagons", b"weight_mobility=0"))
    assert status == 200
    assert set(body[0]) == {"h3_id", "overall_rating"}
    db.execute.assert_awaited_once()
