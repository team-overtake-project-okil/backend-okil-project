from sqlalchemy.dialects.postgresql import JSONB

from app.models import AIKnowledge, AI_EMBEDDING_DIMENSIONS


def test_ai_knowledge_table_has_searchable_embedding_schema():
    table = AIKnowledge.__table__

    assert table.name == "ai_knowledge"
    assert table.c.category.type.length == 50
    assert table.c.content.nullable is False
    assert table.c.embedding.type.dim == AI_EMBEDDING_DIMENSIONS
    assert table.c.embedding.nullable is False
    assert isinstance(table.c["metadata"].type, JSONB)
    assert "ix_ai_knowledge_embedding_hnsw" in {index.name for index in table.indexes}
