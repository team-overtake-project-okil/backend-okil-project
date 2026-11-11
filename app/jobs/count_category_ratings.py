import asyncio
import math
import sys
from geoalchemy2 import Geometry
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.database import async_session_maker
from app.models import HexTable, Point, PointType, Stop, StopRouteConnectable
from app.rounding import round_final

CHUNK_SIZE = 2000

# Референтні координати (Центр Львова для плоскої проєкції в метрах)
REF_LAT = 49.84
METERS_PER_LAT = 111139.0
METERS_PER_LON = 111139.0 * math.cos(math.radians(REF_LAT))

# Кількість найближчих точок одного типу, які враховуємо для об'єктивності
TOP_N_POINTS = 3


def get_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Обчислює відстань у метрах між двома точками."""
    if None in (lat1, lon1, lat2, lon2):
        return float("inf")
    dy = (lat1 - lat2) * METERS_PER_LAT
    dx = (lon1 - lon2) * METERS_PER_LON
    return math.hypot(dx, dy)


def calculate_distance_score(d: float, r1: float, r2: float, r3: float) -> float:
    """Обчислює бали (0..100) залежно від відстані d та радіусів r1, r2, r3."""
    if d is None or not math.isfinite(d):
        return 0.0

    # Радіуси можуть бути нульовими або некоректно впорядкованими в конфігурації.
    r1 = max(0.0, r1 or 0.0)
    r2 = max(r1, r2 or 0.0)
    r3 = max(r2, r3 or 0.0)

    if d <= r1:
        return 100.0
    elif r2 > r1 and d <= r2:
        return 100.0 - 40.0 * ((d - r1) / (r2 - r1))
    elif r3 > r2 and d <= r3:
        return 60.0 - 40.0 * ((d - r2) / (r3 - r2))
    else:
        if r3 == 0:
            return 0.0
        score = 20.0 * math.exp(-(d - r3) / r3)
        return max(0.0, score)


async def main(dry_run: bool = False):
    print("🚀 Запуск розрахунку балів з динамічним розподілом ваг (1 / N)...")

    async with async_session_maker() as session:
        # 1. Завантаження типів POI та згрупування за категоріями
        types_res = await session.execute(select(PointType))
        all_point_types = types_res.scalars().all()

        point_types_by_name = {pt.type: pt for pt in all_point_types}

        categories_map = {}
        for pt in all_point_types:
            if pt.category not in categories_map:
                categories_map[pt.category] = []
            categories_map[pt.category].append(pt.type)

        # 2. Завантаження POI в списки (з кастингом geography -> geometry)
        points_stmt = select(
            Point.type,
            Point.geom.cast(Geometry).ST_Y().label("lat"),
            Point.geom.cast(Geometry).ST_X().label("lon")
        )
        points_res = await session.execute(points_stmt)
        all_points = [{"type": p.type, "lat": p.lat, "lon": p.lon} for p in points_res.all()]

        # 3. Завантаження мапінгу маршрутів (route_id -> set(dest_stop_ids))
        routes_to_stops_stmt = select(StopRouteConnectable.route_id, StopRouteConnectable.stop_id)
        routes_to_stops_res = await session.execute(routes_to_stops_stmt)

        route_destinations = {}
        for r_id, s_id in routes_to_stops_res.all():
            route_destinations.setdefault(r_id, set()).add(s_id)

        # 4. Завантаження зупинок (ORM-об'єкт Stop + розраховані координати lat/lon)
        stops_stmt = select(
            Stop,
            Stop.geom.cast(Geometry).ST_Y().label("lat"),
            Stop.geom.cast(Geometry).ST_X().label("lon")
        ).options(selectinload(Stop.route_links))

        stops_res = await session.execute(stops_stmt)

        all_stops = []
        for stop_obj, lat, lon in stops_res.all():
            route_ids = list({link.route_id for link in stop_obj.route_links}) if stop_obj.route_links else []
            all_stops.append({
                "id": stop_obj.id,
                "lat": lat,
                "lon": lon,
                "route_ids": route_ids
            })

        # 5. Завантаження гексагонів
        hex_stmt = select(
            HexTable.h3_id,
            HexTable.geom.cast(Geometry).ST_Y().label("lat"),
            HexTable.geom.cast(Geometry).ST_X().label("lon")
        )
        hex_res = await session.execute(hex_stmt)
        all_hexagons = [{"h3_id": h.h3_id, "lat": h.lat, "lon": h.lon} for h in hex_res.all()]

        print(f"📊 Дані в пам'яті: POI={len(all_points)}, Зупинок={len(all_stops)}, Гексагонів={len(all_hexagons)}")

        points_by_type = {}
        for p in all_points:
            points_by_type.setdefault(p["type"], []).append(p)

        updates = []

        # 6. Обхід усіх гексагонів
        for h in all_hexagons:
            h_lat, h_lon = h["lat"], h["lon"]
            hex_scores = {"h3_id": h["h3_id"]}

            # Стандартні категорії (health, study, daily, sport, enjoyment, security)
            for category, types_list in categories_map.items():
                if category == "mobility":
                    continue

                num_types = len(types_list)
                if num_types == 0:
                    hex_scores[category] = 0
                    continue

                weight = 1.0 / num_types
                cat_score = 0.0

                for p_type in types_list:
                    pt_config = point_types_by_name.get(p_type)
                    type_points = points_by_type.get(p_type, [])

                    if not pt_config or not type_points:
                        continue

                    min_dist = float("inf")
                    for p in type_points:
                        dist = get_distance_meters(h_lat, h_lon, p["lat"], p["lon"])
                        if dist < min_dist:
                            min_dist = dist

                    score = calculate_distance_score(
                        min_dist,
                        pt_config.radius_of_influence_1 or 300,
                        pt_config.radius_of_influence_2 or 600,
                        pt_config.radius_of_influence_3 or 1200,
                    )
                    cat_score += weight * score

                hex_scores[category] = round_final(cat_score)

            updates.append(hex_scores)

        print(f"✅ Обчислення завершено! Готово оновлень: {len(updates)}")

        if dry_run:
            print("--dry-run: БД не чіпали.")
            return

        # 7. Запис результатів у БД
        print("💾 Запис у БД...")
        for i in range(0, len(updates), CHUNK_SIZE):
            chunk = updates[i: i + CHUNK_SIZE]
            await session.execute(update(HexTable), chunk)
            await session.commit()

    print("🎉 Усі оцінки успішно збережено!")


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))
