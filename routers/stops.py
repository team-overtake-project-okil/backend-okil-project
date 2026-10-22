from fastapi import APIRouter, Depends, HTTPException
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession
from geoalchemy2 import Geometry

from app.database import get_async_db
from app.models import Stop
from app.schemas import StopResponse

router = APIRouter(prefix="/stops", tags=["stops"])


@router.get("/{stop_id}", response_model=StopResponse)
async def get_stop(
    stop_id: int,
    db: AsyncSession = Depends(get_async_db),
):
    stop_geometry = cast(
        Stop.geom,
        Geometry(geometry_type="POINT", srid=4326),
    )
    result = await db.execute(
        select(
            Stop.id,
            cast(Stop.h3_id, String).label("h3_id"),
            Stop.type,
            Stop.source,
            Stop.external_id,
            ST_Y(stop_geometry).label("lat"),
            ST_X(stop_geometry).label("lng"),
        ).where(Stop.id == stop_id)
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Stop not found")

    return dict(row)