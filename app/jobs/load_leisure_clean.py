import asyncio, sys
from math import hypot
import h3
from geoalchemy2 import Geometry
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.database import async_session_maker
from app.jobs.leisure_ids import CLEAN_PLACE_IDS
from app.jobs.parse_leisure_google import SOURCE, RES, TYPES, cache, km_from_center
from app.models import HexTable, Point
from app.jobs.googlemaps import LVIV_GEOJSON_POLYGON


def get_lviv_polygon_cells(res: int = RES) -> set[str]:
    coords = LVIV_GEOJSON_POLYGON["coordinates"][0]
    latlng_ring = [(lat, lon) for lon, lat in coords]
    poly = h3.LatLngPoly(latlng_ring)
    return set(h3.polygon_to_cells(poly, res))


# Гексагони без жодної зупинки й без жодної точки, крім наших трьох типів: майже напевно їх додав
# перший, незафільтрований збір (googlemaps.py додає в hex_table гекси під кожну зібрану точку).
STRAY_SQL = text("""
    SELECT h.h3_id::text
    FROM hex_table h
    WHERE NOT EXISTS (SELECT 1 FROM stops s WHERE s.h3_id = h.h3_id)
      AND NOT EXISTS (SELECT 1 FROM points p WHERE p.h3_id = h.h3_id
                      AND NOT (p.source = :src AND p.type = ANY(:types)))
""")


def build_points():
    """Точки за списком place_id (координати й назви беруться з кешу SerpApi)."""
    recs = {}
    for res in cache.values():
        for p in res:
            if p["place_id"] and p["lat"] and p["lon"]:
                recs.setdefault(p["place_id"], p)
    points, missing, seen = [], [], {}
    for t, ids in CLEAN_PLACE_IDS.items():
        for pid in ids:
            if pid in seen:
                if seen[pid] != t:
                    print(f"⚠ {pid} є і в «{seen[pid]}», і в «{t}»: беру перший")
                continue
            seen[pid] = t
            p = recs.get(pid)
            if not p:
                missing.append((t, pid))
                continue
            points.append({
                "source": SOURCE, "external_id": pid, "name": p["title"][:255], "type": t,
                "h3_id": h3.latlng_to_cell(p["lat"], p["lon"], RES), "geom": f'POINT({p["lon"]} {p["lat"]})',
                "_lat": p["lat"], "_lon": p["lon"], "_addr": p["address"],
            })
    return points, missing


def classify(points, grid, stray):
    """grid: {h3_id: (lat, lon)}. «Справжня» сітка = без гексагонів, доданих першим збором."""
    core = {h: c for h, c in grid.items() if h not in stray}
    for p in points:
        p["_in"] = p["h3_id"] in core
        p["_stray"] = p["h3_id"] in stray
        p["_gap"] = 0.0 if p["_in"] else min(
            (hypot((p["_lat"] - la) * 111.1, (p["_lon"] - lo) * 71.5) for la, lo in core.values()), default=0.0)
    return core


def print_report(points, missing, grid, stray):
    print(f"\n— Гексагонів у hex_table: {len(grid)}; з них, імовірно доданих першим збором (без зупинок і інших точок): {len(stray)}")
    for h in sorted(stray):
        la, lo = grid[h]
        print(f"   {h}  {la:.4f}, {lo:.4f}  ({km_from_center(la, lo):.1f} км від центру)")
    if missing:
        print(f"\n— ID, яких немає в кеші leisure_raw.json: {len(missing)}")
        for t, pid in missing:
            print(f"   {t:7} {pid}")
    out = sorted((p for p in points if not p["_in"]), key=lambda p: p["_gap"])
    print(f"\n— Поза покриттям: {len(out)} (до сітки найближчі перші; один гексагон ≈ 0.3 км)")
    for p in out:
        mark = " [лише в доданому гексагоні]" if p["_stray"] else ""
        print(f'   {p["type"]:7} | {p["name"][:36]:36} | {km_from_center(p["_lat"], p["_lon"]):4.1f} км від центру | '
              f'до сітки {p["_gap"]:.1f} км{mark} | {p["_addr"][:38]}')
    print("\n— У покритті / усього:")
    for t in CLEAN_PLACE_IDS:
        tp = [p for p in points if p["type"] == t]
        print(f"   {t:7} {sum(p['_in'] for p in tp)} / {len(tp)}")


async def main(dry_run=False):
    points, missing = build_points()
    async with async_session_maker() as session, session.begin():
        rows = (await session.execute(select(
            HexTable.h3_id, HexTable.geom.cast(Geometry).ST_Y(), HexTable.geom.cast(Geometry).ST_X()))).all()
        grid = {r[0]: (r[1], r[2]) for r in rows}
        stray = set((await session.execute(STRAY_SQL, {"src": SOURCE, "types": TYPES})).scalars())
        classify(points, grid, stray)
        print_report(points, missing, grid, stray)
        if dry_run:
            print("\n🧪 dry-run: в БД нічого не записано.")
            return

        valid = [{k: v for k, v in p.items() if not k.startswith("_")} for p in points if p["_in"]]
        cond = (Point.source == SOURCE) & Point.type.in_(TYPES)
        n_old = (await session.execute(select(func.count()).select_from(Point).where(cond))).scalar()
        print(f"\n🗑 Видаляю старі точки {TYPES} з джерела {SOURCE}: {n_old}")
        await session.execute(delete(Point).where(cond))

        # Видаляємо тільки сміттєві гексагони (без зупинок та інших точок), що лежать поза полігоном Львова
        lviv_cells = get_lviv_polygon_cells(RES)
        stray_out_of_bounds = stray - lviv_cells
        if stray_out_of_bounds:
            print(f"🗑 Видаляю {len(stray_out_of_bounds)} позаполігонних сміттєвих гексагонів із hex_table...")
            await session.execute(delete(HexTable).where(HexTable.h3_id.in_(stray_out_of_bounds)))

        if valid:
            stmt = pg_insert(Point).values(valid)
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_points_source_external_id",
                    set_={
                        "type": stmt.excluded.type,
                        "name": stmt.excluded.name,
                        "h3_id": stmt.excluded.h3_id,
                        "geom": stmt.excluded.geom,
                    },
                )
            )
        print(f"✅ Записано: {len(valid)}")


if __name__ == "__main__":
    asyncio.run(main("--dry-run" in sys.argv))