from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.models import HexTable
from app.schemas import HexTableResponse
from app.hexagon_repository import fetch_hexagons

router = APIRouter(prefix="/hexagons", tags=["hexagons"])

@router.get("", response_model=list[HexTableResponse])
async def get_hexagons(
    db: AsyncSession = Depends(get_async_db),
):
    return await fetch_hexagons(db)

@router.get("/{h3_id}", response_model=HexTableResponse)
async def get_hexagon(
    h3_id: str,
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

    return dict(row)