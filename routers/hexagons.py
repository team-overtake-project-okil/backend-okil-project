from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.hexagon_repository import fetch_hexagons
from app.hexagon_scoring import calculate_overall_rating
from app.schemas import HexagonRatingResponse, HexagonWeights


router = APIRouter(prefix="/hexagons", tags=["hexagons"])


@router.get("", response_model=list[HexagonRatingResponse])
async def get_hexagons(
    weights: Annotated[HexagonWeights, Query()],
    db: AsyncSession = Depends(get_async_db),
):
    hexagons = await fetch_hexagons(db)
    user_weights = weights.model_dump()

    ratings = []

    for hexagon in hexagons:
        overall_rating = calculate_overall_rating(
            scores=hexagon,
            user_weights=user_weights,
        )

        ratings.append(
            {
                "h3_id": hexagon["h3_id"],
                "overall_rating": overall_rating,
            }
        )

    return ratings