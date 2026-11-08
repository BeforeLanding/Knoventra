import re

import pytest

from knoventra.ingestion.chunking import MAX_CHARS, _bounded_parts, _split_long, chunk_markdown, chunk_pdf
from knoventra.retrieval.identifiers import evidence_identifiers


@pytest.mark.parametrize("text", [
    "中" * (MAX_CHARS + 1), "字" * (MAX_CHARS * 4 + 17),
    "a" * (MAX_CHARS * 3 + 1), ("English words " * 300),
    "繁體文字，測試段落。" * 200, "\n\t " * (MAX_CHARS + 1),
])
def test_long_text_has_bounded_nonempty_chunks_and_preserves_nonwhitespace_text(text):
    chunks = _split_long(text)
    assert all(chunk and len(chunk) <= MAX_CHARS for chunk in chunks)
    assert re.sub(r"\s", "", "".join(chunks)) == re.sub(r"\s", "", text)


@pytest.mark.parametrize("token", ["E17-42", "P03-11", "PN-900-A", "Ｅ１７－４２", "E17 ‐ 42", "maintenance"])
def test_short_word_or_code_crossing_the_boundary_is_preserved(token):
    text = "中" * (MAX_CHARS - 3) + token + "文" * MAX_CHARS
    chunks = _split_long(text)
    assert all(len(chunk) <= MAX_CHARS for chunk in chunks)
    assert any(token in chunk for chunk in chunks)
    assert "".join(chunks) == text
    assert set().union(*(evidence_identifiers("", chunk) for chunk in chunks)) == evidence_identifiers("", text)


def test_bounded_partition_preserves_all_whitespace_and_never_produces_empty_pieces():
    text = (" " * 698 + "E17 - 42" + "\t" * 10 + "段落") * 10
    pieces = list(_bounded_parts(text))
    assert "".join(pieces) == text
    assert all(0 < len(piece) <= MAX_CHARS for piece in pieces)


def test_long_word_requires_hard_split_and_still_preserves_all_characters():
    word = "prefix-" + "X" * (MAX_CHARS * 4)
    pieces = list(_bounded_parts(word))
    assert "".join(pieces) == word
    assert all(0 < len(piece) <= MAX_CHARS for piece in pieces)


def test_markdown_preserves_page_heading_and_code_for_every_long_chunk():
    text = "# 手册\n<!-- page: 7 -->\n## E17-42 排查\n" + "无标点的长正文" * 300
    chunks = chunk_markdown(text)
    assert len(chunks) > 1
    assert all(chunk.page == 7 and chunk.section_path == "手册 > E17-42 排查" for chunk in chunks)
    assert all(0 < len(chunk.text) <= MAX_CHARS for chunk in chunks)
    assert all("E17-42" in evidence_identifiers(chunk.section_path, chunk.text) for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == "无标点的长正文" * 300


def test_small_existing_sentence_chunks_remain_unchanged():
    text = "第一句。第二句！这是 E17-42 的步骤。"
    assert _split_long(text) == [text]
    assert [c.text for c in chunk_markdown("# 手册\n" + text)] == [text]


def test_pdf_long_extracted_paragraph_is_split_with_page_identity(tmp_path):
    import pymupdf
    path = tmp_path / "long.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=4000, height=4000)
        # A single long text line avoids relying on paragraph reflow.
        page.insert_text((20, 30), "E17-42 " + "maintenance " * 140, fontsize=3)
        doc.save(path)
    chunks = chunk_pdf(path)
    assert len(chunks) > 1
    assert all(c.page == 1 and c.section_path == "" and 0 < len(c.text) <= MAX_CHARS for c in chunks)
    assert "E17-42" in set().union(*(evidence_identifiers(c.section_path, c.text) for c in chunks))


def test_ingestion_sends_bounded_chunks_to_encoder_and_keeps_original_hash(db, enc, tmp_path):
    import hashlib
    from collections import Counter
    from sqlalchemy import select
    from knoventra.db.models import Chunk, KnowledgeBase
    from knoventra.ingestion.service import add_revision
    path = tmp_path / "long.md"
    original = "# 长段落手册\n## E17-42 排查\n" + "维护说明" * 1000
    path.write_text(original, encoding="utf-8")
    batches = []
    class RecordingEncoder:
        def encode(self, texts):
            assert all(0 < len(text) <= MAX_CHARS for text in texts)
            batches.append(list(texts))
            return enc.encode(texts)
    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == "it-public"))
    revision = add_revision(db, RecordingEncoder(), kb_id=kb.id, title="有界长段落手册", lang="zh-Hans", path=path)
    stored = db.scalars(select(Chunk).where(Chunk.revision_id == revision.id)).all()
    expected = chunk_markdown(original)
    assert len(batches) == 2 and len(stored) == len(expected) > 1
    assert Counter(row.text for row in stored) == Counter(chunk.text for chunk in expected)
    assert all(row.page == 1 and row.section_path == "长段落手册 > E17-42 排查" for row in stored)
    assert revision.status == "DRAFT" and revision.content_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert path.read_text(encoding="utf-8") == original
