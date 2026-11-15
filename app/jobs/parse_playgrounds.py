import asyncio, json, os, re, sys, time
import h3, pandas as pd, requests
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.database import async_session_maker
from app.models import HexTable, Point

load_dotenv()
KEY = os.environ["GEOAPIFY_API_KEY"]
SOURCE, RES = "lviv_city_council_playgrounds", 9
SRC = "/mnt/c/Users/Admin/Downloads/Telegram Desktop/Sources/Sources/9. Майданчики.xlsx"
CACHE_FILE = "playgrounds_geocache.json"
FULL = {"вул.": "вулиця", "пл.": "площа", "просп.": "проспект", "пров.": "провулок", "бульв.": "бульвар"}

cache = json.load(open(CACHE_FILE, encoding="utf-8")) if os.path.exists(CACHE_FILE) else {}


def point_type(raw):
    """Дитячі та дитячо-спортивні → playground. Чисто спортивні пропускаємо."""
    return "playground" if "дитяч" in str(raw).lower() else None


def s(v):
    return "" if pd.isna(v) else str(v).strip()


def street_text(st):
    for k, v in FULL.items():
        st = st.replace(k, v)
    st = re.sub(r"\b\w{1,5}\.", "", st)  # ініціали, "гетьм." тощо
    return re.sub(r"\s+", " ", st).strip()


def digits(x):
    return re.sub(r"\D", "", re.split(r"[;/]", str(x))[0])


def geocode(street, house, city):
    """Координати за адресою (вулиця + номер + місто) або None. Кешується на диск."""
    key = f"{street}|{house}|{city}"
    if key in cache:
        return cache[key]
    r = requests.get("https://api.geoapify.com/v1/geocode/search", params={
        "text": f"{street} {house}, {city}", "filter": "circle:24.03,49.84,30000",
        "bias": "proximity:24.031,49.842", "lang": "uk", "limit": 5, "apiKey": KEY,
    }, timeout=10)
    time.sleep(0.25)
    if not r.ok:
        return None                       # помилку не кешуємо
    stems = [w[:5].lower() for w in street.split() if len(w) > 3]
    result = None
    for f in r.json().get("features") or []:
        p = f["properties"]
        if city == "Львів" and "Львівський район" in p.get("formatted", ""):
            continue                     # приміське село, підписане як «Львів»
        if city[:4].lower() not in (p.get("city") or "").lower():
            continue
        if not any(x in (p.get("street") or "").lower() for x in stems):
            continue
        if digits(p.get("housenumber", "")) != digits(house):
            continue
        result = [p["lat"], p["lon"]]
        break
    cache[key] = result
    json.dump(cache, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    return result


def parse():
    df = pd.read_excel(SRC, engine="calamine")
    print(f"📂 {os.path.basename(SRC)}: {len(df)} рядків")
    points, review = [], []
    for i, row in df.iterrows():
        if not point_type(row["type"]):
            continue
        street = street_text(s(row["addressThoroughfare"]))
        house = s(row["addressLocatorDesignator"])
        city = s(row["addressPostName"]) or "Львів"
        label = f"{street} {house}".strip()
        coords, status = None, "ok"
        if not street or not house:
            status = "no_house"
        else:
            coords = geocode(street, house, city)
            if not coords:
                status = "not_found"
        review.append({"id": f"playground_{i}", "address": f"{label}, {city}", "status": status,
                       "lat": coords[0] if coords else None, "lon": coords[1] if coords else None})
        if coords:
            lat, lon = coords
            points.append({
                "source": SOURCE, "external_id": f"playground_{i}",
                "name": f'{s(row["type"])}, {label}'.strip(", ")[:255],
                "type": "playground", "h3_id": h3.latlng_to_cell(lat, lon, RES), "geom": f"POINT({lon} {lat})",
            })
    out = pd.DataFrame(review)
    out.to_csv("review_playgrounds.csv", index=False)
    print(f"✅ Придатних: {len(points)} із {len(out)} дитячих | {out.status.value_counts().to_dict()}")
    return points


async def main(dry_run=False):
    points = parse()
    if not points:
        return
    async with async_session_maker() as session, session.begin():
        grid = set((await session.execute(select(HexTable.h3_id))).scalars())
        valid = [p for p in points if p["h3_id"] in grid]
        print(f"🎯 У сітці: {len(valid)} (поза сіткою: {len(points) - len(valid)})")
        if dry_run:
            print("🧪 dry-run: в БД нічого не записано.")
            return
        if not valid:
            return
        res = await session.execute(
            pg_insert(Point).values(valid)
            .on_conflict_do_nothing(constraint="uq_points_source_external_id")
        )
        print(f"✅ Додано в `points`: {res.rowcount}")


if __name__ == "__main__":
    asyncio.run(main("--dry-run" in sys.argv))