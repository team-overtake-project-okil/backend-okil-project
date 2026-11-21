"""Dependency-ordered refresh of the automatic backend data imports."""

import argparse
import asyncio
from importlib import import_module
from pathlib import Path
import sys
import zipfile


# Reviewed/one-off imports are deliberately excluded; see docs/backend_jobs.md.
JOBS = (
    "load_hexagons",
    "load_gtfs",
    "load_segments",
    "googlemaps",
    "load_shelters",
    "count_category_ratings",
)


async def check_prerequisites(args, modules):
    from app.config import settings
    from app.database import async_session_maker
    from app.jobs.validation import require_point_types

    if not any(key.strip() for key in settings.SERPAPI_KEYS.split(",")):
        raise ValueError("Set SERPAPI_KEYS in .env for the Google Maps import")

    with zipfile.ZipFile(args.gtfs_archive) as archive:
        required = {"stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}
        missing = required - set(archive.namelist())
        if missing:
            raise ValueError(f"GTFS archive is missing: {', '.join(sorted(missing))}")

    for filename in modules["load_shelters"].SOURCE_FILES:
        path = args.sources_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"Missing shelter source: {path}")

    required_types = {poi["type"]: poi["category"] for poi in modules["googlemaps"].POI_TYPES}
    required_types.update(gtfs_stop="mobility", shelter="security")
    async with async_session_maker() as session:
        await require_point_types(session, required_types)


async def run_jobs(args):
    current_job = "prerequisites"
    engine = None
    try:
        print("Checking job prerequisites...", flush=True)
        # Import up front so missing dependencies fail before any writes.
        modules = {}
        for name in JOBS:
            current_job = f"prerequisites (import {name})"
            modules[name] = import_module(f"app.jobs.{name}")

        from app.database import engine

        current_job = "prerequisites"
        await check_prerequisites(args, modules)

        for index, name in enumerate(JOBS, start=1):
            current_job = name
            print(f"[{index}/{len(JOBS)}] START {name}", flush=True)
            kwargs = {}
            if name in {"load_gtfs", "load_segments"}:
                kwargs["zip_path"] = str(args.gtfs_archive)
            elif name == "load_shelters":
                kwargs["sources_dir"] = str(args.sources_dir)
            await modules[name].main(**kwargs)
            print(f"[{index}/{len(JOBS)}] OK {name}", flush=True)
    except Exception as exc:
        raise RuntimeError(f"{current_job}: {exc}") from exc
    finally:
        if engine is not None:
            await engine.dispose()

    print(f"All {len(JOBS)} jobs completed.", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs", description=__doc__)
    parser.add_argument(
        "--gtfs-archive", type=Path, default=Path("data/static.zip"),
        help="GTFS ZIP used by both transport jobs (default: data/static.zip)",
    )
    parser.add_argument(
        "--sources-dir", type=Path, default=Path("data/Sources"),
        help="Directory containing both shelter XLS files (default: data/Sources)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan only; no job imports, validation, network calls or DB access",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        print("Plan only; prerequisites are not checked and no jobs are executed.")
        print(f"GTFS archive: {args.gtfs_archive}")
        print(f"Shelter sources: {args.sources_dir}")
        for index, name in enumerate(JOBS, start=1):
            print(f"[{index}/{len(JOBS)}] {name}")
        return 0

    try:
        asyncio.run(run_jobs(args))
    except KeyboardInterrupt:
        print("Jobs interrupted; previously committed jobs are not rolled back.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"FAILED {exc}", file=sys.stderr, flush=True)
        return 1
    return 0
