"""Read counts and nearest distances in one query, without computing scores."""
import math

from sqlalchemy import text

# Same local straight-line approximation as the existing Lviv scoring job.
METERS_PER_LAT = 111139.0
METERS_PER_LON = METERS_PER_LAT * math.cos(math.radians(49.84))
DISTANCE = """sqrt(
    power((ST_Y(o.geom::geometry) - h.lat) * :meters_lat, 2) +
    power((ST_X(o.geom::geometry) - h.lon) * :meters_lon, 2)
)"""
ORIGIN = """SELECT ST_Y(geom::geometry) lat, ST_X(geom::geometry) lon
             FROM hex_table WHERE h3_id::text = :h3_id"""


async def fetch_type_accessibility(db, h3_id):
    # Count within r1, not within an unrelated larger search radius.
    statement = text(f"""
        WITH origin AS ({ORIGIN}), configs AS (
            SELECT type, category, radius_of_influence_1 AS r1
            FROM point_types
        ), objects AS (
            SELECT type, geom FROM points WHERE type <> 'gtfs_stop'
            UNION ALL
            SELECT 'gtfs_stop' AS type, geom FROM stops
        )
        SELECT pt.type, pt.category, pt.r1,
               stats.nearest_point_dist_m, stats.num_objects_in_radius
        FROM configs pt CROSS JOIN origin h
        LEFT JOIN LATERAL (
            SELECT min(d.distance_m) nearest_point_dist_m,
                   count(*) FILTER (WHERE d.distance_m <= pt.r1) num_objects_in_radius
            FROM (SELECT {DISTANCE} distance_m FROM objects o WHERE o.type = pt.type) d
        ) stats ON TRUE
        ORDER BY pt.category, pt.type
    """)
    result = await db.execute(statement, {
        "h3_id": h3_id, "meters_lat": METERS_PER_LAT, "meters_lon": METERS_PER_LON,
    })
    return [dict(row) for row in result.mappings().all()]
