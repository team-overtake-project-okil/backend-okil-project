from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.models import Route
from app.schemas import RouteResponse

router = APIRouter(prefix="/routes", tags=["routes"])

@router.get("/{route_id}", response_model=RouteResponse)
async def get_route(
        route_id: int,
        db: AsyncSession = Depends(get_async_db),
):
    result = await db.execute(
        select(
            Route.id,
            Route.name,
            Route.source,
            Route.external_id,
            Route.start_stop_id,
            Route.end_stop_id,
        ).where(Route.id == route_id)
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Route not found")

    return dict(row)