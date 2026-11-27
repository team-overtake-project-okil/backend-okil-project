import argparse, asyncio, hashlib, json, os, re, time
import socket
from collections import Counter
import h3, pandas as pd, requests
import urllib3.util.connection
from dotenv import load_dotenv
from shapely.geometry import Polygon, shape
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.database import async_session_maker
from app.jobs.validation import require_point_types
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
REQUIRED_POINT_TYPES = {"park": "enjoyment"}
DEFAULT_EXCEL_CANDIDATES = [
    os.environ.get("PARKS_EXCEL_PATH", ""),
    os.path.join("data", "7. Парки і сквери.xls"),
    os.path.join("Sources", "7. Парки і сквери.xls"),
    "7. Парки і сквери.xls",
]
CACHE_FILE = "parks_geocache.json"
OSM_CACHE_FILE = "parks_osm_cache.json"

FULL = {"вул.": "вулиця", "пл.": "площа", "просп.": "проспект", "пров.": "провулок", "бульв.": "бульвар"}


def s(v):
    return "" if pd.isna(v) else str(v).strip()


def _norm(value) -> str:
    text = s(value).lower()
    text = (
        text.replace("\xa0", " ")
        .replace("’", "'")
        .replace("`", "'")
        .replace("ʼ", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("«", '"')
        .replace("»", '"')
        .replace("–", "-")
        .replace("—", "-")
    )
    return re.sub(r"\s+", " ", text).strip()


def _row_uid(raw_name: str, city: str, district: str, loc: str, area: str) -> str:
    payload = "|".join((_norm(raw_name), _norm(city), _norm(district), _norm(loc), _norm(area)))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


# Свідомо пропускаємо заповідне болото "Торфовище Білогорща" (прив'язка за назвою, не за номером рядка)
SKIP_PARK_NAMES = {
    _norm('Парк, ландшафтний заказник місцевого значення "Торфовище Білогорща"'),
}

# 27 великих парків Львова -> точні полігони з OpenStreetMap (прив'язка за нормалізованою назвою з Excel)
LARGE_PARKS_OSM_NAMES = {
    _norm('Пам\'ятка садово-паркового мистецтва загальнодержавного значення "Стрийський парк"'): "Стрийський парк",
    _norm('Пам’ятка садово-паркового мистецтва місцевого значення парк "Високий замок"'): "Високий Замок",
    _norm("Пам’ятка садово-паркового мистецтва місцевого значення парк ім. Івана Франка"): "Парк імені Івана Франка",
    _norm('Регіональний ландшафтний парк  "Знесіння" '): "Знесіння",
    _norm('Парк "Левандівський"'): "Левандівський парк",
    _norm("Парк імені І. Виговського"): "Парк імені Івана Виговського",
    _norm('Парк "Озеро Левандівське"'): "Озеро Левандівське",
    _norm('Парк "Піскові озера"'): "Піскові озера",
    _norm('Парк Святого Івана Павла ІІ Папи Римського (лісопарк "Зубра")'): "Лісопарк Зубра",
    _norm('Пам’ятка садово-паркового мистецтва місцевого значення парк "Залізна вода"'): "Залізна Вода",
    _norm('Пам’ятка природи місцевого значення "Шевченківський гай"'): "Шевченківський гай",
    _norm('Парк "700–річчя Львова"'): "700-річчя Львова",
    _norm('Парк "Скнилівський"'): "Скнилівський парк",
    _norm('Пам’ятка садово-паркового мистецтва місцевого значення "Личаківський парк"'): "Личаківський парк",
    _norm('Парк "На валах"'): "Парк На валах",
    _norm('Парк "Студентів"'): "Парк Студентів",
    _norm('Парк "Боднарівка"'): "Боднарівка",
    _norm('Парк "Горіховий гай"'): "Горіховий Гай",
    _norm('Парк "Замарстинівський"'): "Замарстинівський парк",
    _norm('Пам’ятка садово-паркового мистецтва місцевого значення парк "Снопківський"'): "Снопківський парк",
    _norm('Парк, ботанічна пам’ятка природи місцевого значення "Лісопарк "Погулянка"'): "Погулянка",
    _norm("Парк Архангела Михаїла"): "Парк Архангела Михаїла",
    _norm("Пам’ятка садово-паркового мистецтва місцевого значення Львівський центральний парк культури і відпочинку ім. Б. Хмельницького"): "Парк культури та відпочинку імені Богдана Хмельницького",
    _norm('Парк, зелена зона "Майорівка" (у тому числі геологічна пам’ятка природи місцевого значення "Медова Печера")'): "Майорівка",
    _norm('Геологічна пам’ятка природи місцевого значення парк "Кортумова гора"'): "Кортумова гора",
    _norm('Лісопарк "Білогорща"'): "Білогорща",
    _norm('Об’єкт природно-заповідного фонду парк-пам’ятка садово-паркового мистецтва місцевого значення "Кульпарків"'): "Кульпарків",
}

# Точні адреси для об'єктів, у яких в Excel вказано лише пам'ятник або перехрестя (прив'язка за (name, location))
QUERY_OVERRIDES = {
    (_norm("Сквер ім. Романа Іваничука"), _norm('біля пам’ятника "Товариство "Просвіта"')): "вулиця Лисенка, 12, Львів",
    (_norm("Сквер"), _norm("біля пам’ятника І. Трушу")): "вулиця Івана Франка, 108, Львів",
    (_norm("Сквер"), _norm("на розі вул. Липинського В. - просп. Чорновола В.")): "вулиця Липинського, 28, Львів",
    (_norm("Парк"), _norm("вул. Липинського В. - вул. Лінкольна А.")): "вулиця Авраама Лінкольна, 6а, Львів",
}

# Єдиний ручний fallback: зелена паркова зона Цитаделі (в OSM позначена лише будівля форту)
FALLBACK_COORDS = {
    _norm('Паркова зона "Цитадель"'): [(49.83274, 24.02443)],
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
                with open(OSM_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(osm_cache, f, ensure_ascii=False)
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
                    with open(OSM_CACHE_FILE, "w", encoding="utf-8") as f:
                        json.dump(osm_cache, f, ensure_ascii=False)
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
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    return result


def parse(excel_path: str, strict_mappings: bool = True):
    df = pd.read_excel(excel_path)
    print(f"📂 {os.path.basename(excel_path)}: {len(df)} рядків")
    points, review = [], []
    total_zones = 0
    found_zones = 0
    uid_counts: Counter[str] = Counter()
    skip_hits: Counter[str] = Counter()
    large_hits: Counter[str] = Counter()
    override_hits: Counter[tuple[str, str]] = Counter()
    fallback_hits: Counter[str] = Counter()

    for _, row in df.iterrows():
        city = s(row["settlement"]) or "Львів"
        raw_name = s(row["name"]) or "Сквер"
        district = s(row.get("district"))
        loc = s(row["location"])
        area = s(row.get("area"))
        norm_name = _norm(raw_name)
        name_loc_key = (norm_name, _norm(loc))

        if norm_name in SKIP_PARK_NAMES:
            skip_hits[norm_name] += 1
            continue

        total_zones += 1
        base_uid = _row_uid(raw_name, city, district, loc, area)
        uid_counts[base_uid] += 1
        row_uid = base_uid if uid_counts[base_uid] == 1 else f"{base_uid}_{uid_counts[base_uid]}"

        display_name = raw_name if len(raw_name) > 8 else f"{raw_name} ({loc[:60]})".strip()
        coords_list = []
        status = "not_found"

        # 1. Точний полігон OSM для 27 великих парків
        if norm_name in LARGE_PARKS_OSM_NAMES:
            large_hits[norm_name] += 1
            osm_name = LARGE_PARKS_OSM_NAMES[norm_name]
            coords_list = get_park_perimeter_points(osm_name)
            if coords_list:
                status = f"ok (OSM polygon: {len(coords_list)} pts)"
                display_name = osm_name

        # 2. Fallback (тільки Цитадель)
        if not coords_list and norm_name in FALLBACK_COORDS:
            fallback_hits[norm_name] += 1
            coords_list = FALLBACK_COORDS[norm_name]
            status = f"ok (Fallback: {len(coords_list)} pts)"

        # 3. Геокодування для всіх інших парків і скверів
        if not coords_list:
            if name_loc_key in QUERY_OVERRIDES:
                override_hits[name_loc_key] += 1
                query_text = QUERY_OVERRIDES[name_loc_key]
            else:
                query_text = extract_search_query(raw_name, loc, city)
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
                "external_id": f"park_{row_uid}_{pt_idx}",
                "name": display_name[:255],
                "type": "park",
                "h3_id": h3.latlng_to_cell(lat, lon, RES),
                "geom": f"POINT({lon} {lat})",
            })

        review.append({
            "id": f"park_{row_uid}", "name": display_name, "status": status,
            "lat": coords_list[0][0] if coords_list else None,
            "lon": coords_list[0][1] if coords_list else None,
        })

    if strict_mappings:
        problems = []
        for k in SKIP_PARK_NAMES:
            if skip_hits[k] != 1:
                problems.append(f"SKIP_PARK_NAMES[{k!r}] збігів={skip_hits[k]}")
        for k, osm_name in LARGE_PARKS_OSM_NAMES.items():
            if large_hits[k] != 1:
                problems.append(f"LARGE_PARKS_OSM_NAMES[{osm_name!r}] збігів={large_hits[k]}")
        for k in FALLBACK_COORDS:
            if fallback_hits[k] != 1:
                problems.append(f"FALLBACK_COORDS[{k!r}] збігів={fallback_hits[k]}")
        for k in QUERY_OVERRIDES:
            if override_hits[k] != 1:
                problems.append(f"QUERY_OVERRIDES[{k!r}] збігів={override_hits[k]}")
        if problems:
            raise RuntimeError(
                "⛔ Невідповідність прив'язок парків до Excel (можливо, змінилися назви або дублікати): "
                + "; ".join(problems)
            )

    out = pd.DataFrame(review)
    out.to_csv("review_parks.csv", index=False)
    print(f"\n✅ Згенеровано точок: {len(points)} із {found_zones}/{total_zones} зелених зон")
    return points, total_zones, found_zones


async def main(excel_path: str | None = None, dry_run: bool = False, force: bool = False):
    src_path = resolve_excel_path(excel_path)
    if not dry_run:
        async with async_session_maker() as session:
            await require_point_types(session, REQUIRED_POINT_TYPES)

    points, total_zones, found_zones = parse(src_path, strict_mappings=not force)
    if dry_run:
        print("🧪 dry-run: в БД нічого не записано. Перевір review_parks.csv")
        return
    if not points:
        raise RuntimeError("⛔ Не згенеровано жодної точки парків — запис скасовано.")
    if total_zones > 0 and (found_zones / total_zones) < 0.8 and not force:
        raise RuntimeError(
            f"⛔ Геокодер знайшов лише {found_zones} із {total_zones} зелених зон (<80%). "
            "Запис скасовано; перевірте GEOAPIFY_API_KEY / мережу або використайте --force."
        )

    async with async_session_maker() as session, session.begin():
        await require_point_types(session, REQUIRED_POINT_TYPES)
        grid = set((await session.execute(select(HexTable.h3_id))).scalars())
        valid = [p for p in points if p["h3_id"] in grid]
        print(f"🎯 У сітці Львова: {len(valid)} точок (поза сіткою: {len(points) - len(valid)})")
        if not valid:
            raise RuntimeError("⛔ Після фільтрації за H3-сіткою залишилось 0 точок — запис скасовано.")

        stmt = pg_insert(Point).values(valid)
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
        print(f"✅ Додано/оновлено в `points`: {len(valid)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Парсинг парків і скверів Львова")
    parser.add_argument("--excel-path", default=None, help="Шлях до Excel-файлу з парками")
    parser.add_argument("--dry-run", action="store_true", help="Лише згенерувати CSV без запису в БД")
    parser.add_argument("--force", action="store_true", help="Ігнорувати поріг повноти та строгий збіг правил")
    args = parser.parse_args()
    asyncio.run(main(excel_path=args.excel_path, dry_run=args.dry_run, force=args.force))