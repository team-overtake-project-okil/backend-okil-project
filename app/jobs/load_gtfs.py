import asyncio
import csv
import io
import sys
import zipfile
from collections import defaultdict
import h3

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session_maker
from app.models import HexTable, PointType, Route, Stop, StopRouteConnectable

H3_RESOLUTION = 9
SOURCE_NAME = "easyway"
POINT_TYPE_NAME = "gtfs_stop"
POINT_TYPE_CATEGORY = "mobility"
CHUNK = 2000  # Безпечний розмір пакету для asyncpg


def read_csv_from_zip(zip_file, filename):
    with zip_file.open(filename) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")))


def chunks(items, size=CHUNK):
    for i in range(0, len(items), size):
        yield items[i: i + size]


async def ensure_hexagons_exist(session, h3_ids: set[str]):
    """
    Знаходить відсутні h3_id у hex_table та автоматично створює їх.
    Існуючі гексагони не перезаписуються (ON CONFLICT DO NOTHING).
    """
    if not h3_ids:
        return

    hexagons_to_insert = []
    for h_id in h3_ids:
        if not h_id:
            continue
        try:
            lat, lng = h3.cell_to_latlng(h_id)
        except AttributeError:
            lat, lng = h3.h3_to_geo(h_id)

        hexagons_to_insert.append({
            "h3_id": h_id,
            "geom": f"POINT({lng} {lat})",
            "health": 0,
            "study": 0,
            "daily": 0,
            "mobility": 0,
            "sport": 0,
            "enjoyment": 0,
            "food": 0, 
            "security": 0,
        })

    for part in chunks(hexagons_to_insert):
        stmt = (
            pg_insert(HexTable)
            .values(part)
            .on_conflict_do_nothing(index_elements=["h3_id"])
        )
        await session.execute(stmt)


def parse_archive(zip_path: str) -> dict:
    """Читає GTFS-архів і готує дані до запису."""
    with zipfile.ZipFile(zip_path, "r") as z:
        stops_data = read_csv_from_zip(z, "stops.txt")
        routes_data = read_csv_from_zip(z, "routes.txt")
        trips_data = read_csv_from_zip(z, "trips.txt")
        stop_times_data = read_csv_from_zip(z, "stop_times.txt")

    valid_stops, rejected = [], 0
    for s in stops_data:
        try:
            lat = float(s["stop_lat"])
            lon = float(s["stop_lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("coords out of range")

            name = (s.get("stop_name") or "").strip()
            if not name or name.lower() in ("зупинка", "stop", "null", "none"):
                rejected += 1
                continue

            try:
                h3_id = h3.latlng_to_cell(lat, lon, H3_RESOLUTION)
            except AttributeError:
                h3_id = h3.geo_to_h3(lat, lon, H3_RESOLUTION)

            valid_stops.append({
                "external_id": s["stop_id"],
                "name": name,
                "geom": f"POINT({lon} {lat})",
                "h3_id": h3_id,
            })
        except (ValueError, KeyError):
            rejected += 1

    valid_ids = {s["external_id"] for s in valid_stops}
    stop_name = {s["external_id"]: s["name"] for s in valid_stops}

    route_title = {
        r["route_id"]: (
                r.get("route_short_name", "").strip()
                or r.get("route_long_name", "").strip()
                or "Маршрут без назви"
        )
        for r in routes_data
    }

    trip_info = {t["trip_id"]: (t["route_id"], t.get("direction_id") or "0") for t in trips_data}
    trip_stops = defaultdict(list)
    for st in stop_times_data:
        if st["trip_id"] in trip_info:
            trip_stops[st["trip_id"]].append((int(st["stop_sequence"]), st["stop_id"]))

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

    if not data["stops"] or not data["routes"] or links_total == 0:
        print("Error: у архіві немає придатних зупинок/маршрутів.")
        return

    if dry_run:
        print("--dry-run: БД не чіпав.")
        return

    async with async_session_maker() as session:
        async with session.begin():
            # 1. Автоматично гарантуємо наявність ВСІХ гексагонів зупинок у hex_table
            all_h3_ids = {s["h3_id"] for s in data["stops"] if s["h3_id"]}
            await ensure_hexagons_exist(session, all_h3_ids)

            # 2. Очищення старих даних EasyWay
            await session.execute(
                delete(StopRouteConnectable).where(
                    StopRouteConnectable.route_id.in_(
                        select(Route.id).where(Route.source == SOURCE_NAME)
                    )
                )
            )
            await session.execute(delete(Route).where(Route.source == SOURCE_NAME))
            await session.execute(delete(Stop).where(Stop.source == SOURCE_NAME))

            # 3. Створення типу точки, якщо відсутній
            pt_stmt = (
                pg_insert(PointType)
                .values(type=POINT_TYPE_NAME, category=POINT_TYPE_CATEGORY)
                .on_conflict_do_nothing(index_elements=["type"])
            )
            await session.execute(pt_stmt)

            # 4. Вставка зупинок
            stops_to_insert = [
                {
                    "source": SOURCE_NAME,
                    "external_id": s["external_id"],
                    "type": POINT_TYPE_NAME,
                    "geom": s["geom"],
                    "h3_id": s["h3_id"],
                }
                for s in data["stops"]
            ]

            stop_id_map = {}
            for part in chunks(stops_to_insert):
                res = await session.execute(
                    pg_insert(Stop).values(part).returning(Stop.id, Stop.external_id)
                )
                stop_id_map.update({row.external_id: row.id for row in res})

            # 5. Вставка маршрутів
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

            # 6. Зв'язки між зупинками та маршрутами
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