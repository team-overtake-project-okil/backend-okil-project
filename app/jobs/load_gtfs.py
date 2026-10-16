import asyncio
import csv
import io
import sys
import zipfile
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session_maker
from app.models import Converter, PointType, Route, Stop, StopRouteConnectable

SOURCE_NAME = "easyway"
POINT_TYPE_NAME = "gtfs_stop"


def read_csv_from_zip(zip_file, filename):
    with zip_file.open(filename) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))


async def main(zip_path: str):
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            stops_data = read_csv_from_zip(z, "stops.txt")
            routes_data = read_csv_from_zip(z, "routes.txt")
            trips_data = read_csv_from_zip(z, "trips.txt")
            stop_times_data = read_csv_from_zip(z, "stop_times.txt")
    except FileNotFoundError:
        print(f"Error: File not found at {zip_path}")
        return

    routes_to_insert = [
        {
            "source": SOURCE_NAME,
            "external_id": r["route_id"],
            "name": (
                r.get("route_short_name", "").strip()
                or r.get("route_long_name", "").strip()
                or "Маршрут без назви"
            )[:255],
        }
        for r in routes_data
    ]

    converters_to_insert = []
    valid_stops_raw = []

    for s in stops_data:
        try:
            lat = float(s["stop_lat"])
            lon = float(s["stop_lon"])
            name = (s.get("stop_name") or "Зупинка").strip()[:255]
            converters_to_insert.append(
                {"placement": name, "geom": f"POINT({lon} {lat})"}
            )
            valid_stops_raw.append(s)
        except (ValueError, KeyError):
            continue

    trip_to_route = {t["trip_id"]: t["route_id"] for t in trips_data}

    trip_stops = {}
    for st in stop_times_data:
        tid = st["trip_id"]
        if tid not in trip_stops:
            trip_stops[tid] = []
        trip_stops[tid].append(
            {"stop_id": st["stop_id"], "sequence": int(st["stop_sequence"])}
        )

    route_best_trip = {}
    for tid, rid in trip_to_route.items():
        stops_count = len(trip_stops.get(tid, []))
        if rid not in route_best_trip or stops_count > route_best_trip[rid][0]:
            route_best_trip[rid] = (stops_count, tid)

    async with async_session_maker() as session:
        async with session.begin():
            # Очищення таблиць і скидання автоінкременту ID назад у 1
            await session.execute(
                text(
                    "TRUNCATE TABLE stop_route_connectable, routes, stops, converter RESTART IDENTITY CASCADE;"
                )
            )

            pt_stmt = (
                pg_insert(PointType)
                .values(type=POINT_TYPE_NAME, category="transport")
                .on_conflict_do_nothing(index_elements=["type"])
            )
            await session.execute(pt_stmt)

            conv_stmt = (
                pg_insert(Converter)
                .values(converters_to_insert)
                .returning(Converter.placement_id)
            )
            conv_res = await session.execute(conv_stmt)
            inserted_placement_ids = [row.placement_id for row in conv_res]

            stops_to_insert = [
                {
                    "source": SOURCE_NAME,
                    "external_id": s_raw["stop_id"],
                    "type": POINT_TYPE_NAME,
                    "placement_id": placement_id,
                }
                for s_raw, placement_id in zip(
                    valid_stops_raw, inserted_placement_ids
                )
            ]

            stop_stmt = (
                pg_insert(Stop)
                .values(stops_to_insert)
                .returning(Stop.id, Stop.external_id)
            )
            stop_res = await session.execute(stop_stmt)
            stop_id_map = {row.external_id: row.id for row in stop_res}

            route_stmt = (
                pg_insert(Route)
                .values(routes_to_insert)
                .returning(Route.id, Route.external_id)
            )
            route_res = await session.execute(route_stmt)
            route_id_map = {row.external_id: row.id for row in route_res}

            links_to_insert = []
            for rid, (_, tid) in route_best_trip.items():
                if rid not in route_id_map:
                    continue
                db_route_id = route_id_map[rid]
                sorted_stops = sorted(
                    trip_stops.get(tid, []), key=lambda x: x["sequence"]
                )

                for order, st in enumerate(sorted_stops, start=1):
                    ext_stop_id = st["stop_id"]
                    if ext_stop_id in stop_id_map:
                        links_to_insert.append(
                            {
                                "stop_id": stop_id_map[ext_stop_id],
                                "route_id": db_route_id,
                                "stop_order": order,
                            }
                        )

            if links_to_insert:
                await session.execute(
                    pg_insert(StopRouteConnectable).values(links_to_insert)
                )

    print("GTFS data successfully loaded.")


if __name__ == "__main__":
    archive_path = sys.argv[1] if len(sys.argv) > 1 else "data/static.zip"
    asyncio.run(main(archive_path))