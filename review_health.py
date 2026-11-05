"""Generate one candidate-review spreadsheet from cached SerpAPI results.

Does not call SerpAPI or access PostgreSQL. From repository root:
python review_health.py candidates --input tmp/health-preview.csv --cache tmp/health-serp-cache.json
python review_health.py apply --input tmp/health-preview.csv --candidates tmp/health-candidates.csv --output tmp/health-reviewed.csv

For apply, fill selected=yes and category hospital/polyclinic only after manually verifying
source and candidate on map. All non-selected rows remain unapproved.
"""
import argparse
import csv
import hashlib
import json
import math
import re
from difflib import SequenceMatcher
from collections import defaultdict
from pathlib import Path


def read_csv(path):
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        out = csv.DictWriter(f, fieldnames=fields)
        out.writeheader()
        out.writerows({k:r.get(k,'') for k in fields} for r in rows)


def cache_key(row):
    return hashlib.sha256((row['name']+'|'+row['address']).encode('utf-8')).hexdigest()


def valid(lat, lon):
    try:
        a,b = float(lat),float(lon)
        return math.isfinite(a) and math.isfinite(b) and 49.65 <= a <= 50.02 and 23.65 <= b <= 24.35
    except (TypeError,ValueError):
        return False


def norm(text):
    return re.sub(r"[^\w]+", " ", str(text or "").casefold()).strip()


def street_house(address):
    """Conservative extraction: do not guess if format is unfamiliar."""
    match = re.search(
        r"(?:вулиця|вул|проспект|просп|площа|пл|провулок|пров)\s+"
        r"(.+?)\s+(\d+[а-яіїєґa-z]?)(?=\s|$)", norm(address)
    )
    if not match:
        return set(), ""
    street, house = match.groups()
    stop = {"івана", "володимира", "пилипа", "і", "п", "в", "гетьм", "львівська"}
    tokens = {w for w in street.split() if len(w) >= 5 and w not in stop}
    return tokens, house


def quality(source_name, source_address, found_name, found_address):
    """Sorting hint ONLY; never auto-approves a candidate."""
    streets, house = street_house(source_address)
    found = norm(found_address)
    numbers = set(re.findall(r"(?<!\w)\d+[а-яіїєґa-z]?(?!\w)", found))
    found_tokens = set(found.split())
    street_ok = bool(streets & found_tokens)
    house_ok = bool(house and house in numbers)
    name_score = SequenceMatcher(None, norm(source_name), norm(found_name)).ratio()
    # Exact street+house is essential for top priority; name only breaks ties.
    return ("address_match" if street_ok and house_ok else "manual_check", round(name_score, 3), round((100 if street_ok and house_ok else 0) + name_score * 30, 2))


def candidates(rows, caches):
    by_address=defaultdict(list)
    for row in rows:
        for cache in caches:
            if cache_key(row) in cache:
                by_address[row['address']].extend(cache[cache_key(row)])
    output=[]
    for row in rows:
        seen=set()
        for hit in by_address.get(row['address'], []):
            gps=hit.get('gps_coordinates') or {}
            lat,lon=gps.get('latitude'), gps.get('longitude')
            if not valid(lat,lon):
                continue
            identity=(str(hit.get('title','')).strip(),str(hit.get('address','')).strip(),str(lat),str(lon))
            if identity in seen:
                continue
            seen.add(identity)
            match_kind, similarity, rank_score = quality(row['name'], row['address'], identity[0], identity[1])
            output.append(dict(match_kind=match_kind, name_similarity=similarity, rank_score=rank_score, source_key=row['source_key'],source_row=row.get('source_row',''),source_name=row['name'],source_address=row['address'],suggested_category=row.get('sub_category',''),candidate_number=len(seen),matched_title=identity[0],matched_address=identity[1],latitude=identity[2],longitude=identity[3],selected='no',category='',notes=''))
    output.sort(key=lambda r: (int(r['source_row'] or 0), -float(r['rank_score'])))
    return output

FIELDS=['source_key','source_row','source_name','source_address','suggested_category','candidate_number','match_kind','name_similarity','rank_score','matched_title','matched_address','latitude','longitude','selected','category','notes']


def apply(rows, options):
    by_key={r['source_key']:r for r in rows}
    chosen={}
    for opt in options:
        if opt['selected'].strip().lower() != 'yes':
            continue
        key=opt['source_key']
        if key not in by_key:
            raise ValueError(f'Unknown source_key {key}')
        if key in chosen:
            raise ValueError(f'Multiple selected candidates for {key}')
        if opt['category'].strip() not in ('hospital','polyclinic'):
            raise ValueError(f'Missing valid category for selected {key}')
        if opt.get('match_kind') != 'address_match':
            raise ValueError(f'Selected candidate for {key} lacks verified street+house match; investigate manually before using this workflow')
        if not valid(opt['latitude'],opt['longitude']):
            raise ValueError(f'Invalid coords for {key}')
        # A selected candidate must still be checked by a person against the official source.
        chosen[key]=opt
    reviewed=[]
    for row in rows:
        r={**row, 'reviewed':'no', 'matched_title':'', 'matched_address':'', 'match_reason':'', 'latitude':'', 'longitude':''}
        if row['source_key'] in chosen:
            hit=chosen[row['source_key']]
            r.update(reviewed='yes',sub_category=hit['category'].strip(),matched_title=hit['matched_title'],matched_address=hit['matched_address'],match_reason='human_selected_candidate',latitude=hit['latitude'],longitude=hit['longitude'])
        reviewed.append(r)
    return reviewed,len(chosen)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='action',required=True)
    c=sub.add_parser('candidates'); c.add_argument('--input',type=Path,default=Path('tmp/health-preview.csv')); c.add_argument('--cache',type=Path,action='append',default=[]); c.add_argument('--output',type=Path,default=Path('tmp/health-candidates.csv'))
    a=sub.add_parser('apply'); a.add_argument('--input',type=Path,default=Path('tmp/health-preview.csv')); a.add_argument('--candidates',type=Path,default=Path('tmp/health-candidates.csv')); a.add_argument('--output',type=Path,default=Path('tmp/health-reviewed.csv'))
    args=p.parse_args()
    rows=read_csv(args.input)
    if args.action=='candidates':
        cache_paths=args.cache or [Path('tmp/health-serp-cache.json'),Path('tmp/health-serp-cache-v2.json')]
        caches=[json.loads(p.read_text(encoding='utf-8')) for p in cache_paths if p.exists()]
        opts=candidates(rows,caches)
        write_csv(args.output, opts,FIELDS)
        print(json.dumps({'source_rows':len(rows),'candidate_options':len(opts),'addresses_with_options':len(set(x['source_key'] for x in opts)),'api_calls':0,'db_writes':0,'output':str(args.output)},ensure_ascii=False,indent=2))
    else:
        opts=read_csv(args.candidates)
        approved,n=apply(rows,opts)
        fields=list(dict.fromkeys([*rows[0].keys(),'matched_title','matched_address','match_reason','reviewed']))
        write_csv(args.output,approved,fields)
        print(json.dumps({'source_rows':len(rows),'approved':n,'db_writes':0,'output':str(args.output)},ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
