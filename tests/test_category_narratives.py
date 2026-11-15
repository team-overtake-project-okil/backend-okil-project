import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.category_narratives import explain_categories, explain_category
from app.category_narratives.generator import counted_name, format_distance

CASES = json.loads((Path(__file__).parent / "fixtures/category_test_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("category", CASES, ids=lambda case: case["category_name"])
def test_all_categories_have_short_distance_explanations(category):
    original = deepcopy(category)
    result = explain_category(category)
    assert category == original
    assert set(result) == {"category_name", "score", "max_score", "explanation"}
    assert result["score"] == category["score"]
    assert 2 <= result["explanation"].count(".") <= 3
    assert "найближч" in result["explanation"]
    assert "для максимального бала" in result["explanation"]
    assert "втрат" not in result["explanation"].lower()
    assert "знижує ще на" not in result["explanation"]


@pytest.mark.parametrize("category", CASES, ids=lambda case: case["category_name"])
def test_supplied_score_is_never_recalculated(category):
    baseline = explain_category(category)["explanation"]
    for score in (0, 35, 100):
        modified = deepcopy(category)
        modified["score"] = score
        result = explain_category(modified)
        assert result["score"] == score
        assert result["explanation"] == baseline


def test_original_list_interface():
    assert len(explain_categories(CASES)) == 7


def test_missing_distance_does_not_claim_object_is_far():
    case = deepcopy(CASES[0])
    case["types_data"] = [{"type_name": "Аптеки", "num_objects_in_radius": 0,
                           "closest_radius_m": 300, "nearest_point_dist_m": None}]
    text = explain_category(case)["explanation"]
    assert "відстань до найближчого об’єкта невідома" in text
    assert "бракує даних" in text
    assert "не дає максимального бала" not in text


def test_empty_category():
    case = deepcopy(CASES[0])
    case["types_data"] = []
    assert "Недостатньо даних" in explain_category(case)["explanation"]


@pytest.mark.parametrize("distance,count", [(301, 1), (100, 0), (-1, 0), (float("nan"), 0)])
def test_inconsistent_input_rejected(distance, count):
    case = deepcopy(CASES[0])
    case["types_data"][0].update(nearest_point_dist_m=distance, num_objects_in_radius=count)
    with pytest.raises(ValidationError):
        explain_category(case)


def test_boundary_and_zero_distance_are_inside():
    for distance in (0, 300):
        case = deepcopy(CASES[0])
        case["types_data"] = [{"type_name": "Аптеки", "num_objects_in_radius": 1,
                               "closest_radius_m": 300, "nearest_point_dist_m": distance}]
        text = explain_category(case)["explanation"]
        assert f"за {distance} м" in text
        assert "відповідають умовам" in text


@pytest.mark.parametrize("count,expected", [(1, "1 магазин"), (2, "2 магазини"),
    (5, "5 магазинів"), (11, "11 магазинів"), (21, "21 магазин"), (112, "112 магазинів")])
def test_ukrainian_plural_forms(count, expected):
    assert counted_name("Магазини", count) == expected


def test_selection_retains_largest_gap():
    food = next(case for case in CASES if case["category_name"] == "Харчування")
    text = explain_category(food, max_types=2)["explanation"]
    assert "кав’яр" in text
    assert "ресторанів" in text.lower()
    assert "1,4 км" in text


def test_unknown_type_uses_fallback():
    case = deepcopy(CASES[0])
    case["types_data"] = [{"type_name": "Нова послуга", "num_objects_in_radius": 2,
                           "closest_radius_m": 300, "nearest_point_dist_m": 50}]
    text = explain_category(case)["explanation"]
    assert "Нова послуга" in text
    assert "50 м" in text


def test_distance_formatting():
    assert format_distance(200) == "200 м"
    assert format_distance(1900) == "1,9 км"
    assert format_distance(1850) == "1,85 км"


@pytest.mark.parametrize("limit", [True, 0, 1, 2.5])
def test_invalid_limit(limit):
    with pytest.raises(ValueError):
        explain_category(CASES[0], max_types=limit)
