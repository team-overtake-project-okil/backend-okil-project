import asyncio
from datetime import datetime, timezone
import math

import pytest
import httpx

import app.mobility_score_service as service
import app.here_routes_service as here_routes
from routers.hexagons import _here_error_detail


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, *, headers, json):
        mode = json["departure_searches"][0]["transportation"]["type"]
        times = {"bus": [600, 1200, 1800], "car": [300, 1800, 900]}[mode]
        return FakeResponse(
            {
                "results": [
                    {
                        "search_id": search["id"],
                        "cells": [
                            {"id": "hex-a", "properties": {"mean": times[index]}}
                        ],
                    }
                    for index, search in enumerate(json["departure_searches"])
                ]
            }
        )


def test_map_scores_use_bus_car_minimum_and_average_departures(monkeypatch):
    monkeypatch.setattr(service.settings, "TRAVELTIME_API_KEY", "key")
    monkeypatch.setattr(service.settings, "TRAVELTIME_APP_ID", "app")
    monkeypatch.setattr(service.settings, "TRAVELTIME_TIME_CONSTANT_SECONDS", 1500)
    monkeypatch.setattr(service.settings, "TRAVELTIME_BUS_FULL_SCORE_SECONDS", 1800)
    monkeypatch.setattr(service.settings, "TRAVELTIME_CAR_FULL_SCORE_SECONDS", 1200)
    monkeypatch.setattr(
        service,
        "_departure_times",
        lambda: [datetime(2026, 11, 12, hour, tzinfo=timezone.utc) for hour in (9, 14, 18)],
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: FakeClient())

    scores = asyncio.run(service.get_mobility_scores(["hex-a", "hex-b"], "both"))
    expected = sum(
        min(
            service._travel_time_score(bus, 1500, 1800),
            service._travel_time_score(car, 1500, 1200),
        )
        for bus, car in zip((600, 1200, 1800), (300, 1800, 900))
    ) / 3

    assert scores == {"hex-a": pytest.approx(expected), "hex-b": 0.0}


@pytest.mark.parametrize("threshold", [30 * 60, 20 * 60])
def test_mode_threshold_gets_full_score(threshold):
    assert service._travel_time_score(threshold, 1500, threshold) == 100


@pytest.mark.parametrize(("full_score_minutes", "travel_minutes"), [(30, 35), (20, 25)])
def test_travel_time_decays_after_mode_threshold(full_score_minutes, travel_minutes):
    threshold = full_score_minutes * 60

    assert service._travel_time_score(
        travel_minutes * 60, 1500, threshold
    ) == pytest.approx(100 * math.exp(-0.2))


def test_weighted_anchor_score_never_exceeds_100(monkeypatch):
    class FullScoreClient(FakeClient):
        async def post(self, url, *, headers, json):
            return FakeResponse(
                {
                    "results": [
                        {
                            "search_id": search["id"],
                            "cells": [{"id": "hex-a", "properties": {"mean": 0}}],
                        }
                        for search in json["departure_searches"]
                    ]
                }
            )

    monkeypatch.setattr(service.settings, "TRAVELTIME_API_KEY", "key")
    monkeypatch.setattr(service.settings, "TRAVELTIME_APP_ID", "app")
    monkeypatch.setattr(service.settings, "TRAVELTIME_TIME_CONSTANT_SECONDS", 1500)
    monkeypatch.setattr(service.settings, "TRAVELTIME_BUS_FULL_SCORE_SECONDS", 1800)
    monkeypatch.setattr(
        service,
        "_departure_times",
        lambda: [datetime(2026, 11, 12, 9, tzinfo=timezone.utc)],
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: FullScoreClient())

    scores = asyncio.run(
        service.get_mobility_scores(
            ["hex-a"], anchors=[(49.8, 24.0, 1.0), (49.9, 24.1, 2.0)]
        )
    )

    assert scores["hex-a"] == 100.0


class FakeHereClient(FakeClient):
    def __init__(self):
        self.requests = []

    async def get(self, url, *, params):
        self.requests.append((url, params))
        sections = [{"type": "pedestrian"}]
        if "transit.router.hereapi.com" in url:
            sections.append({"type": "transit"})
        return FakeResponse({"routes": [{"sections": sections}]})


def test_bus_request_uses_public_transit_api(monkeypatch):
    monkeypatch.setattr(service.settings, "HERE_ROUTING_API_KEY", "test-key")
    client = FakeHereClient()
    monkeypatch.setattr(here_routes.httpx, "AsyncClient", lambda **kwargs: client)

    result = asyncio.run(here_routes.get_here_routes("hex-a", (49.84, 24.03)))

    assert result["routes"][0]["transport_mode"] == "bus"
    url, params = client.requests[0]
    assert url == here_routes.HERE_TRANSIT_URL
    assert params["modes"] == "bus"
    assert params["return"] == "polyline,actions,travelSummary,intermediate"
    assert "transportMode" not in params


def test_car_request_uses_here_routing_api(monkeypatch):
    monkeypatch.setattr(service.settings, "HERE_ROUTING_API_KEY", "test-key")
    client = FakeHereClient()
    monkeypatch.setattr(here_routes.httpx, "AsyncClient", lambda **kwargs: client)

    result = asyncio.run(here_routes.get_here_routes("hex-a", (49.84, 24.03), "car"))

    assert result["routes"][0]["transport_mode"] == "car"
    url, params = client.requests[0]
    assert url == here_routes.HERE_ROUTING_URL
    assert params["transportMode"] == "car"
    assert params["return"] == "polyline,summary,travelSummary,actions,instructions"


def test_both_modes_use_separate_transit_and_car_apis(monkeypatch):
    monkeypatch.setattr(service.settings, "HERE_ROUTING_API_KEY", "test-key")
    client = FakeHereClient()
    monkeypatch.setattr(here_routes.httpx, "AsyncClient", lambda **kwargs: client)

    result = asyncio.run(here_routes.get_here_routes("hex-a", (49.84, 24.03), "both"))

    assert [route["transport_mode"] for route in result["routes"]] == ["bus", "car"]
    assert [url for url, _ in client.requests] == [
        here_routes.HERE_TRANSIT_URL,
        here_routes.HERE_ROUTING_URL,
    ]


def test_bus_route_does_not_fall_back_to_walking_only_route(monkeypatch):
    monkeypatch.setattr(service.settings, "HERE_ROUTING_API_KEY", "test-key")

    class WalkingOnlyClient(FakeHereClient):
        async def get(self, url, *, params):
            self.requests.append((url, params))
            return FakeResponse({"routes": [{"sections": [{"type": "pedestrian"}]}]})

    client = WalkingOnlyClient()
    monkeypatch.setattr(here_routes.httpx, "AsyncClient", lambda **kwargs: client)

    result = asyncio.run(here_routes.get_here_routes("hex-a", (49.84, 24.03)))

    assert result["routes"] == []


def test_here_error_detail_reports_provider_reason_without_api_key():
    request = httpx.Request("GET", "https://router.hereapi.com/v8/routes?apiKey=secret")
    response = httpx.Response(
        400,
        json={"title": "Invalid input", "cause": "instructions requires actions"},
        request=request,
    )
    error = httpx.HTTPStatusError("request failed", request=request, response=response)

    detail = _here_error_detail(error)

    assert "HTTP 400" in detail
    assert "instructions requires actions" in detail
    assert "secret" not in detail
