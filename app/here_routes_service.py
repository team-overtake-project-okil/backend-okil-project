import re
from typing import Any

import httpx

from app.config import settings
from app.mobility_transport import (
    LVIV_CENTER,
    LVIV_VALID_TRANSIT_ROUTE_NUMBERS,
    mobility_modes,
)
from app.route_geojson import build_here_routes_geojson

HERE_ROUTING_URL = "https://router.hereapi.com/v8/routes"
HERE_TRANSIT_URL = "https://transit.router.hereapi.com/v8/routes"
TRANSIT_ALTERNATIVE_COUNTS = (10, 20, 30)
_TRANSIT_ROUTE_NUMBER = re.compile(r"(?<![A-Z0-9])(\d{1,3}[A-Z]?)(?![A-Z0-9])", re.IGNORECASE)


class HereRouteFormatError(RuntimeError):
    """Raised when HERE returns an invalid encoded route geometry."""


def _transit_route_number(section: dict[str, Any]) -> str | None:
    transport = section.get("transport")
    if not isinstance(transport, dict):
        return None

    for key in ("shortName", "routeNumber", "name"):
        value = transport.get(key)
        if value is None:
            continue
        match = _TRANSIT_ROUTE_NUMBER.search(str(value))
        if match:
            return match.group(1).upper()
    return None


def _has_only_valid_transit_routes(route: dict[str, Any]) -> bool:
    transit_sections = [
        section for section in route.get("sections", [])
        if section.get("type") == "transit"
    ]
    for section in transit_sections:
        route_number = _transit_route_number(section)
        if route_number is None:
            return False

        transport = section.get("transport")
        transport_description = ""
        if isinstance(transport, dict):
            transport_description = " ".join(
                str(transport.get(key, ""))
                for key in ("mode", "category", "name")
            ).lower()
        if "trolley" in transport_description:
            route_type = "trolleybus"
        elif "tram" in transport_description:
            route_type = "tram"
        elif "minibus" in transport_description or "marshrutka" in transport_description:
            route_type = "minibus"
        else:
            route_type = "bus"
        if route_number not in LVIV_VALID_TRANSIT_ROUTE_NUMBERS[route_type]:
            return False
    return bool(transit_sections)


def _route_duration_seconds(route: dict[str, Any]) -> float | None:
    summary = route.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("duration"), (int, float)):
        return float(summary["duration"])

    durations = []
    for section in route.get("sections", []):
        for key in ("travelSummary", "summary"):
            section_summary = section.get(key)
            if isinstance(section_summary, dict):
                duration = section_summary.get("duration")
                if isinstance(duration, (int, float)):
                    durations.append(float(duration))
                    break
    return sum(durations) if durations else None


async def get_here_routes(
    h3_id: str,
    origin: tuple[float, float],
    transportation: str = "bus",
    destination: tuple[float, float] = LVIV_CENTER,
) -> dict[str, Any]:
    if not settings.HERE_ROUTING_API_KEY:
        raise RuntimeError("HERE API key is not configured")

    routes: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for mode in mobility_modes(transportation):
            is_transit = mode == "bus"
            base_params = {
                "apiKey": settings.HERE_ROUTING_API_KEY,
                "origin": f"{origin[0]},{origin[1]}",
                "destination": f"{destination[0]},{destination[1]}",
                "return": (
                    "polyline,actions,travelSummary,intermediate"
                    if is_transit
                    else "polyline,summary,travelSummary,actions,instructions"
                ),
                **({"modes": "bus"} if is_transit else {"transportMode": "car"}),
            }
            if is_transit:
                fallback_route = None
                selected_route = None
                for alternative_count in TRANSIT_ALTERNATIVE_COUNTS:
                    response = await client.get(
                        HERE_TRANSIT_URL,
                        params={**base_params, "alternatives": alternative_count},
                    )
                    response.raise_for_status()
                    transit_routes = [
                        route
                        for route in response.json().get("routes", [])
                        if any(
                            section.get("type") == "transit"
                            for section in route.get("sections", [])
                        )
                    ]
                    if fallback_route is None and transit_routes:
                        fallback_route = transit_routes[0]

                    preferred_route = next(
                        (
                            route for route in transit_routes
                            if _has_only_valid_transit_routes(route)
                        ),
                        None,
                    )
                    if preferred_route is not None:
                        selected_route = preferred_route
                        break
                else:
                    if fallback_route is not None:
                        selected_route = fallback_route

                if selected_route is not None:
                    selected_duration = _route_duration_seconds(selected_route)
                    walking_route = None
                    if selected_duration is not None:
                        walking_response = await client.get(
                            HERE_ROUTING_URL,
                            params={
                                "apiKey": settings.HERE_ROUTING_API_KEY,
                                "origin": base_params["origin"],
                                "destination": base_params["destination"],
                                "return": "polyline,summary,travelSummary",
                                "transportMode": "pedestrian",
                            },
                        )
                        walking_response.raise_for_status()
                        walking_route = next(
                            iter(walking_response.json().get("routes", [])), None
                        )

                    walking_duration = (
                        _route_duration_seconds(walking_route)
                        if walking_route is not None
                        else None
                    )
                    if (
                        walking_duration is not None
                        and walking_duration < selected_duration
                    ):
                        walking_sections = []
                        for section in walking_route.get("sections", []):
                            transport = section.get("transport")
                            walking_sections.append({
                                **section,
                                "type": "pedestrian",
                                "transport": {
                                    **(transport if isinstance(transport, dict) else {}),
                                    "mode": "pedestrian",
                                },
                            })
                        routes.append({
                            "transport_mode": "walking",
                            **walking_route,
                            "sections": walking_sections,
                        })
                    else:
                        routes.append({"transport_mode": mode, **selected_route})
            else:
                response = await client.get(HERE_ROUTING_URL, params=base_params)
                response.raise_for_status()
                routes.extend(
                    {"transport_mode": mode, **route}
                    for route in response.json().get("routes", [])
                )
    try:
        geojson = build_here_routes_geojson(routes)
    except ValueError as exc:
        raise HereRouteFormatError("HERE API returned an invalid route polyline") from exc
    return {"h3_id": h3_id, "routes": geojson}
