"""Reusable overall comfort score for Okil (issue #18).

Agreed formula from the team screenshot:
    W_i = w_sys,i + w_user,i
    W_norm,i = W_i / sum(W_j)
    S = product(score_i ** W_norm,i)

PROPOSED, NOT YET TEAM-APPROVED POLICY:
- The query parameter weight_i is in [0, 5].
- weight_i == 0 disables the category entirely, including its system weight.
- If every category is disabled, S = 0.0.
- Missing/NULL category scores are interpreted as 0.0, like the old route.
- The old critical thresholds are treated as system weights until agreed otherwise.
"""

import math
from collections.abc import Mapping

SYSTEM_WEIGHTS = {
    "health": 1.0,
    "study": 0.0,
    "daily": 0.5,
    "mobility": 0.5,
    "sport": 0.0,
    "enjoyment": 0.0,
    "security": 1.0,
}


def normalize_weights(weights: Mapping[str, float]) -> dict[str, float]:
    """Return positive weights scaled to sum to 1; return {} for no active weights."""
    total = 0.0
    for weight in weights.values():
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("Weights must be finite and non-negative")
        total += weight

    if total == 0:
        return {}
    return {category: weight / total for category, weight in weights.items() if weight > 0}


def calculate_overall_rating(
    category_scores: Mapping[str, float | None],
    user_weights: Mapping[str, float],
) -> float:
    """Calculate the overall rating on the same 0..100 scale as category scores.

    Passing weight 0 disables a category; missing user weight also means disabled.
    """
    combined_weights: dict[str, float] = {}

    for category, system_weight in SYSTEM_WEIGHTS.items():
        user_weight = float(user_weights.get(category, 0.0))
        if not math.isfinite(user_weight) or not 0.0 <= user_weight <= 5.0:
            raise ValueError(f"Invalid user weight for {category}: expected 0..5")
        if user_weight == 0:
            continue
        combined_weights[category] = system_weight + user_weight

    normalized_weights = normalize_weights(combined_weights)
    if not normalized_weights:
        return 0.0

    rating = 1.0
    for category, normalized_weight in normalized_weights.items():
        raw_score = category_scores.get(category)
        score = 0.0 if raw_score is None else float(raw_score)
        if not math.isfinite(score) or not 0.0 <= score <= 100.0:
            raise ValueError(f"Invalid category score for {category}: expected 0..100")
        if score == 0.0:
            return 0.0
        rating *= score ** normalized_weight

    return round(rating, 2)
