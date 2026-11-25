import asyncio
import pytest

from app.jobs import googlemaps

SAMPLE_HEX = "891e76883c3ffff"


class _FakeScalarResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self._values


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, grid_ids, executed_writes):
        self._grid_ids = grid_ids
        self._executed_writes = executed_writes

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def begin(self):
        return _FakeTx()

    async def execute(self, stmt):
        if stmt.__class__.__name__ == "Select":
            return _FakeScalarResult(self._grid_ids)
        self._executed_writes.append(stmt)
        return _FakeScalarResult([])


def _poi(external_id: str, poi_type: str, name: str) -> dict:
    return {
        "external_id": external_id,
        "name": name,
        "address": "м. Львів",
        "geom": "POINT(24.031 49.842)",
        "h3_id": SAMPLE_HEX,
        "type": poi_type,
    }


def _patch_googlemaps_io(monkeypatch, pois: list[dict]) -> list:
    executed_writes: list = []

    async def _noop_require_point_types(session, expected):
        return None

    monkeypatch.setattr(googlemaps, "require_point_types", _noop_require_point_types)
    monkeypatch.setattr(googlemaps, "fetch_all_pois", lambda **kwargs: pois)
    monkeypatch.setattr(
        googlemaps,
        "async_session_maker",
        lambda: _FakeSession([SAMPLE_HEX], executed_writes),
    )
    return executed_writes


def test_full_import_continues_when_leisure_type_returns_zero_points(monkeypatch):
    """python -m app.jobs calls googlemaps.main() without only_types and must not fail on an empty leisure type."""
    writes = _patch_googlemaps_io(
        monkeypatch,
        [
            _poi("p-pharmacy-1", "pharmacy", "Аптека"),
            _poi("p-cinema-1", "cinema", "Планета Кіно"),
            # museum, theater, entertainment intentionally have 0 points
        ],
    )

    asyncio.run(googlemaps.main())

    assert len(writes) == 1


def test_standalone_leisure_import_fails_when_selected_type_returns_zero_points(monkeypatch):
    """Explicit leisure import (--types ...) must abort before writing if any selected leisure type has 0 points."""
    writes = _patch_googlemaps_io(
        monkeypatch,
        [
            _poi("p-cinema-1", "cinema", "Планета Кіно"),
            _poi("p-museum-1", "museum", "Арсенал"),
            _poi("p-ent-1", "entertainment", "Аквапарк Пляж"),
            # theater intentionally has 0 points
        ],
    )

    with pytest.raises(RuntimeError, match="theater"):
        asyncio.run(
            googlemaps.main(
                only_types={"cinema", "museum", "theater", "entertainment"}
            )
        )

    assert writes == []


def test_standalone_leisure_import_force_allows_zero_points(monkeypatch):
    """--force overrides the strict leisure guard for standalone imports."""
    writes = _patch_googlemaps_io(
        monkeypatch,
        [
            _poi("p-cinema-1", "cinema", "Планета Кіно"),
        ],
    )

    asyncio.run(
        googlemaps.main(
            only_types={"cinema", "theater"},
            force=True,
        )
    )

    assert len(writes) == 1