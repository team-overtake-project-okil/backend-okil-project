from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.models import PointType
from app.schemas import PointTypeResponse

router = APIRouter(prefix="/point_types", tags=["point-type"])

@router.get("/{point_type}", response_model=PointTypeResponse)
async def get_point_type(
        point_type: str,
        db: AsyncSession = Depends(get_async_db),
):
    result = await db.execute(
        select(
            PointType.type,
            PointType.category,
            PointType.radius_of_influence_1,
            PointType.radius_of_influence_2,
            PointType.radius_of_influence_3,
        ).where(PointType.type == point_type)
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Point type not found")

    return dict(row)