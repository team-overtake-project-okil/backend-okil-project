import secrets
import hashlib
import time
from threading import Lock
from typing import Annotated, Literal

import h3
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials, HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, FiniteFloat
from starlette.concurrency import run_in_threadpool

from app.category_calculation import aggregate, components, load_inputs, load_hexes, get_distance_meters
from app.mobility_score_service import get_mobility_scores
from app.config import settings
from app.database import get_async_db
from app.scoring import SYSTEM_WEIGHTS, calculate_category_ratings, calculate_overall_rating
from app.models import PointType, Point, Stop
from app.schemas import HexagonWeights
from app.category_calculation import effective_radii
from sqlalchemy import select, cast, String, literal, union_all, or_
from geoalchemy2 import Geometry


SESSION_SECONDS = 15 * 60
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_BLOCK_SECONDS = 15 * 60
MAX_LOGIN_FAILURES = 5
sessions: dict[str, float] = {}
login_attempts: dict[str, tuple[list[float], float]] = {}
session_lock = Lock()


def valid_basic(credentials):
    return credentials is not None and (
        secrets.compare_digest(credentials.username.encode(), settings.DEBUG_USER.encode()) &
        secrets.compare_digest(credentials.password.encode(), settings.DEBUG_PASSWORD.encode())
    )


def token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def require_debug_transport(request: Request):
    if not settings.DEBUG_ENABLED:
        raise HTTPException(404)
    local = (settings.DEBUG_ALLOW_LOCAL_HTTP and request.client is not None and request.client.host in {"127.0.0.1", "::1"}
             and request.url.hostname in {"localhost", "127.0.0.1", "::1"})
    if settings.DEBUG_REQUIRE_HTTPS and request.url.scheme != "https" and not local:
        raise HTTPException(403, "HTTPS required")


def authorize(bearer: HTTPAuthorizationCredentials = Depends(HTTPBearer(auto_error=False))):
    if not settings.DEBUG_ENABLED:
        raise HTTPException(404)
    if bearer:
        key = token_key(bearer.credentials)
        with session_lock:
            expires_at = sessions.get(key, 0)
            if expires_at > time.time():
                return
            sessions.pop(key, None)
    raise HTTPException(401, headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})


router = APIRouter(prefix="/debug", dependencies=[Depends(require_debug_transport)])
protected_router = APIRouter(dependencies=[Depends(authorize)])
Weight = Annotated[FiniteFloat, Field(ge=0, le=5)]
defaults = dict.fromkeys(SYSTEM_WEIGHTS, 1)


@router.post("/session")
def create_session(request: Request, response: Response,
                   credentials: HTTPBasicCredentials = Depends(HTTPBasic(auto_error=False))):
    if not settings.DEBUG_ENABLED:
        raise HTTPException(404)
    now = time.time()
    client = request.client.host if request.client else "unknown"
    with session_lock:
        # Expire inactive entries so failed attempts do not accumulate indefinitely.
        for address, (failures, blocked_until) in list(login_attempts.items()):
            if blocked_until <= now and (not failures or failures[-1] <= now - LOGIN_WINDOW_SECONDS):
                del login_attempts[address]
        failures, blocked_until = login_attempts.get(client, ([], 0))
        if blocked_until > now:
            raise HTTPException(429, "Too many login attempts", headers={
                "Retry-After": str(max(1, int(blocked_until - now + .999))), "Cache-Control": "no-store"})
        failures = [attempt for attempt in failures if attempt > now - LOGIN_WINDOW_SECONDS]
        if not valid_basic(credentials):
            failures.append(now)
            blocked_until = now + LOGIN_BLOCK_SECONDS if len(failures) >= MAX_LOGIN_FAILURES else 0
            login_attempts[client] = (failures, blocked_until)
            if blocked_until:
                raise HTTPException(429, "Too many login attempts", headers={
                    "Retry-After": str(LOGIN_BLOCK_SECONDS), "Cache-Control": "no-store"})
            raise HTTPException(401, "Invalid credentials", headers={
                "WWW-Authenticate": "Basic", "Cache-Control": "no-store"})
        login_attempts.pop(client, None)
        token = secrets.token_urlsafe(32)
        expires_at = now + SESSION_SECONDS
        for expired in [key for key, expiry in sessions.items() if expiry <= now]:
            del sessions[expired]
        sessions[token_key(token)] = expires_at
    response.headers["Cache-Control"] = "no-store"
    return {"token": token, "expires_at": expires_at}


class Experiment(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    selected: str | None = None
    types: dict[str, Weight] = Field(default_factory=dict)
    categories: dict[str, Weight] = Field(default_factory=dict)


class AreaPoints(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    category: str | None = None


def point_records():
    queries = []
    for model, kind in ((Point, "poi"), (Stop, "stop")):
        geom = model.geom.cast(Geometry)
        queries.append(select(
            model.id, model.name, model.type, model.source, model.external_id,
            cast(model.h3_id, String).label("h3_id"), PointType.category,
            geom.ST_Y().label("lat"), geom.ST_X().label("lon"),
            literal(kind).label("kind"),
        ).outerjoin(PointType, model.type == PointType.type))
    return union_all(*queries).subquery()


@protected_router.get("/points/find")
async def find_point(id: str = Query(min_length=1, max_length=255),
                     kind: Literal["poi", "stop"] = "poi", db=Depends(get_async_db)):
    records = point_records()
    matches = records.c.external_id == id
    if id.isascii() and id.isdigit() and int(id) <= 2147483647:
        matches = or_(matches, records.c.id == int(id))
    query = select(records).where(records.c.kind == kind, matches).order_by(records.c.id)
    rows = [dict(row) for row in (await db.execute(query)).mappings()]
    for row in rows:
        row["h3_id"] = h3.latlng_to_cell(row["lat"], row["lon"], 9)
    return {"rows": rows}


@protected_router.get("/points")
async def all_points(category: str | None = None, type: str | None = None,
                     offset: int = Query(0, ge=0), limit: int = Query(1000, ge=1, le=1000),
                     db=Depends(get_async_db)):
    records = point_records()
    query = select(records)
    if category:
        query = query.where(records.c.category == category)
    if type:
        query = query.where(records.c.type == type)
    query = query.order_by(records.c.kind, records.c.id).offset(offset).limit(limit + 1)
    rows = [dict(row) for row in (await db.execute(query)).mappings()]
    return {"rows": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None}


@protected_router.post("/points")
async def area_points(request: AreaPoints, db=Depends(get_async_db)):
    for cell_id in request.ids:
        check_id(cell_id)
    rows = []
    for model, kind in ((Point, "poi"), (Stop, "stop")):
        geom = model.geom.cast(Geometry)
        query = select(model.id, model.name, model.type, model.source, model.external_id,
                       cast(model.h3_id, String).label("h3_id"), PointType.category,
                       geom.ST_Y().label("lat"), geom.ST_X().label("lon")).join(
                           PointType, model.type == PointType.type).where(
                               cast(model.h3_id, String).in_(request.ids)).order_by(model.id)
        if request.category:
            query = query.where(PointType.category == request.category)
        rows.extend(dict(row, kind=kind) for row in (await db.execute(query)).mappings())
    return {"rows": rows, "total": len(rows)}


@protected_router.get("/config")
async def config(db=Depends(get_async_db)):
    types = (await db.scalars(select(PointType).order_by(PointType.type))).all()
    return {"types": sorted({t.type for t in types} | {"gtfs_stop"}), "categories": list(defaults),
            "point_types": [dict(type=t.type, category=t.category, radii=[t.radius_of_influence_1,
                t.radius_of_influence_2, t.radius_of_influence_3],
                effective_radii=effective_radii(t.type, t.category, t)) for t in types],
            "system_weights": SYSTEM_WEIGHTS,
            "batch_limit": 200, "default_weight": 1, "weight_range": [0, 5]}


def mobility_options(
    transportation: str = Query(default="bus"),
    anchor_lat: list[float] | None = Query(default=None),
    anchor_lon: list[float] | None = Query(default=None),
    weight: list[float] | None = Query(default=None),
):
    anchors = None
    if anchor_lat is not None or anchor_lon is not None or weight is not None:
        if not anchor_lat or not anchor_lon or len(anchor_lat) != len(anchor_lon):
            raise HTTPException(400, "anchor_lat and anchor_lon must match")
        anchor_weights = weight or [1.0] * len(anchor_lat)
        if len(anchor_weights) != len(anchor_lat):
            raise HTTPException(400, "Provide one weight per anchor")
        anchors = list(zip(anchor_lat, anchor_lon, anchor_weights))
    return transportation, anchors


async def current_mobility(cells, options, enabled=True):
    scores = {}
    if enabled:
        try:
            scores = await get_mobility_scores([c["h3_id"] for c in cells], *options)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, "TravelTime API request failed") from exc
    return [dict(cell, mobility=scores.get(cell["h3_id"], 0.0)) for cell in cells]


@protected_router.get("/ratings")
async def ratings(weights: Annotated[HexagonWeights, Query()], db=Depends(get_async_db),
                  options=Depends(mobility_options)):
    cells = await current_mobility(await load_hexes(db), options, weights.mobility > 0)
    return [dict(h3_id=cell["h3_id"], overall_rating=calculate_overall_rating(cell, weights.model_dump()),
                 category_scores={key: cell[key] or 0 for key in SYSTEM_WEIGHTS}) for cell in cells]


def check_id(value):
    if not h3.is_valid_cell(value):
        raise HTTPException(422, "Invalid H3 cell")


def evaluate(cell, inputs, request, detail):
    steps = components(cell, inputs)
    fresh, baseline = aggregate(steps, {})
    experiment, trace = aggregate(steps, request.types)
    fresh["mobility"] = experiment["mobility"] = cell["mobility"]
    weights = defaults | request.categories
    result = {"h3_id": cell["h3_id"], "stored": calculate_overall_rating(cell, defaults),
              "fresh": calculate_overall_rating(fresh, defaults),
              "experiment": calculate_overall_rating(experiment, weights),
              "category_scores": {"stored": {key: cell.get(key) or 0 for key in SYSTEM_WEIGHTS},
                                  "fresh": {key: fresh.get(key, 0) for key in SYSTEM_WEIGHTS},
                                  "experiment": {key: experiment.get(key, 0) for key in SYSTEM_WEIGHTS}}}
    if detail:
        result.update(steps=trace, baseline_steps=baseline, categories={
            "stored": calculate_category_ratings(cell, defaults),
            "fresh": calculate_category_ratings(fresh, defaults),
            "experiment": calculate_category_ratings(experiment, weights)})
    return result


@protected_router.post("/recalculate")
async def recalculate(request: Experiment, db=Depends(get_async_db), options=Depends(mobility_options)):
    for cell_id in request.ids:
        check_id(cell_id)
    cells = await load_hexes(db, request.ids)
    if len(cells) != len(set(request.ids)):
        raise HTTPException(404, "Unknown cell")
    cells = await current_mobility(cells, options)
    inputs = await load_inputs(db)
    if request.types.keys() - (set(inputs[0]) | {"gtfs_stop"}) or request.categories.keys() - defaults.keys():
        raise HTTPException(422, "Unknown weight")
    return await run_in_threadpool(lambda: [evaluate(c, inputs, request, c["h3_id"] == request.selected) for c in cells])


@protected_router.get("/hexagons/{cell_id}")
async def detail(cell_id: str, db=Depends(get_async_db), options=Depends(mobility_options)):
    return (await recalculate(Experiment(ids=[cell_id], selected=cell_id), db, options))[0]


@protected_router.get("/hexagons/{cell_id}/points")
async def points(cell_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
                 location: Literal["inside", "outside"] | None = None, used: bool | None = None,
                 category: str | None = None,
                 db=Depends(get_async_db)):
    check_id(cell_id)
    cells = await load_hexes(db, [cell_id])
    if not cells:
        raise HTTPException(404)
    inputs = await load_inputs(db)
    cell = cells[0]
    _, steps = aggregate(await run_in_threadpool(components, cell, inputs), {})
    candidates = {(p["kind"], p["id"]): s for s in steps for p in s["used"]}
    rows = []
    for p in inputs[2]:
        actual = h3.latlng_to_cell(p["lat"], p["lon"], h3.get_resolution(cell_id))
        inside = actual == cell_id
        step = candidates.get((p["kind"], p["id"]))
        if not (inside or p["h3_id"] == cell_id or step):
            continue
        if location and (location == "inside") != inside or used is not None and used != bool(step):
            continue
        config = inputs[0].get(p["type"])
        if category is not None and (config.category if config else None) != category:
            continue
        rows.append(dict(p, inside=inside, mismatch=actual != p["h3_id"], used=bool(step),
                         category=config.category if config else None,
                         distance=get_distance_meters(cell["lat"],cell["lon"],p["lat"],p["lon"]),
                         contribution=next(item["score"] for item in step["used"] if item["id"] == p["id"]) * step["weight"] / len(step["used"]) if step and p["kind"] == "poi" else None,
                         weight=step["weight"] if step else 0))
    rows.sort(key=lambda point: (point["category"] or "", point["distance"], point["kind"], point["id"]))
    return {"rows": rows[offset:offset + limit], "total": len(rows)}



router.include_router(protected_router)
