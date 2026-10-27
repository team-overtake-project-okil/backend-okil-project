import h3
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.models import Route
from app.route_geojson import build_hex_routes_geojson, hex_boundary_to_wkt
from app.schemas import RouteResponse

router = APIRouter(prefix="/routes", tags=["routes"])

# Маршрути, що мають хоча б одну зупинку в гексагоні (контур гексагона передається як WKT).
# Геометрію порівнюємо як geometry: зупинок ~1000, тому індекс тут не потрібен.
ROUTES_IN_HEX_SQL = text("""
    SELECT DISTINCT r.id, r.name, r.external_id
    FROM stops s
    JOIN stop_route_connectable l ON l.stop_id = s.id
    JOIN routes r ON r.id = l.route_id
    WHERE ST_Intersects(CAST(s.geom AS geometry), ST_GeomFromText(:hex_wkt, 4326))
    ORDER BY r.id
""")

# Лінії цих маршрутів: сегменти по shape, у порядку проходження маршруту.
SEGMENTS_SQL = text("""
    SELECT l.route_id, l.sequence_order, ST_AsGeoJSON(sg.geom, 6) AS geojson
    FROM route_segment_links l
    JOIN route_segments sg ON sg.id = l.segment_id
    WHERE l.route_id = ANY(:route_ids)
    ORDER BY l.route_id, l.sequence_order
""")

# Запасний варіант для маршрутів без сегментів: зупинки по порядку.
STOP_POINTS_SQL = text("""
    SELECT l.route_id, l.stop_order,
           ST_Y(CAST(s.geom AS geometry)) AS lat,
           ST_X(CAST(s.geom AS geometry)) AS lng
    FROM stop_route_connectable l
    JOIN stops s ON s.id = l.stop_id
    WHERE l.route_id = ANY(:route_ids)
    ORDER BY l.route_id, l.stop_order
""")


def _hex_wkt_or_400(h3_id: str) -> str:
    try:
        valid = h3.is_valid_cell(h3_id)
    except Exception:
        valid = False
    if not valid:
        raise HTTPException(status_code=400, detail="Invalid h3_id")
    return hex_boundary_to_wkt(h3.cell_to_boundary(h3_id))


# ВАЖЛИВО: цей ендпоінт має стояти ВИЩЕ за "/{route_id}",
# інакше FastAPI вирішить, що "geojson" це route_id і поверне 422.
@router.get("/geojson")
async def get_hex_routes_geojson(
        h3_id: str = Query(..., description="H3 ID вибраного гексагона"),
        db: AsyncSession = Depends(get_async_db),
):
    """Маршрути із зупинкою в гексагоні, як GeoJSON FeatureCollection (по одній лінії на маршрут)."""
    hex_wkt = _hex_wkt_or_400(h3_id)

    routes = [dict(r) for r in (await db.execute(ROUTES_IN_HEX_SQL, {"hex_wkt": hex_wkt})).mappings().all()]
    if not routes:
        # Гексагон без зупинок: це не помилка, фронт отримає порожній результат.
        return {"type": "FeatureCollection", "features": []}

    route_ids = [r["id"] for r in routes]
    segment_rows = [dict(r) for r in (await db.execute(SEGMENTS_SQL, {"route_ids": route_ids})).mappings().all()]
    fallback_points = [dict(r) for r in (await db.execute(STOP_POINTS_SQL, {"route_ids": route_ids})).mappings().all()]

    return build_hex_routes_geojson(routes, segment_rows, fallback_points)


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
