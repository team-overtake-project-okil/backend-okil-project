from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.hexagon_repository import fetch_hexagons
from app.models import HexTable
from app.schemas import HexTableResponse
from app.scoring import calculate_overall_rating

router = APIRouter(prefix="/hexagons", tags=["hexagons"])


@router.get("", response_model=list[HexTableResponse])
async def get_hexagons(
    db: AsyncSession = Depends(get_async_db),
):
    return await fetch_hexagons(db)


@router.get("/{h3_id}")
async def get_hexagon(
    h3_id: str,
    weight_health: float = Query(1.0, ge=0.0, le=5.0),
    weight_study: float = Query(1.0, ge=0.0, le=5.0),
    weight_daily: float = Query(1.0, ge=0.0, le=5.0),
    weight_mobility: float = Query(1.0, ge=0.0, le=5.0),
    weight_sport: float = Query(1.0, ge=0.0, le=5.0),
    weight_enjoyment: float = Query(1.0, ge=0.0, le=5.0),
    weight_security: float = Query(1.0, ge=0.0, le=5.0),
    db: AsyncSession = Depends(get_async_db),
):
    result = await db.execute(
        select(
            cast(HexTable.h3_id, String).label("h3_id"),
            HexTable.health,
            HexTable.study,
            HexTable.daily,
            HexTable.mobility,
            HexTable.sport,
            HexTable.enjoyment,
            HexTable.security,
        ).where(cast(HexTable.h3_id, String) == h3_id)
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Hexagon not found")

    data = dict(row)
    user_weights = {
        "health": weight_health,
        "study": weight_study,
        "daily": weight_daily,
        "mobility": weight_mobility,
        "sport": weight_sport,
        "enjoyment": weight_enjoyment,
        "security": weight_security,
    }

    # Formula and edge cases are implemented and tested in app/scoring.py.
    data["overall_rating"] = calculate_overall_rating(data, user_weights)
    return data
