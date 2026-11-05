import pytest

from app.scoring import calculate_overall_rating


CATEGORIES = (
    "health",
    "study",
    "daily",
    "mobility",
    "sport",
    "enjoyment",
    "food",
    "security",
)


@pytest.mark.parametrize("score", [0, 80, 100])
def test_equal_scores(score):
    scores = {category: score for category in CATEGORIES}
    weights = {category: 1 for category in CATEGORIES}

    result = calculate_overall_rating(scores, weights)

    assert result == float(score)


def test_zero_study_weight_excludes_study_score():
    scores = {category: 80 for category in CATEGORIES}
    scores["study"] = 0

    weights = {category: 1 for category in CATEGORIES}
    weights["study"] = 0

    result = calculate_overall_rating(scores, weights)

    assert result == 80.0


def test_all_zero_weights_disable_all_categories():
    scores = {category: 25 for category in CATEGORIES}
    scores["health"] = 100
    weights = {category: 0 for category in CATEGORIES}

    result = calculate_overall_rating(scores, weights)

    assert result == 0.0