from collections import defaultdict

from geoalchemy2 import Geometry
from sqlalchemy import String, cast, select, func

from app.models import HexTable, Point, PointType, Stop, StopRouteConnectable
from app.scoring import SYSTEM_WEIGHTS

from app.jobs.count_category_ratings import (
    get_distance_meters, calculate_distance_score, get_radius, TOP_N_POINTS,
)
from app.rounding import round_final


async def load_inputs(session):
    types = {row.type: row for row in (await session.scalars(select(PointType))).all()}
    groups = defaultdict(list)
    for item in types.values():
        groups[item.category].append(item.type)
    groups["mobility"] = [t for t in groups["mobility"] if t != "gtfs_stop"] + ["gtfs_stop"]
    records = []
    by_type = defaultdict(list)
    destinations = defaultdict(set)
    routes = defaultdict(list)
    for route, stop in await session.execute(select(StopRouteConnectable.route_id, StopRouteConnectable.stop_id)):
        destinations[route].add(stop)
        routes[stop].append(route)
    stops = []
    for model, kind in ((Point, "poi"), (Stop, "stop")):
        geom = model.geom.cast(Geometry)
        query = select(model.id, model.name, model.type, model.source, model.external_id, cast(model.h3_id, String).label("h3_id"),
                       geom.ST_Y().label("lat"), geom.ST_X().label("lon")).order_by(model.id)
        for row in (await session.execute(query)).mappings():
            point = dict(row, kind=kind)
            records.append(point)
            if kind == "poi":
                by_type[point["type"]].append(point)
            else:
                stops.append(point)
    return types, groups, records, by_type, stops, routes, destinations


async def load_hexes(session, ids=None):
    geom = HexTable.geom.cast(Geometry)
    columns = [getattr(HexTable, name) for name in SYSTEM_WEIGHTS if name != "mobility"]
    query = select(cast(HexTable.h3_id, String).label("h3_id"), geom.ST_Y().label("lat"), geom.ST_X().label("lon"), *columns)
    if ids is not None:
        query = query.where(cast(HexTable.h3_id, String).in_(ids))
    return (await session.execute(query)).mappings().all()


def effective_radii(name, category, config):
    defaults = (200, 400, 700 if name == "gtfs_stop" else 800) if category == "mobility" else (300, 600, 1200)
    return [get_radius(config, f"radius_of_influence_{i}", default)
            for i, default in enumerate(defaults, 1)]


def components(cell, inputs):
    types, groups, _, by_type, stops, routes, destinations = inputs
    def distance(point):
        return get_distance_meters(cell["lat"], cell["lon"], point["lat"], point["lon"])
    steps = []
    for category, names in groups.items():
        if category == "mobility":
            continue  # TravelTime supplies this category; POIs and GTFS do not score it.
        for name in names:
            conf = types.get(name)
            radii = effective_radii(name, category, conf)
            step = dict(category=category, type=name, radii=radii, score=0, used=[])
            if by_type[name]:
                nearest = sorted(by_type[name], key=distance)[:TOP_N_POINTS]
                scores = [calculate_distance_score(distance(point), *radii) for point in nearest]
                step.update(score=sum(scores) / len(scores),
                            used=[dict(id=point["id"], kind="poi", distance=distance(point), score=score) for point, score in zip(nearest, scores)])
            steps.append(step)
    return steps


def aggregate(steps, weights):
    totals = defaultdict(float)
    scores = defaultdict(float)
    trace = []
    for step in steps:
        totals[step["category"]] += weights.get(step["type"], 1)
    for step in steps:
        category = step["category"]
        share = weights.get(step["type"], 1) / totals[category] if totals[category] else 0
        contribution = share * step["score"]
        scores[category] += contribution
        trace.append(dict(step, weight=share, contribution=contribution))
    return {name: round_final(value) for name, value in scores.items()}, trace
