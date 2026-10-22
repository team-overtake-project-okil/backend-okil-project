from fastapi import APIRouter, Depends, HTTPException
from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.models import Point
from app.schemas import PointResponse


router = APIRouter(prefix="/points", tags=["points"])


@router.get("/{point_id}", response_model=PointResponse)
async def get_point(
    point_id: int,
    db: AsyncSession = Depends(get_async_db),
):
    point_geometry = cast(
        Point.geom,
        Geometry(geometry_type="POINT", srid=4326),
    )

    query = select(
        Point.id,
        cast(Point.h3_id, String).label("h3_id"),
        Point.type,
        Point.source,
        Point.external_id,
        Point.name,
        ST_Y(point_geometry).label("lat"),
        ST_X(point_geometry).label("lng"),
    ).where(Point.id == point_id)

    result = await db.execute(query)
    row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Point not found")

    return dict(row)