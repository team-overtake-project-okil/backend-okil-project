from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv(
        "DB_URL",
        "postgresql+asyncpg://test:test@127.0.0.1/test",
    )

    from app.database import get_async_db
    from routers.point_types import router as point_types_router
    from routers.points import router as points_router
    from routers.routes import router as routes_router
    from routers.stops import router as stops_router
    from routers.hexagons import router as hexagons_router


    app = FastAPI()

    app.include_router(points_router)
    app.include_router(routes_router)
    app.include_router(stops_router)
    app.include_router(point_types_router)
    app.include_router(hexagons_router)

    result = Mock()

    db = Mock()
    db.execute = AsyncMock(return_value=result)

    async def override_db():
        yield db

    app.dependency_overrides[get_async_db] = override_db

    with TestClient(app) as client:
        yield client, db, result


@pytest.mark.parametrize(
    "path, db_row, expected",
    [
        pytest.param(
            "/points/17",
            {
                "id": 17,
                "h3_id": None,
                "type": "school",
                "source": "test",
                "external_id": "point-17",
                "name": "Test school",
                "lat": 49.841,
                "lng": 24.031,
            },
            {
                "id": 17,
                "h3_id": None,
                "type": "school",
                "source": "test",
                "external_id": "point-17",
                "name": "Test school",
                "lat": 49.841,
                "lng": 24.031,
            },
            id="point",
        ),
        pytest.param(
            "/routes/42",
            {
                "id": 42,
                "name": "A25",
                "source": "test",
                "external_id": "route-42",
                "start_stop_id": 7,
                "end_stop_id": 12,
            },
            {
                "id": 42,
                "name": "A25",
                "source": "test",
                "external_id": "route-42",
                "start_stop_id": 7,
                "end_stop_id": 12,
            },
            id="route",
        ),
        pytest.param(
            "/stops/7",
            {
                "id": 7,
                "h3_id": "8928308280fffff",
                "type": "gtfs_stop",
                "source": "test",
                "external_id": "stop-7",
                "lat": 49.8397,
                "lng": 24.0297,
            },
            {
                "id": 7,
                "h3_id": "8928308280fffff",
                "type": "gtfs_stop",
                "source": "test",
                "external_id": "stop-7",
                "lat": 49.8397,
                "lng": 24.0297,
            },
            id="stop-with-h3",
        ),
        pytest.param(
            "/stops/8",
            {
                "id": 8,
                "h3_id": None,
                "type": "gtfs_stop",
                "source": "test",
                "external_id": "stop-8",
                "lat": 49.842,
                "lng": 24.032,
            },
            {
                "id": 8,
                "h3_id": None,
                "type": "gtfs_stop",
                "source": "test",
                "external_id": "stop-8",
                "lat": 49.842,
                "lng": 24.032,
            },
            id="stop-without-h3",
        ),
        pytest.param(
            "/point_types/gtfs_stop",
            {
                "type": "gtfs_stop",
                "category": "transport",
                "radius_of_influence_1": 100.0,
                "radius_of_influence_2": 300.0,
                "radius_of_influence_3": None,
            },
            {
                "type": "gtfs_stop",
                "category": "transport",
                "radius_of_influence_1": 100.0,
                "radius_of_influence_2": 300.0,
                "radius_of_influence_3": None,
            },
            id="point-type",
        ),
    ],
)
def test_existing_record_returns_200(api, path, db_row, expected):
    client, db, result = api

    result.mappings.return_value.first.return_value = db_row

    response = client.get(path)

    assert response.status_code == 200
    assert response.json() == expected
    db.execute.assert_awaited_once()


@pytest.mark.parametrize(
    "path, expected_detail",
    [
        ("/points/999", "Point not found"),
        ("/routes/999", "Route not found"),
        ("/stops/999", "Stop not found"),
        ("/point_types/missing", "Point type not found"),
    ],
)
def test_missing_record_returns_404(api, path, expected_detail):
    client, db, result = api

    result.mappings.return_value.first.return_value = None

    response = client.get(path)

    assert response.status_code == 404
    assert response.json() == {"detail": expected_detail}
    db.execute.assert_awaited_once()


@pytest.mark.parametrize(
    "path, parameter",
    [
        ("/points/abc", "point_id"),
        ("/routes/abc", "route_id"),
        ("/stops/abc", "stop_id"),
    ],
)
def test_invalid_integer_id_returns_422(api, path, parameter):
    client, db, _ = api

    response = client.get(path)

    assert response.status_code == 422

    errors = response.json()["detail"]
    assert any(
        error["loc"] == ["path", parameter]
        for error in errors
    )

    db.execute.assert_not_awaited()

def test_get_hexagons_returns_empty_list(api):
    client, db, result = api

    result.mappings.return_value.all.return_value = []

    response = client.get("/hexagons")

    assert response.status_code == 200
    assert response.json() == []
    db.execute.assert_awaited_once()

def test_get_hexagons_returns_all_rows(api):
    client, db, result = api

    rows = [
        {
            "h3_id": "8928308280bffff",
            "health": 80,
            "study": 60,
            "daily": 70,
            "mobility": 90,
            "sport": 50,
            "enjoyment": 65,
            "food": 60,
            "security": 75,
        },
        {
            "h3_id": "8928308280fffff",
            "health": 40,
            "study": 90,
            "daily": 55,
            "mobility": 60,
            "sport": 85,
            "enjoyment": 70,
            "food": 80,
            "security": 80,
        },
    ]
    result.mappings.return_value.all.return_value = rows

    response = client.get("/hexagons")

    assert response.status_code == 200
    assert response.json() == [
        {
            "h3_id": "8928308280bffff",
            "overall_rating": 70.4,
        },
        {
            "h3_id": "8928308280fffff",
            "overall_rating": 64.7,
        },
    ]
    db.execute.assert_awaited_once()

@pytest.mark.parametrize(
    "params, expected_rating",
    [
        pytest.param({}, 32.17, id="default-weights"),
        pytest.param(
            {"weight_health": 5},
            43.53,
            id="higher-health-weight",
        ),
        pytest.param(
            {"weight_health": 0},
            25.0,
            id="disabled-health",
        ),
    ],
)
def test_get_hexagons_applies_weights(api, params, expected_rating):
    client, db, result = api

    result.mappings.return_value.all.return_value = [
        {
            "h3_id": "8928308280bffff",
            "health": 100,
            "study": 25,
            "daily": 25,
            "mobility": 25,
            "sport": 25,
            "enjoyment": 25,
            "food": 25,
            "security": 25,
        }
    ]

    response = client.get("/hexagons", params=params)

    assert response.status_code == 200
    assert response.json() == [
        {
            "h3_id": "8928308280bffff",
            "overall_rating": expected_rating,
        }
    ]
    db.execute.assert_awaited_once()


@pytest.mark.parametrize(
    "parameter",
    [
        "weight_health",
        "weight_study",
        "weight_daily",
        "weight_mobility",
        "weight_sport",
        "weight_enjoyment",
        "weight_food",
        "weight_security",
    ],
)
@pytest.mark.parametrize("value", ["-1", "6", "abc", "nan", "inf"])
def test_get_hexagons_rejects_invalid_weights(api, parameter, value):
    client, db, _ = api

    response = client.get(
        "/hexagons",
        params={parameter: value},
    )

    assert response.status_code == 422
    assert any(
        error["loc"] == ["query", parameter]
        for error in response.json()["detail"]
    )
    db.execute.assert_not_awaited()

@pytest.mark.parametrize(
    "food_score, food_weight, expected_rating",
    [
        (25, 0, 100.0),
        (25, 1, 50.0),
        (25, 3, 35.36),
        (0, 1, 0.0),
    ],
)
def test_hexagon_endpoints_include_and_apply_food(
    api, food_score, food_weight, expected_rating
):
    client, db, result = api

    row = {
        "h3_id": "8928308280bffff",
        "health": 100,
        "study": 100,
        "daily": 100,
        "mobility": 100,
        "sport": 100,
        "enjoyment": 100,
        "food": food_score,
        "security": 100,
    }
    params = {
        "weight_health": 0,
        "weight_study": 0,
        "weight_daily": 0,
        "weight_mobility": 0,
        "weight_sport": 0,
        "weight_enjoyment": 1,
        "weight_food": food_weight,
        "weight_security": 0,
    }

    result.mappings.return_value.all.return_value = [row]
    result.mappings.return_value.first.return_value = row

    list_response = client.get("/hexagons", params=params)

    assert list_response.status_code == 200
    assert list_response.json() == [
        {"h3_id": row["h3_id"], "overall_rating": expected_rating}
    ]

    detail_response = client.get(
        f"/hexagons/{row['h3_id']}", params=params
    )

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["h3_id"] == row["h3_id"]
    assert detail["overall_rating"] == expected_rating

    categories = detail["categories"]
    assert len(categories) == 8
    assert {item["category"] for item in categories} == set(row) - {"h3_id"}

    food = next(item for item in categories if item["category"] == "food")
    assert food["score"] == food_score
    assert food["enabled"] is (food_weight > 0)

    assert db.execute.await_count == 2
    for call in db.execute.await_args_list:
        query = call.args[0]
        assert "food" in query.selected_columns.keys()