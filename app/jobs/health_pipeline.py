"""Review-first healthcare POI geocoding and explicitly authorized database import.

Runs from repository root using `python -m app.jobs.health_pipeline ...`.
Never writes database without --write-db --approved-reviewed-file.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from difflib import SequenceMatcher
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

import requests

from app.jobs.load_health import FIELDS

OUTPUT_FIELDS = (*FIELDS, "matched_title", "matched_address", "match_reason", "reviewed")
# Wide enough for the Lviv municipality and nearby settlements, not an approval for arbitrary pins.
BOUNDS = (49.65, 50.02, 23.65, 24.35)  # min latitude, max latitude, min longitude, max longitude
SOURCE = "lviv_city_council_health"
H3_RES = 9


def normalized(text: str) -> str:
    return re.sub(r"[^\w]+", " ", (text or "").casefold()).strip()


def has_valid_coords(lat: object, lon: object) -> bool:
    try:
        a, b = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return math.isfinite(a) and math.isfinite(b) and BOUNDS[0] <= a <= BOUNDS[1] and BOUNDS[2] <= b <= BOUNDS[3]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def query_serpapi(query: str, key: str, timeout: float = 25.0) -> list[dict]:
    response = requests.get("https://serpapi.com/search", params={
        "engine": "google_maps", "type": "search", "q": query,
        "ll": "@49.8429,24.0316,12z", "hl": "uk", "gl": "ua", "api_key": key,
    }, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(f"SerpAPI returned an error: {data['error']}")
    return data.get("local_results") or []


def match_candidate(row: dict[str, str], results: list[dict]) -> tuple[dict | None, str]:
    """Find exactly one candidate with the same street and house number.

    A candidate still requires manual confirmation; a matching address does
    not prove two organizations are the same healthcare provider.
    """
    address = normalized(row.get("address", ""))
    match = re.search(
        r"(?:вулиця|вул|проспект|просп|площа|пл|провулок|пров)\s+"
        r"(.+?)\s+(\d+[а-яіїєґa-z]?)(?=\s|$)",
        address,
    )
    if match:
        street_text, house = match.groups()
        ignored = {"івана", "і", "вулиця", "вул", "проспект", "просп", "площа", "пл", "провулок", "пров"}
        street_words = {word for word in street_text.split() if len(word) >= 5 and word not in ignored}
    else:
        # Avoid guessing a number when the source contains several addresses.
        # Supports a simple 'вулиця Пекарська 59а' format via the regex above.
        return None, "address_needs_manual_review"

    candidates = []
    for hit in results:
        coords = hit.get("gps_coordinates") or {}
        if not has_valid_coords(coords.get("latitude"), coords.get("longitude")):
            continue
        found = normalized(hit.get("address", ""))
        if not found:
            continue
        numbers = re.findall(r"(?<!\w)\d+[а-яіїєґa-z]?(?!\w)", found)
        if house in numbers and street_words.intersection(found.split()):
            candidates.append(hit)
    if len(candidates) == 1:
        return candidates[0], "address_match_needs_manual_review"
    if len(candidates) > 1:
        return None, "ambiguous_multiple_results"
    return None, "no_confident_address_match"


def search_key(row: dict[str, str]) -> str:
    """Preserve the keys of already saved SerpAPI caches."""
    return hashlib.sha256((row["name"] + "|" + row["address"]).encode("utf-8")).hexdigest()


def geocode(input_csv: Path, output_csv: Path, cache_path: Path, limit: int, live: bool) -> dict:
    rows = read_csv(input_csv)
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    if live:
        from dotenv import load_dotenv
        load_dotenv()
    keys = [key.strip() for key in os.environ.get("SERPAPI_KEYS", "").split(",") if key.strip()]
    if live and not keys:
        raise RuntimeError("SERPAPI_KEYS not found in environment. Keep keys in .env, not in code.")

    # Index results from older name+address cache entries by normalized address.
    # This lets related organizations at the same location use cached search
    # results without making a second paid request.
    results_by_address: dict[str, list[list[dict]]] = {}
    for row in rows:
        key = search_key(row)
        if key in cache:
            address_key = normalized(row.get("address", ""))
            results_by_address.setdefault(address_key, []).append(cache[key])

    api_calls = 0
    processed = 0
    reused_addresses = 0
    result = []
    for row in rows:
        item = {**row}
        item.update({"matched_title": "", "matched_address": "", "match_reason": "", "reviewed": "no"})
        key = search_key(row)
        address_key = normalized(row.get("address", ""))
        hit_lists = results_by_address.get(address_key, [])
        # Prefer the row's own cache, but try other same-address cached results
        # if the row-specific search failed or gave an ambiguous result.
        if key in cache:
            hit_lists = [cache[key]] + [hits for hits in hit_lists if hits is not cache[key]]
        elif not hit_lists and live and api_calls < limit:
            query = f'{row["name"]}, {row["address"]}'
            hits = query_serpapi(query, keys[api_calls % len(keys)])
            cache[key] = hits
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            api_calls += 1
            time.sleep(1)
            hit_lists = [hits]
            results_by_address.setdefault(address_key, []).append(hits)
        elif not hit_lists:
            item["match_reason"] = "not_queried"
            result.append(item)
            continue
        else:
            reused_addresses += 1

        matched = None
        reasons = []
        for hits in hit_lists:
            match, reason = match_candidate(row, hits)
            reasons.append(reason)
            if match is not None:
                matched = match
                break
        if matched is not None:
            item["latitude"] = str(matched["gps_coordinates"]["latitude"])
            item["longitude"] = str(matched["gps_coordinates"]["longitude"])
            item["matched_title"] = str(matched.get("title", ""))
            item["matched_address"] = str(matched.get("address", ""))
            item["match_reason"] = "address_match_needs_manual_review"
        else:
            item["match_reason"] = (
                "ambiguous_multiple_results" if "ambiguous_multiple_results" in reasons
                else reasons[0] if reasons else "not_queried"
            )
        processed += 1
        result.append(item)
    write_csv(output_csv, result)
    return {"total": len(result), "queried_or_cached": processed,
            "reused_addresses": reused_addresses, "api_calls": api_calls,
            "candidates_for_review": sum(bool(row["latitude"]) for row in result),
            "approved": 0, "db_writes": 0, "output": str(output_csv)}



# Conservative automatic staging: a geocoding candidate is not evidence of a
# real provider unless its name ALSO resembles the source provider's name.
GENERIC_NAME_WORDS = {
    "лікарня", "лікарні", "лікарню", "центр", "медичний", "медичного", "медичної",
    "філія", "відділення", "поліклінічне", "поліклініка", "клінічна",
    "комунальне", "некомерційне", "підприємство", "товариство", "львова", "львів",
    "територіальне", "об'єднання", "обєднання", "першого",
}


def automatic_match_name(row: dict[str, str]) -> bool:
    source = normalized(row.get("name", ""))
    found = normalized(row.get("matched_title", ""))
    if not source or not found:
        return False
    source_tokens = {x for x in source.split() if len(x) >= 5 and x not in GENERIC_NAME_WORDS}
    found_tokens = {x for x in found.split() if len(x) >= 5 and x not in GENERIC_NAME_WORDS}
    shared = source_tokens & found_tokens
    return bool(shared) or SequenceMatcher(None, source, found).ratio() >= 0.72


def stage_automatic(input_csv: Path, output_csv: Path) -> dict:
    """Stage clear, individually verified-by-rules matches; no network or DB."""
    rows = read_csv(input_csv)
    chosen = []
    skipped: dict[str, int] = {}
    seen = set()
    for row in rows:
        reason = None
        category = row.get("sub_category", "")
        if category not in {"hospital", "polyclinic"}:
            reason = "unclassified_category"
        elif row.get("match_reason") != "address_match_needs_manual_review":
            reason = "ambiguous_or_unmatched_address"
        elif not has_valid_coords(row.get("latitude"), row.get("longitude")):
            reason = "invalid_coordinates"
        elif not automatic_match_name(row):
            reason = "provider_name_not_confirmed"
        elif not row.get("source_key") or not row.get("name"):
            reason = "missing_identifier_or_name"
        # Avoid exact duplicate physical candidate of the same type. Distinct
        # departments at one address need separate evidence and remain excluded.
        dedup_key = (category, normalized(row.get("matched_title", "")),
                     normalized(row.get("matched_address", "")))
        if reason is None and dedup_key in seen:
            reason = "duplicate_provider_candidate"
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        seen.add(dedup_key)
        item = {**row, "reviewed": "yes"}
        chosen.append(item)
    # Existing import validator checks unique keys and coordinate bounds.
    validate_reviewed(chosen)
    write_csv(output_csv, chosen)
    return {"total": len(rows), "auto_staged": len(chosen),
            "skipped": skipped, "db_writes": 0, "output": str(output_csv)}

def validate_reviewed(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    approved = []
    seen = set()
    for row in rows:
        if row.get("reviewed", "").strip().lower() != "yes":
            continue
        if row.get("sub_category") not in {"hospital", "polyclinic"}:
            raise ValueError(f"Unapproved point type in row {row.get('source_row')}")
        if not has_valid_coords(row.get("latitude"), row.get("longitude")):
            raise ValueError(f"Invalid coordinates in row {row.get('source_row')}")
        if not row.get("name") or not row.get("source_key"):
            raise ValueError("Approved row needs name and stable source_key")
        if row["source_key"] in seen:
            raise ValueError("Duplicate source_key in reviewed file")
        seen.add(row["source_key"])
        approved.append(row)
    return approved


async def import_reviewed(path: Path, write_db: bool) -> dict:
    rows = validate_reviewed(read_csv(path))
    if not write_db:
        return {"approved": len(rows), "db_writes": 0, "mode": "dry_run"}
    if not rows:
        raise ValueError("No manually reviewed rows with reviewed=yes")
    # Lazy imports make staging and unit tests independent of DB availability.
    import h3
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.database import async_session_maker
    from app.models import HexTable, Point, PointType

    async with async_session_maker() as session:
        async with session.begin():
            types = (await session.execute(select(PointType.type, PointType.category))).all()
            valid_types = {name for name, category in types if category == "health"}
            missing = {row["sub_category"] for row in rows} - valid_types
            if missing:
                raise ValueError(f"PointTypes missing health category/radii: {sorted(missing)}. Ask team to configure them first.")
            hex_ids = set((await session.execute(select(HexTable.h3_id))).scalars().all())
            values = []
            excluded = 0
            for row in rows:
                lat, lng = float(row["latitude"]), float(row["longitude"])
                h_id = h3.latlng_to_cell(lat, lng, H3_RES)
                if h_id not in hex_ids:
                    excluded += 1
                    continue
                values.append({"source": SOURCE, "external_id": row["source_key"],
                               "name": row["name"][:255], "type": row["sub_category"],
                               "h3_id": h_id, "geom": f"POINT({lng} {lat})"})
            for i in range(0, len(values), 200):
                stmt = pg_insert(Point).values(values[i:i+200])
                await session.execute(stmt.on_conflict_do_update(
                    constraint="uq_points_source_external_id",
                    set_={"name": stmt.excluded.name, "type": stmt.excluded.type,
                          "h3_id": stmt.excluded.h3_id, "geom": stmt.excluded.geom}))
    return {"approved": len(rows), "upserted": len(values), "outside_existing_hex_grid": excluded, "mode": "write_db"}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    g = sub.add_parser("geocode")
    g.add_argument("--input", type=Path, default=Path("tmp/health-preview.csv"))
    g.add_argument("--output", type=Path, default=Path("tmp/health-geocoded.csv"))
    g.add_argument("--cache", type=Path, default=Path("tmp/health-serp-cache.json"))
    g.add_argument("--limit", type=int, default=5, help="Maximum NEW paid API requests")
    g.add_argument("--live", action="store_true", help="Actually call SerpAPI; default is cache-only")
    auto = sub.add_parser("auto-stage", help="Stage only unambiguous candidates; no DB writes")
    auto.add_argument("--input", type=Path, required=True)
    auto.add_argument("--output", type=Path, default=Path("tmp/health-auto-staged.csv"))
    db = sub.add_parser("import")
    db.add_argument("--input", type=Path, required=True)
    db.add_argument("--write-db", action="store_true")
    db.add_argument("--approved-reviewed-file", action="store_true")
    db.add_argument("--auto-validated-file", action="store_true")
    args = parser.parse_args()
    if args.action == "geocode":
        print(json.dumps(geocode(args.input, args.output, args.cache, max(0, args.limit), args.live), ensure_ascii=False, indent=2))
    elif args.action == "auto-stage":
        print(json.dumps(stage_automatic(args.input, args.output), ensure_ascii=False, indent=2))
    else:
        if args.write_db and not (args.approved_reviewed_file or args.auto_validated_file):
            parser.error("DB writes require --write-db and explicit validation flag plus team approval")
        if args.write_db and args.auto_validated_file:
            expected = Path("tmp/health-auto-staged.csv").resolve()
            if args.input.resolve() != expected:
                parser.error("--auto-validated-file requires tmp/health-auto-staged.csv")
        print(json.dumps(asyncio.run(import_reviewed(args.input, args.write_db)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
