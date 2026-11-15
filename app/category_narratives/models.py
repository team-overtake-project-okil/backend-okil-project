import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class TypeData(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    type_name: str = Field(min_length=1)
    num_objects_in_radius: StrictInt = Field(ge=0)
    closest_radius_m: float = Field(gt=0)
    nearest_point_dist_m: float | None = Field(ge=0)
    full_score_radius_m: float | None = Field(default=None, gt=0)
    score_basis: Literal["distance", "gtfs_network"] = "distance"

    @model_validator(mode="after")
    def consistent_distance(self):
        if (self.full_score_radius_m is not None
                and not math.isclose(self.full_score_radius_m, self.closest_radius_m, abs_tol=1e-8)):
            raise ValueError("Counts must refer to the full-score radius, not a different search radius")
        if self.nearest_point_dist_m is not None:
            inside = self.nearest_point_dist_m <= self.closest_radius_m
            if inside != (self.num_objects_in_radius > 0):
                raise ValueError(f"{self.type_name}: count and distance disagree")
        return self


class CategoryData(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    category_name: str = Field(min_length=1)
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    weight: float = Field(ge=0)
    types_data: list[TypeData]

    @model_validator(mode="after")
    def consistent_category(self):
        if self.score > self.max_score:
            raise ValueError("score exceeds max_score")
        names = [item.type_name for item in self.types_data]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate type names")
        return self
