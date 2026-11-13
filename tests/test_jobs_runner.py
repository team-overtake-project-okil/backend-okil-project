import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import zipfile

import pytest

from app.jobs import runner


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ORDER = (
    "load_hexagons", "load_gtfs", "load_segments", "googlemaps",
    "load_shelters", "count_category_ratings",
)


@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.setenv("DB_URL", "postgresql+asyncpg://test:test@127.0.0.1/test")
    from app import database

    calls = []
    loops = []
    modules = {}
    for name in EXPECTED_ORDER:
        async def run(name=name, **kwargs):
            calls.append((name, kwargs))
            loops.append(asyncio.get_running_loop())

        modules[name] = SimpleNamespace(main=AsyncMock(side_effect=run))

    monkeypatch.setattr(runner, "import_module", lambda name: modules[name.rsplit(".", 1)[1]])
    preflight = AsyncMock()
    dispose = AsyncMock()
    monkeypatch.setattr(runner, "check_prerequisites", preflight)
    monkeypatch.setattr(database, "engine", SimpleNamespace(dispose=dispose))
    return SimpleNamespace(modules=modules, calls=calls, loops=loops,
                           preflight=preflight, dispose=dispose)


def test_full_run_awaits_dependencies_and_forwards_paths(pipeline, capsys):
    archive = "input data/transport.zip"
    sources = "input data/shelters"
    assert runner.main(["--gtfs-archive", archive, "--sources-dir", sources]) == 0
    assert [name for name, _ in pipeline.calls] == list(EXPECTED_ORDER)
    assert len(set(pipeline.loops)) == 1
    pipeline.preflight.assert_awaited_once()
    pipeline.dispose.assert_awaited_once()
    assert dict(pipeline.calls)["load_gtfs"] == {"zip_path": str(Path(archive))}
    assert dict(pipeline.calls)["load_segments"] == {"zip_path": str(Path(archive))}
    assert dict(pipeline.calls)["load_shelters"] == {"sources_dir": str(Path(sources))}
    output = capsys.readouterr()
    for index, name in enumerate(EXPECTED_ORDER, 1):
        assert f"[{index}/6] START {name}" in output.out
        assert f"[{index}/6] OK {name}" in output.out
    assert "All 6 jobs completed." in output.out
    assert not output.err


@pytest.mark.parametrize("failed_job", EXPECTED_ORDER)
def test_failure_reports_job_and_stops_dependents(pipeline, capsys, failed_job):
    pipeline.modules[failed_job].main.side_effect = ValueError("invalid source")
    assert runner.main([]) == 1
    failed_index = EXPECTED_ORDER.index(failed_job)
    assert [name for name, _ in pipeline.calls] == list(EXPECTED_ORDER[:failed_index])
    for name in EXPECTED_ORDER[failed_index + 1:]:
        pipeline.modules[name].main.assert_not_awaited()
    pipeline.dispose.assert_awaited_once()
    output = capsys.readouterr()
    assert f"FAILED {failed_job}: invalid source" in output.err
    assert f"OK {failed_job}" not in output.out
    assert "All 6 jobs completed." not in output.out


def test_failed_prerequisite_prevents_every_job(pipeline, capsys):
    pipeline.preflight.side_effect = ValueError("missing catalog")
    assert runner.main([]) == 1
    assert not pipeline.calls
    pipeline.dispose.assert_awaited_once()
    assert "FAILED prerequisites: missing catalog" in capsys.readouterr().err


def test_missing_job_dependency_fails_before_any_job(pipeline, monkeypatch, capsys):
    def import_job(name):
        if name.endswith("load_shelters"):
            raise ModuleNotFoundError("pandas")
        return pipeline.modules[name.rsplit(".", 1)[1]]

    monkeypatch.setattr(runner, "import_module", import_job)
    assert runner.main([]) == 1
    assert not pipeline.calls
    assert "FAILED prerequisites (import load_shelters): pandas" in capsys.readouterr().err


def test_interruption_has_nonzero_exit_and_disposes_engine(pipeline, capsys):
    pipeline.modules["load_hexagons"].main.side_effect = KeyboardInterrupt
    assert runner.main([]) == 130
    assert not pipeline.calls
    pipeline.dispose.assert_awaited_once()
    assert "interrupted" in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--help", "--dry-run"])
def test_module_preview_works_without_site_packages_or_configuration(option):
    result = subprocess.run(
        [sys.executable, "-B", "-S", "-m", "app.jobs", option],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    if option == "--dry-run":
        positions = [result.stdout.index(name) for name in EXPECTED_ORDER]
        assert positions == sorted(positions)
        assert "no jobs are executed" in result.stdout


def test_unknown_argument_exits_nonzero_without_running_jobs(pipeline):
    with pytest.raises(SystemExit) as error:
        runner.main(["--unknown-option"])
    assert error.value.code == 2
    assert not pipeline.calls


def test_real_cli_failure_returns_nonzero_before_running_jobs(tmp_path):
    result = subprocess.run(
        [sys.executable, "-B", "-m", "app.jobs", "--gtfs-archive", str(tmp_path / "missing.zip")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        env=dict(os.environ, DB_URL="postgresql+asyncpg://test:test@127.0.0.1/test",
                 SERPAPI_KEYS="test-key", PYTHONIOENCODING="utf-8"),
    )
    assert result.returncode == 1
    assert "FAILED prerequisites:" in result.stderr
    assert "missing.zip" in result.stderr
    assert "START" not in result.stdout


@pytest.mark.parametrize("dry_run", [True, False])
def test_cli_handles_unicode_paths_with_windows_pipe_encoding(tmp_path, dry_run):
    archive = tmp_path / "дані транспорту.zip"
    command = [sys.executable, "-B", "-m", "app.jobs", "--gtfs-archive", str(archive)]
    if dry_run:
        command.append("--dry-run")
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        env=dict(os.environ, DB_URL="postgresql+asyncpg://test:test@127.0.0.1/test",
                 SERPAPI_KEYS="test-key", PYTHONIOENCODING="cp1252"),
    )
    assert result.returncode == (0 if dry_run else 1)
    assert archive.name in (result.stdout if dry_run else result.stderr)
    assert "Traceback" not in result.stderr
    if not dry_run:
        assert "FAILED prerequisites:" in result.stderr
        assert "START" not in result.stdout


@pytest.fixture
def prerequisites(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_URL", "postgresql+asyncpg://test:test@127.0.0.1/test")
    from app import database
    from app.config import settings
    from app.jobs import googlemaps, load_shelters

    monkeypatch.setattr(settings, "SERPAPI_KEYS", "test-key")
    archive = tmp_path / "static.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            output.writestr(name, "")
    for name in load_shelters.SOURCE_FILES:
        (tmp_path / name).write_bytes(b"placeholder; preflight checks presence only")

    types = {poi["type"]: poi["category"] for poi in googlemaps.POI_TYPES}
    types.update(gtfs_stop="mobility", shelter="security")
    rows = [SimpleNamespace(type=name, category=category, radius_of_influence_1=100,
                            radius_of_influence_2=200, radius_of_influence_3=300)
            for name, category in types.items()]
    result = Mock()
    result.scalars.return_value.all.return_value = rows
    session = AsyncMock()
    session.execute.return_value = result
    session.__aenter__.return_value = session
    maker = Mock(return_value=session)
    monkeypatch.setattr(database, "async_session_maker", maker)
    return SimpleNamespace(
        args=SimpleNamespace(gtfs_archive=archive, sources_dir=tmp_path),
        modules={"googlemaps": googlemaps, "load_shelters": load_shelters},
        rows=rows, maker=maker, session=session, settings=settings,
    )


def test_prerequisites_only_read_catalog(prerequisites):
    asyncio.run(runner.check_prerequisites(prerequisites.args, prerequisites.modules))
    statement = prerequisites.session.execute.call_args.args[0]
    assert statement.is_select
    prerequisites.session.commit.assert_not_awaited()


@pytest.mark.parametrize("problem", ["key", "archive", "members", "shelters"])
def test_missing_inputs_fail_before_database_access(prerequisites, monkeypatch, problem):
    if problem == "key":
        monkeypatch.setattr(prerequisites.settings, "SERPAPI_KEYS", " , ")
    elif problem == "archive":
        prerequisites.args.gtfs_archive = prerequisites.args.sources_dir / "missing.zip"
    elif problem == "members":
        with zipfile.ZipFile(prerequisites.args.gtfs_archive, "w") as output:
            output.writestr("stops.txt", "")
    else:
        prerequisites.args.sources_dir = prerequisites.args.sources_dir / "missing"
    with pytest.raises((ValueError, FileNotFoundError)):
        asyncio.run(runner.check_prerequisites(prerequisites.args, prerequisites.modules))
    prerequisites.maker.assert_not_called()


@pytest.mark.parametrize("problem", ["missing", "category", "null", "zero", "nan", "order"])
def test_invalid_catalog_is_rejected(prerequisites, problem):
    if problem == "missing":
        prerequisites.rows.clear()
    elif problem == "category":
        prerequisites.rows[0].category = "wrong"
    else:
        prerequisites.rows[0].radius_of_influence_1 = {
            "null": None, "zero": 0, "nan": float("nan"), "order": 400,
        }[problem]
    with pytest.raises(ValueError, match="point_types"):
        asyncio.run(runner.check_prerequisites(prerequisites.args, prerequisites.modules))
    prerequisites.session.commit.assert_not_awaited()
