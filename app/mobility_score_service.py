import math
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import settings
from app.mobility_transport import LVIV_CENTER, mobility_modes

TRAVELTIME_URL = "https://api.traveltimeapp.com/v4/h3"


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


def _travel_time_score(seconds: float, tau: float, full_score_seconds: int) -> float:
    excess_seconds = max(0.0, seconds - full_score_seconds)
    return 100 * math.exp(-excess_seconds / tau)


def _scores_by_search(
    payload: dict[str, Any], tau: float, full_score_seconds: int
) -> dict[str, dict[str, float]]:
    scores: dict[str, dict[str, float]] = {}
    for result in payload.get("results", []):
        search_scores = {}
        for cell in result.get("cells", result.get("locations", [])):
            properties = cell.get("properties", {})
            seconds = properties.get("mean")
            cell_id = cell.get("id") or cell.get("h3_id")
            if cell_id and isinstance(seconds, (int, float)) and math.isfinite(seconds):
                search_scores[str(cell_id)] = _travel_time_score(seconds, tau, full_score_seconds)
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

    modes = mobility_modes(transportation)
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
                full_score_seconds = (
                    settings.TRAVELTIME_BUS_FULL_SCORE_SECONDS
                    if mode == "bus"
                    else settings.TRAVELTIME_CAR_FULL_SCORE_SECONDS
                )
                scores_by_mode.append(
                    _scores_by_search(response.json(), tau, full_score_seconds)
                )

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
        hex_id: min(
            100.0,
            max(
                0.0,
                0.0
                if any(scores[hex_id] <= 0 for scores in scores_by_anchor)
                else math.exp(
                    sum(
                        (weight / total_weight) * math.log(scores[hex_id])
                        for (_, _, weight), scores in zip(anchors, scores_by_anchor)
                    )
                ),
            ),
        )
        for hex_id in hex_ids
    }
