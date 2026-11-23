import asyncio, json, os, sys
from collections import Counter
from math import hypot
import h3, pandas as pd, requests
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.config import settings
from app.database import async_session_maker
from app.jobs.googlemaps import lviv_hex_centers
from app.models import HexTable, Point

SOURCE, RES = "google_maps", 9
RAW_FILE = "leisure_raw.json"
CENTER = (49.842, 24.031)   # центр Львова
MAX_KM = 12                 # далі від центру не беремо
PERIPHERY_KM = 3            # точки далі за це показуємо окремо
api_calls = 0

ENT_ACCEPT = [
    "боулінг-клуб", "боулінг", "більярдний клуб", "більярд",
    "квест-кімната", "квест-кімнати",
    "розважальний центр", "розважальний центр для дітей", "парк розваг", "атракціон",
    "центр лазертагу", "центр лазерних боїв", "пейнтбольний центр",
    "картинг-центр", "картинг", "трек для картингу", "автодром",
    "комп'ютерний клуб", "ігровий клуб", "кіберспортивний клуб", "тренувальний зал для скелелазів",
    "ковзанка", "скеледром", "аквапарк", "батутний парк",
    "центр екстремальних видів спорту",
]

ENT_ACCEPT_COND = [
    ("спортивний комплекс", ["ролердром", "скеледром", "батут", "картинг", "боулінг"]),
    ("спортивний клуб", ["скеледром", "батут", "стрільб", "тир", "лазертаг", "пейнтбол"]),
    ("басейн", ["аквапарк", "пляж"]),
]

ENT_REJECT_NAMES = [
    "магазин", "кафе", "ресторан", "готель", "хостел", "сауна", "автомийка",
    " сто", "ремонт", "шиномонтаж", "прокат авто", "автошкола", "рієлтор", "нотаріус",
    "школа", "вікторія ґарденс", "вікторія гарденс", "king cross", "форум львів",
    "spartak", "спартак", "південний", "скриня", "роксолана", "магнус",
    "вам", "інтерсіті", "шувар", "булька", "океан", "плазма",
    "кіно", "покупон", "творчості", "squashfit"
]

SPECS = [
    {"query": "кінотеатр", "type": "cinema",
     "accept": ["кінотеатр"], "min_reviews": 5,
     "name_must_contain": ["кіно", "cinema", "multiplex", "movie", "довженка"],
     "reject_names": ["віталіймобі", "leoland спортивно"]},
    {"query": "музей", "type": "museum",
     "accept": ["музей"], "min_reviews": 0, "far_min_reviews": 10,
     "reject_names": ["вітряк", "водяний млин", "кузня козацька", "козацький човен", "господарський двір",
                      "велика виставкова зала", "музей -школа", "хата з села", "quest rooms",
                      "фото зона", "мала сцена", "бо друзі"],
     "reject_exact": ["музей"]},
    {"query": "театр", "type": "theater",
     "accept": ["театр", "оперний театр", "драматичний театр", "театр ляльок", "концертний зал", "філармонія", "цирк"],
     "reject_gtypes": ["кінотеатр"],
     "min_reviews": 15, "far_min_reviews": 30,
     "name_must_contain": ["театр", "опер", "філармон", "органн", "цирк", "сцена", "theatre", "theater"],
     "reject_names": ["кінотеатр", "кінопалац", "студія", "гурток", "школа", "кафе", "ресторан", "квест", "музей"]},
    {"query": "боулінг", "type": "entertainment",
     "accept": ENT_ACCEPT, "accept_conditional": ENT_ACCEPT_COND,
     "min_reviews": 15, "far_min_reviews": 25,
     "reject_names": ENT_REJECT_NAMES},
    {"query": "лазертаг", "type": "entertainment",
     "accept": ENT_ACCEPT, "accept_conditional": ENT_ACCEPT_COND,
     "min_reviews": 10, "far_min_reviews": 20,
     "reject_names": ENT_REJECT_NAMES + ["стрілецький тир"]},
    {"query": "картинг", "type": "entertainment",
     "accept": ENT_ACCEPT, "accept_conditional": ENT_ACCEPT_COND,
     "min_reviews": 15, "far_min_reviews": 25,
     "reject_names": ENT_REJECT_NAMES},
    {"query": "квест кімната", "type": "entertainment",
     "accept": ENT_ACCEPT, "accept_conditional": ENT_ACCEPT_COND,
     "min_reviews": 15, "far_min_reviews": 30,
     "reject_names": ENT_REJECT_NAMES},
    {"query": "розважальний центр", "type": "entertainment",
     "accept": ENT_ACCEPT, "accept_conditional": ENT_ACCEPT_COND,
     "min_reviews": 30, "far_min_reviews": 40,
     "reject_names": ENT_REJECT_NAMES},
    {"query": "комп'ютерний клуб", "type": "entertainment",
     "accept": ENT_ACCEPT, "accept_conditional": ENT_ACCEPT_COND,
     "min_reviews": 15, "far_min_reviews": 25,
     "reject_names": ENT_REJECT_NAMES},
    {"query": "батутний парк", "type": "entertainment",
     "accept": ENT_ACCEPT, "accept_conditional": ENT_ACCEPT_COND,
     "min_reviews": 15, "far_min_reviews": 25,
     "reject_names": ENT_REJECT_NAMES},
    {"query": "скеледром", "type": "entertainment",
     "accept": ENT_ACCEPT, "accept_conditional": ENT_ACCEPT_COND,
     "min_reviews": 10, "far_min_reviews": 20,
     "reject_names": ENT_REJECT_NAMES},
    {"query": "аквапарк", "type": "entertainment",
     "accept": ENT_ACCEPT, "accept_conditional": ENT_ACCEPT_COND,
     "min_reviews": 20, "far_min_reviews": 30,
     "reject_names": ENT_REJECT_NAMES},
]
TYPES = list(dict.fromkeys(sp["type"] for sp in SPECS))
BY_QUERY = {sp["query"]: (i, sp) for i, sp in enumerate(SPECS)}

cache = json.load(open(RAW_FILE, encoding="utf-8")) if os.path.exists(RAW_FILE) else {}


def km_from_center(lat, lon):
    return hypot((lat - CENTER[0]) * 111.1, (lon - CENTER[1]) * 71.5)


def save_cache():
    json.dump(cache, open(RAW_FILE, "w", encoding="utf-8"), ensure_ascii=False)


def fetch_page(q, lat, lon, start, keys, idx, cache_only):
    """(результати | None, індекс ключа). None = ключі вичерпано або сторінка відсутня в кеші при --cache-only."""
    ck = f"{q}|{lat:.5f}|{lon:.5f}|{start}"
    if ck in cache:
        return cache[ck], idx
    if cache_only:
        print(f"⚠️ Пропущено в кеші (--cache-only): {ck}")
        return None, idx
    global api_calls
    while idx < len(keys):
        api_calls += 1
        r = requests.get("https://serpapi.com/search", params={
            "engine": "google_maps", "q": q, "lat": str(lat), "lon": str(lon), "z": "16",
            "hl": "uk", "gl": "ua", "start": start, "api_key": keys[idx],
        }, timeout=60).json()
        if "error" in r:
            if "hasn't returned any results" in str(r["error"]):
                cache[ck] = []
                return [], idx
            print(f"⚠️ Ключ №{idx + 1}: {r['error']}. Перемикаюсь на наступний.")
            idx += 1
            continue
        res = [{
            "place_id": p.get("place_id"), "title": p.get("title", ""), "gtype": p.get("type") or "",
            "address": (p.get("address") or "").replace("\n", " ").strip(),
            "reviews": p.get("reviews") or 0,
            "lat": (p.get("gps_coordinates") or {}).get("latitude"),
            "lon": (p.get("gps_coordinates") or {}).get("longitude"),
        } for p in r.get("local_results", [])]
        cache[ck] = res
        return res, idx
    return None, idx


def collect(cache_only=False):
    """Збирає сирі відповіді в кеш. False, якщо ключі вичерпались або кеш неповний при --cache-only."""
    keys = [k.strip() for k in settings.SERPAPI_KEYS.split(",") if k.strip()]
    if not keys and not cache_only:
        raise ValueError("Не знайдено жодного ключа в SERPAPI_KEYS!")
    centers = lviv_hex_centers()
    print(f"Центрів: {len(centers)} × запитів: {len(SPECS)} (до 3 сторінок кожен)")
    idx = 0
    for sp in SPECS:
        for lat, lon in centers:
            for start in (0, 20, 40):
                res, idx = fetch_page(sp["query"], lat, lon, start, keys, idx, cache_only)
                if res is None:
                    if cache_only:
                        print("❌ Кеш неповний: у режимі --cache-only запис у БД заблоковано.")
                    else:
                        save_cache()
                        print("❌ Ключі вичерпано. Прогрес збережено, повторний запуск продовжить з цього місця.")
                    return False
                if len(res) < 20:
                    break
            if not cache_only:
                save_cache()
    print(f"Запитів до SerpApi у цьому запуску: {api_calls}")
    return True


def select_points():
    """Фільтрація кешу: відстань, категорія Google, відгуки, стоп-слова та фільтр ТРЦ."""
    rows, accepted = [], {}
    for ck, res in cache.items():
        hit = BY_QUERY.get(ck.split("|")[0])
        if not hit:
            continue
        order, sp = hit
        for p in res:
            reason, km = None, None
            gtype, title = p["gtype"].lower(), p["title"].lower()
            if not (p["place_id"] and p["lat"] and p["lon"] and p["title"]):
                reason = "немає координат/назви"
            else:
                km = km_from_center(p["lat"], p["lon"])
                if km > MAX_KM:
                    reason = "поза Львовом"
                elif any(rg in gtype for rg in sp.get("reject_gtypes", [])):
                    reason = "категорія"
                elif not (
                    any(gtype.startswith(a) or gtype.endswith(a) for a in sp["accept"])
                    or any(
                        gtype == gt and any(w in title for w in words)
                        for gt, words in sp.get("accept_conditional", [])
                    )
                ):
                    reason = "категорія"
                elif sp.get("name_must_contain") and not any(w in title for w in sp["name_must_contain"]):
                    reason = "назва не схожа"
                elif p["reviews"] < sp["min_reviews"]:
                    reason = "мало відгуків"
                elif km > PERIPHERY_KM and p["reviews"] < sp.get("far_min_reviews", 0):
                    reason = "мало відгуків (околиця)"
                elif any(w in title for w in sp["reject_names"]) or title in sp.get("reject_exact", []):
                    reason = "стоп-слово"
            rows.append({"query": sp["query"], "type": sp["type"], "title": p["title"], "gtype": p["gtype"],
                         "reviews": p["reviews"], "km": None if km is None else round(km, 1),
                         "address": p["address"], "status": reason or "ok", "place_id": p["place_id"]})
            if reason:
                continue
            old = accepted.get(p["place_id"])
            if old is None:
                accepted[p["place_id"]] = {
                    "order": order, "source": SOURCE, "external_id": p["place_id"], "name": p["title"][:255],
                    "type": sp["type"], "h3_id": h3.latlng_to_cell(p["lat"], p["lon"], RES),
                    "geom": f'POINT({p["lon"]} {p["lat"]})',
                    "_lat": p["lat"], "_lon": p["lon"],
                    "_show": {"type": sp["type"], "name": p["title"], "reviews": p["reviews"],
                              "gtype": p["gtype"], "km": km, "address": p["address"]},
                }

    points = [{k: v for k, v in pt.items() if k not in ("order", "_show", "_lat", "_lon")} for pt in accepted.values()]
    shown = pd.DataFrame([pt["_show"] for pt in accepted.values()])
    return points, pd.DataFrame(rows), shown


def print_accepted(shown, review=None):
    if shown.empty:
        return
    if review is not None:
        ok_rev = review[review.status == "ok"].drop_duplicates(["place_id", "query"])
        print("\n— Підсумок прийнятих точок за пошуковими запитами:")
        for (tp, q), cnt in ok_rev.groupby(["type", "query"]).size().items():
            print(f"   {tp:14} | {q:22} | {cnt}")

    ent = shown[shown.type == "entertainment"]
    if not ent.empty:
        print(f"\n— Розбивка entertainment ({len(ent)}) за підкатегоріями Google:")
        for gt, cnt in ent["gtype"].value_counts().items():
            print(f"   {gt:35} | {cnt}")

    for t in TYPES:
        d = shown[shown.type == t].sort_values("reviews", ascending=False)
        print(f"\n— {t}: {len(d)}")
        if t != "museum":
            for r in d.itertuples():
                print(f"   {r.name[:45]:45} | {r.reviews:>6} відг. | {r.gtype[:22]:22} | {r.km:4.1f} км")
    far = shown[shown.km > PERIPHERY_KM].sort_values(["type", "km"])
    print(f"\n— Прийняті точки далі {PERIPHERY_KM} км від центру ({len(far)}): тут сміття шкодить найбільше")
    for r in far.itertuples():
        print(f"   {r.type:7} | {r.name[:42]:42} | {r.reviews:>5} | {r.gtype[:20]:20} | {r.km:4.1f} км | {r.address[:45]}")


async def main(dry_run=False, cache_only=False, force=False):
    complete = collect(cache_only)
    points, review, shown = select_points()
    review.drop_duplicates(["place_id", "query"]).to_csv("leisure_review.csv", index=False)

    print("\n— Відхилено за причиною:")
    print(review[review.status != "ok"].drop_duplicates(["place_id", "query"]).groupby(["query", "status"]).size().to_string())
    print("\n— Відхилені категорії Google:")
    rej = review[review.status == "категорія"].drop_duplicates(["place_id", "query"])
    print(Counter(zip(rej["query"], rej["gtype"])).most_common(25))
    print_accepted(shown, review)

    if dry_run:
        print("\n🧪 dry-run: в БД нічого не записано. Повний список у leisure_review.csv")
        return
    if not complete:
        print("\n⛔ Збір/кеш неповний, у БД не пишу (щоб не замінити дані неповними).")
        return

    async with async_session_maker() as session, session.begin():
        grid = set((await session.execute(select(HexTable.h3_id))).scalars())
        valid = [p for p in points if p["h3_id"] in grid]
        print(f"\n🎯 У сітці: {len(valid)} (поза сіткою: {len(points) - len(valid)})")
        if not valid:
            raise RuntimeError("⛔ Після фільтрації за H3-сіткою залишилось 0 точок — скасовую оновлення БД.")

        cond = (Point.source == SOURCE) & (Point.type.in_(TYPES))
        n_old = (await session.execute(select(func.count()).select_from(Point).where(cond))).scalar() or 0
        if n_old > 0 and len(valid) < int(n_old * 0.5) and not force:
            raise RuntimeError(
                f"⛔ Знайдено лише {len(valid)} точок проти {n_old} у БД (<50%). "
                "Запис скасовано; якщо зменшення очікуване, запустіть із --force."
            )

        print(f"🗑 Видаляю старі точки {TYPES} з джерела {SOURCE}: {n_old}")
        await session.execute(delete(Point).where(cond))
        await session.execute(
            pg_insert(Point).values(valid)
            .on_conflict_do_nothing(constraint="uq_points_source_external_id")
        )
        print(f"✅ Записано: {len(valid)}")


if __name__ == "__main__":
    asyncio.run(main("--dry-run" in sys.argv, "--cache-only" in sys.argv, "--force" in sys.argv))