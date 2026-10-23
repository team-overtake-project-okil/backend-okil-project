from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.hexagon_repository import fetch_hexagons
from app.schemas import HexTableResponse


router = APIRouter(prefix="/hexagons", tags=["hexagons"])

@router.get("", response_model=list[HexTableResponse])
async def get_hexagons(
        db: AsyncSession = Depends(get_async_db),
):
    return await fetch_hexagons(db)