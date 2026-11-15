import math
from collections import defaultdict

from geoalchemy2 import Geometry
from sqlalchemy import String, Float, cast, select, func

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
    # Main now computes mobility externally. Older databases may still carry its stored score.
    legacy_mobility = func.coalesce(cast(func.to_jsonb(HexTable.__table__.table_valued()).op("->>")("mobility"), Float), 0).label("mobility")
    columns = [getattr(HexTable, name) if name != "mobility" else legacy_mobility for name in SYSTEM_WEIGHTS]
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
        for name in names:
            transit = category == "mobility" and name == "gtfs_stop"
            conf = types.get(name)
            radii = effective_radii(name, category, conf)
            step = dict(category=category, type=name, radii=radii, score=0, used=[])
            if transit:
                ranked = [(distance(p), p) for p in stops]
                candidates = [(d,p) for d,p in ranked if d <= radii[2]]
                penalty = 1.0
                if ranked and not candidates:
                    candidates = [min(ranked, key=lambda pair: pair[0])]
                    penalty = 0.3
                reachable = {}
                for d, point in candidates:
                    w = calculate_distance_score(d, *radii) / 100
                    links = routes[point["id"]]
                    step["used"].append(dict(id=point["id"], kind="stop", distance=d, routes=links, weight=w))
                    for route in links:
                        for dest in destinations[route] - {point["id"]}:
                            reachable[dest] = max(reachable.get(dest, 0), w)
                raw = sum(reachable.values()) * penalty
                step.update(score=min(100, 100 * (math.log1p(raw) / math.log(20))),
                            penalty=penalty, destinations=reachable, network_score=raw)
            elif by_type[name]:
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
