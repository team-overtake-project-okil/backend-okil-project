"""Read healthcare registries and prepare auditable, cache-first address matches.

No database access. Network requests require explicit --live and a bounded limit.
Raw workbooks are never changed. A medical registry proves provider identity;
address geocoding proves the location, without guessing from a similar POI name.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import re
import time
from pathlib import Path

from app.jobs.health.load_health import build_address, categorize, clean, prepare, read_xls
from app.jobs.health.health_pipeline import has_valid_coords, read_csv, write_csv, match_candidate, search_key

ACCREDITATION_SOURCE = "moz_accredited_health"
REGIONAL_SOURCE = "lviv_oblast_health_registry"
HEALTH_TYPES = frozenset({"hospital", "polyclinic", "dentistry"})
LOCALITIES = {
    "Львів", "Винники", "Брюховичі", "Рудно", "Дубляни", "Малехів", "Гряда",
    "Великі Грибовичі", "Малі Грибовичі", "Зашків", "Зарудці", "Завадів",
    "Лисиничі", "Підбірці", "Зимна Вода", "Лапаївка", "Оброшине", "Солонка",
    "Сокільники", "Зубра", "Пасіки-Зубрицькі", "Муроване", "Сороки-Львівські",
    "Пустомити", "Липники", "Ясниська", "Куликів", "Давидів", "Рясне-Руське",
}
CITY_PATTERN = re.compile(
    r"(?<!\w)(?:м\.|м\s|місто\s|смт\.?\s|селище\s|село\s|с\.\s)\s*"
    r"([А-ЯІЇЄҐ][А-Яа-яІіЇїЄєҐґ'’\-]*(?:\s+[А-ЯІЇЄҐ][А-Яа-яІіЇїЄєҐґ'’\-]*)?)"
)
STREET_PATTERN = re.compile(r"(?<!\w)(вул(?:иця|\.)?|проспект|просп\.?|пр-т|пр\.|пл(?:оща|\.)?|бульвар|провулок)\s*", re.I)


def normalized(value: object) -> str:
    return re.sub(r"[^\w]+", " ", clean(value).casefold()).strip()


def compact(value: object) -> str:
    return normalized(value).replace(" ", "")


def house_key(value: object) -> str:
    text = re.sub(r"\s+", "", clean(value)).casefold()
    return re.sub(r"(?<=\d)-(?=[а-яіїєґa-z]$)", "", text)


def street_house(address: str) -> tuple[str, str]:
    """Keep suffixes, fractions and house ranges distinct; never pick a postal code."""
    address = clean(address).replace(",", " ")
    match = STREET_PATTERN.search(address)
    if not match:
        return "", ""
    part = address[match.end():]
    part = re.sub(r"\b(?:будинок|буд\.?|б\.)\s*", "", part, flags=re.I)
    found = re.search(r"\s(\d+(?:\s*[-/]\s*\d+)?(?:\s*-?\s*[а-яіїєґa-z])?)(?=\s|$)", part, re.I)
    if not found:
        return "", ""
    return clean(part[:found.start()]), house_key(found.group(1))


def street_matches(first: str, second: str) -> bool:
    # Ignore given names and initials, not distinctive surnames or numbered streets.
    ignored = {"івана", "тарасa", "тараса", "володимира", "пилипа", "василя", "михайла", "ярослава", "гетьмана", "генерала"}
    def words(text):
        return {w for w in normalized(text).split() if len(w) > 2 and w not in ignored and w not in {"вулиця", "проспект", "вул", "просп"}}
    a, b = words(first), words(second)
    return bool(a and b and (a <= b or b <= a))


def street_kind(address: str) -> str:
    found = STREET_PATTERN.search(address)
    if not found:
        return ''
    word = found.group(1).casefold()
    if word.startswith('пр') and not word.startswith('пров'):
        return 'проспект'
    if word.startswith('пл'):
        return 'площа'
    if word.startswith('пров'):
        return 'провулок'
    return 'бульвар' if word == 'бульвар' else 'вулиця'


def same_address(first: dict, second: dict) -> bool:
    a, b = street_house(first.get('address', '')), street_house(second.get('address', ''))
    return bool(compact(first.get('city')) == compact(second.get('city')) and a[1]
                and a[1] == b[1] and street_matches(a[0], b[0])
                and street_kind(first['address']) == street_kind(second['address']))


def identifier(value: object) -> str:
    text = clean(value)
    if text.endswith('.0'):
        text = text[:-2]
    return text.zfill(8) if text.isdigit() else text


def split_addresses(text: str) -> list[tuple[str, str]]:
    """Split explicit localities/streets, inheriting a town only inside its segment."""
    text = clean(text)
    # A registry sometimes spells the locality as Львів-Винники.
    text = text.replace("Львів-Винники", "м. Винники")
    towns = list(CITY_PATTERN.finditer(text))
    output = []
    for i, town in enumerate(towns):
        city = town.group(1)
        segment = text[town.end():towns[i+1].start() if i+1 < len(towns) else len(text)]
        starts = list(STREET_PATTERN.finditer(segment))
        for j, start in enumerate(starts):
            street = segment[start.start():starts[j+1].start() if j+1 < len(starts) else len(segment)]
            street = street.strip(' ,;')
            if street_house(street)[1]:
                output.append((city, f"{city}, {street}"))
    return list(dict.fromkeys(output))


def read_xlsx(path: Path) -> list[tuple[int, dict]]:
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError("Install the XLSX reader: pip install 'openpyxl>=3.1,<4'") from exc
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        result = []
        for sheet in workbook:
            rows = sheet.iter_rows(values_only=True)
            headers = [clean(v) for v in next(rows, ())]
            if not any(headers):
                continue
            for number, values in enumerate(rows, 2):
                row = dict(zip(headers, values))
                if any(clean(v) for v in values):
                    result.append((number, row))
        return result
    finally:
        workbook.close()


def load_registry(path: Path) -> tuple[list[dict], dict]:
    if path.suffix.lower() == '.xls':
        records, report = prepare(read_xls(path))
        report = {**report, 'source_file': path.name}
        return records, report
    if path.suffix.lower() != '.xlsx':
        raise ValueError(f"Unsupported registry format: {path.suffix}")
    rows = read_xlsx(path)
    records, invalid, outside = [], [], []
    for number, row in rows:
        if 'Назва закладу охорони здоров’я' in row:
            name = clean(row['Назва закладу охорони здоров’я'])
            org = identifier(row.get('Ідентифікаційний код'))
            locations = split_addresses(clean(row.get('Адреса(и) закладу охорони здоров’я')))
            source = ACCREDITATION_SOURCE
            # A territorial association lists administrative addresses of many
            # kinds of branch. Its legal name must not turn them all into hospitals.
            org_type = "Територіальне медичне об'єднання" if "територіальне медичне об" in name.casefold() else ''
        elif 'fullName' in row and 'settlement' in row:
            name = clean(row['fullName'])
            org = identifier(row.get('orgIdentifier'))
            city = clean(row['settlement'])
            locations = [(city, f"{city}, {clean(row.get('streetType'))} {clean(row.get('streetName'))} {clean(row.get('houseNumber'))}")]
            source, org_type = REGIONAL_SOURCE, ''
        else:
            raise ValueError(f"Unknown registry headers in {path.name}")
        if not name or not locations:
            invalid.append(number)
            continue
        local = [(city, address) for city, address in locations if compact(city) in {compact(c) for c in LOCALITIES}]
        if not local:
            outside.append(number)
        for city, address in local:
            category = categorize(name, org_type)
            # An EDRPOU identifies an organization, not all of its physical branches.
            unit = compact(name) if re.search(r'відділен|амбулатор|філія', name, re.I) else ''
            key = hashlib.sha256(f"{org or compact(name)}|{compact(address)}|{unit}".encode()).hexdigest()[:32]
            records.append(dict(source=source, source_key=key, source_row=str(number),
                                name=name, address=address, city=city, org_type=org_type,
                                org_identifier=org, sub_category=category,
                                latitude='', longitude='', reviewed='no',
                                status='needs_geocoding' if category else 'review_category_and_geocoding'))
    return records, {'source_file': path.name, 'total_source_rows': len(rows),
                     'staged_locations': len(records), 'invalid_source_rows': invalid,
                     'outside_target_localities': outside}


def same_provider(first: dict, second: dict) -> bool:
    """Require identity AND location. Co-location alone never merges departments."""
    if first.get('sub_category') != second.get('sub_category'):
        return False
    if not same_address(first, second):
        return False
    if compact(first.get('name')) == compact(second.get('name')):
        return True
    # Explicit departments must keep their own identity, even under one EDRPOU.
    department = re.compile(r"відділен|амбулатор|філія", re.I)
    return bool(first.get('org_identifier') and first.get('org_identifier') == second.get('org_identifier')
                and not department.search(first['name']) and not department.search(second['name']))


def reuse_existing(rows: list[dict], existing: list[dict]) -> list[dict]:
    """Reuse checked DB coordinates only for the same immutable source identity."""
    import h3
    index = {(point['source'], point['external_id']): point for point in existing}
    result = []
    for row in rows:
        item = dict(row)
        old = index.get((row.get('source'), row.get('source_key')))
        if (old and not row.get('coordinate_source')
                and has_valid_coords(old.get('latitude'), old.get('longitude'))
                and old.get('h3_id') == h3.latlng_to_cell(float(old['latitude']), float(old['longitude']), 9)):
            item.update(latitude=str(old['latitude']), longitude=str(old['longitude']),
                        coordinate_source='existing_database_identity', evidence=f"{old['source']}:{old['external_id']}")
        result.append(item)
    return result


def apply_type_profiles(rows: list[dict], path: Path) -> list[dict]:
    """Use GIS institution metadata for type evidence, never its suspect coordinates.

    A diagnostic centre may be an inpatient hospital. Require its exact provider
    identity, city and address, an explicit hospital type and positive bed count.
    Administrative associations and explicitly named outpatient units stay put.
    """
    profiles = json.loads(path.read_text(encoding='utf-8'))['features']
    def identity(name):
        result = compact(name)
        for prefix in ('комунальненекомерційнепідприємство', 'львівськоїобласноїради', 'львівськоїміськоїради', 'кнплор', 'кнп'):
            result = result.replace(prefix, '')
        return result
    for row in rows:
        if row.get('sub_category') != 'polyclinic' or re.search('поліклінік|амбулатор|відділен', row['name'], re.I):
            continue
        matches = []
        for feature in profiles:
            data = feature['properties']
            if identity(data.get('name')) != identity(row['name']):
                continue
            address = clean(data.get('address')).replace(',', ' ')
            address = re.sub(r'(?<=[а-яіїєґ])(?=\d)', ' ', address, flags=re.I)
            if not STREET_PATTERN.search(address):
                address = 'вулиця ' + address
            if not same_address(row, dict(city=data.get('settlement'), address=address)):
                continue
            try:
                beds = float(data.get('cap_in') or 0)
            except (TypeError, ValueError):
                continue
            if (clean(data.get('type_')).startswith('лікарняний заклад')
                    and clean(data.get('operation')) == 'так'
                    and beds > 0):
                matches.append(data)
        if matches:
            row.update(sub_category='hospital', category_source='GIS inpatient type and beds; GlobalID=' + clean(matches[0].get('GlobalID')))
    return rows


def exact_address_hit(row: dict, hits: list[dict]) -> dict | None:
    street, house = street_house(row.get('address', ''))
    good = {}
    for hit in hits:
        address = hit.get('address') or {}
        if not has_valid_coords(hit.get('lat'), hit.get('lon')) or house_key(address.get('house_number')) != house:
            continue
        localities = [address.get(k, '') for k in ('city', 'town', 'village', 'hamlet', 'municipality', 'suburb')]
        if compact(row.get('city')) not in {compact(v) for v in localities}:
            continue
        road = address.get('road') or address.get('pedestrian') or address.get('square') or ''
        if street_kind(road) and street_kind(row['address']) != street_kind(road):
            continue
        if not street_matches(street, road):
            continue
        good[(round(float(hit['lat']), 6), round(float(hit['lon']), 6))] = hit
    # Multiple remote objects at the address are not silently ranked.
    return next(iter(good.values())) if len(good) == 1 else None


def enrich(rows: list[dict], cache_path: Path, *, live=False, limit=0,
           endpoint='', user_agent='', serp_caches=(), normalize_query=False) -> tuple[list[dict], dict]:
    """A finite, single-threaded import. Use a configured geocoder, never POI scans.

    If choosing the public Nominatim service, read its usage policy first:
    https://operations.osmfoundation.org/policies/nominatim/
    One machine, <=1 request/sec, persistent cache, attribution; no recurring bulk jobs.
    """
    import requests
    if live and (not endpoint or not user_agent):
        raise ValueError('Live geocoding needs --endpoint and an identifying --user-agent')
    cache = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else {}
    serp = {}
    for path in serp_caches:
        serp.update(json.loads(path.read_text(encoding='utf-8')))
    output, calls, cache_hits = [], 0, 0
    for row in rows:
        item = {**row, 'reviewed': 'no'}
        if row.get('sub_category') not in HEALTH_TYPES:
            item['match_reason'] = 'unclassified_category'
            output.append(item)
            continue
        if has_valid_coords(row.get('latitude'), row.get('longitude')) and row.get('coordinate_source'):
            item.update(reviewed='yes', match_reason='source_coordinates')
            output.append(item)
            continue
        # Existing SerpAPI responses stay usable without new paid requests.
        hits = serp.get(search_key(row), [])
        from app.jobs.health.health_pipeline import automatic_match_name
        hit, _ = match_candidate(row, hits)
        if hit and automatic_match_name({**row, 'matched_title': hit.get('title', '')}):
            item.update(latitude=str(hit['gps_coordinates']['latitude']), longitude=str(hit['gps_coordinates']['longitude']),
                        coordinate_source='serpapi_cache', reviewed='yes', match_reason='cached_name_and_address',
                        matched_title=hit.get('title', ''), matched_address=hit.get('address', ''))
            output.append(item)
            continue
        street, house = street_house(row.get('address', ''))
        legacy_key = hashlib.sha256(f"{compact(row.get('city'))}|{compact(street)}|{house}".encode()).hexdigest()
        base_key = f'{street_kind(row["address"])}:{legacy_key}'
        if base_key not in cache and legacy_key in cache:
            cache[base_key] = cache[legacy_key]
        original_hit = exact_address_hit(row, cache.get(base_key, []))
        # A normal cache-only rerun must reuse a successful second pass, too.
        use_normalized = original_hit is None and (normalize_query or base_key + ':normalized_v1' in cache)
        key = base_key + (':normalized_v1' if use_normalized else '')
        query_street = street
        if use_normalized:
            query_street = re.sub(r'(?<!\w)[А-ЯІЇЄҐ]\.(?!\w)', '', street)
            query_street = re.sub(r'\b(?:ген|гетьм)\.?', '', query_street, flags=re.I).strip(' ,.')
        if key in cache:
            cache_hits += 1
        elif live and calls < max(0, limit) and street and house:
            response = requests.get(endpoint, params={'street': f'{house} {street_kind(row["address"])} {query_street}', 'city': row['city'],
                                    'countrycodes': 'ua', 'format': 'jsonv2', 'addressdetails': 1, 'limit': 3},
                                    headers={'User-Agent': user_agent}, timeout=30)
            response.raise_for_status()
            cache[key] = response.json()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(cache_path)
            calls += 1
            print(f'Geocoded address {calls}/{limit}', flush=True)
            time.sleep(1.1)
        found = exact_address_hit(row, cache.get(key, []))
        if found:
            item.update(latitude=str(found['lat']), longitude=str(found['lon']), reviewed='yes',
                        coordinate_source='nominatim_exact_address', match_reason='exact_street_house_locality',
                        matched_address=found.get('display_name', ''),
                        evidence=f"https://www.openstreetmap.org/{found.get('osm_type')}/{found.get('osm_id')}")
        else:
            item.update(latitude='', longitude='', match_reason='no_unique_exact_address')
        output.append(item)
    return output, {'records': len(rows), 'api_calls': calls, 'cache_hits': cache_hits,
                    'approved': sum(r['reviewed'] == 'yes' for r in output), 'db_writes': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--serp-cache', type=Path, action='append', default=[])
    parser.add_argument('--existing-points', type=Path, help='Read-only DB snapshot JSON with points; no credentials')
    parser.add_argument('--type-profiles', type=Path, help='GIS health GeoJSON: corroborate inpatient type, not coordinates')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--endpoint', default='')
    parser.add_argument('--user-agent', default='')
    parser.add_argument('--normalize-query', action='store_true', help='Explicit second pass for cached misses; preserves house/locality validation')
    args = parser.parse_args()
    rows, sources = [], []
    for path in args.file:
        records, report = load_registry(path)
        rows.extend(records)
        sources.append(report)
    if args.existing_points:
        snapshot = json.loads(args.existing_points.read_text(encoding='utf-8'))
        rows = reuse_existing(rows, snapshot['points'])
    if args.type_profiles:
        rows = apply_type_profiles(rows, args.type_profiles)
    rows.sort(key=lambda row: {'lviv_city_council_health': 0, ACCREDITATION_SOURCE: 1, REGIONAL_SOURCE: 2}[row['source']])
    enriched, report = enrich(rows, args.cache, live=args.live, limit=args.limit, endpoint=args.endpoint,
                              user_agent=args.user_agent, serp_caches=args.serp_cache, normalize_query=args.normalize_query)
    unique, duplicates = [], []
    for row in enriched:
        previous = next((old for old in unique if (old.get('source'), old['source_key']) == (row.get('source'), row['source_key']) or same_provider(old, row)), None)
        if previous:
            duplicates.append({'source': row['source'], 'source_key': row['source_key'], 'canonical_source': previous['source'], 'canonical_key': previous['source_key']})
            if previous.get('reviewed') != 'yes' and row.get('reviewed') == 'yes':
                previous.update({key: row.get(key, '') for key in ('latitude', 'longitude', 'coordinate_source', 'reviewed', 'match_reason', 'evidence')})
        else:
            # A renamed parent/branch from a second registry at the same address
            # may duplicate an existing location. Keep it visible for review,
            # while preserving distinct departments within the municipal source.
            collision = next((old for old in unique if old['source'] != row['source']
                              and old['sub_category'] == row['sub_category'] and same_address(old, row)), None)
            if collision and row.get('reviewed') == 'yes':
                row.update(reviewed='no', match_reason='cross_source_identity_needs_review')
            unique.append(row)
    write_csv(args.output, unique)
    report.update(sources=sources, unique_locations=len(unique), duplicates=duplicates,
                  approved=sum(r.get('reviewed') == 'yes' for r in unique),
                  skipped=dict(Counter(r.get('match_reason') for r in unique if r.get('reviewed') != 'yes')),
                  types=dict(Counter(r['sub_category'] for r in unique if r.get('reviewed') == 'yes')))
    args.output.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'sources','duplicates'}}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
