"""Validate the existing point-type catalog without inventing scoring radii."""

import math

from sqlalchemy import select

from app.models import PointType


async def require_point_types(session, expected: dict[str, str]) -> None:
    result = await session.execute(select(PointType).where(PointType.type.in_(expected)))
    types = {point_type.type: point_type for point_type in result.scalars().all()}
    for name, category in expected.items():
        point_type = types.get(name)
        if point_type is None:
            raise ValueError(f"Missing point_types entry '{name}'; seed the catalog before running jobs")
        if point_type.category != category:
            raise ValueError(f"point_types '{name}' must belong to '{category}'")
        radii = (
            point_type.radius_of_influence_1,
            point_type.radius_of_influence_2,
            point_type.radius_of_influence_3,
        )
        if (any(radius is None or not math.isfinite(radius) for radius in radii)
                or not 0 < radii[0] <= radii[1] <= radii[2]):
            raise ValueError(f"point_types '{name}' needs finite radii with 0 < r1 <= r2 <= r3")
