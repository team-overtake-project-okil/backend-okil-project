"""Second-pass healthcare registry candidate recovery, without network or DB writes.

Run from project root: python recover_health.py
Reads existing SerpAPI caches + original registry CSV and merges with 22 staged POIs.
Only picks a unique, strongly named and address-matched healthcare POI.
"""
import csv
import hashlib
import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from app.jobs.health_pipeline import (
    has_valid_coords, match_candidate, normalized, read_csv, write_csv,
    validate_reviewed, automatic_match_name,
)

ROOT = Path('tmp')
SOURCE_FILE = ROOT / 'health-preview.csv'
BASE_FILE = ROOT / 'health-auto-staged.csv'
OUT_FILE = ROOT / 'health-auto-expanded.csv'
CACHE_FILES = [ROOT / 'health-serp-cache.json', ROOT / 'health-serp-cache-v2.json']
REPORT_FILE = ROOT / 'health-recovery-report.json'

# Some records are administrative entities, not physical service locations.
ADMIN_WORDS = ('територіальне медичне об єднання', 'центр здоров я та медичної статистики')
# Common words add very little evidence of provider identity.
STOP = {'лікарня','лікарні','лікарню','лікарні','філія','центр','медичний','медичного','медичної','клінічна',
        'комунальне','некомерційне','підприємство','товариство','львова','львів','територіальне',
        'обєднання','поліклінічне','відділення','поліклініка','амбулаторія','сімейної','медицини',
        'міська','перше','друге','першого','другого','святого','святої'}


def words(value):
    return {x for x in normalized(value).split() if len(x) >= 4 and x not in STOP}


def infer_type(row):
    current = row.get('sub_category', '').strip()
    if current in {'hospital', 'polyclinic'}:
        return current
    text = normalized(row.get('name', '') + ' ' + row.get('org_type', ''))
    if any(x in text for x in ('адміністрац', 'статистик', 'територіальне медичне об єднання', 'територіальне медичне обєднання')):
        return None
    if any(x in text for x in ('стоматолог', 'зубн')):
        return None  # dentistry is a separate type and is not inferred here
    if any(x in text for x in ('поліклінік', 'амбулатор', 'сімейної медицини', 'лікувально діагностичний', 'педіатричне відділення')):
        return 'polyclinic'
    if 'лікарня' in text or 'лікарні' in text:
        return 'hospital'
    return None


def compatible(row, candidate):
    # The original vetted function enforces street/house and geo bounds.
    match, reason = match_candidate(row, [candidate])
    return match is not None and reason == 'address_match_needs_manual_review'


def confidence(row, candidate):
    src = normalized(row.get('name', ''))
    title = normalized(candidate.get('title', ''))
    a, b = words(src), words(title)
    shared = a & b
    # Require distinctive overlap or an unusually strong full-name match.
    similarity = SequenceMatcher(None, src, title).ratio()
    overlap = len(shared) / max(1, min(len(a), len(b)))
    if not shared and similarity < 0.82:
        return None
    if overlap < 0.5 and similarity < 0.82:
        return None
    return (overlap * 0.65 + similarity * 0.35, shared)


def run():
    rows = read_csv(SOURCE_FILE)
    previous = validate_reviewed(read_csv(BASE_FILE))
    existing_keys = {r['source_key'] for r in previous}
    existing_places = {
        (r['sub_category'], round(float(r['latitude']), 5), round(float(r['longitude']), 5))
        for r in previous
    }
    caches = []
    for path in CACHE_FILES:
        if path.exists():
            caches.append(json.loads(path.read_text(encoding='utf-8')))
    if not caches:
        raise RuntimeError('No SerpAPI cache found. Nothing to recover.')

    # Each saved result belongs to the row whose original name+address generated it;
    # we can also re-use other rows' result sets if source addresses are identical.
    by_address = {}
    for row in rows:
        k = hashlib.sha256((row['name'] + '|' + row['address']).encode('utf-8')).hexdigest()
        addr = normalized(row['address'])
        for cache in caches:
            if k in cache:
                by_address.setdefault(addr, []).extend(cache[k])

    added = []
    skipped = Counter()
    for row in rows:
        key = row['source_key']
        if key in existing_keys:
            continue
        point_type = infer_type(row)
        if not point_type:
            skipped['no_safe_type'] += 1
            continue
        hits = by_address.get(normalized(row['address']), [])
        good = {}
        for hit in hits:
            if not isinstance(hit, dict) or not compatible(row, hit):
                continue
            score = confidence(row, hit)
            if score is None:
                continue
            coords = hit['gps_coordinates']
            lat, lon = float(coords['latitude']), float(coords['longitude'])
            if not has_valid_coords(lat, lon):
                continue
            dedup = (normalized(hit.get('title', '')), round(lat, 6), round(lon, 6))
            if dedup not in good or score[0] > good[dedup][0]:
                good[dedup] = (score[0], hit)
        if not good:
            skipped['no_strong_address_and_name_match'] += 1
            continue
        ranked = sorted(good.values(), key=lambda x: x[0], reverse=True)
        if ranked[0][0] < 0.55 or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.12):
            skipped['ambiguous_match'] += 1
            continue
        hit = ranked[0][1]
        lat = float(hit['gps_coordinates']['latitude'])
        lon = float(hit['gps_coordinates']['longitude'])
        location_key = (point_type, round(lat, 5), round(lon, 5))
        if location_key in existing_places:
            skipped['same_type_same_location'] += 1
            continue
        existing_places.add(location_key)
        new_row = {**row, 'sub_category': point_type, 'latitude': str(lat), 'longitude': str(lon),
                   'matched_title': hit.get('title', ''), 'matched_address': hit.get('address', ''),
                   'match_reason': 'automatic_second_pass_strong_match', 'reviewed': 'yes'}
        added.append(new_row)
    combined = previous + added
    validate_reviewed(combined)
    write_csv(OUT_FILE, combined)
    report = {'source_records': len(rows), 'already_staged': len(previous), 'new_auto_matches': len(added),
              'total_staged': len(combined), 'skipped': dict(skipped), 'api_calls': 0, 'db_writes': 0,
              'output': str(OUT_FILE)}
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    run()
