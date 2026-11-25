import asyncio
import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from geoalchemy2 import Geometry
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.category_narratives.service import fetch_hexagon_explanations
from app.anchor_routes import PALETTE, style_anchor_routes
from app.config import settings
from app.database import get_async_db
from app.traveltime_routes_service import (
    TravelTimeRouteFormatError,
    get_traveltime_routes,
    use_route_specific_bus_geometry,
)
from app.hexagon_repository import fetch_hexagon, fetch_hexagons
from app.mobility_score_service import get_mobility_scores
from app.mobility_transport import LVIV_CENTER
from app.models import HexTable
from app.schemas import HexagonDetailResponse, HexagonRatingResponse, HexagonWeights
from app.scoring import calculate_category_ratings, calculate_overall_rating


router = APIRouter(prefix="/hexagons", tags=["hexagons"])
logger = logging.getLogger(__name__)
ROUTE_LOOKUP_TIMEOUT_SECONDS = 60.0


@router.get("", response_model=list[HexagonRatingResponse])
async def get_hexagons(
    weights: Annotated[HexagonWeights, Depends()],
    transportation: str = Query(default="bus"),
    anchor_lat: list[float] | None = Query(default=None),
    anchor_lon: list[float] | None = Query(default=None),
    weight: list[float] | None = Query(default=None),
    db: AsyncSession = Depends(get_async_db),
):
    return await rate_all_hexagons(db, weights, transportation, anchor_lat, anchor_lon, weight)


async def rate_all_hexagons(db, weights, transportation="bus", anchor_lat=None, anchor_lon=None, weight=None):
    if not settings.EXTERNAL_TRANSPORT_ENABLED:
        weights = weights.model_copy(update={"mobility": 0.0})
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
            if hexagons and not mobility_scores:
                weights = weights.model_copy(update={"mobility": 0.0})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (RuntimeError, httpx.HTTPError) as exc:
            logger.warning("Mobility scoring unavailable (%s); excluding mobility", type(exc).__name__)
            weights = weights.model_copy(update={"mobility": 0.0})

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
    weights: Annotated[HexagonWeights, Depends()],
    transportation: str = Query(default="bus"),
    anchor_lat: list[float] | None = Query(default=None),
    anchor_lon: list[float] | None = Query(default=None),
    weight: list[float] | None = Query(default=None),
    db: AsyncSession = Depends(get_async_db),
):
    if not settings.EXTERNAL_TRANSPORT_ENABLED:
        weights = weights.model_copy(update={"mobility": 0.0})
    hexagon = await fetch_hexagon(db, h3_id)
    if hexagon is None:
        raise HTTPException(status_code=404, detail="Hexagon not found")

    anchors = None
    if anchor_lat is not None or anchor_lon is not None or weight is not None:
        if not anchor_lat or not anchor_lon or len(anchor_lat) != len(anchor_lon):
            raise HTTPException(status_code=400, detail="anchor_lat and anchor_lon must match")
        if len(anchor_lat) > len(PALETTE):
            raise HTTPException(status_code=400, detail="Select at most five route destinations")
        anchor_weights = weight or [1.0] * len(anchor_lat)
        if len(anchor_weights) != len(anchor_lat):
            raise HTTPException(status_code=400, detail="Provide one weight per anchor")
        anchors = [
            (lat, lon, anchor_weight)
            for lat, lon, anchor_weight in zip(anchor_lat, anchor_lon, anchor_weights)
        ]
    mobility_score = 0.0
    transport_available = settings.EXTERNAL_TRANSPORT_ENABLED
    if weights.mobility > 0:
        try:
            scores = await get_mobility_scores([h3_id], transportation, anchors)
            if h3_id not in scores:
                weights = weights.model_copy(update={"mobility": 0.0})
                transport_available = False
            else:
                mobility_score = scores[h3_id]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (RuntimeError, httpx.HTTPError) as exc:
            logger.warning("Mobility scoring unavailable (%s); excluding mobility", type(exc).__name__)
            weights = weights.model_copy(update={"mobility": 0.0})
            transport_available = False

    routes = {"type": "FeatureCollection", "features": []}
    if transport_available and settings.TRAVELTIME_API_KEY and settings.TRAVELTIME_APP_ID:
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
            destinations = [(lat, lon) for lat, lon, _ in anchors] if anchors else [LVIV_CENTER]

            async def fetch_route_collections():
                collections = []
                for destination in destinations:
                    route_result = await get_traveltime_routes(
                        h3_id, (float(row["lat"]), float(row["lon"])), transportation, destination,
                    )
                    collections.append(route_result["routes"])
                return collections

            collections = await asyncio.wait_for(
                fetch_route_collections(), timeout=ROUTE_LOOKUP_TIMEOUT_SECONDS
            )
            await use_route_specific_bus_geometry(collections, db)
            routes = style_anchor_routes(collections) if anchors else collections[0]
        except (TravelTimeRouteFormatError, httpx.HTTPError, TimeoutError) as exc:
            logger.warning("TravelTime routes unavailable (%s)", type(exc).__name__)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
