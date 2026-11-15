"""Run: python -m app.category_narratives examples/category_test_cases.json"""
import argparse
import json
from pathlib import Path

from .generator import explain_categories


def main():
    parser = argparse.ArgumentParser(description="Explain category accessibility without LLMs")
    parser.add_argument("input", type=Path, help="UTF-8 JSON array in the test_cases format")
    parser.add_argument("--max-types", type=int, default=4)
    args = parser.parse_args()
    cases = json.loads(args.input.read_text(encoding="utf-8-sig"))
    print(json.dumps(explain_categories(cases, args.max_types), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
