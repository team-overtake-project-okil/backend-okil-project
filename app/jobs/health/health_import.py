"""Shared dry-run/write planner for Issue #34. No schema or rating changes."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path

from app.jobs.health_pipeline import H3_RES, SOURCE, normalized, read_csv, validate_reviewed


def distance_m(first: dict, second: dict) -> float:
    lat1, lon1 = float(first['latitude']), float(first['longitude'])
    lat2, lon2 = float(second['latitude']), float(second['longitude'])
    a, b = math.radians(lat1), math.radians(lat2)
    value = math.sin((a-b)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(lon1-lon2)/2)**2
    return 12742000 * math.asin(min(1, math.sqrt(value)))


def build_plan(rows: list[dict], hex_ids: set[str], health_types: set[str], existing: list[dict]) -> tuple[list[dict], dict]:
    import h3
    rows = validate_reviewed(rows)
    missing = {row['sub_category'] for row in rows} - health_types
    if missing:
        raise ValueError(f'Health point types need team configuration: {sorted(missing)}')
    by_key = {(r['source'], r['external_id']): r for r in existing}
    values, skipped, actions = [], Counter(), Counter()
    # Existing database keys remain canonical across sources and reruns.
    physical = list(existing)
    for row in rows:
        lat, lon = float(row['latitude']), float(row['longitude'])
        h_id = h3.latlng_to_cell(lat, lon, H3_RES)
        if h_id not in hex_ids:
            skipped['outside_existing_hex_grid'] += 1
            continue
        value = dict(source=row.get('source') or SOURCE, external_id=row['source_key'],
                     name=row['name'][:255], type=row['sub_category'], h3_id=h_id,
                     latitude=lat, longitude=lon)
        key = (value['source'], value['external_id'])
        old = by_key.get(key)
        if old is None:
            duplicate = next((point for point in physical if point['type'] == value['type']
                              # The OSM reader resolves node/area duplicates with
                              # address/contact evidence. Different stable IDs
                              # at one address can be different practitioners.
                              and not (point['source'] == value['source'] == 'openstreetmap_health')
                              and normalized(point['name']) == normalized(value['name'])
                              and distance_m(point, value) <= 80), None)
            if duplicate:
                skipped['existing_provider_other_identifier'] += 1
                continue
        elif (old['name'] == value['name'] and old['type'] == value['type']
              and old['h3_id'] == h_id and distance_m(old, value) < 0.5):
            actions['unchanged'] += 1
            continue
        actions['update' if old else 'insert'] += 1
        values.append(value)
        physical.append(value)
    return values, {'approved': len(rows), 'planned_inserts': actions['insert'],
                    'planned_updates': actions['update'], 'unchanged': actions['unchanged'],
                    'skipped': dict(skipped), 'planned_types': dict(Counter(v['type'] for v in values)),
                    'db_writes': 0}


def database_target(url) -> dict:
    target = {'host': url.host, 'database': url.database}
    target['target_fingerprint'] = hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()[:16]
    return target


UPSERT = """
INSERT INTO points (source,external_id,name,type,h3_id,geom)
VALUES (:source,:external_id,:name,:type,CAST(:h3_id AS h3index),
        ST_SetSRID(ST_MakePoint(:longitude,:latitude),4326)::geography)
ON CONFLICT (source,external_id) DO UPDATE SET
name=EXCLUDED.name,type=EXCLUDED.type,h3_id=EXCLUDED.h3_id,geom=EXCLUDED.geom
"""


async def run_import(path: Path, *, write_db=False, check_db=False, expected_target=None, env_file=None) -> dict:
    rows = validate_reviewed(read_csv(path))
    if not write_db and not check_db:
        # Offline dry-runs must never even import application DB configuration.
        return {'approved': len(rows), 'db_writes': 0, 'mode': 'offline_dry_run', 'grid_checked': False}
    from dotenv import dotenv_values
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    raw = os.environ.get('DB_URL') or dotenv_values(env_file or '.env').get('DB_URL')
    if not raw:
        raise ValueError('DB_URL is not configured')
    url = make_url(raw)
    target = database_target(url)
    if write_db and (not expected_target or expected_target != target['target_fingerprint']):
        raise ValueError('Database target differs from the explicitly approved fingerprint')
    # No application-level echo=True engine, and no credential-bearing URL logs.
    engine = create_async_engine(url.set(drivername='postgresql+asyncpg', query={}), echo=False,
                                 connect_args={'ssl': 'require'}, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                if not write_db:
                    await connection.execute(text('SET TRANSACTION READ ONLY'))
                else:
                    # Serialize cooperating health importers; unrelated jobs remain free.
                    await connection.execute(text("SELECT pg_advisory_xact_lock(340034)"))
                type_rows = (await connection.execute(text("""
                    SELECT type,radius_of_influence_1 r1,radius_of_influence_2 r2,radius_of_influence_3 r3
                    FROM point_types WHERE category='health'
                """))).mappings().all()
                types = {r['type'] for r in type_rows if all(
                    r[key] is not None and math.isfinite(r[key]) and r[key] > 0 for key in ('r1','r2','r3'))}
                hex_ids = set((await connection.execute(text('SELECT h3_id::text FROM hex_table'))).scalars())
                existing = [dict(r) for r in (await connection.execute(text("""
                    SELECT p.source,p.external_id,p.name,p.type,p.h3_id::text,
                           ST_Y(p.geom::geometry) latitude,ST_X(p.geom::geometry) longitude
                    FROM points p JOIN point_types t ON p.type=t.type WHERE t.category='health'
                """))).mappings()]
                # Keys owned by another category may not be overwritten either.
                occupied = {(r.source, r.external_id) for r in (await connection.execute(text("""
                    SELECT p.source,p.external_id FROM points p JOIN point_types t ON p.type=t.type
                    WHERE t.category<>'health'
                """)))}
                if any((r.get('source') or SOURCE, r['source_key']) in occupied for r in rows):
                    raise ValueError('Import identifier collides with a non-health point')
                values, report = build_plan(rows, hex_ids, types, existing)
                report.update(target=target, grid_checked=True, existing_health_points=len(existing),
                              existing_types=dict(Counter(r['type'] for r in existing)),
                              mode='write_db' if write_db else 'database_dry_run')
                if write_db:
                    if not rows:
                        raise ValueError('No approved healthcare rows')
                    for start in range(0, len(values), 200):
                        await connection.execute(text(UPSERT), values[start:start+200])
                    report['db_writes'] = len(values)
                    report['after_counts'] = [dict(r) for r in (await connection.execute(text("""
                        SELECT p.source,p.type,count(*)::int count FROM points p
                        JOIN point_types t ON p.type=t.type WHERE t.category='health'
                        GROUP BY p.source,p.type ORDER BY p.source,p.type
                    """))).mappings()]
            return report
    finally:
        await engine.dispose()
