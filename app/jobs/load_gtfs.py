import asyncio
import csv
import io
import sys
import zipfile
from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session_maker
from app.models import PointType, Route, Stop, StopRouteConnectable

SOURCE_NAME = "easyway"
POINT_TYPE_NAME = "gtfs_stop"
POINT_TYPE_CATEGORY = "transport"  # TODO: узгодити з командою (у HexTable колонка називається mobility)
CHUNK = 3000  # asyncpg: максимум 32767 параметрів на запит; 3000 рядків * 4 колонки - з запасом


def read_csv_from_zip(zip_file, filename):
    with zip_file.open(filename) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")))


def chunks(items, size=CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_archive(zip_path: str) -> dict:
    """Читає архів і готує дані до запису. БД тут не чіпається."""
    with zipfile.ZipFile(zip_path, "r") as z:
        stops_data = read_csv_from_zip(z, "stops.txt")
        routes_data = read_csv_from_zip(z, "routes.txt")
        trips_data = read_csv_from_zip(z, "trips.txt")
        stop_times_data = read_csv_from_zip(z, "stop_times.txt")

    # --- зупинки: тільки з коректними координатами ---
        # --- зупинки: тільки з коректними координатами та реальною назвою ---
        valid_stops, rejected = [], 0
        for s in stops_data:
            try:
                lat = float(s["stop_lat"])
                lon = float(s["stop_lon"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("coords out of range")

                # Очищаємо назву від пробілів
                name = (s.get("stop_name") or "").strip()

                # Якщо назва порожня або має дефолтний заповнювач — бракуємо запис
                if not name or name.lower() in ("зупинка", "stop", "null", "none"):
                    rejected += 1
                    continue

                valid_stops.append({
                    "external_id": s["stop_id"],
                    "name": name,
                    "geom": f"POINT({lon} {lat})"
                })
            except (ValueError, KeyError):
                rejected += 1

    valid_ids = {s["external_id"] for s in valid_stops}
    stop_name = {s["external_id"]: s["name"] for s in valid_stops}

    # --- маршрути: назва за номером (або довгою назвою) ---
    route_title = {
        r["route_id"]: (
                r.get("route_short_name", "").strip()
                or r.get("route_long_name", "").strip()
                or "Маршрут без назви"
        )
        for r in routes_data
    }

    # --- рейси та їхні зупинки ---
    trip_info = {t["trip_id"]: (t["route_id"], t.get("direction_id") or "0") for t in trips_data}
    trip_stops = defaultdict(list)
    for st in stop_times_data:
        if st["trip_id"] in trip_info:
            trip_stops[st["trip_id"]].append((int(st["stop_sequence"]), st["stop_id"]))

    # еталонний рейс окремо для кожного (маршрут, напрямок)
    best = {}
    for tid, (rid, direction) in trip_info.items():
        n = len(trip_stops.get(tid, []))
        key = (rid, direction)
        if key not in best or n > best[key][0]:
            best[key] = (n, tid)

    routes = []
    for (rid, direction), (_, tid) in sorted(best.items()):
        if rid not in route_title:
            continue
        ids = [sid for _, sid in sorted(trip_stops.get(tid, [])) if sid in valid_ids]
        if not ids:
            continue
        name = f"{route_title[rid]}: {stop_name[ids[0]]} → {stop_name[ids[-1]]}"
        routes.append({
            "external_id": f"{rid}_{direction}",
            "name": name[:255],
            "stop_external_ids": ids
        })

    return {
        "stops": valid_stops,
        "routes": routes,
        "rejected_stops": rejected
    }


async def main(zip_path: str, dry_run: bool = False):
    try:
        data = parse_archive(zip_path)
    except FileNotFoundError:
        print(f"Error: File not found at {zip_path}")
        return
    except (zipfile.BadZipFile, KeyError) as e:
        print(f"Error: некоректний GTFS-архів ({e!r})")
        return

    links_total = sum(len(r["stop_external_ids"]) for r in data["routes"])
    print(f"План: stops={len(data['stops'])} routes={len(data['routes'])} "
          f"links={links_total} відхилено_зупинок={data['rejected_stops']}")

    # ЗАХИСТ: якщо нові дані непридатні, старі не чіпаємо взагалі.
    if not data["stops"] or not data["routes"] or links_total == 0:
        print("Error: у архіві немає придатних зупинок/маршрутів. "
              "Імпорт скасовано, старі дані EasyWay збережено.")
        return

    if dry_run:
        print("--dry-run: БД не чіпав.")
        return

    async with async_session_maker() as session:
        async with session.begin():
            # Очищення старих даних лише конкретного джерела
            await session.execute(
                delete(StopRouteConnectable).where(
                    StopRouteConnectable.route_id.in_(
                        select(Route.id).where(Route.source == SOURCE_NAME)
                    )
                )
            )
            await session.execute(delete(Route).where(Route.source == SOURCE_NAME))
            await session.execute(delete(Stop).where(Stop.source == SOURCE_NAME))

            # Створення типу точки, якщо відсутній
            pt_stmt = (
                pg_insert(PointType)
                .values(type=POINT_TYPE_NAME, category=POINT_TYPE_CATEGORY)
                .on_conflict_do_nothing(index_elements=["type"])
            )
            await session.execute(pt_stmt)

            # Вставка зупинок напряму з геометрією
            stops_to_insert = [
                {
                    "source": SOURCE_NAME,
                    "external_id": s["external_id"],
                    "type": POINT_TYPE_NAME,
                    "geom": s["geom"],
                }
                for s in data["stops"]
            ]
            stop_id_map = {}
            for part in chunks(stops_to_insert):
                res = await session.execute(
                    pg_insert(Stop).values(part).returning(Stop.id, Stop.external_id)
                )
                stop_id_map.update({row.external_id: row.id for row in res})

            # Вставка маршрутів
            routes_to_insert = [
                {
                    "source": SOURCE_NAME,
                    "external_id": r["external_id"],
                    "name": r["name"],
                    "start_stop_id": stop_id_map[r["stop_external_ids"][0]],
                    "end_stop_id": stop_id_map[r["stop_external_ids"][-1]],
                }
                for r in data["routes"]
            ]
            route_id_map = {}
            for part in chunks(routes_to_insert):
                res = await session.execute(
                    pg_insert(Route).values(part).returning(Route.id, Route.external_id)
                )
                route_id_map.update({row.external_id: row.id for row in res})

            # Зв'язки між зупинками та маршрутами
            links_to_insert = [
                {
                    "stop_id": stop_id_map[ext],
                    "route_id": route_id_map[r["external_id"]],
                    "stop_order": order,
                }
                for r in data["routes"]
                for order, ext in enumerate(r["stop_external_ids"], start=1)
            ]
            for part in chunks(links_to_insert):
                await session.execute(pg_insert(StopRouteConnectable).values(part))

    print(f"GTFS data successfully loaded: stops={len(stop_id_map)} "
          f"routes={len(route_id_map)} links={len(links_to_insert)}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    archive_path = args[0] if args else "data/static.zip"
    asyncio.run(main(archive_path, dry_run="--dry-run" in sys.argv))