import hashlib
from typing import Annotated, Literal
from uuid import RFC_4122, UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StringConstraints, field_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

router = APIRouter(prefix="/mobility/places")
bearer = HTTPBearer(auto_error=False)
TTL_SECONDS = 30 * 24 * 60 * 60


class Place(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    source: Literal["default", "address", "map", "preset"]
    address: str | None = Field(default=None, max_length=1024)


class MobilityPlaces(BaseModel):
    revision: int = Field(strict=True, ge=1, le=2**53 - 1)
    anchors: list[Place] = Field(max_length=5)
    selected_preset_id: str | None = Field(alias="selectedPresetId", max_length=64)
    personal_routes_enabled: bool = Field(alias="personalRoutesEnabled", strict=True)

    @field_validator("anchors")
    @classmethod
    def unique_anchors(cls, anchors: list[Place]) -> list[Place]:
        if len({anchor.id for anchor in anchors}) != len(anchors):
            raise ValueError("Place IDs must be unique")
        return anchors


def cache_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    token = credentials.credentials if credentials else ""
    try:
        identifier = UUID(token)
        if identifier.version == 4 and identifier.variant == RFC_4122 and str(identifier) == token:
            return "mobility:places:" + hashlib.sha256(token.encode("ascii")).hexdigest()
    except ValueError:
        pass
    raise HTTPException(401, "Invalid places token", headers={"WWW-Authenticate": "Bearer"})


def redis_client(request: Request) -> Redis:
    return request.app.state.redis


@router.get("", response_model_exclude_unset=True)
async def get_places(
    response: Response,
    key: Annotated[str, Depends(cache_key)],
    redis: Annotated[Redis, Depends(redis_client)],
) -> MobilityPlaces | None:
    response.headers["Cache-Control"] = "no-store"
    try:
        raw = await redis.get(key)
    except RedisError as error:
        raise HTTPException(503, "Places are temporarily unavailable") from error
    return MobilityPlaces.model_validate_json(raw) if raw else None


@router.put("", response_model_exclude_unset=True)
async def save_places(
    places: MobilityPlaces,
    response: Response,
    key: Annotated[str, Depends(cache_key)],
    redis: Annotated[Redis, Depends(redis_client)],
) -> MobilityPlaces:
    response.headers["Cache-Control"] = "no-store"
    try:
        for _ in range(5):
            try:
                async with redis.pipeline() as pipe:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if raw:
                        saved = MobilityPlaces.model_validate_json(raw)
                        if saved.revision >= places.revision:
                            return saved
                    pipe.multi()
                    pipe.set(
                        key,
                        places.model_dump_json(by_alias=True, exclude_unset=True),
                        ex=TTL_SECONDS,
                    )
                    await pipe.execute()
                    return places
            except WatchError:
                continue
    except RedisError as error:
        raise HTTPException(503, "Places are temporarily unavailable") from error
    raise HTTPException(503, "Places are busy. Try again.")
