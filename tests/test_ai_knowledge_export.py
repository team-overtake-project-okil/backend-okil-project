import json
import re
import pytest

from app.export_ai_knowledge import DEFAULT_SOURCE, parse_knowledge_markdown


def test_curated_knowledge_exports_self_contained_records_with_provenance():
    markdown = DEFAULT_SOURCE.read_text(encoding="utf-8")
    records = parse_knowledge_markdown(markdown, source=DEFAULT_SOURCE.name)
    assert len(records) == len(re.findall(r"^## ", markdown, re.MULTILINE))
    ids = {record["metadata"]["chunk_id"] for record in records}
    assert len(ids) == len(records)
    assert {
        "okil.overall_score", "okil.zero_score", "okil.category.health",
        "okil.mobility.traveltime", "okil.mobility.stored", "okil.mobility.routes",
    } <= ids
    for record in records:
        assert set(record) == {"category", "content", "metadata"}
        assert record["metadata"]["language"] == "uk"
        assert record["metadata"]["source_document"] == "ai_context.md"
        assert record["metadata"]["title"] in record["content"]
        assert "Питання:" in record["content"]
        assert "<!--" not in record["content"]
        assert "INTENT_DESCRIPTIONS" not in record["content"]
        assert json.loads(json.dumps(record, ensure_ascii=False)) == record
        for source in record["metadata"]["code_sources"]:
            if not source.startswith("frontend:"):
                assert (DEFAULT_SOURCE.parents[1] / source).is_file(), source


def minimal_document(sections):
    return (
        '# Knowledge\n<!-- knowledge-document: '
        '{"id":"okil","language":"uk","reviewed_at":"2026-11-12",'
        '"source_revision":"example"} -->\n' + sections
    )


def section(chunk_id="okil.example", answer="Окіл пояснює механіку."):
    return (
        f'## Окіл: приклад\n<!-- knowledge: {{"id":"{chunk_id}",'
        '"category":"project","sources":["README.md"]} -->\n\n'
        f'Питання: Що таке Окіл?\n\n{answer}\n'
    )


def test_rejects_duplicate_ids_instead_of_exporting_duplicate_rows():
    with pytest.raises(ValueError, match="Duplicate chunk ID"):
        parse_knowledge_markdown(minimal_document(section() * 2), source="example.md")


@pytest.mark.parametrize("document", [
    "# Empty document",
    minimal_document(""),
    minimal_document("## Missing metadata\nText\n"),
    minimal_document(section(answer="")),
])
def test_rejects_incomplete_knowledge(document):
    with pytest.raises(ValueError):
        parse_knowledge_markdown(document, source="example.md")
