from datetime import datetime, time, timedelta
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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


def _database_route_number(route_name: str) -> str | None:
    prefix = route_name.split(":", 1)[0].strip().upper()
    match = re.search(r"\d{1,3}", prefix)
    if not match:
        return None
    tail = prefix[match.end():]
    suffix = "A" if tail and tail[0].isalpha() else ""
    return f"{match.group(0)}{suffix}"


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


async def _route_geometry_from_stops(
    db: AsyncSession,
    points: list[list[float]],
    route_number: str,
) -> list[list[float]] | None:
    stop_ids: list[int] = []
    for point in points:
        if (
            not isinstance(point, (list, tuple))
            or len(point) < 2
            or not isinstance(point[0], (int, float))
            or not isinstance(point[1], (int, float))
        ):
            return None
        lon, lat = point[0], point[1]
        result = await db.execute(
            text("""
                SELECT id
                FROM stops
                WHERE source = 'easyway'
                  AND ST_DWithin(
                    geom,
                    ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                    5
                  )
                ORDER BY ST_Distance(
                    geom,
                    ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography
                )
                LIMIT 1
            """),
            {"lon": lon, "lat": lat},
        )
        row = result.first()
        if row is None:
            return None
        if not stop_ids or stop_ids[-1] != row[0]:
            stop_ids.append(row[0])

    if len(stop_ids) < 2:
        return None

    coordinates: list[list[float]] = []
    for start_stop_id, end_stop_id in zip(stop_ids, stop_ids[1:]):
        result = await db.execute(
            text("""
                SELECT ST_AsGeoJSON(g.geom::geometry) AS geojson, r.name AS route_name
                FROM route_segment_geometries g
                JOIN routes r ON r.id = g.route_id
                WHERE g.start_stop_id = :start_stop_id
                  AND g.end_stop_id = :end_stop_id
                  AND r.source = 'easyway'
            """),
            {"start_stop_id": start_stop_id, "end_stop_id": end_stop_id},
        )
        match = next(
            (
                row for row in result.mappings()
                if _database_route_number(row["route_name"]) == route_number
            ),
            None,
        )
        if match is None:
            return None
        geometry = match["geojson"]
        if isinstance(geometry, str):
            geometry = json.loads(geometry)
        if geometry.get("type") != "LineString":
            return None
        for coordinate in geometry.get("coordinates", []):
            coord = [float(coordinate[0]), float(coordinate[1])]
            if not coordinates or coordinates[-1] != coord:
                coordinates.append(coord)

    return coordinates if len(coordinates) >= 2 else None


async def _use_route_specific_bus_geometry(
    geojson: dict[str, Any], db: AsyncSession
) -> None:
    valid_bus_numbers = LVIV_VALID_TRANSIT_ROUTE_NUMBERS["bus"]
    for feature in geojson.get("features", []):
        properties = feature.get("properties", {})
        if (
            properties.get("transport_mode") != "bus"
            or str(properties.get("mode", "")).lower() not in {"bus", "minibus", "marshrutka"}
        ):
            continue
        route_number = properties.get("route_number")
        if route_number not in valid_bus_numbers:
            continue
        geometry = feature.get("geometry", {})
        replacement = await _route_geometry_from_stops(
            db, geometry.get("coordinates", []), route_number
        )
        if replacement:
            geometry["coordinates"] = replacement


def _route_departure_times() -> list[datetime]:
    zone = ZoneInfo("Europe/Kyiv")
    now = datetime.now(zone)
    departures = []
    for hour in (9, 14, 18):
        departure = datetime.combine(now.date(), time(hour), zone)
        if departure <= now:
            departure += timedelta(days=1)
        departures.append(departure)
    return departures


def _routes_from_response(payload: dict[str, Any]) -> list[tuple[dict[str, Any], float]]:
    found = []
    for result in payload.get("results", []):
        for location in result.get("locations", []):
            if location.get("id") != "destination":
                continue
            for prop in location.get("properties", []):
                route = prop.get("route")
                if isinstance(route, dict) and route.get("parts"):
                    seconds = prop.get("travel_time", route.get("travel_time"))
                    if not isinstance(seconds, (int, float)):
                        seconds = float("inf")
                    found.append((route, float(seconds)))
    return found


async def get_traveltime_routes(
    h3_id: str,
    origin: tuple[float, float],
    transportation: str = "bus",
    destination: tuple[float, float] = LVIV_CENTER,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    if not settings.TRAVELTIME_API_KEY or not settings.TRAVELTIME_APP_ID:
        raise RuntimeError("TravelTime API credentials are not configured")

    headers = {
        "Content-Type": "application/json",
        "X-Application-Id": settings.TRAVELTIME_APP_ID,
        "X-Api-Key": settings.TRAVELTIME_API_KEY,
    }
    routes: list[dict[str, Any]] = []
    scheduled_departures = _route_departure_times()
    async with httpx.AsyncClient(timeout=30.0) as client:
        for mode in mobility_modes(transportation):
            transport_type = "public_transport" if mode == "bus" else "driving"
            choices_for_schedule = []
            for scheduled_departure in scheduled_departures:
                first_route = None
                chosen_route = None
                retry_count = 5 if mode == "bus" else 0
                for retry in range(retry_count + 1):
                    departure_time = scheduled_departure + timedelta(minutes=15 * retry)
                    departure_search = {
                        "id": f"route-{mode}-{scheduled_departure.hour}-{retry}",
                        "departure_location_id": "origin",
                        "arrival_location_ids": ["destination"],
                        "departure_time": departure_time.isoformat(timespec="seconds"),
                        "transportation": {"type": transport_type},
                        "properties": ["route", "travel_time"],
                    }
                    payload = {
                        "locations": [
                            {"id": "origin", "coords": {"lat": origin[0], "lng": origin[1]}},
                            {"id": "destination", "coords": {"lat": destination[0], "lng": destination[1]}},
                        ],
                        "departure_searches": [departure_search],
                    }
                    response = await client.post(
                        TRAVELTIME_ROUTES_URL, headers=headers, json=payload
                    )
                    response.raise_for_status()
                    candidates = _routes_from_response(response.json())
                    if candidates and first_route is None:
                        first_route = candidates[0]
                    valid_candidates = [
                        candidate for candidate in candidates
                        if mode != "bus" or _is_valid_transit_route(candidate[0])
                    ]
                    if valid_candidates:
                        chosen_route = min(valid_candidates, key=lambda candidate: candidate[1])
                        break
                chosen_route = chosen_route or first_route
                if chosen_route is not None:
                    choices_for_schedule.append(chosen_route)

            if choices_for_schedule:
                route, seconds = min(choices_for_schedule, key=lambda candidate: candidate[1])
                routes.append({"transport_mode": mode, "travel_time": seconds, **route})

    try:
        geojson = build_traveltime_routes_geojson(routes)
    except (KeyError, TypeError, ValueError) as exc:
        raise TravelTimeRouteFormatError("TravelTime API returned invalid route geometry") from exc
    if db is not None:
        await _use_route_specific_bus_geometry(geojson, db)
    return {"h3_id": h3_id, "routes": geojson}
