from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.models import Converter, Stop
from app.schemas import StopResponse

router = APIRouter(prefix="/stops", tags=["stops"])

@router.get("/{stop_id}", response_model=StopResponse)
async def get_stop(
        stop_id: int,
        db: AsyncSession = Depends(get_async_db),
):
    result = await db.execute(
        select(
            Stop.id,
            cast(Stop.h3_id, String).label("h3_id"),
            Stop.type,
            Stop.placement_id,
            Stop.source,
            Stop.external_id,
            Converter.placement.label("converter_placement"),
        )
        .outerjoin(Converter, Stop.placement_id == Converter.placement_id)
        .where(Stop.id == stop_id)
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Stop not found")

    data = dict(row)
    placement = data.pop("converter_placement")
    data["converter_ref"] = (
        {
        "placement_id": data["placement_id"],
        "placement": placement,
        }
        if placement is not None
        else None
    )
    return data