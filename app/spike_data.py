"""Fetch and normalize approved public Lviv datasets for AI knowledge import."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

CKAN_API = "https://opendata.city-adm.lviv.ua/api/3/action"
PORTAL = "https://opendata.city-adm.lviv.ua"
PAGE_SIZE = 500
# These are the same broad Lviv MTH bounds used by the existing healthcare import.
COORDINATE_BOUNDS = (49.65, 50.02, 23.65, 24.35)
EM_DASH = " — "
MISSING = {"", "null", "none", "nan", "n/a", "na", "undefined"}


@dataclass(frozen=True)
class SourceDefinition:
    key: str
    dataset_id: str
    resource_id: str
    category: str
    entity_label: str
    scope: str
    name_fields: tuple[str, ...]
    type_fields: tuple[str, ...]
    locality_fields: tuple[str, ...]
    district_fields: tuple[str, ...]
    street_type_fields: tuple[str, ...]
    street_fields: tuple[str, ...]
    house_fields: tuple[str, ...]
    identity_fields: tuple[str, ...]
    detail_fields: tuple[tuple[str, str], ...] = ()
    coordinate_fields: tuple[str, str] | None = None
    type_fallback_name: bool = False


SOURCE_DEFINITIONS: dict[str, SourceDefinition] = {
    "health_facilities": SourceDefinition(
        key="health_facilities",
        dataset_id="0c2ecd79-f08a-4788-aaea-bc6a2a8bfb98",
        resource_id="8e695e09-bb92-463c-8f5d-ec0b09b765bf",
        category="health",
        entity_label="Медичний заклад",
        scope="Комунальні заклади охорони здоров’я Львівської МТГ; приватні заклади не охоплені цим набором.",
        name_fields=("name",),
        type_fields=("orgType",),
        locality_fields=("addressPostName", "addressAdminUnitL5", "addressAdminUnitL4"),
        district_fields=("addressAdminUnitL3",),
        street_type_fields=("streetType",),
        street_fields=("addressThoroughfare",),
        house_fields=("addressLocatorDesignator", "addressLocatorBuilding"),
        identity_fields=("orgIdentifier",),
        detail_fields=(("Попередня назва в джерелі", "oldName"), ("Форма власності", "ownerShip")),
    ),
    "schools": SourceDefinition(
        key="schools",
        dataset_id="f065411d-f7e4-42d9-bf98-003a39794554",
        resource_id="3a4e73ec-928e-48a4-975f-cbc3376ec156",
        category="study",
        entity_label="Заклад загальної середньої освіти",
        scope="Перелік закладів загальної середньої освіти Львівської МТГ; у знімку всі записи позначені як комунальні.",
        name_fields=("fullName", "shortName"),
        type_fields=("subordination",),
        locality_fields=("settlement",),
        district_fields=("district",),
        street_type_fields=("streetType",),
        street_fields=("streetName",),
        house_fields=("streetNumber",),
        identity_fields=("orgIdentifier",),
        detail_fields=(("Коротка назва", "shortName"), ("Форма власності", "ownerShip")),
    ),
    "preschools": SourceDefinition(
        key="preschools",
        dataset_id="25cef98b-b36b-48b3-a45d-922684920572",
        resource_id="05beeb3a-4f6e-4bbe-a8b3-b740e165ff28",
        category="study",
        entity_label="Заклад дошкільної освіти, дитячий садок",
        scope="Перелік закладів дошкільної освіти Львівської МТГ; у знімку всі записи позначені як комунальні.",
        name_fields=("fullName", "shortName"),
        type_fields=("subordination",),
        locality_fields=("addressPostName", "addressAdminUnitL5", "addressAdminUnitL4"),
        district_fields=("addressAdminUnitL3",),
        street_type_fields=("streetType",),
        street_fields=("addressThoroughfare",),
        house_fields=("addressLocatorDesignator", "addressLocatorBuilding"),
        identity_fields=("orgIdentifier",),
        detail_fields=(("Коротка назва", "shortName"), ("Форма власності", "ownerShip")),
    ),
    "out_of_school_education": SourceDefinition(
        key="out_of_school_education",
        dataset_id="c7afb878-2b48-45db-a822-20d62c4fb52e",
        resource_id="f438b85c-140d-48f9-872a-b6bcc0eeec8b",
        category="study",
        entity_label="Заклад позашкільної освіти",
        scope="Перелік закладів позашкільної освіти Львівської МТГ; у знімку всі записи позначені як комунальні.",
        name_fields=("name",),
        type_fields=("subordination",),
        locality_fields=("addressPostName", "addressAdminUnitL5", "addressAdminUnitL4"),
        district_fields=("addressAdminUnitL3",),
        street_type_fields=("streetType",),
        street_fields=("addressThoroughfare",),
        house_fields=("addressLocatorDesignator", "addressLocatorBuilding"),
        identity_fields=("orgIdentifier",),
        detail_fields=(("Форма власності", "ownerShip"),),
    ),
    "cultural_facilities": SourceDefinition(
        key="cultural_facilities",
        dataset_id="5037e76d-7670-4caf-ad5e-346915455cdf",
        resource_id="4a6b034c-9313-4fd8-99d0-f8beab43547b",
        category="enjoyment",
        entity_label="Комунальний заклад культури",
        scope="Комунальні заклади культури Львівської міської ради; ресурсний знімок від 14 січня 2026 року.",
        name_fields=("name",),
        type_fields=("type",),
        locality_fields=("settlement",),
        district_fields=("district",),
        street_type_fields=("streetType",),
        street_fields=("streetName",),
        house_fields=("houseNumber",),
        identity_fields=("registrationCode",),
        detail_fields=(("Дані про облаштування доступності з джерела", "inclusion"),),
    ),
    "food_establishments": SourceDefinition(
        key="food_establishments",
        dataset_id="df38acd7-d449-4297-bee2-2682eb6caa8f",
        resource_id="71bdf4e5-b6bb-4774-b268-160dbfc1784d",
        category="food",
        entity_label="Заклад харчування",
        scope="Заклади ресторанного господарства Львова; години роботи з каталогу можуть змінюватися після публікації знімка.",
        name_fields=("name",),
        type_fields=("type", "activityType"),
        locality_fields=("addressPostName", "addressAdminUnitL2", "addressAdminUnitL1"),
        district_fields=("addressDistrict",),
        street_type_fields=("typeStreet",),
        street_fields=("addressThoroughfare",),
        house_fields=("addressLocatorDesignator", "addressLocatorBuilding"),
        identity_fields=("identifier",),
        detail_fields=(("Опис годин роботи з джерела", "openingHoursDescription"), ("Години роботи з джерела", "openingHours")),
    ),
    "playgrounds": SourceDefinition(
        key="playgrounds",
        dataset_id="17cfc089-9430-4ace-aa62-ae1ea42bcf86",
        resource_id="b63c5882-e78e-47cf-ad74-024563e67c13",
        category="enjoyment",
        entity_label="Дитячий або спортивний майданчик, громадський простір",
        scope="Комунальні дитячі, спортивні та інші майданчики Львівської МТГ; координати поза межами проєктного діапазону відхиляються.",
        name_fields=("name",),
        type_fields=("type",),
        locality_fields=("addressPostName", "addressAdminUnitL5", "addressAdminUnitL4"),
        district_fields=("addressAdminUnitL3",),
        street_type_fields=(),
        street_fields=("addressThoroughfare",),
        house_fields=("addressLocatorDesignator", "addressLocatorBuilding"),
        identity_fields=("uid",),
        detail_fields=(("Облаштування з джерела", "elements"), ("Площа, м²", "area"), ("Дата інвентаризації", "inventoryDate")),
        coordinate_fields=("lat", "lon"),
        type_fallback_name=True,
    ),
}

CATEGORY_LABELS = {
    "health": "здоров’я",
    "study": "освіта",
    "enjoyment": "дозвілля",
    "food": "їжа та заклади харчування",
    "sport": "спорт",
}


class SpikeSourceError(RuntimeError):
    """A configured source could not be fetched completely or safely."""


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = " ".join(text.split())
    return "" if text.casefold() in MISSING else text


def _first(record: Mapping[str, Any], fields: Sequence[str]) -> str:
    for field in fields:
        value = _clean(record.get(field))
        if value:
            return value
    return ""


def _coordinate(value: Any) -> float | None:
    text = _clean(value)
    if not text:
        return None
    if "," in text and "." not in text and text.count(",") == 1:
        text = text.replace(",", ".")
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _address(source: SourceDefinition, row: Mapping[str, Any]) -> tuple[str, str, str]:
    locality = _first(row, source.locality_fields)
    district = _first(row, source.district_fields)
    street_name = _first(row, source.street_fields)
    street_type = _first(row, source.street_type_fields)
    house = _first(row, source.house_fields)
    street = " ".join(part for part in (street_type, street_name) if part)
    street_line = ", ".join(part for part in (street, house) if part)
    address_parts = [locality]
    if district:
        address_parts.append(f"район {district}")
    if street_line:
        address_parts.append(street_line)
    return ", ".join(part for part in address_parts if part), locality, district


def _stable_entity_id(
    source: SourceDefinition,
    row: Mapping[str, Any],
    name: str,
    entity_type: str,
    address: str,
    latitude: float | None,
    longitude: float | None,
    disambiguate_source_id: bool,
) -> tuple[str, str, str]:
    source_record_id = _first(row, source.identity_fields) or _clean(row.get("_id"))
    if source_record_id:
        source_row_id = _clean(row.get("_id"))
        if source_row_id and source_record_id != source_row_id:
            # CKAN's row key disambiguates reused registration codes without
            # changing the key when a facility's name or address is corrected.
            identity = f"{source_record_id}|row:{source_row_id}"
            strategy = "source_identifier_plus_row_id"
        elif disambiguate_source_id:
            identity_parts = [
                source_record_id,
                name.casefold(),
                entity_type.casefold(),
                address.casefold(),
            ]
            if latitude is not None and longitude is not None:
                identity_parts.extend((f"{latitude:.6f}", f"{longitude:.6f}"))
            identity = "|".join(identity_parts)
            strategy = "source_identifier_plus_entity_fields"
        else:
            identity = source_record_id
            strategy = "source_identifier" if _first(row, source.identity_fields) else "ckan_row_id"
    else:
        identity_text = "|".join((
            name.casefold(),
            entity_type.casefold(),
            address.casefold(),
            f"{latitude:.6f}" if latitude is not None else "",
            f"{longitude:.6f}" if longitude is not None else "",
        ))
        if not name or not entity_type or not address:
            return "", "", ""
        identity = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()
        source_record_id = identity
        strategy = "normalized_name_type_address_sha256"
    stable_id = hashlib.sha256(f"{source.key}|{identity}".encode("utf-8")).hexdigest()[:24]
    return source_record_id, stable_id, strategy


def _build_content(
    source: SourceDefinition,
    row: Mapping[str, Any],
    name: str,
    entity_type: str,
    address: str,
    category: str,
    source_updated_at: str,
) -> str:
    category_label = CATEGORY_LABELS[category]
    parts = [f"{source.entity_label}: {name}.", f"Категорія інфраструктури: {category_label}."]
    if entity_type:
        parts.append(f"Тип у джерелі: {entity_type}.")
    if address:
        parts.append(f"Адреса з джерела: {address}.")
    for label, field in source.detail_fields:
        value = _clean(row.get(field))
        if value:
            parts.append(f"{label}: {value}.")
    if source.key == "food_establishments" and _first(row, ("openingHours", "openingHoursDescription")):
        parts.append(
            f"Це години з каталожного знімка, опублікованого {source_updated_at}; "
            "вони можуть бути неактуальними."
        )
    return "\n".join(parts)


def normalize_source_record(
    source: SourceDefinition,
    row: Mapping[str, Any],
    provenance: Mapping[str, Any],
    *,
    disambiguate_source_id: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """Convert one CKAN row to AIKnowledge's category/content/metadata shape."""
    if not isinstance(row, Mapping):
        return None, "record_not_object"
    name = _first(row, source.name_fields)
    entity_type = _first(row, source.type_fields)
    if not entity_type:
        return None, "missing_type"
    if not name and source.type_fallback_name:
        name = entity_type
    if not name:
        return None, "missing_name"

    address, locality, district = _address(source, row)
    latitude = longitude = None
    if source.coordinate_fields:
        latitude = _coordinate(row.get(source.coordinate_fields[0]))
        longitude = _coordinate(row.get(source.coordinate_fields[1]))
        if latitude is None or longitude is None:
            return None, "invalid_or_missing_coordinates"
        if not (
            COORDINATE_BOUNDS[0] <= latitude <= COORDINATE_BOUNDS[1]
            and COORDINATE_BOUNDS[2] <= longitude <= COORDINATE_BOUNDS[3]
        ):
            return None, "coordinates_outside_lviv_bounds"
    elif not address:
        return None, "missing_address"

    category = source.category
    if source.key == "playgrounds" and re.search(r"спорт|футбол|стадіон", entity_type, re.IGNORECASE):
        category = "sport"
    source_record_id, stable_id, identity_strategy = _stable_entity_id(
        source,
        row,
        name,
        entity_type,
        address,
        latitude,
        longitude,
        disambiguate_source_id,
    )
    if not source_record_id:
        return None, "missing_stable_identity"

    source_updated_at = str(provenance.get("source_updated_at") or "")
    document_id = f"lviv-open-data.{source.key}"
    chunk_id = f"lviv.{source.key}.{stable_id}"
    metadata: dict[str, Any] = {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "title": name,
        "language": "uk",
        "source_kind": "city_open_data",
        "source_provider": "Lviv City Council Open Data Portal",
        "source_url": provenance["source_url"],
        "source_dataset_id": source.dataset_id,
        "source_resource_id": source.resource_id,
        "source_record_id": source_record_id,
        "source_row_id": _clean(row.get("_id")),
        "entity_id": f"{source.key}:{stable_id}",
        "identity_strategy": identity_strategy,
        "source_license": provenance["license_id"],
        "source_license_url": provenance.get("license_url") or "",
        "source_updated_at": source_updated_at,
        "source_scope": source.scope,
        "entity_type": entity_type,
        "address": address,
        "settlement": locality,
        "district": district,
    }
    if latitude is not None and longitude is not None:
        metadata["latitude"] = latitude
        metadata["longitude"] = longitude
    content = _build_content(
        source, row, name, entity_type, address, category, source_updated_at,
    )
    return {"category": category, "content": content, "metadata": metadata}, None


def parse_source_rows(
    source: SourceDefinition,
    rows: Sequence[Mapping[str, Any]],
    provenance: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_id_counts: dict[str, int] = {}
    for row in rows:
        if isinstance(row, Mapping):
            source_id = _first(row, source.identity_fields) or _clean(row.get("_id"))
            if source_id:
                source_id_counts[source_id] = source_id_counts.get(source_id, 0) + 1
    for index, row in enumerate(rows, start=1):
        source_id = (
            _first(row, source.identity_fields) or _clean(row.get("_id"))
            if isinstance(row, Mapping) else ""
        )
        record, reason = normalize_source_record(
            source,
            row,
            provenance,
            disambiguate_source_id=source_id_counts.get(source_id, 0) > 1,
        )
        source_row_id = _clean(row.get("_id")) if isinstance(row, Mapping) else ""
        source_id = (
            _first(row, source.identity_fields) or source_row_id
            if isinstance(row, Mapping) else ""
        )
        if record is None:
            rejected.append({
                "source_key": source.key,
                "source_row_id": source_row_id,
                "source_record_id": source_id,
                "row_number": index,
                "reason": reason,
            })
            continue
        chunk_id = record["metadata"]["chunk_id"]
        if chunk_id in seen:
            rejected.append({
                "source_key": source.key,
                "source_row_id": source_row_id,
                "source_record_id": source_id,
                "row_number": index,
                "reason": "duplicate_stable_identity",
            })
            continue
        seen.add(chunk_id)
        records.append(record)
    return records, rejected


def _ckan_result(client: httpx.Client, action: str, params: Mapping[str, Any]) -> dict[str, Any]:
    try:
        response = client.get(f"{CKAN_API}/{action}", params=params)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise SpikeSourceError(f"CKAN {action} request failed; source data was not imported.") from None
    if not payload.get("success") or not isinstance(payload.get("result"), dict):
        raise SpikeSourceError(f"CKAN {action} returned an invalid response; source data was not imported.")
    return payload["result"]


def fetch_source(
    client: httpx.Client,
    source: SourceDefinition,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch a complete, licensed CKAN resource; partial pages are never returned."""
    package = _ckan_result(client, "package_show", {"id": source.dataset_id})
    if package.get("id") != source.dataset_id:
        raise SpikeSourceError(f"Unexpected dataset for source {source.key}.")
    license_id = _clean(package.get("license_id")).casefold()
    if license_id != "cc-by":
        raise SpikeSourceError(f"Source {source.key} does not have the expected CC-BY license.")
    resource = _ckan_result(client, "resource_show", {"id": source.resource_id})
    if resource.get("id") != source.resource_id or resource.get("package_id") != source.dataset_id:
        raise SpikeSourceError(f"Unexpected resource for source {source.key}.")
    if not resource.get("datastore_active"):
        raise SpikeSourceError(f"Source {source.key} has no active CKAN Datastore resource.")

    rows: list[dict[str, Any]] = []
    expected_total: int | None = None
    offset = 0
    while expected_total is None or offset < expected_total:
        page = _ckan_result(client, "datastore_search", {
            "resource_id": source.resource_id,
            "limit": PAGE_SIZE,
            "offset": offset,
        })
        total = page.get("total")
        page_rows = page.get("records")
        if type(total) is not int or not isinstance(page_rows, list):
            raise SpikeSourceError(f"Source {source.key} returned an invalid Datastore page.")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise SpikeSourceError(f"Source {source.key} changed during paging; retry the snapshot.")
        if not page_rows and offset < expected_total:
            raise SpikeSourceError(f"Source {source.key} returned an incomplete page.")
        rows.extend(page_rows)
        offset += len(page_rows)
        if len(page_rows) < PAGE_SIZE and offset < expected_total:
            raise SpikeSourceError(f"Source {source.key} returned a truncated page.")

    provenance = {
        "source_url": f"{PORTAL}/dataset/{source.dataset_id}",
        "license_id": license_id,
        "license_url": _clean(package.get("license_url")),
        "source_updated_at": _clean(resource.get("last_modified")) or _clean(package.get("metadata_modified")),
        "resource_name": _clean(resource.get("name")),
    }
    return rows, provenance


def fetch_and_parse(
    source_keys: Sequence[str] | None = None,
    *,
    client: httpx.Client | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = list(source_keys or SOURCE_DEFINITIONS)
    unknown = sorted(set(selected) - set(SOURCE_DEFINITIONS))
    if unknown:
        raise ValueError(f"Unknown spike data source(s): {', '.join(unknown)}")
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=30.0,
            follow_redirects=False,
            headers={"User-Agent": "OKIL-Issue99-DataImporter/1.0"},
        )
    all_records: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "source_provider": "Lviv City Council Open Data Portal",
        "source_keys": selected,
        "sources": {},
        "rejected": [],
        "valid_records": 0,
        "rejected_records": 0,
    }
    try:
        for key in selected:
            source = SOURCE_DEFINITIONS[key]
            rows, provenance = fetch_source(client, source)
            records, rejected = parse_source_rows(source, rows, provenance)
            all_records.extend(records)
            report["sources"][key] = {
                "dataset_id": source.dataset_id,
                "resource_id": source.resource_id,
                "license_id": provenance["license_id"],
                "source_updated_at": provenance["source_updated_at"],
                "source_url": provenance["source_url"],
                "fetched_rows": len(rows),
                "valid_records": len(records),
                "rejected_records": len(rejected),
                "rejections_by_reason": {
                    reason: sum(item["reason"] == reason for item in rejected)
                    for reason in sorted({item["reason"] for item in rejected})
                },
            }
            report["rejected"].extend(rejected)
        report["valid_records"] = len(all_records)
        report["rejected_records"] = len(report["rejected"])
    finally:
        if owns_client:
            client.close()
    return all_records, report


def load_normalized_jsonl(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the stable JSONL hand-off format and reject malformed rows safely."""
    records: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            rejected.append({"line_number": line_number, "reason": "invalid_json"})
            continue
        if not isinstance(record, dict):
            rejected.append({"line_number": line_number, "reason": "record_not_object"})
            continue
        category = record.get("category")
        content = record.get("content")
        metadata = record.get("metadata")
        if not isinstance(category, str) or not 1 <= len(category.strip()) <= 50:
            rejected.append({"line_number": line_number, "reason": "invalid_category"})
            continue
        if not isinstance(content, str) or not content.strip():
            rejected.append({"line_number": line_number, "reason": "missing_content"})
            continue
        required_metadata = (
            "document_id", "chunk_id", "source_kind", "source_url",
            "source_dataset_id", "source_resource_id", "source_record_id",
        )
        if not isinstance(metadata, dict) or any(
            not isinstance(metadata.get(key), str) or not metadata[key].strip()
            for key in required_metadata
        ):
            rejected.append({"line_number": line_number, "reason": "missing_required_metadata"})
            continue
        key = (metadata["document_id"], metadata["chunk_id"])
        if key in seen:
            rejected.append({"line_number": line_number, "reason": "duplicate_document_chunk_id"})
            continue
        seen.add(key)
        records.append({
            "category": category.strip(),
            "content": content.strip(),
            "metadata": metadata,
        })
    return records, rejected
