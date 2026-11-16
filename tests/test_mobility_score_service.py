import asyncio
from datetime import datetime, timezone
import math

import pytest
import httpx

import app.mobility_score_service as service
import app.traveltime_routes_service as route_service


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


def test_route_allowlist_respects_transit_kind():
    def route(mode, number):
        return {"parts": [{
            "type": "public_transport",
            "mode": mode,
            "line": f"{mode} {number}",
            "coords": [],
        }]}

    assert route_service._is_valid_transit_route(route("tram", "7"))
    assert not route_service._is_valid_transit_route(route("minibus", "7"))
    assert not route_service._is_valid_transit_route(route("trolleybus", "27"))
    assert route_service._is_valid_transit_route(route("minibus", "27"))
    assert not route_service._is_valid_transit_route(route("bus", "80"))


def test_bus_route_asks_for_alternatives_and_chooses_first_allowed_route(monkeypatch):
    monkeypatch.setattr(service.settings, "TRAVELTIME_API_KEY", "key")
    monkeypatch.setattr(service.settings, "TRAVELTIME_APP_ID", "app")

    class AlternativesClient:
        def __init__(self):
            self.payload = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, headers, json):
            self.payload = json
            invalid = {"parts": [{"type": "public_transport", "mode": "bus", "line": "Bus 80",
                                  "coords": [{"lat": 49.8, "lng": 24.0}, {"lat": 49.81, "lng": 24.01}]}]}
            valid = {"parts": [{"type": "public_transport", "mode": "bus", "line": "Bus 22 route",
                                "coords": [{"lat": 49.8, "lng": 24.0}, {"lat": 49.81, "lng": 24.01}]}]}
            return FakeResponse({"results": [{"locations": [{"id": "destination", "properties": [
                {"route": invalid}, {"route": valid},
            ]}]}]})

    client = AlternativesClient()
    monkeypatch.setattr(route_service.httpx, "AsyncClient", lambda **kwargs: client)

    result = asyncio.run(route_service.get_traveltime_routes("hex-a", (49.84, 24.03)))

    search = client.payload["departure_searches"][0]
    assert search["range"] == {"enabled": True, "max_results": 5, "width": 1800}
    assert result["routes"]["features"][0]["properties"]["label"] == "22"
