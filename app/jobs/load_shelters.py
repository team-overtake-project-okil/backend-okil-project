import asyncio
import os
import sys
import pandas as pd
import h3
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session_maker
from app.models import HexTable, Point

SOURCE_NAME = "lviv_city_council"
H3_RESOLUTION = 9
CHUNK = 2000


def chunks(items, size=CHUNK):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def load_shelter_data(simplest_path: str, protective_path: str) -> list[dict]:
    points = []

    # 1. Найпростіші укриття (xls)
    if os.path.exists(simplest_path):
        df_simplest = pd.read_excel(simplest_path)
        for idx, row in df_simplest.iterrows():
            lon = float(row['x'])
            lat = float(row['y'])
            if pd.isna(lat) or pd.isna(lon):
                continue

            obj = str(row.get('object', 'Найпростіше укриття')).strip()
            street = str(row.get('street_name', '')).strip()
            house = str(row.get('housenumber', '')).strip()
            name = f"{obj}, вул. {street} {house}".strip()

            try:
                h3_id = h3.latlng_to_cell(lat, lon, H3_RESOLUTION)
            except AttributeError:
                h3_id = h3.geo_to_h3(lat, lon, H3_RESOLUTION)

            points.append({
                "external_id": f"simplest_{idx}",
                "name": name[:255],
                "type": "shelter",
                "h3_id": h3_id,
                "geom": f"POINT({lon} {lat})",
            })

    # 2. Захисні споруди (xls)
    if os.path.exists(protective_path):
        df_protective = pd.read_excel(protective_path)
        for idx, row in df_protective.iterrows():
            lon = float(row['longitude'])
            lat = float(row['latitude'])
            if pd.isna(lat) or pd.isna(lon):
                continue

            obj_type = str(row.get('object_type', '')).strip().lower()
            status = str(row.get('status', '')).strip().lower()

            # Фільтр: відсіюємо протирадіаційні укриття
            if "радіаційн" in obj_type:
                continue

            # Фільтр: лише "готове" та "рекомендоване як найпростіше..."
            allowed_statuses = [
                "готове",
                "рекомендоване як найпростіше укриття для населення",
            ]
            if status not in allowed_statuses:
                continue

            obj_name = str(row.get('object_type', 'Сховище')).strip()
            street = str(row.get('street_name', '')).strip()
            house = str(row.get('housenumber', '')).strip()
            name = f"{obj_name}, вул. {street} {house}".strip()

            try:
                h3_id = h3.latlng_to_cell(lat, lon, H3_RESOLUTION)
            except AttributeError:
                h3_id = h3.geo_to_h3(lat, lon, H3_RESOLUTION)

            points.append({
                "external_id": f"protective_{idx}",
                "name": name[:255],
                "type": "shelter",
                "h3_id": h3_id,
                "geom": f"POINT({lon} {lat})",
            })

    return points


async def main(dry_run: bool = False):
    simplest_path = "data/Sources/2. Найпростіші укриття.xls" if os.path.exists("data/Sources/2. Найпростіші укриття.xls") else "2. Найпростіші укриття.xls"
    protective_path = "data/Sources/3. Захисні споруди.xls" if os.path.exists("data/Sources/3. Захисні споруди.xls") else "3. Захисні споруди.xls"

    points = load_shelter_data(simplest_path, protective_path)
    print(f"📦 Відфільтровано точок для імпорту: {len(points)}")

    if not points:
        print("❌ Немає даних для запису.")
        return

    if dry_run:
        print("🧪 --dry-run: Дані не було збережено в БД.")
        return

    async with async_session_maker() as session:
        # 1. Завантажуємо існуючі гексагони
        hex_res = await session.execute(select(HexTable.h3_id))
        existing_h3_ids = set(hex_res.scalars().all())
        print(f"🗺️ Існуючих гексагонів у БД: {len(existing_h3_ids)}")

        # 2. Фільтруємо точки під сітку
        valid_points = [p for p in points if p["h3_id"] in existing_h3_ids]
        skipped_count = len(points) - len(valid_points)
        print(f"🎯 Точок для запису: {len(valid_points)} (пропущено поза сіткою: {skipped_count})")

        # 3. Дописуємо точки у `points` без створення нової транзакції з нуля
        points_to_insert = [
            {
                "source": SOURCE_NAME,
                "external_id": p["external_id"],
                "name": p["name"],
                "type": p["type"],
                "h3_id": p["h3_id"],
                "geom": p["geom"],
            }
            for p in valid_points
        ]

        inserted_count = 0
        for part in chunks(points_to_insert):
            stmt = (
                pg_insert(Point)
                .values(part)
                .on_conflict_do_nothing(constraint="uq_points_source_external_id")
            )
            res = await session.execute(stmt)
            inserted_count += res.rowcount or 0

        await session.commit()

    print(f"✅ Успішно додано нових точок `shelter` у таблицю `points`: {inserted_count}!")


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))