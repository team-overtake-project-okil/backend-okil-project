"""Markdown parsing, chunking, embedding and indexing of AI knowledge.

Complements ``test_ai_knowledge_export.py`` and ``test_ai_knowledge_import.py``:
those check the curated project document and the importer happy paths; these
check that every Markdown element survives parsing, that chunking never drops
text, that invalid input fails cleanly and that re-indexing stays idempotent.
No model, network or database is used.
"""

import asyncio
import copy
import json
import math
import re
from contextlib import contextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.export_ai_knowledge import parse_knowledge_markdown
from app.import_ai_knowledge import embed_records, split_embedding_text, write_records
from app.models import AIKnowledge

DOCUMENT_COMMENT = (
    '<!-- knowledge-document: {"id":"okil","language":"uk",'
    '"reviewed_at":"2026-11-12","source_revision":"rev-1"} -->'
)
QUESTION = "Питання: Що таке Окіл?\n\nОкіл — карта комфорту."


def section(title="Окіл: тема", chunk_id="okil.topic", category="project", sources=("README.md",), body=QUESTION):
    meta = json.dumps({"id": chunk_id, "category": category, "sources": list(sources)}, ensure_ascii=False)
    return f"## {title}\n<!-- knowledge: {meta} -->\n\n{body}\n"


def document(*sections, comment=DOCUMENT_COMMENT, preamble="# Knowledge\n"):
    return preamble + comment + "\n\n" + "\n".join(sections)


def parse(markdown):
    return parse_knowledge_markdown(markdown, source="example.md")


# --- every Markdown element is preserved ---------------------------------------

ELEMENTS = {
    "subheading": "### Підзаголовок\n\nТекст під ним.",
    "paragraphs": "Перший абзац.\n\nДругий абзац.",
    "nested_bullets": "- a\n  - b\n    - c\n- d",
    "nested_numbered": "1. один\n2. два\n   1. вкладений\n   2. ще один",
    "table": "| A | B |\n|---|---|\n| 1 | 2 |\n| Кав'ярня | `код` |",
    "fenced_code": '```python\n# comment\nprint("x")\n```',
    "inline_code": "Викликає `answer_intent()` для відповіді.",
    "link": "[текст посилання](https://example.com/a?b=1&c=2)",
    "image": "![опис](https://example.com/i.png)",
    "blockquote": "> цитата\n> друга лінія\n>\n> новий абзац цитати",
    "emphasis": "**жирний**, _курсив_ та ~~закреслений~~",
    "unicode": "Є, Ї, Ґ, ’, «лапки», emoji 🙂 і нормальна кома.",
}


@pytest.mark.parametrize("snippet", ELEMENTS.values(), ids=ELEMENTS.keys())
def test_markdown_element_is_stored_verbatim_in_the_chunk(snippet):
    (record,) = parse(document(section(body=f"{QUESTION}\n\n{snippet}")))

    assert snippet in record["content"]


def test_all_elements_together_keep_their_order_and_exact_text():
    body = QUESTION + "\n\n" + "\n\n".join(ELEMENTS.values())
    (record,) = parse(document(section(body=body)))

    assert record["content"] == f"Окіл: тема\n\n{body}"
    positions = [record["content"].index(snippet) for snippet in ELEMENTS.values()]
    assert positions == sorted(positions)


def test_each_section_becomes_one_independent_chunk_in_document_order():
    records = parse(
        document(
            section("Перша", "okil.first", body=QUESTION + "\n\nПерша відповідь."),
            section("Друга", "okil.second", "health", ("docs/a.md", "docs/b.md"), QUESTION + "\n\nДруга відповідь."),
        )
    )

    assert [r["metadata"]["chunk_id"] for r in records] == ["okil.first", "okil.second"]
    assert "Друга відповідь." not in records[0]["content"]
    assert "Перша відповідь." not in records[1]["content"]
    assert records[1]["category"] == "health"
    assert records[1]["metadata"]["code_sources"] == ["docs/a.md", "docs/b.md"]


def test_source_metadata_is_stored_with_every_chunk_and_comments_are_not_embedded():
    records = parse(document(section(chunk_id="okil.one"), section("Інша", "okil.two")))

    for record in records:
        metadata = record["metadata"]
        assert metadata["document_id"] == "okil"
        assert metadata["source_document"] == "example.md"
        assert metadata["language"] == "uk"
        assert metadata["reviewed_at"] == "2026-11-12"
        assert metadata["source_revision"] == "rev-1"
        assert metadata["title"] in record["content"]
        assert "<!--" not in record["content"]


def test_category_of_exactly_fifty_characters_is_accepted():
    (record,) = parse(document(section(category="c" * 50)))
    assert record["category"] == "c" * 50


@pytest.mark.xfail(
    strict=True,
    reason="A '## ' line inside a fenced code block is treated as a new section, so valid documents fail to parse.",
)
def test_heading_like_line_inside_a_code_block_does_not_split_the_section():
    body = f"{QUESTION}\n\n```markdown\n## Це приклад, а не секція\n```"
    (record,) = parse(document(section(body=body)))
    assert "## Це приклад, а не секція" in record["content"]


# --- invalid and empty documents fail with a clear error --------------------------


@pytest.mark.parametrize("markdown", ["", "   \n\n", "# Тільки заголовок", DOCUMENT_COMMENT, document()])
def test_empty_or_section_less_documents_are_rejected(markdown):
    with pytest.raises(ValueError):
        parse(markdown)


def test_missing_or_duplicated_document_metadata_is_rejected():
    with pytest.raises(ValueError, match="knowledge-document"):
        parse(document(section(), comment=""))
    with pytest.raises(ValueError, match="knowledge-document"):
        parse(document(section(), comment=DOCUMENT_COMMENT + "\n" + DOCUMENT_COMMENT))


@pytest.mark.parametrize("key", ["id", "language", "reviewed_at", "source_revision"])
def test_every_document_metadata_field_is_required(key):
    meta = {"id": "okil", "language": "uk", "reviewed_at": "2026-11-12", "source_revision": "r"}
    meta[key] = " "
    comment = f"<!-- knowledge-document: {json.dumps(meta)} -->"
    with pytest.raises(ValueError, match=key):
        parse(document(section(), comment=comment))


@pytest.mark.parametrize(
    "bad_section",
    [
        section(chunk_id="Okil.Upper"),
        section(chunk_id="other.topic"),
        section(chunk_id="okil."),
        section(category=""),
        section(category="c" * 51),
        section(sources=()),
        section(sources=("",)),
        section(body="Відповідь без питання."),
        section(body="Питання: Лише питання?"),
        section(title=" "),
        "## Без коментаря\n\nПитання: Що?\n\nВідповідь.\n",
        '## Зламаний JSON\n<!-- knowledge: {"id": -->\n\nПитання: Що?\n\nВідповідь.\n',
    ],
)
def test_invalid_sections_reject_the_whole_document(bad_section):
    with pytest.raises(ValueError):
        parse(document(section(chunk_id="okil.valid"), bad_section))


def test_sources_must_be_a_list_not_a_bare_string():
    broken = section().replace('["README.md"]', '"README.md"')
    with pytest.raises(ValueError, match="sources"):
        parse(document(broken))


# --- chunking never drops text ------------------------------------------------------

LONG_TEXTS = {
    "words": "слово " * 200,
    "sentences": "Це речення про Окіл. " * 80 + "Кінцева думка.",
    "one_long_word": "х" * 500,
    "newlines_and_tabs": "рядок\n\nабзац\tз табуляцією " * 60,
    "markdown": "| a | b |\n|---|---|\n" + "| 1 | 2 |\n" * 80,
}


def squash(text):
    return re.sub(r"\s+", "", text)


@pytest.mark.parametrize("text", LONG_TEXTS.values(), ids=LONG_TEXTS.keys())
@pytest.mark.parametrize("limit", [20, 64, 120])
def test_split_windows_fit_the_limit_and_together_contain_all_the_text(text, limit):
    windows = split_embedding_text(text, len, limit)

    assert len(windows) > 1
    assert all(window and len(window) <= limit for window in windows)
    assert squash("".join(windows)) == squash(text)


def test_split_counts_with_the_supplied_token_counter_not_characters():
    text = "один два три чотири п'ять шість сім вісім дев'ять десять"
    windows = split_embedding_text(text, lambda value: len(value.split()), 3)

    assert all(len(window.split()) <= 3 for window in windows)
    assert " ".join(windows).split() == text.split()


def test_text_within_the_limit_is_returned_unchanged():
    assert split_embedding_text("Короткий текст", len, 100) == ["Короткий текст"]


def test_text_that_cannot_fit_any_window_is_an_error_not_silent_truncation():
    with pytest.raises(ValueError, match="token limit"):
        split_embedding_text("x", len, 0)


# --- embeddings ------------------------------------------------------------------------


def one_hot(axis, size=1024):
    vector = [0.0] * size
    vector[axis] = 3.0
    return vector


def record(chunk_id="okil.a", content="Текст", document_id="okil"):
    return {
        "category": "project",
        "content": content,
        "metadata": {"document_id": document_id, "chunk_id": chunk_id, "title": chunk_id},
    }


def test_each_record_gets_a_unit_vector_of_the_database_dimension_and_keeps_its_text():
    async def embed_text(text):
        return one_hot(len(text) % 5)

    records = [record("okil.a", "Перший"), record("okil.b", "Другий текст")]
    originals = copy.deepcopy(records)
    prepared = asyncio.run(embed_records(records, embed_text, len, 100))

    assert [item["metadata"]["chunk_id"] for item in prepared] == ["okil.a", "okil.b"]
    assert [item["content"] for item in prepared] == ["Перший", "Другий текст"]
    for item in prepared:
        assert len(item["embedding"]) == 1024
        assert math.sqrt(sum(value * value for value in item["embedding"])) == pytest.approx(1.0)
        assert item["metadata"]["embedding_dimensions"] == 1024
    assert records == originals, "embedding must not mutate the parsed records"


@pytest.mark.parametrize("size", [0, 1, 1023, 1025, 1536])
def test_embedding_of_the_wrong_dimension_is_rejected(size):
    async def embed_text(text):
        return [1.0] * size

    with pytest.raises(ValueError):
        asyncio.run(embed_records([record()], embed_text, len, 100))


def test_all_windows_of_a_long_record_contribute_to_its_single_embedding():
    seen = []

    async def embed_text(text):
        seen.append(text)
        return one_hot(0 if "початок" in text else 1)

    content = "початок " + "наповнення " * 30 + "кінець"
    (prepared,) = asyncio.run(embed_records([record(content=content)], embed_text, len, 60))

    assert len(seen) > 1
    assert prepared["content"] == content
    assert prepared["embedding"][0] > 0 and prepared["embedding"][1] > 0
    assert prepared["metadata"]["embedding_windows"] == len(seen)


def test_windows_weigh_equally_regardless_of_the_length_of_their_raw_vectors():
    async def embed_text(text):
        vector = [0.0] * 1024
        vector[0 if text == "початок" else 1] = 1000.0 if text == "початок" else 1.0
        return vector

    (prepared,) = asyncio.run(embed_records([record(content="початок кінець")], embed_text, len, 8))

    assert prepared["metadata"]["embedding_windows"] == 2
    assert prepared["embedding"][0] == pytest.approx(prepared["embedding"][1])
    assert prepared["embedding"][0] == pytest.approx(2**-0.5)


# --- writing and re-indexing ---------------------------------------------------------------


class FakeKnowledgeTable:
    """Minimal stand-in for the psycopg connection ``write_records`` expects."""

    def __init__(self, dimension="vector(1024)"):
        self.dimension = dimension
        self.rows = []
        self.writes = 0
        self.result = None

    @contextmanager
    def transaction(self):
        snapshot = copy.deepcopy(self.rows)
        try:
            yield
        except Exception:
            self.rows = snapshot
            raise

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, params=()):
        if sql.startswith(("SET LOCAL", "SELECT pg_advisory_xact_lock")):
            return
        if sql.startswith("SELECT format_type"):
            self.result = (self.dimension,)
        elif sql.startswith("SELECT id"):
            # The importer must only ever read (and lock) the rows of its own document.
            assert "metadata->>'document_id' = %s" in sql and len(params) == 1
            self.result = [row for row in self.rows if row[3]["document_id"] == params[0]]
        elif sql.startswith("INSERT"):
            category, content, vector, metadata = params
            assert len(json.loads(vector)) == 1024
            self.rows.append((len(self.rows) + 1, category, content, json.loads(metadata)))
            self.writes += 1
        elif sql.startswith("UPDATE"):
            category, content, vector, metadata, row_id = params
            self.rows = [
                (row_id, category, content, json.loads(metadata)) if row[0] == row_id else row for row in self.rows
            ]
            self.writes += 1
        else:
            pytest.fail(f"Unexpected SQL: {sql}")

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


def indexed(chunk_id="okil.a", content="Текст", document_id="okil", **metadata):
    item = record(chunk_id, content, document_id)
    item["metadata"].update(metadata)
    return {**item, "embedding": [1.0] + [0.0] * 1023}


def test_reindexing_an_unchanged_document_writes_nothing():
    table = FakeKnowledgeTable()
    batch = [indexed("okil.a"), indexed("okil.b", "Інше")]

    assert write_records(table, batch) == {"inserted": 2, "updated": 0, "unchanged": 0}
    writes_after_first_import = table.writes
    assert write_records(table, batch) == {"inserted": 0, "updated": 0, "unchanged": 2}
    assert table.writes == writes_after_first_import
    assert len(table.rows) == 2


def test_changed_text_or_metadata_updates_the_existing_row_instead_of_adding_one():
    table = FakeKnowledgeTable()
    write_records(table, [indexed(source_revision="rev-1")])

    assert write_records(table, [indexed(source_revision="rev-2")])["updated"] == 1
    assert write_records(table, [indexed("okil.a", "Новий текст", source_revision="rev-2")])["updated"] == 1
    assert len(table.rows) == 1
    assert table.rows[0][2] == "Новий текст"
    assert table.rows[0][3]["source_revision"] == "rev-2"


def test_new_sections_of_an_updated_document_are_added_next_to_existing_ones():
    table = FakeKnowledgeTable()
    write_records(table, [indexed("okil.a")])

    counts = write_records(table, [indexed("okil.a"), indexed("okil.b", "Додаткова тема")])

    assert counts == {"inserted": 1, "updated": 0, "unchanged": 1}
    assert [row[3]["chunk_id"] for row in table.rows] == ["okil.a", "okil.b"]


def test_same_chunk_id_in_another_document_is_never_touched():
    table = FakeKnowledgeTable()
    write_records(table, [indexed("okil.a", "Чужий текст", document_id="other")])

    write_records(table, [indexed("okil.a", "Наш текст")])
    write_records(table, [indexed("okil.a", "Наш оновлений текст")])

    by_document = {row[3]["document_id"]: row[2] for row in table.rows}
    assert by_document == {"other": "Чужий текст", "okil": "Наш оновлений текст"}


def test_a_batch_must_describe_exactly_one_document_with_unique_chunks():
    table = FakeKnowledgeTable()
    with pytest.raises(ValueError, match="exactly one"):
        write_records(table, [indexed("okil.a"), indexed("okil.b", document_id="other")])
    with pytest.raises(ValueError, match="exactly one"):
        write_records(table, [])
    with pytest.raises(ValueError, match="Duplicate input chunk IDs"):
        write_records(table, [indexed("okil.a"), indexed("okil.a", "Інший текст")])
    assert table.writes == 0 and table.rows == []


# --- semantic search readiness ---------------------------------------------------------------


def test_cosine_similarity_query_matches_the_hnsw_index_operator_class():
    """No search endpoint exists yet; this guards the query the index is built for."""
    query = select(AIKnowledge.content).order_by(AIKnowledge.embedding.cosine_distance([0.1] * 1024)).limit(3)
    sql = str(query.compile(dialect=postgresql.dialect()))
    (index,) = [index for index in AIKnowledge.__table__.indexes if index.name == "ix_ai_knowledge_embedding_hnsw"]

    assert "<=>" in sql
    assert index.dialect_options["postgresql"]["ops"] == {"embedding": "vector_cosine_ops"}
    assert index.dialect_options["postgresql"]["using"] == "hnsw"
