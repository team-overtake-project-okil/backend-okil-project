from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.category_narratives import explain_category


@pytest.fixture
def daily():
    return {
        "category_name": "Щоденні потреби", "score": 88, "max_score": 100, "weight": 0.9,
        "types_data": [
            {"type_name": "Аптеки", "num_objects_in_radius": 4,
             "closest_radius_m": 200, "nearest_point_dist_m": 150},
            {"type_name": "Супермаркети", "num_objects_in_radius": 0,
             "closest_radius_m": 300, "nearest_point_dist_m": 320},
            {"type_name": "Банки та банкомати", "num_objects_in_radius": 0,
             "closest_radius_m": 300, "nearest_point_dist_m": 450},
        ],
    }


def test_agreed_three_sentence_format(daily):
    assert explain_category(daily)["explanation"] == (
        "У радіусі 200 м для максимального бала є 4 аптеки, найближча — за 150 м. "
        "Супермаркетів у межах 300 м для максимального бала не знайдено, найближчий — за 320 м; "
        "банків або банкоматів у межах 300 м для максимального бала не знайдено, найближчий — за 450 м. "
        "Віддаленість об’єктів за межами цих радіусів не дає максимального бала за відстань."
    )


def test_counts_from_larger_radius_cannot_be_relabelled(daily):
    daily["types_data"][1].update(closest_radius_m=500, num_objects_in_radius=2, full_score_radius_m=250)
    with pytest.raises(ValidationError, match="Counts must refer"):
        explain_category(daily)


def test_legacy_numeric_score_components_are_ignored(daily):
    text = explain_category(daily)["explanation"]
    for item in daily["types_data"]:
        item.update(type_score=0, type_weight=1, lost_category_points=100)
    assert explain_category(daily)["explanation"] == text


def test_user_weights_do_not_change_distance_facts(daily):
    original = deepcopy(daily)
    daily["weight"] = 0
    assert explain_category(daily)["explanation"] == explain_category(original)["explanation"]
