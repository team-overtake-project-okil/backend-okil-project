from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HexTable

async def fetch_hexagons(db: AsyncSession) -> list[dict]:
    query = select(
        cast(HexTable.h3_id, String).label("h3_id"),
        HexTable.health,
        HexTable.study,
        HexTable.daily,
        HexTable.mobility,
        HexTable.sport,
        HexTable.enjoyment,
        HexTable.food,
        HexTable.security,
    ).order_by(HexTable.h3_id)

    result = await db.execute(query)

    return [dict(row) for row in result.mappings().all()]

async def fetch_hexagon(db, h3_id: str) -> dict | None:
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
    return dict(row) if row else None
