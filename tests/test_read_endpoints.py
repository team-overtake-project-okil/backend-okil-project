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
                "placement_id": None,
                "source": "test",
                "external_id": "point-17",
                "name": "Test school",
            },
            {
                "id": 17,
                "h3_id": None,
                "type": "school",
                "placement_id": None,
                "source": "test",
                "external_id": "point-17",
                "name": "Test school",
                "converter_ref": None,
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
                "placement_id": 3,
                "source": "test",
                "external_id": "stop-7",
                "converter_placement": "Test stop address",
            },
            {
                "id": 7,
                "h3_id": "8928308280fffff",
                "type": "gtfs_stop",
                "placement_id": 3,
                "source": "test",
                "external_id": "stop-7",
                "converter_ref": {
                    "placement_id": 3,
                    "placement": "Test stop address",
                },
            },
            id="stop-with-location",
        ),
        pytest.param(
            "/stops/8",
            {
                "id": 8,
                "h3_id": None,
                "type": "gtfs_stop",
                "placement_id": None,
                "source": "test",
                "external_id": "stop-8",
                "converter_placement": None,
            },
            {
                "id": 8,
                "h3_id": None,
                "type": "gtfs_stop",
                "placement_id": None,
                "source": "test",
                "external_id": "stop-8",
                "converter_ref": None,
            },
            id="stop-without-location",
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
            "security": 80,
        },
    ]
    result.mappings.return_value.all.return_value = rows

    response = client.get("/hexagons")

    assert response.status_code == 200
    assert response.json() == rows
    db.execute.assert_awaited_once()