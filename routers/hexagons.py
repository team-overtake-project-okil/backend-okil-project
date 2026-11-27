import asyncio
import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from geoalchemy2 import Geometry
from pydantic import ValidationError
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
from app.mobility_score_service import combine_anchor_scores, get_mobility_scores
from app.mobility_transport import LVIV_CENTER
from app.models import HexTable
from app.rounding import round_final
from app.schemas import ComparisonAssessment, HexagonComparisonRequest, HexagonDetailResponse, HexagonRatingResponse, HexagonWeights
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


@router.get("/compare", response_model=list[ComparisonAssessment])
async def compare_hexagons(
    weights: Annotated[HexagonWeights, Depends()],
    h3_id: list[str] = Query(...),
    color: list[str] = Query(...),
    anchor_lat: list[float] | None = Query(default=None),
    anchor_lon: list[float] | None = Query(default=None),
    selected_anchor_index: int | None = Query(default=None, ge=0),
    db: AsyncSession = Depends(get_async_db),
):
    if len(h3_id) != len(color):
        raise HTTPException(422, "Provide one color per hexagon")
    if len(anchor_lat or []) != len(anchor_lon or []):
        raise HTTPException(422, "anchor_lat and anchor_lon must match")
    try:
        request = HexagonComparisonRequest(
            hexagons=[{"h3_id": i, "color": c} for i, c in zip(h3_id, color)],
            anchors=[{"lat": lat, "lng": lon} for lat, lon in zip(anchor_lat or [], anchor_lon or [])],
            weights=weights,
            selected_anchor_index=selected_anchor_index,
        )
    except ValidationError as exc:
        raise HTTPException(422, "Invalid comparison parameters") from exc
    ids = [item.h3_id for item in request.hexagons]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Select distinct hexagons")
    destinations = [(a.lat, a.lng) for a in request.anchors] or [LVIV_CENTER]
    selected = request.selected_anchor_index
    if selected is not None and selected >= len(destinations):
        raise HTTPException(422, "Selected anchor index is out of range")

    id_column = cast(HexTable.h3_id, String)
    result = await db.execute(select(
        id_column.label("h3_id"), HexTable.health, HexTable.study, HexTable.daily,
        HexTable.sport, HexTable.enjoyment, HexTable.food, HexTable.security,
        HexTable.geom.cast(Geometry).ST_Y().label("lat"),
        HexTable.geom.cast(Geometry).ST_X().label("lon"),
    ).where(id_column.in_(ids)))
    rows = {row["h3_id"]: dict(row) for row in result.mappings()}
    if len(rows) != len(ids):
        raise HTTPException(404, "Hexagon not found")

    weights = request.weights or HexagonWeights()
    colors = [item.color for item in request.hexagons]
    routes = [{"type": "FeatureCollection", "features": []} for _ in ids]
    scores_by_anchor = []
    anchors = [(lat, lon, 1.0) for lat, lon in destinations]
    enabled = (settings.EXTERNAL_TRANSPORT_ENABLED and weights.mobility > 0
               and settings.TRAVELTIME_API_KEY and settings.TRAVELTIME_APP_ID)

    async def fetch_route(h3_id, destination):
        row = rows[h3_id]
        try:
            route = await asyncio.wait_for(get_traveltime_routes(
                h3_id, (row["lat"], row["lon"]), "bus", destination,
            ), timeout=ROUTE_LOOKUP_TIMEOUT_SECONDS)
            return route["routes"]
        except (TravelTimeRouteFormatError, httpx.HTTPError, TimeoutError) as exc:
            logger.warning("Comparison route unavailable (%s)", type(exc).__name__)
            return {"type": "FeatureCollection", "features": []}

    for index, anchor in enumerate(anchors):
        scores = {}
        if enabled:
            try:
                scores = await get_mobility_scores(ids, "bus", [anchor])
            except (RuntimeError, httpx.HTTPError) as exc:
                logger.warning("Comparison mobility unavailable (%s)", type(exc).__name__)
        scores_by_anchor.append(scores)
        if enabled and (selected is None or selected == index):
            collections = await asyncio.gather(*(fetch_route(i, destinations[index]) for i in ids))
            await use_route_specific_bus_geometry(collections, db)
            for feature in style_anchor_routes(collections, colors)["features"]:
                source = feature["properties"]["anchor_index"]
                feature["properties"]["anchor_index"] = index
                routes[source]["features"].append(feature)

    assessments = []
    for h3_id, collection in zip(ids, routes):
        available = all(h3_id in scores for scores in scores_by_anchor)
        mobility = combine_anchor_scores([h3_id], anchors, scores_by_anchor)[h3_id] if available else 0
        effective = weights if available else weights.model_copy(update={"mobility": 0.0})
        item = await _assessment(db, {**rows[h3_id], "mobility": mobility}, effective, collection)
        item["mobility_scores"] = [
            round_final(scores[h3_id]) if h3_id in scores else None for scores in scores_by_anchor
        ]
        assessments.append(item)
    return assessments


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
    return await _assessment(db, hexagon, weights, routes)


async def _assessment(db, hexagon, weights, routes):
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
