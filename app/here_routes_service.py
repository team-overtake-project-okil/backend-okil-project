from typing import Any

import httpx

from app.config import settings
from app.mobility_transport import LVIV_CENTER, mobility_modes

HERE_ROUTING_URL = "https://router.hereapi.com/v8/routes"
HERE_TRANSIT_URL = "https://transit.router.hereapi.com/v8/routes"


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
            params = {
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
            response = await client.get(
                HERE_TRANSIT_URL if is_transit else HERE_ROUTING_URL,
                params=params,
            )
            response.raise_for_status()
            mode_routes = response.json().get("routes", [])
            if is_transit:
                mode_routes = [
                    route
                    for route in mode_routes
                    if any(
                        section.get("type") == "transit"
                        for section in route.get("sections", [])
                    )
                ]
            routes.extend({"transport_mode": mode, **route} for route in mode_routes)
    return {"h3_id": h3_id, "routes": routes}
