from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.hexagon_repository import fetch_hexagon, fetch_hexagons
from app.schemas import HexagonDetailResponse, HexagonRatingResponse, HexagonWeights
from app.scoring import calculate_category_ratings, calculate_overall_rating


router = APIRouter(prefix="/hexagons", tags=["hexagons"])


@router.get("", response_model=list[HexagonRatingResponse])
async def get_hexagons(
    weights: Annotated[HexagonWeights, Query()],
    db: AsyncSession = Depends(get_async_db),
):
    user_weights = weights.model_dump()
    hexagons = await fetch_hexagons(db)

    return [
        {
            "h3_id": hexagon["h3_id"],
            "overall_rating": calculate_overall_rating(hexagon, user_weights),
        }
        for hexagon in hexagons
    ]


@router.get("/{h3_id}", response_model=HexagonDetailResponse)
async def get_hexagon(
    h3_id: str,
    weights: Annotated[HexagonWeights, Query()],
    db: AsyncSession = Depends(get_async_db),
):
    hexagon = await fetch_hexagon(db, h3_id)
    if hexagon is None:
        raise HTTPException(status_code=404, detail="Hexagon not found")

    user_weights = weights.model_dump()

    return {
        "h3_id": hexagon["h3_id"],
        "overall_rating": calculate_overall_rating(hexagon, user_weights),
        "categories": calculate_category_ratings(hexagon, user_weights),
    }