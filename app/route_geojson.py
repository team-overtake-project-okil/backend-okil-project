"""
Збірка GeoJSON маршрутів для вибраного гексагона.
Тут лише чисті функції: без БД і без FastAPI, тому їх легко перевірити окремо.
"""
import json
from collections import defaultdict


def hex_boundary_to_wkt(boundary) -> str:
    """
    boundary: [(lat, lng), ...], як повертає h3.cell_to_boundary().
    Повертає WKT-полігон у порядку (lng lat) із замкненим кільцем.
    """
    ring = [(lng, lat) for lat, lng in boundary]
    if len(ring) < 3:
        raise ValueError("hex boundary must have at least 3 points")
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return "POLYGON((" + ", ".join(f"{lng} {lat}" for lng, lat in ring) + "))"


def _line_coords(geojson) -> list:
    """Координати з ST_AsGeoJSON (рядок або dict). Порожній список, якщо геометрії немає."""
    if not geojson:
        return []
    geom = json.loads(geojson) if isinstance(geojson, str) else geojson
    kind, coords = geom.get("type"), geom.get("coordinates")
    if kind == "LineString":
        return coords or []
    if kind == "MultiLineString":
        return [p for part in (coords or []) for p in part]
    return []


def _extend(target: list, points) -> None:
    """Додає точки в лінію, пропускаючи дубль на стику двох сегментів."""
    for p in points:
        pt = [round(p[0], 6), round(p[1], 6)]
        if not target or target[-1] != pt:
            target.append(pt)


def build_hex_routes_geojson(routes: list, segment_rows: list, fallback_points: list) -> dict:
    """
    routes:          [{"id", "name", "external_id"}]
    segment_rows:    [{"route_id", "sequence_order", "geojson"}]  сегменти маршрутів (geojson може бути None)
    fallback_points: [{"route_id", "stop_order", "lat", "lng"}]   зупинки, якщо в маршруту немає сегментів

    Повертає FeatureCollection: один LineString на маршрут (довжиною менше 2 точок пропускаємо).
    """
    segs = defaultdict(list)
    for r in segment_rows:
        if r.get("geojson"):
            segs[r["route_id"]].append((r["sequence_order"], r["geojson"]))

    stops = defaultdict(list)
    for p in fallback_points:
        if p["lat"] is not None and p["lng"] is not None:
            stops[p["route_id"]].append((p["stop_order"], p["lng"], p["lat"]))

    features = []
    for r in sorted(routes, key=lambda x: x["id"]):
        coords, source = [], "segments"
        for _, geojson in sorted(segs.get(r["id"], []), key=lambda x: x[0]):
            _extend(coords, _line_coords(geojson))

        if len(coords) < 2:
            coords, source = [], "stops"
            _extend(coords, [[lng, lat] for _, lng, lat in sorted(stops.get(r["id"], []))])
        if len(coords) < 2:
            continue

        # name має вигляд "А01: Початкова → Кінцева", external_id: "<route_id>_<напрямок>"
        number = r["name"].split(":", 1)[0].strip()
        ext = r["external_id"]
        direction = ext.rsplit("_", 1)[-1] if "_" in ext else None

        features.append({
            "type": "Feature",
            "properties": {
                "id": r["id"],
                "name": r["name"],
                "number": number,
                "direction": direction,
                "geometry_source": source,
            },
            "geometry": {"type": "LineString", "coordinates": coords},
        })

    return {"type": "FeatureCollection", "features": features}
