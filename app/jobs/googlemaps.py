import asyncio

import sys
import h3
import requests
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.database import async_session_maker
from app.models import  Point, PointType

SOURCE_NAME = "google_maps"
POINT_TYPE_NAME = "pharmacy"
POINT_TYPE_CATEGORY = "health"
CHUNK = 3000


def chunks(items, size=CHUNK):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def fetch_pharmacies() -> list[dict]:
    """1. Чистий збір даних із SerpAPI в пам'ять (без БД)."""
    serp_keys = [
        k.strip() for k in settings.SERPAPI_KEYS.split(",") if k.strip()
    ]
    if not serp_keys:
        raise ValueError("Не знайдено жодного SerpAPI ключа у SERPAPI_KEYS!")

    LAT_START, LAT_END = 49.830, 49.848
    LON_START, LON_END = 24.015, 24.040
    STEP = 0.009
    H3_RESOLUTION = 9

    current_key_idx = 0
    pharmacies_map = {}

    lat = LAT_START
    while lat <= LAT_END:
        lon = LON_START
        while lon <= LON_END:
            start = 0
            while True:
                if current_key_idx >= len(serp_keys):
                    print("❌ Усі API ключі вичерпано!")
                    break

                active_api_key = serp_keys[current_key_idx]

                params = {
                    "engine": "google_maps",
                    "q": "аптека",
                    "lat": str(lat),
                    "lon": str(lon),
                    "z": "16",
                    "hl": "uk",
                    "gl": "ua",
                    "start": start,
                    "api_key": active_api_key,
                }

                response = requests.get(
                    "https://serpapi.com/search", params=params
                ).json()

                if "error" in response:
                    print(
                        f"⚠️ Помилка ключа {active_api_key[:8]}... Перемикаємось на наступний."
                    )
                    current_key_idx += 1
                    continue

                results = response.get("local_results", [])
                if not results:
                    break

                for place in results:
                    place_id = place.get("place_id")
                    p_lat = place.get("gps_coordinates", {}).get("latitude")
                    p_lon = place.get("gps_coordinates", {}).get("longitude")
                    address = (
                        place.get("address", "").replace("\n", " ").strip()
                    )

                    if place_id and p_lat and p_lon and address:
                        if place_id not in pharmacies_map:
                            h3_id = h3.latlng_to_cell(
                                p_lat, p_lon, H3_RESOLUTION
                            )
                            pharmacies_map[place_id] = {
                                "external_id": place_id,
                                "name": place.get("title", ""),
                                "address": address[:255],
                                "geom": f"POINT({p_lon} {p_lat})",
                                "h3_id": h3_id,
                            }

                if len(results) < 20 or start >= 40:
                    break

                start += 20

            if current_key_idx >= len(serp_keys):
                break

            lon += STEP

        if current_key_idx >= len(serp_keys):
            break

        lat += STEP

    return list(pharmacies_map.values())


async def main(dry_run: bool = False):
    pharmacies = fetch_pharmacies()
    print(f"План: зібрано {len(pharmacies)} унікальних аптек.")

    if not pharmacies:
        print("Нічого зберігати.")
        return

    if dry_run:
        print("--dry-run: БД не чіпали.")
        return

    # 2. Запис у БД в одній асинхронній транзакції
    async with async_session_maker() as session:
        async with session.begin():
            # Гарантуємо наявність типу точки
            pt_stmt = (
                pg_insert(PointType)
                .values(type=POINT_TYPE_NAME, category=POINT_TYPE_CATEGORY)
                .on_conflict_do_nothing(index_elements=["type"])
            )
            await session.execute(pt_stmt)



            # Вставляємо Points пакетами зі збереженням зв'язку placement_id
            points_to_insert = [
                {
                    "source": SOURCE_NAME,
                    "external_id": p["external_id"],
                    "name": p["name"],
                    "type": POINT_TYPE_NAME,
                    "h3_id": p["h3_id"],
                    "geom": p["geom"],
                }
                for p in pharmacies
            ]

            # 3. ON CONFLICT DO UPDATE
            for part in chunks(points_to_insert):
                stmt = pg_insert(Point).values(part)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_points_source_external_id",
                    set_={
                        "name": stmt.excluded.name,
                        "type": stmt.excluded.type,
                        "h3_id": stmt.excluded.h3_id,
                        "geom": stmt.excluded.geom,
                    },
                )
                await session.execute(stmt)

    print(f"Успішно імпортовано {len(pharmacies)} аптек в БД!")


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))