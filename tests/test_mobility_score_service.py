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


def test_bus_route_retries_by_fifteen_minutes_and_chooses_fastest(monkeypatch):
    monkeypatch.setattr(service.settings, "TRAVELTIME_API_KEY", "key")
    monkeypatch.setattr(service.settings, "TRAVELTIME_APP_ID", "app")
    monkeypatch.setattr(
        route_service,
        "_route_departure_times",
        lambda: [
            datetime(2026, 11, 17, hour, tzinfo=timezone.utc)
            for hour in (9, 14, 18)
        ],
    )

    class ScheduleClient:
        def __init__(self):
            self.departures = []
            self.travel_times = (900, 800, 700, 1000)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, headers, json):
            search = json["departure_searches"][0]
            self.departures.append(search["departure_time"])
            call_index = len(self.departures) - 1
            line = "Bus 80" if call_index == 0 else "Bus 22"
            route = {"parts": [{
                "type": "public_transport", "mode": "bus", "line": line,
                "coords": [{"lat": 49.8, "lng": 24.0}, {"lat": 49.81, "lng": 24.01}],
            }]}
            return FakeResponse({"results": [{"locations": [{"id": "destination", "properties": [
                {"route": route, "travel_time": self.travel_times[call_index]},
            ]}]}]})

    client = ScheduleClient()
    monkeypatch.setattr(route_service.httpx, "AsyncClient", lambda **kwargs: client)

    result = asyncio.run(route_service.get_traveltime_routes("hex-a", (49.84, 24.03)))

    assert len(client.departures) == 4
    assert client.departures[:3] == [
        "2026-11-17T09:00:00+00:00",
        "2026-11-17T09:15:00+00:00",
        "2026-11-17T14:00:00+00:00",
    ]
    assert result["routes"]["features"][0]["properties"]["label"] == "22"
