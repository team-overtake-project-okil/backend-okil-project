import pytest

from app.hexagon_scoring import calculate_overall_rating


CATEGORIES = (
    "health",
    "study",
    "daily",
    "mobility",
    "sport",
    "enjoyment",
    "security",
)


@pytest.mark.parametrize(
    "score, expected",
    [
        (0, 1.0),
        (80, 80.0),
        (100, 100.0),
    ],
)
def test_equal_scores(score, expected):
    scores = {category: score for category in CATEGORIES}

    result = calculate_overall_rating(scores, {})

    assert result == expected


def test_zero_study_weight_excludes_study_score():
    scores = {category: 80 for category in CATEGORIES}
    scores["study"] = 0

    result = calculate_overall_rating(
        scores,
        {"study": 0},
    )

    assert result == 80.0


def test_all_zero_weights_keep_required_categories():
    scores = {category: 25 for category in CATEGORIES}
    scores["health"] = 100
    weights = {category: 0 for category in CATEGORIES}

    result = calculate_overall_rating(scores, weights)

    assert result == 39.69