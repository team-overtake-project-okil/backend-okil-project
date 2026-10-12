from pydantic import BaseModel, ConfigDict, Field
from typing import Any, Dict

class IndicatorBase(BaseModel):
    source: str
    external_id: str
    category: str
    h3_index: str

class IndicatorCreate(IndicatorBase):
    lat: float
    lng: float

class IndicatorResponse(IndicatorBase):
    id: int
    model_config = ConfigDict(from_attributes=True)

class HexagonStatBase(BaseModel):
    h3_index: str

class HexagonStatCreate(HexagonStatBase):
    stats: Dict[str, Any] = Field(default_factory=dict)

class HexagonStatResponse(HexagonStatBase):
    stats: Dict[str, Any]
    model_config = ConfigDict(from_attributes=True)