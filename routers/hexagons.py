from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession
import math

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

@router.get("/{h3_id}")
async def get_hexagon(
    h3_id: str,
    weight_health: float = Query(1.0, ge=0.0, le=5.0),
    weight_study: float = Query(1.0, ge=0.0, le=5.0),
    weight_daily: float = Query(1.0, ge=0.0, le=5.0),
    weight_mobility: float = Query(1.0, ge=0.0, le=5.0),
    weight_sport: float = Query(1.0, ge=0.0, le=5.0),
    weight_enjoyment: float = Query(1.0, ge=0.0, le=5.0),
    weight_security: float = Query(1.0, ge=0.0, le=5.0),
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

    data = dict(row)

    #Задаємо критичні пороги (w_crit) для кожної категорії
    critical_thresholds = {
        "health": 1.0,     # Дуже важливо
        "security": 1.0,   # Дуже важливо
        "daily": 0.5,      # Важливо (магазини)
        "mobility": 0.5,   # Важливо (транспорт)
        "study": 0.0,      # Не критично для всіх
        "sport": 0.0,      # Не критично
        "enjoyment": 0.0,  # Не критично
    }

    user_weights = {
        "health": weight_health,
        "study": weight_study,
        "daily": weight_daily,
        "mobility": weight_mobility,
        "sport": weight_sport,
        "enjoyment": weight_enjoyment,
        "security": weight_security,
    }

    #Метод "Мінімальний поріг" (Wi = max(w_crit, w_user))
    weights = {}
    for category in user_weights:
        weights[category] = max(critical_thresholds[category], user_weights[category])
    
    #Нормалізація ваг
    total_weight = sum(weights.values())
    if total_weight == 0:
        normalized_weights = {k: 1.0 / len(weights) for k in weights}
    else:
        normalized_weights = {k: v / total_weight for k, v in weights.items()}

    #Мультиплікативна формула
    overall_rating = 1.0
    for category in weights.keys():
        score = data.get(category) or 0.0
        score_safe = max(1.0, float(score)) 
        overall_rating *= math.pow(score_safe, normalized_weights[category])

    data["overall_rating"] = min(100.0, round(overall_rating, 2))

    return data