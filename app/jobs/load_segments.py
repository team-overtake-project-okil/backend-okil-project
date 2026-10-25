"""
Заповнює ТІЛЬКИ route_segments і route_segment_links.

Запускати ПІСЛЯ load_gtfs.py: скрипт бере зупинки й маршрути, які вже лежать у БД
(source = easyway), і будує для них стиснуті сегменти за shapes.txt.

    python -m app.scripts.load_segments data/static.zip [--dry-run]
"""
import asyncio
import csv
import io
import sys
import zipfile
from collections import defaultdict

from shapely.geometry import LineString, Point
from shapely.ops import substring
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session_maker
from app.models import Route, RouteSegment, RouteSegmentLink, Stop

SOURCE_NAME = "easyway"
CHUNK = 3000  # asyncpg: максимум 32767 параметрів на запит


def read_csv_from_zip(zip_file, filename, optional=False):
    try:
        f = zip_file.open(filename)
    except KeyError:
        if optional:
            return []
        raise
    with f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")))


def chunks(items, size=CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def build_shape_lines(shapes_data, needed_ids):
    """shape_id -> LineString (lon, lat). Беремо лише потрібні shape."""
    pts = defaultdict(list)
    for r in shapes_data:
        sid = r.get("shape_id")
        if sid not in needed_ids:
            continue
        try:
            pts[sid].append((
                int(r["shape_pt_sequence"]),
                float(r["shape_pt_lon"]),
                float(r["shape_pt_lat"]),
            ))
        except (ValueError, KeyError):
            continue

    lines = {}
    for sid, points in pts.items():
        points.sort()
        coords = [(lon, lat) for _, lon, lat in points]
        if len(coords) > 1:
            lines[sid] = LineString(coords)
    return lines


def make_segment(p_start: Point, p_end: Point, line):
    """Ділянка shape між двома зупинками; якщо не вийшло - пряма лінія."""
    if line is not None:
        d1 = line.project(p_start)
        d2 = line.project(p_end)
        if d2 > d1:
            seg = substring(line, d1, d2)
            if seg.geom_type == "LineString" and len(seg.coords) >= 2:
                return seg
    return LineString([(p_start.x, p_start.y), (p_end.x, p_end.y)])


def parse_archive(zip_path: str, known_stops: set, known_routes: set) -> dict:
    """
    known_stops  - GTFS stop_id, які є в БД
    known_routes - "{route_id}_{direction}", які є в БД
    БД тут не чіпається.
    """
    with zipfile.ZipFile(zip_path, "r") as z:
        stops_data = read_csv_from_zip(z, "stops.txt")
        trips_data = read_csv_from_zip(z, "trips.txt")
        stop_times_data = read_csv_from_zip(z, "stop_times.txt")
        shapes_data = read_csv_from_zip(z, "shapes.txt", optional=True)

    stop_point = {}
    for s in stops_data:
        if s["stop_id"] in known_stops:
            try:
                stop_point[s["stop_id"]] = Point(float(s["stop_lon"]), float(s["stop_lat"]))
            except (ValueError, KeyError):
                pass

    trip_info = {
        t["trip_id"]: (t["route_id"], t.get("direction_id") or "0", t.get("shape_id") or None)
        for t in trips_data
    }
    trip_stops = defaultdict(list)
    for st in stop_times_data:
        if st["trip_id"] in trip_info:
            trip_stops[st["trip_id"]].append((int(st["stop_sequence"]), st["stop_id"]))

    # той самий еталонний рейс, що й у load_gtfs.py (найбільше зупинок для маршруту+напрямку)
    best = {}
    for tid, (rid, direction, _) in trip_info.items():
        n = len(trip_stops.get(tid, []))
        key = (rid, direction)
        if key not in best or n > best[key][0]:
            best[key] = (n, tid)

    shape_lines = build_shape_lines(
        shapes_data,
        {trip_info[tid][2] for _, tid in best.values() if trip_info[tid][2]},
    )

    segments = {}       # (start_ext, end_ext) -> WKT (однакові ділянки стиснуті в одну)
    segment_links = []  # (route_ext_id, start_ext, end_ext, sequence_order)
    straight_fallbacks = 0

    for (rid, direction), (_, tid) in sorted(best.items()):
        route_ext_id = f"{rid}_{direction}"
        if route_ext_id not in known_routes:
            continue

        ids = [sid for _, sid in sorted(trip_stops.get(tid, [])) if sid in stop_point]
        line = shape_lines.get(trip_info[tid][2])

        for i in range(len(ids) - 1):
            a, b = ids[i], ids[i + 1]
            if a == b:
                continue
            if (a, b) not in segments:
                if line is None:
                    straight_fallbacks += 1
                segments[(a, b)] = make_segment(stop_point[a], stop_point[b], line).wkt
            segment_links.append((route_ext_id, a, b, i + 1))

    return {
        "segments": segments,
        "segment_links": segment_links,
        "straight_fallbacks": straight_fallbacks,
    }


async def main(zip_path: str, dry_run: bool = False):
    # 1. Що вже є в БД (після load_gtfs.py)
    async with async_session_maker() as session:
        stop_rows = (await session.execute(
            select(Stop.id, Stop.external_id).where(Stop.source == SOURCE_NAME)
        )).all()
        route_rows = (await session.execute(
            select(Route.id, Route.external_id).where(Route.source == SOURCE_NAME)
        )).all()

    stop_id_map = {ext: db_id for db_id, ext in stop_rows}
    route_id_map = {ext: db_id for db_id, ext in route_rows}

    if not stop_id_map or not route_id_map:
        print("Error: у БД немає зупинок/маршрутів EasyWay. Спочатку запустіть load_gtfs.py.")
        return

    # 2. Парсинг архіву
    try:
        data = parse_archive(zip_path, set(stop_id_map), set(route_id_map))
    except FileNotFoundError:
        print(f"Error: File not found at {zip_path}")
        return
    except (zipfile.BadZipFile, KeyError) as e:
        print(f"Error: некоректний GTFS-архів ({e!r})")
        return

    print(f"План: segments={len(data['segments'])} "
          f"segment_links={len(data['segment_links'])} "
          f"прямих_ліній_без_shape={data['straight_fallbacks']}")

    # ЗАХИСТ: якщо сегментів немає, старі не чіпаємо.
    if not data["segments"] or not data["segment_links"]:
        print("Error: не вдалося побудувати сегменти. Імпорт скасовано, старі дані збережено.")
        return

    if dry_run:
        print("--dry-run: БД не чіпав.")
        return

    # 3. Запис
    async with async_session_maker() as session:
        async with session.begin():
            source_routes = select(Route.id).where(Route.source == SOURCE_NAME)
            source_stops = select(Stop.id).where(Stop.source == SOURCE_NAME)

            await session.execute(
                delete(RouteSegmentLink).where(RouteSegmentLink.route_id.in_(source_routes))
            )
            await session.execute(
                delete(RouteSegment).where(
                    RouteSegment.start_stop_id.in_(source_stops)
                    | RouteSegment.end_stop_id.in_(source_stops)
                )
            )

            segments_to_insert = [
                {
                    "start_stop_id": stop_id_map[a],
                    "end_stop_id": stop_id_map[b],
                    "geom": f"SRID=4326;{wkt}",
                }
                for (a, b), wkt in data["segments"].items()
            ]
            segment_id_map = {}
            for part in chunks(segments_to_insert):
                res = await session.execute(
                    pg_insert(RouteSegment).values(part).returning(
                        RouteSegment.id, RouteSegment.start_stop_id, RouteSegment.end_stop_id
                    )
                )
                segment_id_map.update({(r.start_stop_id, r.end_stop_id): r.id for r in res})

            links_to_insert = [
                {
                    "segment_id": segment_id_map[(stop_id_map[a], stop_id_map[b])],
                    "route_id": route_id_map[route_ext],
                    "sequence_order": order,
                }
                for route_ext, a, b, order in data["segment_links"]
            ]
            for part in chunks(links_to_insert):
                await session.execute(pg_insert(RouteSegmentLink).values(part))

    print(f"Segments successfully loaded: segments={len(segment_id_map)} "
          f"segment_links={len(links_to_insert)}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    archive_path = args[0] if args else "data/static.zip"
    asyncio.run(main(archive_path, dry_run="--dry-run" in sys.argv))
