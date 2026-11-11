import math
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import settings

TRAVELTIME_URL = "https://api.traveltimeapp.com/v4/h3"
HERE_URL = "https://router.hereapi.com/v8/routes"
LVIV_CENTER = (49.8397, 24.0297)


def _modes(transportation: str) -> list[str]:
    values = [value.strip().lower().replace("_", "") for value in transportation.split(",")]
    modes = []
    for value in values:
        if value in {"both", "busandcar", "carandbus"}:
            modes.extend(["bus", "car"])
        elif value in {"bus", "publictransport"}:
            modes.append("bus")
        elif value == "car":
            modes.append("car")
        else:
            raise ValueError("transportation must be bus, car, or both")
    return list(dict.fromkeys(modes))


def _departure_times() -> list[datetime]:
    zone = ZoneInfo("Europe/Kyiv")
    now = datetime.now(zone)
    times = []
    for hour in (9, 14, 18):
        departure = datetime.combine(now.date(), time(hour), zone)
        if departure <= now:
            departure += timedelta(days=1)
        times.append(departure)
    return times


def _scores_by_search(payload: dict[str, Any], tau: float) -> dict[str, dict[str, float]]:
    scores: dict[str, dict[str, float]] = {}
    for result in payload.get("results", []):
        search_scores = {}
        for cell in result.get("cells", result.get("locations", [])):
            properties = cell.get("properties", {})
            seconds = properties.get("mean")
            cell_id = cell.get("id") or cell.get("h3_id")
            if cell_id and isinstance(seconds, (int, float)):
                search_scores[str(cell_id)] = 100 * math.exp(-seconds / tau)
        scores[result.get("search_id", "")] = search_scores
    return scores


async def get_mobility_scores(
    hex_ids: list[str],
    transportation: str = "bus",
    anchors: list[tuple[float, float, float]] | None = None,
) -> dict[str, float]:
    if not hex_ids:
        return {}
    if not settings.TRAVELTIME_API_KEY or not settings.TRAVELTIME_APP_ID:
        raise RuntimeError("TravelTime API credentials are not configured")

    anchors = anchors or [(LVIV_CENTER[0], LVIV_CENTER[1], 1.0)]
    if any(
        not all(math.isfinite(value) for value in (lat, lon, weight)) or weight <= 0
        for lat, lon, weight in anchors
    ):
        raise ValueError("anchor coordinates and weights must be finite; weights must be positive")

    modes = _modes(transportation)
    departures = _departure_times()
    headers = {
        "Content-Type": "application/json",
        "X-Application-Id": settings.TRAVELTIME_APP_ID,
        "X-Api-Key": settings.TRAVELTIME_API_KEY,
    }
    tau = settings.TRAVELTIME_TIME_CONSTANT_SECONDS
    if tau <= 0:
        raise RuntimeError("TRAVELTIME_TIME_CONSTANT_SECONDS must be positive")

    scores_by_anchor = []
    for lat, lon, _ in anchors:
        scores_by_mode = []
        for mode in modes:
            searches = [
                {
                    "id": f"{mode}-{index}",
                    "coords": {"lat": lat, "lng": lon},
                    "departure_time": departure.isoformat(timespec="seconds"),
                    "travel_time": settings.TRAVELTIME_MAX_SEARCH_SECONDS,
                    "transportation": {"type": mode},
                }
                for index, departure in enumerate(departures)
            ]
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    TRAVELTIME_URL,
                    headers=headers,
                    json={"resolution": 9, "departure_searches": searches, "properties": ["mean"]},
                )
                response.raise_for_status()
                scores_by_mode.append(_scores_by_search(response.json(), tau))

        per_anchor = {}
        for hex_id in hex_ids:
            slots = [
                min(
                    scores.get(f"{mode}-{index}", {}).get(hex_id, 0.0)
                    for mode, scores in zip(modes, scores_by_mode)
                )
                for index in range(len(departures))
            ]
            per_anchor[hex_id] = sum(slots) / len(slots)
        scores_by_anchor.append(per_anchor)

    total_weight = sum(weight for _, _, weight in anchors)
    return {
        hex_id: (
            0.0
            if any(scores[hex_id] <= 0 for scores in scores_by_anchor)
            else math.exp(
                sum(
                    (weight / total_weight) * math.log(scores[hex_id])
                    for (_, _, weight), scores in zip(anchors, scores_by_anchor)
                )
            )
        )
        for hex_id in hex_ids
    }


async def get_here_routes(
    h3_id: str,
    destination: tuple[float, float],
    transportation: str = "bus",
    origin: tuple[float, float] = LVIV_CENTER,
) -> dict[str, Any]:
    if not settings.HERE_ROUTING_API_KEY:
        raise RuntimeError("HERE Routing API key is not configured")

    routes = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for mode in _modes(transportation):
            response = await client.get(
                HERE_URL,
                params={
                    "apiKey": settings.HERE_ROUTING_API_KEY,
                    "transportMode": mode,
                    "origin": f"{origin[0]},{origin[1]}",
                    "destination": f"{destination[0]},{destination[1]}",
                    "return": "polyline,summary,travelSummary,instructions",
                },
            )
            response.raise_for_status()
            routes.extend(
                {"transport_mode": mode, **route}
                for route in response.json().get("routes", [])
            )
    return {"h3_id": h3_id, "routes": routes}
