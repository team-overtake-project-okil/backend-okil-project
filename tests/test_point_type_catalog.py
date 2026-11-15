"""Contract against point_types read through Neon on 2026-11-06.

This is a regression snapshot, not a source of runtime radii.
"""
import json
from pathlib import Path

import pytest

from app.category_narratives.config import CATEGORY_NAMES, TYPE_LANGUAGE, TYPE_NAMES
from app.category_narratives.generator import explain_category
from app.category_narratives.service import prepare_category_data, prepare_type
from app.scoring import SYSTEM_WEIGHTS


FIXTURES = Path(__file__).parent / "fixtures"
CATALOG = json.loads((FIXTURES / "point_types.json").read_text(encoding="utf-8"))
SNAPSHOT = json.loads((FIXTURES / "hexagon_accessibility.json").read_text(encoding="utf-8"))


def test_every_db_type_has_exactly_one_supported_name_and_language():
    assert len(CATALOG) == 25
    assert set(TYPE_NAMES) == {row["type"] for row in CATALOG}
    assert set(TYPE_NAMES.values()) == set(TYPE_LANGUAGE)
    assert len(set(TYPE_NAMES.values())) == len(TYPE_NAMES)
    assert {row["category"] for row in CATALOG} == set(CATEGORY_NAMES) == set(SYSTEM_WEIGHTS)
    assert TYPE_NAMES["bank_atm"] == "Банки та банкомати"
    assert "atm" not in TYPE_NAMES
    assert "swimming_pool" not in TYPE_NAMES
    assert "entertainment" not in TYPE_NAMES


@pytest.mark.parametrize("row", CATALOG, ids=lambda row: row["type"])
def test_all_types_use_db_category_and_full_score_radius(row):
    radius = row["radius_of_influence_1"]
    source = {"category": row["category"], "type": row["type"], "r1": radius,
              "nearest_point_dist_m": radius, "num_objects_in_radius": 1}
    payloads = prepare_category_data(SNAPSHOT["hexagon"], [source], {})
    prepared = payloads[row["category"]]["types_data"][0]
    assert prepared["type_name"] == TYPE_NAMES[row["type"]]
    assert prepared["closest_radius_m"] == radius
    assert sum(len(payload["types_data"]) for payload in payloads.values()) == 1
    text = explain_category(payloads[row["category"]])["explanation"]
    assert "об’єкт типу" not in text
    assert "1 " + TYPE_LANGUAGE[TYPE_NAMES[row["type"]]][0] in text


def test_updated_db_radius_is_used_without_updating_python_constants():
    row = {"type": "pharmacy", "r1": 123, "num_objects_in_radius": 1,
           "nearest_point_dist_m": 100}
    assert prepare_type(row)["closest_radius_m"] == 123


@pytest.mark.parametrize("radius", [None, 0, -1, float("nan")])
def test_missing_or_invalid_db_radius_is_not_replaced_with_guessed_value(radius):
    with pytest.raises(ValueError):
        prepare_type({"type": "pharmacy", "r1": radius,
                      "num_objects_in_radius": 0, "nearest_point_dist_m": None})


def test_live_snapshot_generates_all_eight_categories_without_fallback_names():
    payloads = prepare_category_data(SNAPSHOT["hexagon"], SNAPSHOT["types_data"], {})
    expected_counts = {"daily": 5, "enjoyment": 4, "food": 6, "health": 3,
                       "mobility": 1, "security": 1, "sport": 3, "study": 2}
    assert {category: len(p["types_data"]) for category, p in payloads.items()} == expected_counts
    for payload in payloads.values():
        text = explain_category(payload, max_types=25)["explanation"]
        assert "об’єкт типу" not in text
        assert 2 <= text.count(".") <= 3


def test_both_cli_examples_match_all_db_types_categories_and_radii():
    by_name = {TYPE_NAMES[row["type"]]: row for row in CATALOG}
    for filename in ("category_test_cases.json", "category_scored_cases.json"):
        examples = json.loads((FIXTURES.parents[1] / "examples" / filename).read_text(encoding="utf-8"))
        assert len(examples) == 8
        seen = set()
        for category in examples:
            for item in category["types_data"]:
                definition = by_name[item["type_name"]]
                assert category["category_name"] == CATEGORY_NAMES[definition["category"]]
                assert item["closest_radius_m"] == definition["radius_of_influence_1"]
                assert item["type_name"] not in seen
                seen.add(item["type_name"])
        assert seen == set(by_name)
