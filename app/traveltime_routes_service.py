from collections import defaultdict
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


_BUS_GEOMETRY_SQL = text("""
    WITH points AS MATERIALIZED (
        SELECT feature_id, point_order, lon, lat
        FROM jsonb_to_recordset(CAST(:points AS jsonb)) AS input(
            feature_id integer, point_order integer, lon double precision, lat double precision
        )
    ),
    nearest AS MATERIALIZED (
        SELECT coordinate.lon, coordinate.lat, stop.id AS stop_id
        FROM (SELECT DISTINCT lon, lat FROM points) AS coordinate
        CROSS JOIN LATERAL (
            SELECT ST_SetSRID(ST_MakePoint(coordinate.lon, coordinate.lat), 4326)::geography AS geom
        ) AS location
        LEFT JOIN LATERAL (
            SELECT id
            FROM stops
            WHERE source = 'easyway' AND ST_DWithin(geom, location.geom, 5)
            ORDER BY ST_Distance(geom, location.geom), id
            LIMIT 1
        ) AS stop ON true
    ),
    legs AS MATERIALIZED (
        SELECT p.feature_id, p.point_order, n.stop_id AS start_id,
               lead(n.stop_id) OVER (PARTITION BY p.feature_id ORDER BY p.point_order) AS end_id
        FROM points p
        JOIN nearest n USING (lon, lat)
    ),
    segments AS MATERIALIZED (
        SELECT g.start_stop_id, g.end_stop_id, g.id, r.name AS route_name,
               ST_AsGeoJSON(g.geom::geometry) AS geojson
        FROM (SELECT DISTINCT start_id, end_id FROM legs WHERE start_id <> end_id) AS pair
        JOIN route_segment_geometries g ON g.start_stop_id = pair.start_id AND g.end_stop_id = pair.end_id
        JOIN routes r ON r.id = g.route_id AND r.source = 'easyway'
    )
    SELECT l.feature_id, l.point_order, l.start_id IS NULL AS missing_stop, s.route_name, s.geojson
    FROM legs l
    LEFT JOIN segments s ON s.start_stop_id = l.start_id AND s.end_stop_id = l.end_id
    WHERE l.start_id IS NULL OR l.start_id <> l.end_id
    ORDER BY l.feature_id, l.point_order, s.id
""")


async def use_route_specific_bus_geometry(
    collections: list[dict[str, Any]], db: AsyncSession,
) -> None:
    features = []
    points = []
    for collection in collections:
        for feature in collection.get("features", []):
            props = feature.get("properties", {})
            number = props.get("route_number")
            if (
                props.get("transport_mode") != "bus"
                or str(props.get("mode", "")).lower() not in {"bus", "minibus", "marshrutka"}
                or number not in LVIV_VALID_TRANSIT_ROUTE_NUMBERS["bus"]
            ):
                continue
            geometry = feature.get("geometry", {})
            coords = geometry.get("coordinates", [])
            if len(coords) < 2:
                continue
            feature_id = len(features)
            features.append((geometry, number))
            points.extend(
                {"feature_id": feature_id, "point_order": order, "lon": lon, "lat": lat}
                for order, (lon, lat) in enumerate(coords)
            )
    if not points:
        return

    result = await db.execute(_BUS_GEOMETRY_SQL, {"points": json.dumps(points)})
    legs = defaultdict(dict)
    missing_stops = set()
    for row in result.mappings():
        feature_id = row["feature_id"]
        if row["missing_stop"]:
            missing_stops.add(feature_id)
            continue
        matches = legs[feature_id].setdefault(row["point_order"], {})
        if row["route_name"]:
            matches.setdefault(_database_route_number(row["route_name"]), row["geojson"])

    for feature_id, (geometry, number) in enumerate(features):
        if feature_id in missing_stops:
            continue
        coordinates = []
        for matches in legs[feature_id].values():
            shape = matches.get(number)
            if not shape:
                break
            if isinstance(shape, str):
                shape = json.loads(shape)
            if shape.get("type") != "LineString":
                break
            for lon, lat in shape["coordinates"]:
                point = [float(lon), float(lat)]
                if not coordinates or coordinates[-1] != point:
                    coordinates.append(point)
        else:
            if len(coordinates) >= 2:
                geometry["coordinates"] = coordinates


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
    async with httpx.AsyncClient(timeout=60.0) as client:
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
                    try:
                        candidates = _routes_from_response(response.json())
                    except (ValueError, TypeError, AttributeError) as exc:
                        raise TravelTimeRouteFormatError("TravelTime returned invalid route data") from exc
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
        await use_route_specific_bus_geometry([geojson], db)
    return {"h3_id": h3_id, "routes": geojson}
