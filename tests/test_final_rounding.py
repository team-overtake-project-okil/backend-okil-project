import pytest

from app.rounding import round_final
from app.scoring import calculate_category_ratings, calculate_overall_rating
from app.category_narratives.generator import explain_category, format_distance


@pytest.mark.parametrize("value,expected", [(0, 0), (35.36, 35), (88.49, 88), (88.5, 89), (99.5, 100)])
def test_final_scores_round_half_up(value, expected):
    assert round_final(value) == expected
    assert isinstance(round_final(value), int)


@pytest.mark.parametrize("distance,text", [(48.36, "48 м"), (88.5, "89 м"),
    (999.5, "1 км"), (1850, "1,85 км"), (1900, "1,9 км"), (1235, "1,24 км")])
def test_explanation_distances_are_rounded(distance, text):
    assert format_distance(distance) == text


def test_explanation_returns_final_integer_scores_without_recalculation():
    result = explain_category({"category_name": "Щоденні потреби", "score": 88.5,
        "max_score": 100, "weight": 1, "types_data": [
            {"type_name": "Аптеки", "num_objects_in_radius": 1,
             "closest_radius_m": 200, "nearest_point_dist_m": 150.5}]})
    assert result["score"] == 89
    assert "151 м" in result["explanation"]


def test_weights_and_raw_scores_keep_precision_until_final_result():
    ratings = calculate_category_ratings({"health": 88.5}, {"health": 1})
    health = next(row for row in ratings if row["category"] == "health")
    assert health["score"] == 89
    assert health["adjusted_score"] == 88.5
    assert calculate_overall_rating({"health": 88.5}, {"health": 1}) == 89


def test_rounding_does_not_change_radius_membership():
    text = explain_category({"category_name": "Щоденні потреби", "score": 88,
        "max_score": 100, "weight": 1, "types_data": [
            {"type_name": "Аптеки", "num_objects_in_radius": 0,
             "closest_radius_m": 200, "nearest_point_dist_m": 200.1}]})["explanation"]
    assert "не знайдено" in text
    assert "понад 200 м" in text
    assert "не дає максимального бала" in text
