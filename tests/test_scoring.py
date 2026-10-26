import math

import pytest

from app.scoring import calculate_overall_rating, normalize_weights


def test_weight_normalization():
    result = normalize_weights({"health": 2.0, "daily": 2.0})
    assert result == {"health": 0.5, "daily": 0.5}
    assert sum(result.values()) == pytest.approx(1.0)


def test_normalization_of_empty_weights():
    assert normalize_weights({"health": 0.0}) == {}
    assert normalize_weights({}) == {}


def test_weighted_geometric_mean():
    # System weights: health=1, study=0; user weights: health=1, study=2.
    # Combined weights are 2 and 2 -> 50/50 -> sqrt(100 * 25) == 50.
    result = calculate_overall_rating(
        {"health": 100, "study": 25},
        {"health": 1, "study": 2},
    )
    assert result == 50.0


def test_single_enabled_category():
    assert calculate_overall_rating({"health": 74}, {"health": 2}) == 74.0


def test_zero_weight_disables_low_scoring_category():
    assert calculate_overall_rating(
        {"health": 74, "daily": 0},
        {"health": 1, "daily": 0},
    ) == 74.0


def test_zero_score_in_enabled_category_makes_result_zero():
    assert calculate_overall_rating(
        {"health": 100, "daily": 0},
        {"health": 1, "daily": 1},
    ) == 0.0


def test_all_disabled_returns_zero():
    assert calculate_overall_rating({"health": 100}, {}) == 0.0
    assert calculate_overall_rating({"health": 100}, {"health": 0}) == 0.0


def test_missing_score_is_zero_as_in_existing_endpoint():
    assert calculate_overall_rating({}, {"health": 1}) == 0.0
    assert calculate_overall_rating({"health": None}, {"health": 1}) == 0.0


@pytest.mark.parametrize("bad", [-0.1, 5.1, math.nan, math.inf])
def test_invalid_user_weights_rejected(bad):
    with pytest.raises(ValueError):
        calculate_overall_rating({"health": 50}, {"health": bad})


@pytest.mark.parametrize("bad", [-1, 101, math.nan, math.inf])
def test_invalid_scores_rejected(bad):
    with pytest.raises(ValueError):
        calculate_overall_rating({"health": bad}, {"health": 1})


def test_normalization_rejects_negative():
    with pytest.raises(ValueError):
        normalize_weights({"health": -1})
