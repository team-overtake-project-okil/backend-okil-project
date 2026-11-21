import pytest


@pytest.fixture
def redis_server():
    """A private in-memory Redis server for one test.

    ``fakeredis`` clients created without an explicit server share one
    process-wide server, so a bare ``FakeAsyncRedis()`` can leak sessions
    between tests. Passing a fresh ``FakeServer`` guarantees isolation and,
    unlike ``FLUSHDB``, never needs to wipe shared data.
    """
    fakeredis = pytest.importorskip("fakeredis", reason="Install tests/requirements-chat.txt")
    return fakeredis.FakeServer()
