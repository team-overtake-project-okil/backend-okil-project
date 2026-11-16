from datetime import datetime, timedelta, timezone
import re
from typing import Any

import httpx

from app.config import settings
from app.mobility_transport import LVIV_CENTER, LVIV_VALID_TRANSIT_ROUTE_NUMBERS, mobility_modes
from app.route_geojson import build_traveltime_routes_geojson

TRAVELTIME_ROUTES_URL = "https://api.traveltimeapp.com/v4/routes"
_ROUTE_NUMBER = re.compile(r"(?<![A-Z0-9])(\d{1,3}[A-Z]?)(?![A-Z0-9])", re.IGNORECASE)
_TRANSIT_NUMBER_IN_DIRECTIONS = re.compile(
    r"(?:bus|tram|trolleybus|minibus|marshrutka)\s+(\d{1,3}[A-Z]?)(?![A-Z0-9])",
    re.IGNORECASE,
)


def _transit_route_number(part: dict[str, Any]) -> str | None:
    for key in ("line", "route_number", "route", "name"):
        value = part.get(key)
        if value is not None:
            match = _ROUTE_NUMBER.search(str(value))
            if match:
                return match.group(1).upper()
    directions = part.get("directions")
    if isinstance(directions, str):
        match = _TRANSIT_NUMBER_IN_DIRECTIONS.search(directions)
        if match:
            return match.group(1).upper()
    return None


def _is_valid_transit_route(route: dict[str, Any]) -> bool:
    transit_parts = []
    for part in route.get("parts", []):
        mode = str(part.get("mode", "")).lower()
        if mode in {"walk", "walking"} or part.get("type") == "start_end":
            continue
        transit_parts.append(part)
    if not transit_parts:
        return False
    for part in transit_parts:
        number = _transit_route_number(part)
        if number is None:
            return False
        mode = str(part.get("mode", "")).lower()
        route_type = (
            "trolleybus" if "trolley" in mode else
            "tram" if "tram" in mode else
            "minibus" if "minibus" in mode or "marshrutka" in mode else
            "bus"
        )
        if number not in LVIV_VALID_TRANSIT_ROUTE_NUMBERS[route_type]:
            return False
    return True


class TravelTimeRouteFormatError(RuntimeError):
    """Raised when TravelTime returns invalid route geometry."""


async def get_traveltime_routes(
    h3_id: str,
    origin: tuple[float, float],
    transportation: str = "bus",
    destination: tuple[float, float] = LVIV_CENTER,
) -> dict[str, Any]:
    if not settings.TRAVELTIME_API_KEY or not settings.TRAVELTIME_APP_ID:
        raise RuntimeError("TravelTime API credentials are not configured")

    departure_time = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
    headers = {
        "Content-Type": "application/json",
        "X-Application-Id": settings.TRAVELTIME_APP_ID,
        "X-Api-Key": settings.TRAVELTIME_API_KEY,
    }
    routes: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for mode in mobility_modes(transportation):
            transport_type = "public_transport" if mode == "bus" else "driving"
            departure_search = {
                "id": f"route-{mode}",
                "departure_location_id": "origin",
                "arrival_location_ids": ["destination"],
                "departure_time": departure_time,
                "transportation": {"type": transport_type},
                "properties": ["route"],
            }
            if mode == "bus":
                # TravelTime can return up to five alternatives in a time window.
                departure_search["range"] = {
                    "enabled": True,
                    "max_results": 5,
                    "width": 1800,
                }
            payload = {
                "locations": [
                    {"id": "origin", "coords": {"lat": origin[0], "lng": origin[1]}},
                    {"id": "destination", "coords": {"lat": destination[0], "lng": destination[1]}},
                ],
                "departure_searches": [departure_search],
            }
            response = await client.post(TRAVELTIME_ROUTES_URL, headers=headers, json=payload)
            response.raise_for_status()
            result = next(iter(response.json().get("results", [])), {})
            fallback_route = None
            valid_route = None
            for location in result.get("locations", []):
                if location.get("id") != "destination":
                    continue
                for prop in location.get("properties", []):
                    route = prop.get("route")
                    if isinstance(route, dict) and route.get("parts"):
                        if fallback_route is None:
                            fallback_route = route
                        if mode != "bus" or _is_valid_transit_route(route):
                            valid_route = route
                            break
                if valid_route is not None:
                    break
            chosen_route = valid_route or fallback_route
            if chosen_route is not None:
                routes.append({"transport_mode": mode, **chosen_route})

    try:
        geojson = build_traveltime_routes_geojson(routes)
    except (KeyError, TypeError, ValueError) as exc:
        raise TravelTimeRouteFormatError("TravelTime API returned invalid route geometry") from exc
    return {"h3_id": h3_id, "routes": geojson}
