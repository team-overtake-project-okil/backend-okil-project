import unittest
from app.jobs.health_pipeline import (
    has_valid_coords,
    match_candidate,
    validate_reviewed,
)


class HealthPipelineTests(unittest.TestCase):

    def test_mykolaichuka_address(self):
        row = {
            "address": "Львів, вулиця Миколайчука І. 9, Львівська область, Україна"
        }

        results = [
            {
                "title": "Лікарня Святого Пантелеймона",
                "address": "вулиця Івана Миколайчука, 9, Львів",
                "gps_coordinates": {
                    "latitude": 49.8741774,
                    "longitude": 24.039617
                }
            }
        ]

        candidate, reason = match_candidate(row, results)

        self.assertIsNotNone(candidate)
        self.assertEqual(reason, "address_match_needs_manual_review")

    def test_lviv(self):
        self.assertTrue(has_valid_coords(49.84, 24.03))

    def test_outside(self):
        self.assertFalse(has_valid_coords(51, 24.03))

    def test_bad(self):
        self.assertFalse(has_valid_coords('', ''))

    def test_match(self):
        row = {"address": "Львів, вулиця Пекарська 59а, Львівська область"}
        hit = {"address": "Пекарська 59а, Львів", "gps_coordinates": {"latitude": 49.83, "longitude": 24.03}}
        self.assertIsNotNone(match_candidate(row, [hit])[0])

    def test_no_house(self):
        row = {"address": "Львів, вулиця Пекарська 59а"}
        hit = {"address": "Пекарська 12, Львів", "gps_coordinates": {"latitude": 49.83, "longitude": 24.03}}
        self.assertIsNone(match_candidate(row, [hit])[0])

    def test_review_required(self):
        row = {"reviewed": "no", "source_key": "a", "name": "Hospital", "sub_category": "hospital", "latitude": "49.84", "longitude": "24.03"}
        self.assertEqual(validate_reviewed([row]), [])

    def test_approved(self):
        row = {"reviewed": "yes", "source_key": "a", "name": "Hospital", "sub_category": "hospital", "latitude": "49.84", "longitude": "24.03"}
        self.assertEqual(len(validate_reviewed([row])), 1)

    def test_duplicate(self):
        row = {"reviewed": "yes", "source_key": "a", "name": "Hospital", "sub_category": "hospital", "latitude": "49.84", "longitude": "24.03"}
        with self.assertRaises(ValueError):
            validate_reviewed([row, row])


if __name__ == '__main__':
    unittest.main()
