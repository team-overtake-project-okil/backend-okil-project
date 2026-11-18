import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import zipfile

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert


@pytest.fixture
def jobs(monkeypatch):
    monkeypatch.setenv("DB_URL", "postgresql+asyncpg://test:test@127.0.0.1/test")
    from app.jobs import googlemaps, load_gtfs, load_hexagons, load_segments, load_shelters

    return SimpleNamespace(google=googlemaps, gtfs=load_gtfs, hexagons=load_hexagons,
                           segments=load_segments, shelters=load_shelters)


@pytest.fixture
def gtfs_archive(tmp_path):
    archive = tmp_path / "transport.zip"
    files = {
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\na,First,49.84,24.03\nb,Second,49.85,24.04\n",
        "routes.txt": "route_id,route_short_name\nr,1\n",
        "trips.txt": "route_id,trip_id,direction_id\nr,t,0\n",
        "stop_times.txt": "trip_id,stop_id,stop_sequence\nt,a,1\nt,b,2\n",
    }
    with zipfile.ZipFile(archive, "w") as output:
        for name, content in files.items():
            output.writestr(name, content)
    return str(archive)


def test_hexagon_and_gtfs_inserts_match_current_schema(jobs, gtfs_archive, monkeypatch):
    from app.models import HexTable

    hexagons = jobs.hexagons.generate_lviv_hexagons(res=7)
    assert hexagons
    pg_insert(HexTable).values(hexagons[:1]).compile(dialect=postgresql.dialect())
    statements = []

    async def execute(statement):
        # Compile real PostgreSQL statements: obsolete mobility/name columns
        # must fail here even though this test never connects to a database.
        statement.compile(dialect=postgresql.dialect())
        statements.append(statement)
        if statement.is_select:
            result = Mock()
            result.scalars.return_value.all.return_value = [SimpleNamespace(
                type="gtfs_stop", category="mobility", radius_of_influence_1=200,
                radius_of_influence_2=400, radius_of_influence_3=700,
            )]
            return result
        if statement.is_insert and statement.table.name == "stops":
            return [SimpleNamespace(id=1, external_id="a"), SimpleNamespace(id=2, external_id="b")]
        if statement.is_insert and statement.table.name == "routes":
            return [SimpleNamespace(id=3, external_id="r_0")]
        return Mock(rowcount=1)

    session = AsyncMock()
    session.__aenter__.return_value = session
    session.begin = Mock(return_value=AsyncMock())
    session.execute.side_effect = execute
    monkeypatch.setattr(jobs.gtfs, "async_session_maker", Mock(return_value=session))
    asyncio.run(jobs.gtfs.main(gtfs_archive))
    inserted_tables = [statement.table.name for statement in statements if statement.is_insert]
    assert inserted_tables == ["hex_table", "stops", "routes", "stop_route_connectable"]


@pytest.mark.parametrize("problem", ["missing", "bad_zip", "missing_member", "empty"])
def test_gtfs_source_failures_propagate_before_database_writes(jobs, tmp_path, monkeypatch, problem):
    archive = tmp_path / "invalid.zip"
    if problem == "bad_zip":
        archive.write_text("not a zip", encoding="utf-8")
    elif problem in {"missing_member", "empty"}:
        with zipfile.ZipFile(archive, "w") as output:
            if problem == "empty":
                for name in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
                    output.writestr(name, "")
    maker = Mock(side_effect=AssertionError("database must not be accessed"))
    monkeypatch.setattr(jobs.gtfs, "async_session_maker", maker)
    with pytest.raises((FileNotFoundError, zipfile.BadZipFile, KeyError, ValueError)):
        asyncio.run(jobs.gtfs.main(str(archive)))
    maker.assert_not_called()


@pytest.mark.parametrize("problem", ["missing_routes", "bad_archive", "empty_segments"])
def test_segment_failures_are_not_successful_skips(jobs, monkeypatch, problem):
    stops, routes = Mock(), Mock()
    stops.all.return_value = [(1, "a"), (2, "b")]
    routes.all.return_value = [] if problem == "missing_routes" else [(3, "r_0")]
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.execute.side_effect = [stops, routes]
    maker = Mock(return_value=session)
    monkeypatch.setattr(jobs.segments, "async_session_maker", maker)
    parser = Mock(side_effect=zipfile.BadZipFile("invalid transport input"))
    if problem == "empty_segments":
        parser = Mock(return_value={"segments": {}, "segment_links": [], "straight_fallbacks": 0})
    monkeypatch.setattr(jobs.segments, "parse_archive", parser)
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        asyncio.run(jobs.segments.main("unused.zip"))
    maker.assert_called_once()  # read existing IDs only; no write transaction


def test_shelter_import_requires_both_files(jobs, tmp_path, monkeypatch):
    first, second = (tmp_path / name for name in jobs.shelters.SOURCE_FILES)
    first.write_bytes(b"placeholder")
    read_excel = Mock(side_effect=AssertionError("validate all paths before reading"))
    monkeypatch.setattr(jobs.shelters.pd, "read_excel", read_excel)
    with pytest.raises(FileNotFoundError, match="Missing shelter source"):
        jobs.shelters.load_shelter_data(str(first), str(second))
    read_excel.assert_not_called()


def test_empty_shelter_import_is_a_failure(jobs, monkeypatch):
    monkeypatch.setattr(jobs.shelters, "load_shelter_data", Mock(return_value=[]))
    maker = Mock(side_effect=AssertionError("no empty write"))
    monkeypatch.setattr(jobs.shelters, "async_session_maker", maker)
    with pytest.raises(ValueError):
        asyncio.run(jobs.shelters.main(sources_dir="source files"))
    maker.assert_not_called()


def test_exhausted_serpapi_keys_abort_instead_of_returning_partial_data(jobs, monkeypatch, capsys):
    response = Mock(ok=False, status_code=429)
    response.json.return_value = {"error": "quota exceeded"}
    request = Mock(return_value=response)
    monkeypatch.setattr(jobs.google.requests, "get", request)
    with pytest.raises(RuntimeError, match="SerpAPI"):
        jobs.google.fetch_poi_type("bakery", "bakery", [(49.84, 24.03)], ["private-key"], 0)
    assert request.call_count == 1
    assert request.call_args.kwargs["timeout"] == 60
    assert "private-key" not in capsys.readouterr().out


def test_serpapi_can_rotate_keys_and_treat_no_results_as_empty(jobs, monkeypatch):
    denied = Mock(ok=False, status_code=429)
    denied.json.return_value = {"error": "quota exceeded"}
    empty = Mock(ok=True, status_code=200)
    empty.json.return_value = {"error": "Google Maps hasn't returned any results for this query."}
    request = Mock(side_effect=[denied, empty])
    monkeypatch.setattr(jobs.google.requests, "get", request)
    points, key_index = jobs.google.fetch_poi_type(
        "bakery", "bakery", [(49.84, 24.03)], ["key-one", "key-two"], 0,
    )
    assert points == {}
    assert key_index == 1
    assert request.call_count == 2


@pytest.mark.parametrize("problem", ["network", "http"])
def test_serpapi_failures_propagate_without_credentials(jobs, monkeypatch, problem):
    if problem == "network":
        request = Mock(side_effect=jobs.google.requests.Timeout("url?api_key=private-key"))
    else:
        response = Mock(ok=False, status_code=500)
        response.json.return_value = {}
        request = Mock(return_value=response)
    monkeypatch.setattr(jobs.google.requests, "get", request)
    with pytest.raises(RuntimeError, match="SerpAPI") as error:
        jobs.google.fetch_poi_type("bakery", "bakery", [(49.84, 24.03)], ["private-key"], 0)
    assert "private-key" not in str(error.value)


def test_google_import_rejects_empty_results(jobs, monkeypatch):
    monkeypatch.setattr(jobs.google, "fetch_all_pois", Mock(return_value=[]))
    with pytest.raises(ValueError, match="POI"):
        asyncio.run(jobs.google.main(dry_run=True))
