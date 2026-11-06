"""Read facts and render explanations without recalculating any scores."""
import math

from app.scoring import SYSTEM_WEIGHTS
from .config import CATEGORY_NAMES, TYPE_NAMES
from .generator import explain_category
from .repository import fetch_type_accessibility




def prepare_type(row):
    if row["r1"] is None:
        raise ValueError(f"Missing full-score radius for {row['type']}")
    radius = float(row["r1"])
    if not math.isfinite(radius) or radius <= 0:
        raise ValueError(f"Invalid full-score radius for {row['type']}")
    return {
        "type_name": TYPE_NAMES.get(row["type"], row["type"]),
        "num_objects_in_radius": int(row["num_objects_in_radius"] or 0),
        "closest_radius_m": radius,
        "nearest_point_dist_m": row["nearest_point_dist_m"],
        "score_basis": "gtfs_network" if row["type"] == "gtfs_stop" else "distance",
    }


def prepare_category_data(hexagon, rows, user_weights):
    grouped = {category: [] for category in SYSTEM_WEIGHTS}
    for row in rows:
        if row["category"] in grouped:
            grouped[row["category"]].append(prepare_type(row))
    return {
        category: {
            "category_name": CATEGORY_NAMES[category],
            "score": hexagon.get(category) or 0, "max_score": 100,
            "weight": user_weights.get(category, 0), "types_data": types,
        }
        for category, types in grouped.items()
    }


def generate_explanations(payloads):
    return {
        category: {"explanation": explain_category(payload)["explanation"]}
        for category, payload in payloads.items()
    }


async def fetch_hexagon_explanations(db, hexagon, user_weights):
    rows = await fetch_type_accessibility(db, hexagon["h3_id"])
    payloads = prepare_category_data(hexagon, rows, user_weights)
    return generate_explanations(payloads)
