"""Snapshot approved city datasets and import normalized records into ai_knowledge."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

from app.embedding_api import create_embedder
from app.import_ai_knowledge import (
    MAX_WINDOW_BYTES,
    embedding_input_size,
    write_records,
)
from app.spike_embeddings import embed_spike_records
from app.spike_data import (
    SOURCE_DEFINITIONS,
    SpikeSourceError,
    fetch_and_parse,
    load_normalized_jsonl,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_snapshot(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    path.write_text(payload, encoding="utf-8")


def _summarize_rejections(rejected: list[dict]) -> dict[str, int]:
    return dict(sorted(Counter(item["reason"] for item in rejected).items()))


def write_spike_records(
    connection, records: list[dict], *, replace_sources: bool = False,
) -> dict[str, int]:
    """Write each source document atomically using the existing idempotent writer."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        grouped[record["metadata"]["document_id"]].append(record)
    totals = {"inserted": 0, "updated": 0, "unchanged": 0}
    if replace_sources:
        sources = {f"lviv-open-data.{source.key}": source for source in SOURCE_DEFINITIONS.values()}
        for document_id, rows in grouped.items():
            source = sources.get(document_id)
            if source is None or any(
                row["metadata"].get("source_kind") != "city_open_data"
                or row["metadata"].get("source_dataset_id") != source.dataset_id
                or row["metadata"].get("source_resource_id") != source.resource_id
                for row in rows
            ):
                raise ValueError(f"Cannot replace unrecognized spike source: {document_id}")
        totals["deleted"] = 0

    for document_id in sorted(grouped):
        rows = grouped[document_id]
        if replace_sources:
            # The writer acquires the document lock. The outer transaction holds
            # it until the upserts and removal of stale rows both commit.
            with connection.transaction():
                counts = write_records(connection, rows)
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM public.ai_knowledge "
                        "WHERE metadata->>'document_id' = %s "
                        "AND metadata->>'source_kind' = 'city_open_data' "
                        "AND metadata->>'source_dataset_id' = %s "
                        "AND ((metadata->>'chunk_id') IS NULL "
                        "OR (metadata->>'chunk_id') <> ALL(%s))",
                        (
                            document_id,
                            sources[document_id].dataset_id,
                            [row["metadata"]["chunk_id"] for row in rows],
                        ),
                    )
                    counts["deleted"] = cursor.rowcount
        else:
            counts = write_records(connection, rows)
        for key in counts:
            totals[key] += counts[key]
    return totals


def _safe_database_target(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or "configured database"
    database = parsed.path.lstrip("/") or "unknown"
    return f"{host}/{database}"


async def import_snapshot(
    records: list[dict], *, apply: bool, replace_sources: bool = False,
) -> dict:
    embedder = create_embedder()
    prepared = await embed_spike_records(
        records,
        embedder.embed_many,
        embedding_input_size,
        MAX_WINDOW_BYTES,
    )
    result = {
        "embedded_records": len(prepared),
        "embedding_model": prepared[0]["metadata"]["embedding_model"] if prepared else "",
        "embedding_dimensions": prepared[0]["metadata"]["embedding_dimensions"] if prepared else 0,
        "database_write": False,
    }
    if not apply:
        return result

    import psycopg
    from app.config import settings

    database_url = settings.DB_URL
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if database_url.startswith(prefix):
            database_url = "postgresql://" + database_url[len(prefix):]
    with psycopg.connect(database_url, sslmode="require", connect_timeout=10) as connection:
        result["database_counts"] = write_spike_records(
            connection, prepared, replace_sources=replace_sources,
        )
    result["database_write"] = True
    result["database_target"] = _safe_database_target(database_url)
    return result


async def run(args: argparse.Namespace) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    if args.fetch:
        keys = list(SOURCE_DEFINITIONS) if args.fetch == "all" else [args.fetch]
        records, report = fetch_and_parse(keys)
        report["fetched_at"] = now
        report["valid_by_category"] = dict(sorted(Counter(record["category"] for record in records).items()))
        report["rejected_by_reason"] = _summarize_rejections(report["rejected"])
        if args.snapshot:
            _write_snapshot(args.snapshot, records)
            report["snapshot_path"] = str(args.snapshot)
        if args.validate_only:
            report["embedding_requested"] = False
            report["database_write"] = False
        if args.report:
            _write_json(args.report, report)
        return report

    if args.input is None:
        raise ValueError("Use --fetch <source|all> or --input <normalized.jsonl>.")
    records, rejected = load_normalized_jsonl(args.input.read_text(encoding="utf-8-sig"))
    report = {
        "input": str(args.input),
        "fetched_at": now,
        "valid_records": len(records),
        "rejected_records": len(rejected),
        "rejected_by_reason": _summarize_rejections(rejected),
        "rejected": rejected,
        "valid_by_category": dict(sorted(Counter(record["category"] for record in records).items())),
        "embedding_requested": False,
        "database_write": False,
    }
    if args.validate_only:
        if args.report:
            _write_json(args.report, report)
        return report
    if not records:
        if args.report:
            _write_json(args.report, report)
        raise ValueError("The input has no valid records; no embeddings or database writes were attempted.")

    if args.replace_sources and rejected:
        if args.report:
            _write_json(args.report, report)
        raise ValueError("Source replacement requires a complete, valid JSONL snapshot.")

    result = await import_snapshot(records, apply=args.apply, replace_sources=args.replace_sources)
    report.update(result)
    report["rejected_by_reason"] = _summarize_rejections(rejected)
    report["embedding_requested"] = True
    if args.report:
        _write_json(args.report, report)
    return report


def _print_report(report: dict) -> None:
    safe = dict(report)
    safe.pop("rejected", None)
    print(json.dumps(safe, ensure_ascii=False, indent=2))
    rejected = report.get("rejected", [])
    if rejected:
        print(f"Excluded {len(rejected)} invalid record(s); details are in --report.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--fetch",
        choices=("all", *SOURCE_DEFINITIONS),
        help="Fetch and validate one configured source or all approved sources from CKAN.",
    )
    source.add_argument("--input", type=Path, help="Read a normalized UTF-8 JSONL snapshot.")
    parser.add_argument("--snapshot", type=Path, help="Write valid fetched records as normalized UTF-8 JSONL.")
    parser.add_argument("--report", type=Path, help="Write source/import counts and rejected-row details as JSON.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_true", help="Validate records without embeddings or database access.")
    mode.add_argument("--apply", action="store_true", help="Write embeddings to the configured DB_URL.")
    parser.add_argument(
        "--replace-sources",
        action="store_true",
        help="With --apply, treat each included source as a complete snapshot and remove its stale records.",
    )
    args = parser.parse_args()

    if args.fetch and args.apply:
        parser.error("Fetch a reviewed snapshot first; --apply is allowed only with --input.")
    if args.input and args.snapshot:
        parser.error("--snapshot is only available when fetching a source.")
    if args.replace_sources and not args.apply:
        parser.error("--replace-sources requires --input and --apply.")
    try:
        report = asyncio.run(run(args))
    except ModuleNotFoundError as error:
        parser.exit(
            1,
            f"Import failed: missing Python module '{error.name}'.\n"
            "Install dependencies with the same Python interpreter:\n"
            "  python -m pip install -r requirements.txt\n",
        )
    except Exception as error:
        safe_types = (ValueError, SpikeSourceError)
        message = str(error) if isinstance(error, safe_types) else type(error).__name__
        parser.exit(1, f"Import failed: {message}\n")
    _print_report(report)
    if report.get("database_target"):
        print(f"Database target: {report['database_target']}")


if __name__ == "__main__":
    main()
