import asyncio
import copy
import json
from contextlib import contextmanager

import pytest

from app.import_ai_knowledge import embed_records, split_embedding_text, write_records


def test_missing_dependency_names_module_and_install_command(monkeypatch, capsys):
    from app import import_ai_knowledge

    async def missing_dependency(args):
        raise ModuleNotFoundError("hidden internal error", name="sentence_transformers")

    monkeypatch.setattr(import_ai_knowledge, "run", missing_dependency)
    monkeypatch.setattr("sys.argv", ["import_ai_knowledge"])
    with pytest.raises(SystemExit) as error:
        import_ai_knowledge.main()
    assert error.value.code == 1
    output = capsys.readouterr().err
    assert "sentence_transformers" in output
    assert "python -m pip install -r requirements.txt" in output
    assert "hidden internal error" not in output


def record(content="Окіл: тестова відповідь."):
    return {
        "category": "project", "content": content,
        "metadata": {"document_id": "test", "chunk_id": "okil.test"},
    }


def test_split_preserves_later_text_and_respects_model_limit():
    text = "Початок документа. " * 20 + "Важлива відповідь наприкінці."
    windows = split_embedding_text(text, len, 50)
    assert all(len(window) <= 50 for window in windows)
    assert " ".join(windows).split() == text.split()
    assert "Важлива відповідь наприкінці." in " ".join(windows)


def test_embedding_uses_every_window_and_records_model_provenance():
    calls = []

    async def embed_text(text):
        calls.append(text)
        return [0.0, 2.0] + [0.0] * 382

    prepared = asyncio.run(embed_records([record("word " * 50)], embed_text, len, 40))
    assert len(calls) > 1
    assert prepared[0]["content"] == "word " * 50
    assert prepared[0]["embedding"] == [0.0, 1.0] + [0.0] * 382
    assert prepared[0]["metadata"]["embedding_windows"] == len(calls)
    assert prepared[0]["metadata"]["embedding_dimensions"] == 384
    assert len(prepared[0]["metadata"]["content_sha256"]) == 64


@pytest.mark.parametrize("vector", [[0.0] * 384, [1.0] * 3, [float("nan")] * 384])
def test_invalid_embeddings_never_reach_database(vector):
    async def embed_text(text):
        return vector

    with pytest.raises(ValueError):
        asyncio.run(embed_records([record()], embed_text, len, 100))


class FakeConnection:
    def __init__(self, dimension="vector(384)"):
        self.dimension = dimension
        self.rows = []
        self.result = None
        self.fail_insert = False
        self.writes = 0

    @contextmanager
    def transaction(self):
        previous = copy.deepcopy(self.rows)
        try:
            yield
        except Exception:
            self.rows = previous
            raise

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, params=()):
        if sql.startswith("SET LOCAL"):
            return
        if sql.startswith("SELECT format_type"):
            self.result = (self.dimension,)
        elif sql.startswith("SELECT id"):
            self.result = [row for row in self.rows if row[3]["document_id"] == params[0]]
        elif sql.startswith("INSERT"):
            if self.fail_insert:
                raise RuntimeError("simulated write failure")
            category, content, vector, metadata = params
            assert len(json.loads(vector)) == 384
            self.rows.append((len(self.rows) + 1, category, content, json.loads(metadata)))
            self.writes += 1
        elif sql.startswith("UPDATE"):
            category, content, vector, metadata, row_id = params
            self.rows = [
                (row_id, category, content, json.loads(metadata)) if row[0] == row_id else row
                for row in self.rows
            ]
            self.writes += 1
        else:
            assert sql.startswith("SELECT pg_advisory_xact_lock")

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


def prepared(content="Окіл"):
    return {**record(content), "embedding": [1.0] + [0.0] * 383}


def test_repeat_import_updates_same_row_and_preserves_other_documents():
    db = FakeConnection()
    db.rows = [(1, "other", "Інше джерело", {"document_id": "other", "chunk_id": "other"})]
    assert write_records(db, [prepared()])["inserted"] == 1
    assert write_records(db, [prepared()])["unchanged"] == 1
    assert write_records(db, [prepared("Оновлений текст")])["updated"] == 1
    assert len(db.rows) == 2
    assert db.rows[0][2] == "Інше джерело"
    assert db.rows[1][2] == "Оновлений текст"


def test_wrong_column_dimension_blocks_all_writes():
    db = FakeConnection("vector(768)")
    with pytest.raises(ValueError, match=r"vector\(384\)"):
        write_records(db, [prepared()])
    assert db.writes == 0


def test_write_failure_rolls_back_previous_updates():
    db = FakeConnection()
    write_records(db, [prepared()])
    before = copy.deepcopy(db.rows)
    second = prepared("Друга тема")
    second["metadata"]["chunk_id"] = "okil.second"
    db.fail_insert = True
    with pytest.raises(RuntimeError):
        write_records(db, [prepared("Змінений текст"), second])
    assert db.rows == before


def test_existing_duplicates_abort_without_guessing_which_to_overwrite():
    db = FakeConnection()
    db.rows = [
        (1, "project", "first", record()["metadata"]),
        (2, "project", "second", record()["metadata"]),
    ]
    with pytest.raises(ValueError, match="Existing duplicate"):
        write_records(db, [prepared()])
    assert db.writes == 0
