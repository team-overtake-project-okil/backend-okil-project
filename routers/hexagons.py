from fastapi import APIRouter, Depends
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.models import HexTable
from app.schemas import HexTableResponse


router = APIRouter(prefix="/hexagons", tags=["hexagons"])

@router.get("", response_model=list[HexTableResponse])
async def get_hexagons(
        db: AsyncSession = Depends(get_async_db),
):
    query = select(
        cast(HexTable.h3_id, String).label("h3_id"),
        HexTable.health,
        HexTable.study,
        HexTable.daily,
        HexTable.mobility,
        HexTable.sport,
        HexTable.enjoyment,
        HexTable.security,
    ).order_by(HexTable.h3_id)

    result = await db.execute(query)
    rows = result.mappings().all()

    return [dict(row) for row in rows]