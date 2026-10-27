from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.hexagon_repository import fetch_hexagons
from app.schemas import HexagonRatingResponse, HexagonWeights
from app.scoring import calculate_overall_rating


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
