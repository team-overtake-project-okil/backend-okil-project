import asyncio, json, os, re, sys
import h3, pandas as pd, requests
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.database import async_session_maker
from app.models import HexTable, Point

load_dotenv()
SERP_KEY = os.environ["SERPAPI_KEY"]
SOURCE, RES = "lviv_city_council_education", 9
SRC_DIR = "/mnt/c/Users/Admin/Downloads/Telegram Desktop/Sources/Sources/"
FILES = [
    ("5. Заклади дошкільної освіти.xls", "kindergarten"),
    ("6. Заклади середньої освіти.xls", "school"),
]
CACHE_FILE = "serp_cache.json"

COLS = {
    "name":   ["shortName"],
    "full":   ["fullName", "shortName"],
    "city":   ["addressPostName", "settlement"],
    "stype":  ["streetType"],
    "street": ["addressThoroughfare", "streetName"],
    "house":  ["addressLocatorDesignator", "streetNumber"],
}


MANUAL = { 
    "edu_5_84": None, "edu_5_35": None, "edu_5_59": None, "edu_5_85": None,
    "edu_5_76": None, "edu_6_77": None, "edu_6_106": None,
    "edu_5_86": None, "edu_6_108": None,
}

cache = json.load(open(CACHE_FILE, encoding="utf-8")) if os.path.exists(CACHE_FILE) else {}


def pick(row, cols):
    return next((str(row[c]).strip() for c in cols if c in row and pd.notna(row[c])), "")


def clean_street(s):
    s = re.sub(r"\b\w{1,5}\.", "", s)  # прибрати "П.", "ген.", "гетьм.", "акад."
    return re.sub(r"[\s,]+", " ", s).strip()


def street_matches(wanted, found):
    stems = [w[:5].lower() for w in wanted.split() if len(w) > 3]
    return bool(stems) and sum(s in found.lower() for s in stems) >= (len(stems) + 1) // 2


def far_from_lviv(lat, lon, km=30):
    return (((lat - 49.842) * 111) ** 2 + ((lon - 24.031) * 71.8) ** 2) ** 0.5 > km


def candidates(q):
    """Результати Google Maps через SerpApi. Кешується на диск, повтор не коштує запитів."""
    if q in cache:
        return cache[q]
    r = requests.get("https://serpapi.com/search.json", params={
        "engine": "google_maps", "type": "search", "q": q,
        "ll": "@49.842,24.031,12z", "hl": "uk", "api_key": SERP_KEY,
    }, timeout=60).json()
    raw = r.get("local_results") or []
    if r.get("place_results"):
        raw = [r["place_results"]] + raw
    out = [{"title": x.get("title", ""), "address": x.get("address", ""),
            "lat": x["gps_coordinates"]["latitude"], "lon": x["gps_coordinates"]["longitude"]}
           for x in raw[:5] if (x.get("gps_coordinates") or {}).get("latitude") is not None]
    if r.get("error") and not out:
        print(f"   ⚠ SerpApi: {r['error']} (не кешую): {q}")
        return out
    cache[q] = out
    json.dump(cache, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    return out


def parse(file, ptype):
    df = pd.read_excel(SRC_DIR + file, engine="calamine")
    prefix = file.split(".")[0]
    print(f"\n📂 {file}: {len(df)} рядків → тип «{ptype}»")
    points, review = [], []
    for i, row in df.iterrows():
        eid = f"edu_{prefix}_{i}"
        name = pick(row, COLS["name"]) or "Невідомий заклад"
        stype = pick(row, COLS["stype"])
        street = clean_street(pick(row, COLS["street"]))
        house = re.split(r"[;/]", pick(row, COLS["house"]))[0].strip()
        city = pick(row, COLS["city"]) or "Львів"
        q = f"{pick(row, COLS['full'])}, {stype} {street} {house}, {city}"

        lat = lon = None
        title = addr = ""
        status = "none"
        if eid in MANUAL:
            if MANUAL[eid] is None:      # None = навмисно пропустити заклад
                status = "skip"
            else:
                (lat, lon), status = MANUAL[eid], "manual"
        else:
            cands = candidates(q)
            digits = re.sub(r"\D", "", house)
            best = next((c for c in cands if street_matches(street, c["address"])
                         and re.search(rf"(?<!\d){digits}(?!\d)", c["address"])), None)
            c = best or (cands[0] if cands else None)
            if c and not far_from_lviv(c["lat"], c["lon"]):
                lat, lon, title, addr = c["lat"], c["lon"], c["title"], c["address"]
                status = "ok" if best else "check"
            elif c:
                title, addr, status = c["title"], c["address"], "far"

        review.append({"id": eid, "name": name, "query": q, "status": status,
                       "serp_title": title, "serp_address": addr, "lat": lat, "lon": lon})
        if lat is None:
            print(f"   ✗ {eid} | {name} | {status}: {title} {addr}")
            continue
        points.append({
            "source": SOURCE, "external_id": eid, "name": name[:255], "type": ptype,
            "h3_id": h3.latlng_to_cell(lat, lon, RES), "geom": f"POINT({lon} {lat})",
        })
    pd.DataFrame(review).to_csv(f"review_{prefix}.csv", index=False)
    print(f"✅ {len(points)} із {len(df)} (усе в review_{prefix}.csv)")
    return points


async def main(dry_run=False):
    points = [p for f, t in FILES for p in parse(f, t)]
    print(f"\n📦 Всього точок: {len(points)}")
    if dry_run or not points:
        print("🧪 dry-run: в БД нічого не записано.")
        return

    async with async_session_maker() as session, session.begin():
        grid = set((await session.execute(select(HexTable.h3_id))).scalars())
        valid = [p for p in points if p["h3_id"] in grid]
        for p in points:
            if p["h3_id"] not in grid:
                print(f"   ⚠ поза сіткою: {p['name']} | {p['geom']}")
        print(f"🎯 У сітці: {len(valid)} (поза сіткою: {len(points) - len(valid)})")
        if not valid:
            print("❌ Жодна точка не потрапила в сітку, нічого не записано.")
            return
        res = await session.execute(
            pg_insert(Point).values(valid)
            .on_conflict_do_nothing(constraint="uq_points_source_external_id")
        )
        print(f"✅ Додано в `points`: {res.rowcount}")


if __name__ == "__main__":
    asyncio.run(main("--dry-run" in sys.argv))