"""Prepare named public/private health POIs from an offline OpenStreetMap extract.

No network calls or database writes. OSM node positions and points inside valid
provider polygons supply coordinates; unnamed/closed/ambiguous objects stay in
the audit. Reuse health_pipeline import for database validation and upserts.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from app.jobs.health_import import distance_m
from app.jobs.health_pipeline import has_valid_coords, read_csv, write_csv
from app.jobs.health_sources import compact, house_key, street_house, street_matches

SOURCE = 'openstreetmap_health'
ATTRIBUTION = '© OpenStreetMap contributors, ODbL: https://www.openstreetmap.org/copyright'
GENERIC_WORDS = {'стоматологія','стоматолог','стоматологічний','стоматологічна','кабінет',
                 'клініка','клініки','медичний','медична','центр','лікарня','поліклініка',
                 'приватна','сучасної','сучасна','клінічний','клінічна','львів','львова',
                 'львівська','львівський','обласний','обласна','комунальна','міська',
                 'некомерційне','підприємство','комунальне','товариство','ради','кнп','лор'}


def name_identity(name: str) -> str:
    text = re.sub(r'[^\w]+',' ',name.casefold()).strip()
    words = [word for word in text.split() if word not in GENERIC_WORDS]
    return ''.join(words)


def classification(tags: dict) -> str:
    name = str(tags.get('name:uk') or tags.get('name') or tags.get('official_name') or tags.get('name:en') or tags.get('brand') or tags.get('operator') or '')
    text = (name + ' ' + str(tags.get('website') or tags.get('contact:website') or '')).casefold()
    if (tags.get('amenity') == 'veterinary' or 'ветеринар' in text
            or tags.get('healthcare') in {'laboratory','alternative','pharmacy','optometrist','blood_donation'}
            or re.search(r'лаборатор|ескулаб|esculab|synevo|сінево|csdlab|\bcsd\b|\bdila\b|\bділа\b|\bмедіс\b|unilab|унілаб',text)):
        return ''
    amenity, healthcare = tags.get('amenity'), tags.get('healthcare')
    if amenity == 'dentist' or healthcare == 'dentist' or re.search('стоматолог|dental|dentistry',text):
        return 'dentistry'
    if re.search('поліклінік|амбулатор',text):
        return 'polyclinic'
    if healthcare in {'clinic','doctor','doctors'}:
        return 'polyclinic'
    if healthcare == 'hospital' or amenity == 'hospital':
        return 'hospital'
    if amenity in {'clinic','doctors'}:
        return 'polyclinic'
    return ''


def inactive(tags: dict, as_of: date) -> bool:
    if any(str(tags.get(key,'')).lower() in {'yes','true','1','closed','disused','abandoned'}
           for key in ('disused','abandoned','demolished','construction','proposed','status','operational_status')):
        return True
    if tags.get('opening_hours') in {'closed','off'}:
        return True
    end = tags.get('end_date')
    if end:
        try:
            return date.fromisoformat(end) < as_of
        except ValueError:
            return True  # uncertain closure dates need review
    return False


def read_features(path: Path):
    import osmium
    factory = osmium.geom.GeoJSONFactory()
    processor = osmium.FileProcessor(path).with_areas().with_filter(osmium.filter.KeyFilter('amenity','healthcare'))
    for obj in processor:
        tags = dict(obj.tags)
        # Restrict the source to medical objects, not every named business.
        if tags.get('amenity') not in {'hospital','clinic','doctors','dentist'} and tags.get('healthcare') not in {
                'hospital','clinic','doctor','doctors','dentist','centre','rehabilitation','physiotherapist','laboratory'}:
            continue
        if obj.is_node():
            geometry = {'type':'Point','coordinates':[obj.lon,obj.lat]} if obj.location.valid() else None
            kind, identifier = 'node', obj.id
        elif obj.is_area():
            kind, identifier = ('way' if obj.from_way() else 'relation'), obj.orig_id()
            try:
                geometry = json.loads(factory.create_multipolygon(obj))
            except RuntimeError:
                geometry = None
        else:
            continue  # closed ways/relations are handled once, as assembled areas
        yield {'kind':kind,'id':identifier,'tags':tags,'geometry':geometry,'updated_at':str(obj.timestamp)}


def inherit_building_addresses(features: list[dict], path: Path) -> int:
    """Fill only missing address fields from an unambiguous containing building.

    Do not copy its provider name, contacts or ownership to unrelated tenants.
    Multiple address alternatives remain unresolved; POI coordinates do not move.
    """
    import osmium
    from shapely.geometry import shape
    from shapely.strtree import STRtree
    from app.jobs.health_sources import house_key,street_matches
    targets,points=[],[]
    for feature in features:
        if feature['tags'].get('addr:street') and feature['tags'].get('addr:housenumber'):
            continue
        try:
            geometry=shape(feature['geometry'])
            if geometry.is_empty or not geometry.is_valid: continue
            point=geometry if geometry.geom_type=='Point' else geometry.representative_point()
        except (TypeError,ValueError,AttributeError):
            continue
        if has_valid_coords(point.y,point.x):
            targets.append(feature)
            points.append(point)
    if not points: return 0
    tree=STRtree(points)
    choices={}
    factory=osmium.geom.GeoJSONFactory()
    for area in osmium.FileProcessor(path).with_areas().with_filter(osmium.filter.KeyFilter('addr:street')):
        if not area.is_area(): continue
        tags=dict(area.tags)
        if not tags.get('building') or not tags.get('addr:housenumber'): continue
        try:
            geometry=shape(json.loads(factory.create_multipolygon(area)))
            if not geometry.is_valid: continue
        except (TypeError,ValueError,RuntimeError):
            continue
        for index in tree.query(geometry,predicate='covers'):
            target=targets[int(index)]['tags']
            if target.get('addr:street') and not street_matches(target['addr:street'],tags['addr:street']): continue
            if target.get('addr:housenumber') and house_key(target['addr:housenumber'])!=house_key(tags['addr:housenumber']): continue
            identity=(compact(tags['addr:street']),house_key(tags['addr:housenumber']))
            choices.setdefault(int(index),{})[identity]=(tags,f"https://www.openstreetmap.org/{'way' if area.from_way() else 'relation'}/{area.orig_id()}")
    filled=0
    for index,options in choices.items():
        if len(options)!=1: continue
        tags,url=next(iter(options.values()))
        feature=targets[index]
        for key in ('addr:street','addr:housenumber','addr:city','addr:postcode'):
            if not feature['tags'].get(key) and tags.get(key): feature['tags'][key]=tags[key]
        feature['address_source']=url
        filled+=1
    return filled


def to_record(feature: dict, hex_ids: set[str], as_of: date) -> tuple[dict | None,str]:
    import h3
    from shapely.geometry import shape
    tags = feature['tags']
    if inactive(tags,as_of):
        return None,'inactive'
    category = classification(tags)
    if not category:
        return None,'unapproved_medical_type'
    name = str(tags.get('name:uk') or tags.get('name') or tags.get('official_name') or tags.get('name:en') or tags.get('brand') or tags.get('operator') or '').strip()
    if not name:
        return None,'unnamed_provider'
    if category == 'hospital' and re.match(r'^(відділен|корпус|кабінет)',name,re.I):
        return None,'department_needs_identity_review'
    try:
        geometry = shape(feature['geometry'])
        if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in {'Point','Polygon','MultiPolygon'}:
            return None,'invalid_geometry'
        point = geometry if geometry.geom_type == 'Point' else geometry.representative_point()
    except (TypeError,ValueError,AttributeError):
        return None,'invalid_geometry'
    if not has_valid_coords(point.y,point.x):
        return None,'outside_lviv_bounds'
    h3_id = h3.latlng_to_cell(point.y,point.x,9)
    if h3_id not in hex_ids:
        return None,'outside_existing_hex_grid'
    street, house = str(tags.get('addr:street','')).strip(), str(tags.get('addr:housenumber','')).strip()
    city = str(tags.get('addr:city','')).strip()
    suffix_type=re.fullmatch(r'(.+)\s+(вулиця|проспект|площа|провулок|бульвар)',street,re.I)
    if suffix_type:
        street=f'{suffix_type.group(2)} {suffix_type.group(1)}'
    address = ', '.join(value for value in (city, ' '.join(value for value in (street,house) if value)) if value)
    website = str(tags.get('contact:website') or tags.get('website') or '')
    phone = str(tags.get('contact:phone') or tags.get('phone') or '')
    if not name_identity(name) and not (street and house or website or phone):
        return None,'generic_name_without_contact_or_address'
    key = f"{feature['kind']}/{feature['id']}"
    return dict(source=SOURCE,source_key=key,source_row=key,name=name,sub_category=category,
                org_type=tags.get('healthcare') or tags.get('amenity'),org_identifier='',
                address=address,city=city,latitude=str(point.y),longitude=str(point.x),
                h3_id=h3_id,reviewed='yes',status='source_coordinates',
                coordinate_source='osm_node' if geometry.geom_type=='Point' else 'osm_polygon_interior_point',
                match_reason='named_active_medical_object_in_existing_grid',
                evidence=f'https://www.openstreetmap.org/{key}',website=website,phone=phone,
                ownership=tags.get('operator:type') or tags.get('ownership') or '',
                source_updated_at=feature.get('updated_at',''),source_geometry=geometry.geom_type,
                address_source=feature.get('address_source',''),
                _geometry=geometry,_street=street,_house=house),''


def same_location(first: dict, second: dict) -> bool:
    a = dict(first, latitude=float(first['latitude']),longitude=float(first['longitude']))
    b = dict(second,latitude=float(second['latitude']),longitude=float(second['longitude']))
    if distance_m(a,b) <= 50:
        return True
    from shapely.geometry import Point
    return bool(first.get('_geometry') is not None and first['_geometry'].covers(Point(b['longitude'],b['latitude']))
                or second.get('_geometry') is not None and second['_geometry'].covers(Point(a['longitude'],a['latitude'])))


def same_institution(first: dict, second: dict) -> bool:
    if (first.get('sub_category') or first.get('type')) != (second.get('sub_category') or second.get('type')):
        return False
    if not same_location(first,second):
        return False
    # Two separate addresses of the same chain remain separate locations.
    a_street,a_house=street_house(first.get('address',''))
    b_street,b_house=street_house(second.get('address',''))
    a_street,a_house=first.get('_street') or a_street,first.get('_house') or a_house
    b_street,b_house=second.get('_street') or b_street,second.get('_house') or b_house
    if a_house and b_house and house_key(a_house)!=house_key(b_house): return False
    if a_street and b_street and not street_matches(a_street,b_street): return False
    a,b = name_identity(first['name']),name_identity(second['name'])
    if a and a == b:
        return True
    def phone_identity(row):
        numbers=[re.sub(r'\D','',part) for part in re.split(r'[;,/]',str(row.get('phone','')))]
        return {number[-9:] for number in numbers if len(number)>=9}
    phones = [phone_identity(r) for r in (first,second)]
    def website_identity(row):
        value=str(row.get('website','')).strip()
        if not value: return ''
        url=urlsplit(value if '://' in value else 'https://'+value)
        host=(url.hostname or '').lower().removeprefix('www.')
        return host+url.path.rstrip('/') if host in {'facebook.com','instagram.com','google.com','t.me'} else host
    websites=[website_identity(r) for r in (first,second)]
    return bool(phones[0] & phones[1] or websites[0] and websites[0]==websites[1]
                or len(a)>=4 and len(b)>=4 and (a in b or b in a)
                and (first.get('_geometry') is not None or second.get('_geometry') is not None))


def prepare(features, hex_ids: set[str], existing: list[dict], as_of: date,
            included_types=frozenset({'hospital','polyclinic','dentistry'})):
    accepted, audit, skipped = [], [], Counter()
    total = 0
    for feature in features:
        total += 1
        row, reason = to_record(feature,hex_ids,as_of)
        if row is None:
            skipped[reason] += 1
            audit.append(dict(source_key=f"{feature['kind']}/{feature['id']}",reason=reason))
            continue
        if row['sub_category'] not in included_types:
            skipped['outside_selected_types']+=1
            audit.append(dict(source_key=row['source_key'],name=row['name'],reason='outside_selected_types'))
            continue
        # Historical municipal names often differ from the current registry.
        # Keep potential duplicates for review; a nearby unrelated private brand
        # is not rejected just because it shares a building or coordinates.
        public_name=bool(re.search(r'комунальн|міськ|обласн|амбулатор|поліклінічне|відділен',row['name'],re.I))
        public_overlap=next((old for old in existing if public_name
                             and old.get('source')!=SOURCE
                             and old.get('type')==row['sub_category'] and distance_m(row,old)<=120),None)
        if public_overlap:
            skipped['municipal_identity_needs_review']+=1
            audit.append(dict(source_key=row['source_key'],name=row['name'],reason='municipal_identity_needs_review',
                              canonical_source=public_overlap['source'],canonical_id=public_overlap['external_id']))
            continue
        uncertain_colocation=next((old for old in existing if old.get('source')!=SOURCE
                                   and old.get('type')==row['sub_category']
                                   and not row['website'] and not row['phone']
                                   and distance_m(row,old)<=30),None)
        if uncertain_colocation:
            skipped['colocated_provider_identity_needs_review']+=1
            audit.append(dict(source_key=row['source_key'],name=row['name'],reason='colocated_provider_identity_needs_review',
                              canonical_source=uncertain_colocation['source'],canonical_id=uncertain_colocation['external_id']))
            continue
        duplicate = next((old for old in [*existing,*accepted]
                          if (old.get('source'),old.get('external_id') or old.get('source_key')) != (SOURCE,row['source_key'])
                          and same_institution(row,old)),None)
        if duplicate:
            reason = 'existing_or_repeated_institution'
            skipped[reason] += 1
            audit.append(dict(source_key=row['source_key'],name=row['name'],reason=reason,
                              canonical_source=duplicate.get('source'),canonical_id=duplicate.get('external_id') or duplicate.get('source_key')))
        else:
            accepted.append(row)
    return accepted,dict(source_objects=total,staged=len(accepted),types=dict(Counter(r['sub_category'] for r in accepted)),
                         skipped=dict(skipped),audit=audit,attribution=ATTRIBUTION,api_calls=0,db_writes=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file',type=Path,required=True)
    parser.add_argument('--snapshot',type=Path,required=True,help='Read-only DB JSON: points, hex_ids, types')
    parser.add_argument('--known-records',type=Path,action='append',default=[])
    parser.add_argument('--as-of',type=date.fromisoformat,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--types',nargs='+',choices=['hospital','polyclinic','dentistry'],default=['hospital','polyclinic','dentistry'])
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding='utf-8'))
    existing = snapshot['points']
    index = {(r['source'],r['external_id']):r for r in existing}
    for path in args.known_records:
        for row in read_csv(path):
            key=(row.get('source'),row.get('source_key'))
            if key in index:
                index[key].update({k:row.get(k,'') for k in ('address','city','website','phone')})
    hex_ids = {r['h3_id'] if isinstance(r,dict) else r for r in snapshot['hex_ids']}
    features=list(read_features(args.file))
    addresses_inherited=inherit_building_addresses(features,args.file)
    rows,report=prepare(features,hex_ids,existing,args.as_of,frozenset(args.types))
    report['addresses_inherited_from_buildings']=addresses_inherited
    write_csv(args.output,rows)
    with args.file.open('rb') as stream:
        report['source_file_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    args.output.with_suffix('.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='audit'},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
