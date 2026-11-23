import argparse, asyncio, json, os, re, time
import socket
import h3, pandas as pd, requests
import urllib3.util.connection
from dotenv import load_dotenv
from shapely.geometry import Polygon, shape
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.database import async_session_maker
from app.models import HexTable, Point

urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET

# Точні OSM ID для парків, які Nominatim не знаходить за текстовим пошуком
OSM_ID_OVERRIDES = {
    "Лісопарк Зубра": "R22008153,W78066208",
}

OSM_QUERY_ALIASES = {
    "Високий Замок": ["парк Високий Замок, Львів", "Високий Замок парк, Львів"],
    "Парк імені Івана Франка": ["Парк імені Івана Франка, Львів", "Парк Івана Франка, Львів"],
    "Залізна Вода": ["Залізні води, Львів", "парк Залізні води, Львів"],
    "Парк культури та відпочинку імені Богдана Хмельницького": [
        "Богдана Хмельницького парк, Львів",
        "Парк культури і відпочинку імені Богдана Хмельницького, Львів",
        "Парк культури імені Богдана Хмельницького, Львів",
        "Парк культури, Львів",
    ],
    "Кульпарків": ["парк Кульпарків, Львів", "Кульпарківський парк, Львів"],
    "Піскові озера": ["парк Піскові озера, Львів", "Піскові озера, Львів"],
    "Парк імені Івана Виговського": ["Парк імені Івана Виговського, Львів", "парк Виговського, Львів"],
    "Озеро Левандівське": ["Левандівське озеро, Львів", "Парк Озеро Левандівське, Львів"],
    "Парк На валах": ["Парк На валах, Львів", "На валах, Львів"],
    "Парк Студентів": ["Парк Студентів, Львів", "Студентський парк, Львів"],
    "Парк Архангела Михаїла": ["Парк Архангела Михаїла, Львів"],
    "Майорівка": ["лісопарк Майорівка, Львів", "Медова печера, Львів", "парк Майорівка, Львів"],
}

VALID_OSM_CLASSES = {"leisure", "landuse", "natural", "boundary"}
REJECT_OSM_TYPES = {
    "administrative", "suburb", "neighbourhood", "quarter",
    "city", "town", "village", "residential", "commercial", "industrial",
}

load_dotenv()
KEY = os.environ.get("GEOAPIFY_API_KEY", "")
SOURCE, RES = "lviv_city_council_parks", 9
DEFAULT_EXCEL_CANDIDATES = [
    os.environ.get("PARKS_EXCEL_PATH", ""),
    os.path.join("data", "7. Парки і сквери.xls"),
    os.path.join("Sources", "7. Парки і сквери.xls"),
    "7. Парки і сквери.xls",
]
CACHE_FILE = "parks_geocache.json"
OSM_CACHE_FILE = "parks_osm_cache.json"

FULL = {"вул.": "вулиця", "пл.": "площа", "просп.": "проспект", "пров.": "провулок", "бульв.": "бульвар"}

# Свідомо пропускаємо заповідне болото "Торфовище Білогорща" (180)
SKIP_PARK_IDS = {180}

# 27 великих парків Львова -> точні полігони з OpenStreetMap
LARGE_PARKS_OSM_NAMES = {
    149: "Стрийський парк",
    150: "Високий Замок",
    151: "Парк імені Івана Франка",
    152: "Знесіння",
    153: "Левандівський парк",
    154: "Парк імені Івана Виговського",
    155: "Озеро Левандівське",
    156: "Піскові озера",
    159: "Лісопарк Зубра",
    160: "Залізна Вода",
    161: "Шевченківський гай",
    162: "700-річчя Львова",
    163: "Скнилівський парк",
    164: "Личаківський парк",
    165: "Парк На валах",
    166: "Парк Студентів",
    167: "Боднарівка",
    168: "Горіховий Гай",
    169: "Замарстинівський парк",
    172: "Снопківський парк",
    173: "Погулянка",
    174: "Парк Архангела Михаїла",
    175: "Парк культури та відпочинку імені Богдана Хмельницького",
    176: "Майорівка",
    177: "Кортумова гора",
    178: "Білогорща",
    181: "Кульпарків",
}

# Точні адреси для об'єктів, у яких в Excel вказано лише пам'ятник або перехрестя
QUERY_OVERRIDES = {
    3: "вулиця Лисенка, 12, Львів",             # Сквер ім. Романа Іваничука (біля "Просвіти")
    4: "вулиця Івана Франка, 108, Львів",       # Сквер біля пам'ятника І. Трушу
    118: "вулиця Липинського, 28, Львів",       # Сквер на розі Липинського - Чорновола
    183: "вулиця Авраама Лінкольна, 6а, Львів", # Парк вул. Липинського - Лінкольна
}

# Єдиний ручний fallback: зелена паркова зона Цитаделі (в OSM позначена лише будівля форту)
FALLBACK_COORDS = {
    147: [(49.83274, 24.02443)],
}

cache = json.load(open(CACHE_FILE, encoding="utf-8")) if os.path.exists(CACHE_FILE) else {}
osm_cache = json.load(open(OSM_CACHE_FILE, encoding="utf-8")) if os.path.exists(OSM_CACHE_FILE) else {}

BAD_LVIV_CENTROIDS = {
    (49.84156, 24.03139),
    (49.84210, 24.02987),
    (49.84310, 24.03054),
    (49.84212, 24.02917),
}


def resolve_excel_path(cli_path: str | None = None) -> str:
    candidates = [cli_path] if cli_path else DEFAULT_EXCEL_CANDIDATES
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise FileNotFoundError(
        "Не знайдено Excel-файл із парками. Передайте шлях через --excel-path <шлях> "
        "або змінну середовища PARKS_EXCEL_PATH."
    )


def _normalize_street_case(text: str) -> str:
    """Переводить лише прикметники жіночого роду в місцевому відмінку (Ряшівській -> Ряшівська)."""
    words = text.split()
    fixed = []
    for w in words:
        if len(w) > 4 and w[0].isupper():
            w = re.sub(r"(ській|цькій|зькій)$", lambda m: m.group(1)[:-2] + "а", w)
            w = re.sub(r"ій$", "а", w)
        fixed.append(w)
    return " ".join(fixed)


def s(v):
    return "" if pd.isna(v) else str(v).strip()


def extract_search_query(name: str, loc: str, city: str) -> str:
    m = re.search(r"(вул\.|пл\.|просп\.|пров\.|бульв\.|майдан\w*)\s*([А-ЯІЇЄҐа-яіїєґ0-9’'\-\s\.,]+)", loc)
    if m:
        prefix = FULL.get(m.group(1), m.group(1))
        rest = m.group(2)
        rest = re.split(r"[\(\);–—]| - |\bта\b|\bміж\b|\bу м\.|\bжитлов|\bтеритор|\bділянк", rest)[0]
        rest = re.sub(r"\b(ген|акад|проф|гет)\.", "", rest)
        rest = re.sub(r"\b[А-ЯІЇЄҐ]\.", "", rest)
        rest = re.sub(r"\s+", " ", rest).strip(" ,.-")
        if rest:
            rest = _normalize_street_case(rest)
            return f"{prefix} {rest}, {city}"
    return f"{name}, {city}"


def _geom_to_h3_points(geom, res: int = RES, min_overlap: float = 0.35) -> list[tuple[float, float]]:
    """Повертає по 1 точці (всередині парку) на кожен H3-гексагон, який перекривається парком >= min_overlap."""
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    cell_points: dict[str, tuple[float, float]] = {}

    for poly in polys:
        if poly.is_empty:
            continue
        outer = [(lat, lon) for lon, lat in poly.exterior.coords]
        inner: set[str] = set()
        if len(outer) >= 4:
            try:
                inner = set(h3.polygon_to_cells(h3.LatLngPoly(outer), res))
            except Exception:
                pass

        # Добираємо гексагони вздовж усього периметру парку + 1 кільце сусідів
        boundary_cells: set[str] = set()
        steps = max(4, int(poly.exterior.length / 0.0008))
        for k in range(steps):
            pt = poly.exterior.interpolate(k / steps, normalized=True)
            boundary_cells.add(h3.latlng_to_cell(pt.y, pt.x, res))

        candidates = set(inner)
        for c in boundary_cells:
            candidates.update(h3.grid_disk(c, 1))

        added = False
        for c in sorted(candidates):
            if c in cell_points:
                continue
            if c in inner:
                cell_points[c] = h3.cell_to_latlng(c)
                added = True
                continue
            hex_poly = Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(c)])
            inter = poly.intersection(hex_poly)
            if not inter.is_empty and (inter.area / hex_poly.area) >= min_overlap:
                rp = inter.representative_point()
                cell_points[c] = (rp.y, rp.x)
                added = True

        if not added:
            rp = poly.representative_point()
            c = h3.latlng_to_cell(rp.y, rp.x, res)
            cell_points.setdefault(c, (rp.y, rp.x))

    return list(cell_points.values())


def get_park_perimeter_points(osm_name: str, res: int = RES) -> list[tuple[float, float]]:
    """Отримує GeoJSON-полігон великого парку з Nominatim (OSM) і розбиває його на H3-точки."""
    if osm_name in osm_cache and osm_cache[osm_name]:
        return osm_cache[osm_name]

    headers = {"User-Agent": "OKIL-Lviv-Parks/1.0 (student-project)"}

    # 1. Прямий lookup за точними OSM ID (100% реальний полігон)
    if osm_name in OSM_ID_OVERRIDES:
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/lookup",
                params={
                    "osm_ids": OSM_ID_OVERRIDES[osm_name],
                    "format": "json",
                    "polygon_geojson": 1,
                    "accept-language": "uk",
                },
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            time.sleep(1.1)
            pts: list[tuple[float, float]] = []
            for item in resp.json():
                gj = item.get("geojson") or {}
                if gj.get("type") in ("Polygon", "MultiPolygon"):
                    pts.extend(_geom_to_h3_points(shape(gj), res))
            pts = sorted(set(pts))
            if pts:
                print(f"   ✅ {osm_name} (OSM lookup {OSM_ID_OVERRIDES[osm_name]}) -> {len(pts)} H3-точок.")
                osm_cache[osm_name] = pts
                json.dump(osm_cache, open(OSM_CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
                return pts
        except Exception as e:
            print(f"   Помилка Nominatim lookup для '{osm_name}': {e}")
            time.sleep(1.1)

    low = osm_name.lower()
    queries = OSM_QUERY_ALIASES.get(
        osm_name,
        [f"{osm_name}, Львів"]
        if any(w in low for w in ("парк", "гай", "озера", "гора"))
        else [f"парк {osm_name}, Львів", f"{osm_name}, Львів"],
    )

    for q in queries:
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": q,
                    "format": "json",
                    "polygon_geojson": 1,
                    "limit": 15,
                    "viewbox": "23.90,49.92,24.16,49.76",
                    "bounded": 1,
                    "accept-language": "uk",
                },
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            items = resp.json()
            time.sleep(1.1)
        except Exception as e:
            print(f"   Помилка Nominatim для '{q}': {e}")
            time.sleep(1.1)
            continue

        items.sort(key=lambda x: (
            0 if (x.get("class") == "leisure" and x.get("type") == "park") else
            1 if x.get("class") == "landuse" else 2
        ))

        for item in items:
            gj = item.get("geojson") or {}
            if (
                gj.get("type") in ("Polygon", "MultiPolygon")
                and item.get("class") in VALID_OSM_CLASSES
                and item.get("type") not in REJECT_OSM_TYPES
            ):
                points = _geom_to_h3_points(shape(gj), res)
                if points:
                    print(f"   ✅ {osm_name} ({item.get('class')}={item.get('type')}) -> {len(points)} H3-точок.")
                    osm_cache[osm_name] = points
                    json.dump(osm_cache, open(OSM_CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
                    return points

    print(f"   ⚠️ Полігон для '{osm_name}' не знайдено в Nominatim (піде у Fallback/Geoapify).")
    return []


BAD_GEOAPIFY_TYPES = {"city", "municipality", "county", "state", "postcode", "country"}


def geocode_park(query_text: str, city: str):
    if query_text in cache:
        cached = cache[query_text]
        if cached and (round(cached[0], 5), round(cached[1], 5)) not in BAD_LVIV_CENTROIDS:
            return cached

    result = None
    had_api_error = False

    if KEY:
        try:
            r = requests.get("https://api.geoapify.com/v1/geocode/search", params={
                "text": query_text, "filter": "circle:24.031,49.842,18000",
                "bias": "proximity:24.031,49.842", "lang": "uk", "limit": 5, "apiKey": KEY,
            }, timeout=10)
            time.sleep(0.2)
            if r.ok:
                for f in r.json().get("features") or []:
                    p = f["properties"]
                    if p.get("result_type") in BAD_GEOAPIFY_TYPES:
                        continue
                    if city == "Львів" and "Львівський район" in p.get("formatted", "") and "Львівська міська громада" not in p.get("formatted", ""):
                        continue
                    lat, lon = p["lat"], p["lon"]
                    if (round(lat, 5), round(lon, 5)) in BAD_LVIV_CENTROIDS:
                        continue
                    result = [lat, lon]
                    break
            else:
                had_api_error = True
        except Exception:
            had_api_error = True
            time.sleep(0.2)
    else:
        had_api_error = True

    if result is None:
        try:
            nr = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": query_text,
                    "format": "json",
                    "limit": 3,
                    "viewbox": "23.90,49.92,24.16,49.76",
                    "bounded": 1,
                    "accept-language": "uk",
                },
                headers={"User-Agent": "OKIL-Lviv-Parks/1.0 (student-project)"},
                timeout=15,
            )
            nr.raise_for_status()
            time.sleep(1.1)
            for item in nr.json():
                if item.get("class") == "boundary" or item.get("type") in REJECT_OSM_TYPES:
                    continue
                lat, lon = float(item["lat"]), float(item["lon"])
                if (round(lat, 5), round(lon, 5)) not in BAD_LVIV_CENTROIDS:
                    result = [lat, lon]
                    break
        except Exception:
            had_api_error = True
            time.sleep(1.1)

    if result is not None or not had_api_error:
        cache[query_text] = result
        json.dump(cache, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    return result


def parse(excel_path: str):
    df = pd.read_excel(excel_path)
    print(f"📂 {os.path.basename(excel_path)}: {len(df)} рядків")
    points, review = [], []
    total_zones = 0
    found_zones = 0

    for i, row in df.iterrows():
        if i in SKIP_PARK_IDS:
            continue

        total_zones += 1
        city = s(row["settlement"]) or "Львів"
        raw_name = s(row["name"]) or "Сквер"
        loc = s(row["location"])
        display_name = raw_name if len(raw_name) > 8 else f"{raw_name} ({loc[:60]})".strip()
        coords_list = []
        status = "not_found"

        # 1. Точний полігон OSM для 27 великих парків
        if i in LARGE_PARKS_OSM_NAMES:
            osm_name = LARGE_PARKS_OSM_NAMES[i]
            coords_list = get_park_perimeter_points(osm_name)
            if coords_list:
                status = f"ok (OSM polygon: {len(coords_list)} pts)"
                display_name = osm_name

        # 2. Fallback (тільки Цитадель)
        if not coords_list and i in FALLBACK_COORDS:
            coords_list = FALLBACK_COORDS[i]
            status = f"ok (Fallback: {len(coords_list)} pts)"

        # 3. Геокодування для всіх інших парків і скверів
        if not coords_list:
            query_text = QUERY_OVERRIDES.get(i) or extract_search_query(raw_name, loc, city)
            geo_coords = geocode_park(query_text, city)
            if geo_coords and (round(geo_coords[0], 5), round(geo_coords[1], 5)) not in BAD_LVIV_CENTROIDS:
                coords_list = [geo_coords]
                status = "ok (Geoapify)"
            else:
                status = "not_found (Geoapify failed)"

        if coords_list:
            found_zones += 1

        for pt_idx, (lat, lon) in enumerate(coords_list):
            points.append({
                "source": SOURCE,
                "external_id": f"park_{i}_{pt_idx}",
                "name": display_name[:255],
                "type": "park",
                "h3_id": h3.latlng_to_cell(lat, lon, RES),
                "geom": f"POINT({lon} {lat})",
            })

        review.append({
            "id": f"park_{i}", "name": display_name, "status": status,
            "lat": coords_list[0][0] if coords_list else None,
            "lon": coords_list[0][1] if coords_list else None,
        })

    out = pd.DataFrame(review)
    out.to_csv("review_parks.csv", index=False)
    print(f"\n✅ Згенеровано точок: {len(points)} із {found_zones}/{total_zones} зелених зон")
    return points, total_zones, found_zones


async def main(excel_path: str | None = None, dry_run: bool = False, force: bool = False):
    src_path = resolve_excel_path(excel_path)
    points, total_zones, found_zones = parse(src_path)
    if dry_run:
        print("🧪 dry-run: в БД нічого не записано. Перевір review_parks.csv")
        return
    if not points:
        raise RuntimeError("⛔ Не згенеровано жодної точки парків — скасовую оновлення БД.")
    if total_zones > 0 and (found_zones / total_zones) < 0.8 and not force:
        raise RuntimeError(
            f"⛔ Геокодер знайшов лише {found_zones} із {total_zones} зелених зон (<80%). "
            "Оновлення БД скасовано; перевірте GEOAPIFY_API_KEY / мережу або використайте --force."
        )

    async with async_session_maker() as session, session.begin():
        grid = set((await session.execute(select(HexTable.h3_id))).scalars())
        valid = [p for p in points if p["h3_id"] in grid]
        print(f"🎯 У сітці Львова: {len(valid)} точок (поза сіткою: {len(points) - len(valid)})")
        if not valid:
            raise RuntimeError("⛔ Після фільтрації за H3-сіткою залишилось 0 точок — скасовую оновлення БД.")

        cond = (Point.source == SOURCE) & (Point.type == "park")
        n_old = (await session.execute(select(func.count()).select_from(Point).where(cond))).scalar() or 0
        if n_old > 0 and len(valid) < int(n_old * 0.7) and not force:
            raise RuntimeError(
                f"⛔ Знайдено лише {len(valid)} точок проти {n_old} у БД (<70%). "
                "Запис скасовано; якщо зменшення очікуване, запустіть із --force."
            )

        if n_old:
            print(f"🗑 Видаляю старі точки park ({n_old})...")
            await session.execute(delete(Point).where(cond))
        res = await session.execute(
            pg_insert(Point).values(valid)
            .on_conflict_do_nothing(constraint="uq_points_source_external_id")
        )
        print(f"✅ Додано в `points`: {res.rowcount}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Парсинг парків і скверів Львова")
    parser.add_argument("--excel-path", default=None, help="Шлях до Excel-файлу з парками")
    parser.add_argument("--dry-run", action="store_true", help="Лише згенерувати CSV без запису в БД")
    parser.add_argument("--force", action="store_true", help="Ігнорувати поріг зменшення кількості точок")
    args = parser.parse_args()
    asyncio.run(main(excel_path=args.excel_path, dry_run=args.dry_run, force=args.force))