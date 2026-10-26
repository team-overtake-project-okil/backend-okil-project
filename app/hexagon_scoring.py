import math


MINIMUM_WEIGHTS = {
    "health": 1.0,
    "study": 0.0,
    "daily": 0.5,
    "mobility": 0.5,
    "sport": 0.0,
    "enjoyment": 0.0,
    "security": 1.0,
}


def calculate_overall_rating(
    scores: dict[str, int | float],
    user_weights: dict[str, float],
) -> float:
    effective_weights = {}

    for category, minimum_weight in MINIMUM_WEIGHTS.items():
        requested_weight = user_weights.get(category, 1.0)
        effective_weights[category] = max(
            minimum_weight,
            requested_weight,
        )

    total_weight = sum(effective_weights.values())

    overall_rating = 1.0

    for category, weight in effective_weights.items():
        normalized_weight = weight / total_weight
        score = scores.get(category) or 0.0
        safe_score = max(1.0, float(score))

        overall_rating *= math.pow(
            safe_score,
            normalized_weight,
        )

    return min(100.0, round(overall_rating, 2))