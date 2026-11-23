import asyncio
from collections import Counter
import json
from pathlib import Path

import httpx

from app import import_spike_data
from app.spike_data import (
    SOURCE_DEFINITIONS,
    fetch_source,
    load_normalized_jsonl,
    normalize_source_record,
    parse_source_rows,
    SpikeSourceError,
)


def provenance(source):
    return {
        "source_url": f"https://example.test/dataset/{source.dataset_id}",
        "license_id": "cc-by",
        "license_url": "https://example.test/license",
        "source_updated_at": "2026-09-30T07:51:27",
    }


def healthcare_row(**changes):
    row = {
        "_id": 17,
        "orgIdentifier": "12345678",
        "name": "Клінічна лікарня",
        "orgType": "лікарня",
        "ownerShip": "комунальна",
        "addressPostName": "Львів",
        "streetType": "вулиця",
        "addressThoroughfare": "Зелена",
        "addressLocatorDesignator": "12",
        "telephone": "+380000000000",
        "email": "private@example.test",
        "head": "Не індексувати",
    }
    return {**row, **changes}


def test_healthcare_record_keeps_provenance_but_omits_personal_contacts():
    source = SOURCE_DEFINITIONS["health_facilities"]
    records, rejected = parse_source_rows(source, [healthcare_row()], provenance(source))

    assert rejected == []
    assert len(records) == 1
    record = records[0]
    assert record["category"] == "health"
    assert "Клінічна лікарня" in record["content"]
    assert "вулиця Зелена, 12" in record["content"]
    assert "+380000000000" not in record["content"]
    assert "private@example.test" not in json.dumps(record, ensure_ascii=False)
    assert "Не індексувати" not in json.dumps(record, ensure_ascii=False)
    assert record["metadata"]["source_dataset_id"] == source.dataset_id
    assert record["metadata"]["source_resource_id"] == source.resource_id
    assert record["metadata"]["source_record_id"] == "12345678"
    assert record["metadata"]["source_license"] == "cc-by"
    assert record["metadata"]["source_updated_at"] == provenance(source)["source_updated_at"]


def test_record_ids_are_stable_when_source_content_changes():
    source = SOURCE_DEFINITIONS["health_facilities"]
    first, reason = normalize_source_record(source, healthcare_row(), provenance(source))
    changed, changed_reason = normalize_source_record(
        source,
        healthcare_row(name="Нова назва лікарні"),
        provenance(source),
    )

    assert reason is None
    assert changed_reason is None
    assert first["metadata"]["chunk_id"] == changed["metadata"]["chunk_id"]
    assert first["content"] != changed["content"]


def test_reused_registration_code_does_not_merge_separate_facility_addresses():
    source = SOURCE_DEFINITIONS["cultural_facilities"]
    first = {
        "_id": 1,
        "registrationCode": "shared-code",
        "name": "Народний дім Збоїща",
        "type": "клубний заклад",
        "settlement": "Львів",
        "streetName": "Мурована",
        "houseNumber": "32",
    }
    second = {
        **first,
        "_id": 2,
        "name": "Народний дім Малехова",
        "settlement": "Малехів",
        "streetName": "Шевченка",
        "houseNumber": "1",
    }

    records, rejected = parse_source_rows(source, [first, second, first], provenance(source))

    assert len(records) == 2
    assert [record["metadata"]["source_record_id"] for record in records] == [
        "shared-code",
        "shared-code",
    ]
    assert records[0]["metadata"]["chunk_id"] != records[1]["metadata"]["chunk_id"]
    assert [item["reason"] for item in rejected] == ["duplicate_stable_identity"]


def test_reused_source_identifier_uses_stable_ckan_row_id_when_details_change():
    source = SOURCE_DEFINITIONS["cultural_facilities"]
    row = {
        "_id": 45,
        "registrationCode": "shared-code",
        "name": "Народний дім",
        "type": "клубний заклад",
        "settlement": "Львів",
        "streetName": "Мурована",
        "houseNumber": "32",
    }
    changed = {**row, "streetName": "Нова", "houseNumber": "3"}

    first, first_rejections = parse_source_rows(
        source, [row, {**row, "_id": 46}], provenance(source),
    )
    updated, updated_rejections = parse_source_rows(source, [changed], provenance(source))

    assert first_rejections == []
    assert updated_rejections == []
    assert first[0]["metadata"]["chunk_id"] == updated[0]["metadata"]["chunk_id"]
    assert first[1]["metadata"]["chunk_id"] != updated[0]["metadata"]["chunk_id"]
    assert first[0]["metadata"]["identity_strategy"] == "source_identifier_plus_row_id"


def test_incomplete_source_rows_are_reported_and_not_emitted():
    source = SOURCE_DEFINITIONS["health_facilities"]
    records, rejected = parse_source_rows(
        source,
        [
            healthcare_row(name="null"),
            healthcare_row(orgType=""),
            healthcare_row(addressPostName="", streetType="", addressThoroughfare="", addressLocatorDesignator=""),
        ],
        provenance(source),
    )

    assert records == []
    assert [item["reason"] for item in rejected] == [
        "missing_name",
        "missing_type",
        "missing_address",
    ]
    assert all(item["source_row_id"] == "17" for item in rejected)


def test_culture_null_accessibility_is_not_rewritten_as_negative():
    source = SOURCE_DEFINITIONS["cultural_facilities"]
    base = {
        "_id": 1,
        "registrationCode": "culture-1",
        "name": "Міський музей",
        "type": "музей",
        "settlement": "Львів",
        "streetType": "вулиця",
        "streetName": "Музейна",
        "houseNumber": "2",
    }

    no_value, reason = normalize_source_record(
        source, {**base, "inclusion": "null"}, provenance(source),
    )
    explicit_none, explicit_reason = normalize_source_record(
        source, {**base, "inclusion": "немає"}, provenance(source),
    )

    assert reason is None
    assert explicit_reason is None
    assert "облаштування доступності" not in no_value["content"]
    assert "Дані про облаштування доступності з джерела: немає." in explicit_none["content"]


def test_playground_decimal_comma_is_parsed_and_bad_coordinates_are_excluded():
    source = SOURCE_DEFINITIONS["playgrounds"]
    row = {
        "_id": 5,
        "uid": "park-5",
        "type": "спортивний майданчик",
        "addressPostName": "Львів",
        "addressThoroughfare": "Зелена",
        "addressLocatorDesignator": "20",
        "lat": "49,84",
        "lon": "24,03",
        "elements": "турнік, тренажери",
    }

    record, reason = normalize_source_record(source, row, provenance(source))
    invalid, invalid_reason = normalize_source_record(
        source, {**row, "lon": "30.53309"}, provenance(source),
    )

    assert reason is None
    assert record["category"] == "sport"
    assert record["metadata"]["latitude"] == 49.84
    assert record["metadata"]["longitude"] == 24.03
    assert "турнік, тренажери" in record["content"]
    assert invalid is None
    assert invalid_reason == "coordinates_outside_lviv_bounds"


def test_normalized_jsonl_rejects_bad_and_duplicate_rows_safely():
    record = {
        "category": "health",
        "content": "Медичний заклад.",
        "metadata": {
            "document_id": "lviv-open-data.health",
            "chunk_id": "lviv.health.abc",
            "source_kind": "city_open_data",
            "source_url": "https://example.test/source",
            "source_dataset_id": "dataset",
            "source_resource_id": "resource",
            "source_record_id": "record",
        },
    }
    payload = "\n".join([
        json.dumps(record),
        "{malformed",
        json.dumps(record),
        json.dumps({"category": "health", "content": "Missing metadata"}),
    ])

    records, rejected = load_normalized_jsonl(payload)

    assert len(records) == 1
    assert [item["reason"] for item in rejected] == [
        "invalid_json",
        "duplicate_document_chunk_id",
        "missing_required_metadata",
    ]


def test_checked_in_snapshot_has_valid_records_and_source_identifiers():
    snapshot = Path(__file__).resolve().parents[1] / "data" / "spike" / "lviv_open_data.jsonl"
    records, rejected = load_normalized_jsonl(snapshot.read_text(encoding="utf-8-sig"))

    assert len(records) == 1925
    assert rejected == []
    assert Counter(record["category"] for record in records) == {
        "enjoyment": 512,
        "food": 945,
        "health": 73,
        "sport": 135,
        "study": 260,
    }
    assert all(
        record["metadata"][key]
        for record in records
        for key in ("document_id", "chunk_id", "source_url", "source_record_id")
    )


def test_fetch_source_checks_license_and_returns_full_datastore_rows():
    source = SOURCE_DEFINITIONS["health_facilities"]

    def handler(request):
        if request.url.path.endswith("/package_show"):
            payload = {
                "id": source.dataset_id,
                "license_id": "cc-by",
                "license_url": "https://example.test/license",
            }
        elif request.url.path.endswith("/resource_show"):
            payload = {
                "id": source.resource_id,
                "package_id": source.dataset_id,
                "datastore_active": True,
                "last_modified": "2026-09-30T07:51:27",
            }
        else:
            payload = {
                "total": 1,
                "records": [healthcare_row()],
            }
        return httpx.Response(200, json={"success": True, "result": payload})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows, source_info = fetch_source(client, source)
    client.close()

    assert len(rows) == 1
    assert rows[0]["orgIdentifier"] == "12345678"
    assert source_info["source_updated_at"] == "2026-09-30T07:51:27"
    assert source_info["license_id"] == "cc-by"


def test_fetch_source_rejects_unapproved_noncommercial_license():
    source = SOURCE_DEFINITIONS["health_facilities"]

    def handler(_request):
        return httpx.Response(200, json={
            "success": True,
            "result": {"id": source.dataset_id, "license_id": "cc-by-nc"},
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        try:
            fetch_source(client, source)
        except SpikeSourceError as error:
            assert "CC-BY license" in str(error)
        else:
            raise AssertionError("A noncommercial license must not be accepted")
    finally:
        client.close()


def test_write_spike_records_groups_by_document_and_counts(monkeypatch):
    calls = []

    def fake_write(connection, records):
        documents = {row["metadata"]["document_id"] for row in records}
        assert len(documents) == 1
        calls.append((documents.pop(), len(records)))
        return {"inserted": len(records), "updated": 0, "unchanged": 0}

    monkeypatch.setattr(import_spike_data, "write_records", fake_write)
    rows = [
        {"metadata": {"document_id": "b"}},
        {"metadata": {"document_id": "a"}},
        {"metadata": {"document_id": "a"}},
    ]

    result = import_spike_data.write_spike_records(object(), rows)

    assert calls == [("a", 2), ("b", 1)]
    assert result == {"inserted": 3, "updated": 0, "unchanged": 0}
