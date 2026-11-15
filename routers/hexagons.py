from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from geoalchemy2 import Geometry
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.category_narratives.service import fetch_hexagon_explanations
from app.config import settings
from app.database import get_async_db
from app.here_routes_service import get_here_routes
from app.hexagon_repository import fetch_hexagon, fetch_hexagons
from app.mobility_score_service import get_mobility_scores
from app.mobility_transport import LVIV_CENTER
from app.models import HexTable
from app.schemas import HexagonDetailResponse, HexagonRatingResponse, HexagonWeights
from app.scoring import calculate_category_ratings, calculate_overall_rating


router = APIRouter(prefix="/hexagons", tags=["hexagons"])


def _here_error_detail(error: httpx.HTTPStatusError) -> str:
    try:
        body = error.response.json()
    except ValueError:
        body = {}
    message = " ".join(
        body[key].strip()
        for key in ("title", "cause", "code", "error_description")
        if isinstance(body, dict)
        and isinstance(body.get(key), str)
        and body[key].strip()
    )
    detail = f"HERE API returned HTTP {error.response.status_code}"
    return f"{detail}: {message[:300]}" if message else detail


@router.get("", response_model=list[HexagonRatingResponse])
async def get_hexagons(
    weights: Annotated[HexagonWeights, Query()],
    transportation: str = Query(default="bus"),
    anchor_lat: list[float] | None = Query(default=None),
    anchor_lon: list[float] | None = Query(default=None),
    weight: list[float] | None = Query(default=None),
    db: AsyncSession = Depends(get_async_db),
):
    hexagons = await fetch_hexagons(db)
    anchors = None
    if anchor_lat is not None or anchor_lon is not None or weight is not None:
        if not anchor_lat or not anchor_lon or len(anchor_lat) != len(anchor_lon):
            raise HTTPException(status_code=400, detail="anchor_lat and anchor_lon must match")
        anchor_weights = weight or [1.0] * len(anchor_lat)
        if len(anchor_weights) != len(anchor_lat):
            raise HTTPException(status_code=400, detail="Provide one weight per anchor")
        anchors = [
            (lat, lon, anchor_weight)
            for lat, lon, anchor_weight in zip(anchor_lat, anchor_lon, anchor_weights)
        ]
    mobility_scores = {}
    if weights.mobility > 0:
        try:
            mobility_scores = await get_mobility_scores(
                [str(hexagon["h3_id"]) for hexagon in hexagons], transportation, anchors
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="TravelTime API request failed") from exc

    user_weights = weights.model_dump()
    return [
        {
            "h3_id": hexagon["h3_id"],
            "overall_rating": calculate_overall_rating(
                {**hexagon, "mobility": mobility_scores.get(str(hexagon["h3_id"]), 0.0)},
                user_weights,
            ),
        }
        for hexagon in hexagons
    ]


@router.get("/{h3_id}", response_model=HexagonDetailResponse)
async def get_hexagon(
    h3_id: str,
    weights: Annotated[HexagonWeights, Query()],
    transportation: str = Query(default="bus"),
    anchor_lat: list[float] | None = Query(default=None),
    anchor_lon: list[float] | None = Query(default=None),
    weight: list[float] | None = Query(default=None),
    db: AsyncSession = Depends(get_async_db),
):
    hexagon = await fetch_hexagon(db, h3_id)
    if hexagon is None:
        raise HTTPException(status_code=404, detail="Hexagon not found")

    anchors = None
    if anchor_lat is not None or anchor_lon is not None or weight is not None:
        if not anchor_lat or not anchor_lon or len(anchor_lat) != len(anchor_lon):
            raise HTTPException(status_code=400, detail="anchor_lat and anchor_lon must match")
        anchor_weights = weight or [1.0] * len(anchor_lat)
        if len(anchor_weights) != len(anchor_lat):
            raise HTTPException(status_code=400, detail="Provide one weight per anchor")
        anchors = [
            (lat, lon, anchor_weight)
            for lat, lon, anchor_weight in zip(anchor_lat, anchor_lon, anchor_weights)
        ]
    mobility_score = 0.0
    if weights.mobility > 0:
        try:
            mobility_score = (await get_mobility_scores([h3_id], transportation, anchors)).get(h3_id, 0.0)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="TravelTime API request failed") from exc

    if not settings.HERE_ROUTING_API_KEY:
        raise HTTPException(status_code=503, detail="HERE API is not configured")
    result = await db.execute(
        select(
            HexTable.geom.cast(Geometry).ST_Y().label("lat"),
            HexTable.geom.cast(Geometry).ST_X().label("lon"),
        ).where(cast(HexTable.h3_id, String) == h3_id)
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Hexagon not found")
    try:
        route_result = await get_here_routes(
            h3_id, (float(row["lat"]), float(row["lon"])), transportation, LVIV_CENTER
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=_here_error_detail(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"HERE API request failed ({type(exc).__name__})",
        ) from exc
    routes = route_result["routes"]

    hexagon = {**hexagon, "mobility": mobility_score}
    user_weights = weights.model_dump()
    explanations = await fetch_hexagon_explanations(db, hexagon, user_weights)
    categories = [
        {**rating, **explanations[rating["category"]]}
        for rating in calculate_category_ratings(hexagon, user_weights)
    ]
    return {
        "h3_id": hexagon["h3_id"],
        "overall_rating": calculate_overall_rating(hexagon, user_weights),
        "categories": categories,
        "routes": routes,
    }
