"""Prepare a reviewable healthcare POI staging CSV from the city's legacy .xls.

This tool intentionally does not geocode or write to PostgreSQL. The source has
no coordinates; importing fictitious locations would corrupt H3 ratings.

Usage:
    python -m app.jobs.load_health --file "data/health.xls" --output "tmp/health-preview.csv"
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


FIELDS = (
    "source_key", "source_row", "name", "sub_category", "org_type",
    "address", "city", "latitude", "longitude", "status",
)


def clean(value: object) -> str:
    """Normalize empty values, whitespace, and literal 'null' cells."""
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return "" if text.lower() in {"null", "none", "nan"} else text


def categorize(name: str, org_type: str) -> str:
    """Conservative mapping to the two categories allowed by sources.docx.

    Ambiguous institutions are left blank for manual review instead of being
    silently mapped to hospitals/polyclinics.
    """
    text = f"{name} {org_type}".casefold()
    if ("поліклінік" in text or "поліклінічн" in text) or "амбулатор" in text or "сімейної медицини" in text:
        return "polyclinic"
    if "лікарня" in text or "шпитал" in text:
        return "hospital"
    return ""


def build_address(source: dict[str, object]) -> str:
    city = clean(source.get("addressPostName"))
    street_type = clean(source.get("streetType"))
    street = clean(source.get("addressThoroughfare"))
    house = clean(source.get("addressLocatorDesignator"))
    building = clean(source.get("addressLocatorBuilding"))
    # Avoid: 'проспект Червоної Калини, просп.'
    street = re.sub(r",\s*(вул\.|просп\.)$", "", street, flags=re.IGNORECASE)
    address = " ".join(p for p in (street_type, street, house, building) if p)
    return ", ".join(p for p in (city, address, "Львівська область, Україна") if p)


def convert(source: dict[str, object], excel_row: int) -> dict[str, str] | None:
    name = clean(source.get("name"))
    city = clean(source.get("addressPostName"))
    street = clean(source.get("addressThoroughfare"))
    house = clean(source.get("addressLocatorDesignator"))
    if not all((name, city, street, house)):
        return None
    org_type = clean(source.get("orgType"))
    address = build_address(source)
    key_input = "|".join((name.casefold(), city.casefold(), street.casefold(), house.casefold(), org_type.casefold()))
    source_key = hashlib.sha256(key_input.encode("utf-8")).hexdigest()[:20]
    sub = categorize(name, org_type)
    return {
        "source_key": source_key,
        "source_row": str(excel_row),
        "name": name,
        "sub_category": sub,
        "org_type": org_type,
        "address": address,
        "city": city,
        "latitude": "",
        "longitude": "",
        "status": "needs_geocoding" if sub else "review_category_and_geocoding",
    }


def read_xls(path: Path) -> list[tuple[int, dict[str, object]]]:
    try:
        import xlrd  # supports legacy .xls, not .xlsx
    except ImportError as ex:
        raise SystemExit("Install the .xls reader: python -m pip install 'xlrd>=2,<3'") from ex
    workbook = xlrd.open_workbook(str(path), on_demand=True)
    sheet = workbook.sheet_by_index(0)
    headers = [clean(sheet.cell_value(0, col)) for col in range(sheet.ncols)]
    required = {"name", "orgType", "addressPostName", "addressThoroughfare", "addressLocatorDesignator"}
    missing = required - set(headers)
    if missing:
        raise ValueError(f"Missing expected source columns: {sorted(missing)}")
    result = []
    for row_index in range(1, sheet.nrows):
        row = {header: sheet.cell_value(row_index, col) for col, header in enumerate(headers) if header}
        if any(clean(value) for value in row.values()):
            result.append((row_index + 1, row))
    return result


def prepare(items: list[tuple[int, dict[str, object]]]) -> tuple[list[dict[str, str]], dict]:
    records: list[dict[str, str]] = []
    duplicates: list[int] = []
    invalid: list[int] = []
    seen: set[str] = set()
    for excel_row, data in items:
        record = convert(data, excel_row)
        if record is None:
            invalid.append(excel_row)
            continue
        if record["source_key"] in seen:
            duplicates.append(excel_row)
            continue
        seen.add(record["source_key"])
        records.append(record)
    counts = Counter(item["sub_category"] or "needs_review" for item in records)
    report = {
        "total_source_rows": len(items),
        "staged_records": len(records),
        "invalid_source_rows": invalid,
        "exact_duplicate_rows": duplicates,
        "subcategories": dict(counts),
        "without_coordinates": sum(not row["latitude"] or not row["longitude"] for row in records),
        "written_to_database": 0,
        "mode": "dry_run_only",
    }
    return records, report


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a healthcare POI staging CSV; no database writes")
    parser.add_argument("--file", type=Path, required=True, help="Source .xls file")
    parser.add_argument("--output", type=Path, default=Path("tmp/health-preview.csv"))
    args = parser.parse_args()
    records, report = prepare(read_xls(args.file))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)
    report_path = args.output.with_name(args.output.stem + "-report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Review CSV: {args.output}\nReport: {report_path}")
    print("No data was inserted into PostgreSQL.")


if __name__ == "__main__":
    main()
