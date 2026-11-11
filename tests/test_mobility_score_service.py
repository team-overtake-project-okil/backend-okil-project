import asyncio
from datetime import datetime, timezone
import math

import pytest

import app.mobility_score_service as service


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
    monkeypatch.setattr(
        service,
        "_departure_times",
        lambda: [datetime(2026, 11, 12, hour, tzinfo=timezone.utc) for hour in (9, 14, 18)],
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: FakeClient())

    scores = asyncio.run(service.get_mobility_scores(["hex-a", "hex-b"], "both"))
    expected = sum(100 * math.exp(-seconds / 1500) for seconds in (600, 1800, 1800)) / 3

    assert scores == {"hex-a": pytest.approx(expected), "hex-b": 0.0}
