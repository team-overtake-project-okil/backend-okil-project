"""Export curated Markdown sections to JSONL; no embeddings or database writes."""

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / "docs" / "ai_context.md"
_DOCUMENT = re.compile(r"^<!-- knowledge-document: (.+) -->$", re.MULTILINE)
_CHUNK = re.compile(r"^<!-- knowledge: (.+) -->$", re.MULTILINE)


def parse_knowledge_markdown(markdown: str, *, source: str) -> list[dict]:
    """Each H2 is one independent record; keep provenance out of embedding text."""
    documents = _DOCUMENT.findall(markdown)
    if len(documents) != 1:
        raise ValueError("Exactly one knowledge-document metadata comment is required")
    document = json.loads(documents[0])
    for key in ("id", "language", "reviewed_at", "source_revision"):
        if not isinstance(document.get(key), str) or not document[key].strip():
            raise ValueError(f"Missing document metadata: {key}")

    records = []
    seen_ids = set()
    for section in re.split(r"^## ", markdown, flags=re.MULTILINE)[1:]:
        title, separator, body = section.partition("\n")
        comments = _CHUNK.findall(body)
        if not separator or not title.strip() or len(comments) != 1:
            raise ValueError("Each H2 section needs a title and one knowledge comment")
        metadata = json.loads(comments[0])
        chunk_id = metadata.get("id")
        category = metadata.get("category")
        sources = metadata.get("sources")
        if not isinstance(chunk_id, str) or not re.fullmatch(r"okil\.[a-z0-9_.]+", chunk_id):
            raise ValueError("Chunk ID must be a stable okil.* identifier")
        if chunk_id in seen_ids:
            raise ValueError(f"Duplicate chunk ID: {chunk_id}")
        if not isinstance(category, str) or not 1 <= len(category) <= 50:
            raise ValueError("Chunk category must contain 1..50 characters")
        if not isinstance(sources, list) or not sources or not all(
            isinstance(item, str) and item.strip() for item in sources
        ):
            raise ValueError("Chunk sources must be a nonempty list of paths")
        content = _CHUNK.sub("", body).strip()
        if not content.startswith("Питання:") or not content.partition("\n\n")[2].strip():
            raise ValueError(f"Chunk {chunk_id} needs retrieval questions and an answer")
        seen_ids.add(chunk_id)
        records.append({
            "category": category,
            "content": f"{title.strip()}\n\n{content}",
            "metadata": {
                "document_id": document["id"],
                "chunk_id": chunk_id,
                "title": title.strip(),
                "source_document": source,
                "language": document["language"],
                "reviewed_at": document["reviewed_at"],
                "source_revision": document["source_revision"],
                "code_sources": sources,
            },
        })
    if not records:
        raise ValueError("No knowledge sections found")
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, help="UTF-8 JSONL file; defaults to stdout")
    args = parser.parse_args()
    records = parse_knowledge_markdown(
        args.source.read_text(encoding="utf-8"), source=args.source.name,
    )
    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(payload)


if __name__ == "__main__":
    main()
