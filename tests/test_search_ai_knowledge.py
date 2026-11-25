import asyncio

from sqlalchemy.dialects import postgresql

from app import search_ai_knowledge


class FakeResult:
    def mappings(self):
        return self

    def all(self):
        return [{
            "text": "Комунальний заклад культури має вказані засоби доступності.",
            "category": "enjoyment",
            "similarity": 0.84,
            "metadata": {
                "document_id": "lviv-open-data.cultural_facilities",
                "chunk_id": "lviv.cultural_facilities.abc123",
                "title": "Міський музей",
                "source_kind": "city_open_data",
                "source_record_id": "culture-7",
                "source_url": "https://example.test/culture",
                "source_updated_at": "2026-01-14",
                "private_field": "must not be returned",
            },
        }]


class FakeAsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class FakeSession:
    def __init__(self, _engine, **_kwargs):
        self.queries = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return FakeAsyncContext()

    async def execute(self, statement):
        if not hasattr(statement, "text"):
            self.queries.append(statement)
        return FakeResult()


class FakeEngine:
    async def dispose(self):
        pass


def test_semantic_search_returns_source_identity_and_provenance(monkeypatch):
    import sqlalchemy.ext.asyncio

    monkeypatch.setattr(sqlalchemy.ext.asyncio, "AsyncSession", FakeSession)
    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine", lambda **_kwargs: FakeEngine())

    class Embedder:
        async def __call__(self, query):
            assert query == "Які культурні заклади мають засоби доступності?"
            return [0.0] * 1024

    monkeypatch.setattr(search_ai_knowledge, "create_embedder", lambda: Embedder())

    result = asyncio.run(search_ai_knowledge.search(
        "Які культурні заклади мають засоби доступності?",
        limit=5,
        min_similarity=0.52,
    ))

    assert result["results"] == [{
        "text": "Комунальний заклад культури має вказані засоби доступності.",
        "category": "enjoyment",
        "similarity": 0.84,
        "metadata": {
            "document_id": "lviv-open-data.cultural_facilities",
            "chunk_id": "lviv.cultural_facilities.abc123",
            "title": "Міський музей",
            "source_kind": "city_open_data",
            "source_record_id": "culture-7",
            "source_url": "https://example.test/culture",
            "source_updated_at": "2026-01-14",
        },
    }]
    assert result["query"] == "Які культурні заклади мають засоби доступності?"


def test_semantic_search_uses_cosine_index_and_source_filters(monkeypatch):
    import sqlalchemy.ext.asyncio

    captured = []

    class CapturingSession(FakeSession):
        async def execute(self, statement):
            if hasattr(statement, "compile") and not hasattr(statement, "text"):
                captured.append(statement)
            return FakeResult()

    monkeypatch.setattr(sqlalchemy.ext.asyncio, "AsyncSession", CapturingSession)
    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine", lambda **_kwargs: FakeEngine())

    class Embedder:
        async def __call__(self, _query):
            return [0.0] * 1024

    monkeypatch.setattr(search_ai_knowledge, "create_embedder", lambda: Embedder())

    asyncio.run(search_ai_knowledge.search("майданчик з тренажерами", limit=5, min_similarity=0.52))

    compiled = captured[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "ai_knowledge.metadata" in sql
    assert "ai_knowledge.embedding <=>" in sql
    assert "metadata ->>" in sql
    assert search_ai_knowledge.EMBEDDING_MODEL in compiled.params.values()
    assert str(search_ai_knowledge.EMBEDDING_DIMENSIONS) in compiled.params.values()
