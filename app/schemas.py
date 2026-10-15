from pydantic import BaseModel, ConfigDict
from typing import Optional


# --- HexTable ---
class HexTableBase(BaseModel):
    h3_id: str
    health: int = 0
    study: int = 0
    daily: int = 0
    mobility: int = 0
    sport: int = 0
    enjoyment: int = 0
    security: int = 0


class HexTableCreate(HexTableBase):
    lat: float
    lng: float


class HexTableResponse(HexTableBase):
    model_config = ConfigDict(from_attributes=True)


# --- PointType ---
class PointTypeBase(BaseModel):
    type: str
    category: str
    radius_of_influence_1: Optional[float] = None
    radius_of_influence_2: Optional[float] = None
    radius_of_influence_3: Optional[float] = None


class PointTypeCreate(PointTypeBase):
    pass


class PointTypeResponse(PointTypeBase):
    model_config = ConfigDict(from_attributes=True)


# --- Converter ---
class ConverterBase(BaseModel):
    placement: str


class ConverterCreate(ConverterBase):
    lat: float
    lng: float


class ConverterResponse(ConverterBase):
    placement_id: int
    model_config = ConfigDict(from_attributes=True)


# --- Point ---
class PointBase(BaseModel):
    h3_id: Optional[str] = None
    type: str
    placement_id: Optional[int] = None
    source: str
    external_id: str
    name: Optional[str] = None


class PointCreate(PointBase):
    pass


class PointResponse(PointBase):
    id: int
    converter_ref: Optional[ConverterResponse] = None
    model_config = ConfigDict(from_attributes=True)


# --- Stop ---
class StopBase(BaseModel):
    h3_id: Optional[str] = None
    type: str
    placement_id: Optional[int] = None
    source: str
    external_id: str


class StopCreate(StopBase):
    pass


class StopResponse(StopBase):
    id: int
    converter_ref: Optional[ConverterResponse] = None
    model_config = ConfigDict(from_attributes=True)


# --- Route ---
class RouteBase(BaseModel):
    source: str
    external_id: str
    name: str
    start_stop_id: Optional[int] = None
    end_stop_id: Optional[int] = None


class RouteCreate(RouteBase):
    pass


class RouteResponse(RouteBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


# --- StopRouteConnectable ---
class StopRouteConnectableBase(BaseModel):
    stop_id: int
    route_id: int
    stop_order: int


class StopRouteConnectableCreate(StopRouteConnectableBase):
    pass


class StopRouteConnectableResponse(StopRouteConnectableBase):
    id: int
    model_config = ConfigDict(from_attributes=True)