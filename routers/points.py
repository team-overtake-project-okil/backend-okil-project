from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db

router = APIRouter(prefix="/points", tags=["points"])


@router.get("/{point_id}")
async def get_point(
    point_id: int,
    db: AsyncSession = Depends(get_async_db),
):
    query = text("""
        SELECT id, h3_id::text AS h3_id, type, placement_id,
               source, external_id, name
        FROM points
        WHERE id = :id
    """)

    result = await db.execute(query, {"id": point_id})
    row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Point not found")

    return dict(row)