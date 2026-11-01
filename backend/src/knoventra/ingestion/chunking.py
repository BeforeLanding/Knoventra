"""Parse Markdown / PDF into chunks that keep page number and section path."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_CHARS = 700
_HEADING = re.compile(r"^(#{1,4})\s+(.*\S)\s*$")
_PAGE_MARK = re.compile(r"^<!--\s*page:\s*(\d+)\s*-->\s*$")


@dataclass
class RawChunk:
    page: int | None
    section_path: str
    text: str


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    parts, cur = [], ""
    for para in re.split(r"(?<=[。！？.!?\n])", text):
        if cur and len(cur) + len(para) > MAX_CHARS:
            parts.append(cur.strip())
            cur = ""
        cur += para
    if cur.strip():
        parts.append(cur.strip())
    return parts


def chunk_markdown(md: str) -> list[RawChunk]:
    """Split by headings. A `<!-- page: N -->` line sets the page for following text
    (Markdown has no native pages; manuals mark them explicitly)."""
    chunks: list[RawChunk] = []
    stack: list[tuple[int, str]] = []
    page: int | None = 1
    buf: list[str] = []
    buf_page = page

    def flush():
        nonlocal buf
        body = "\n".join(buf).strip()
        buf = []
        if not body:
            return
        path = " > ".join(t for _, t in stack)
        for piece in _split_long(body):
            chunks.append(RawChunk(buf_page, path, piece))

    for line in md.splitlines():
        m = _PAGE_MARK.match(line)
        if m:
            flush()
            page = int(m.group(1))
            buf_page = page
            continue
        h = _HEADING.match(line)
        if h:
            flush()
            level = len(h.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, h.group(2)))
            buf_page = page
            continue
        if not buf:
            buf_page = page
        buf.append(line)
    flush()
    return chunks


def chunk_pdf(path: Path) -> list[RawChunk]:
    import fitz  # PyMuPDF

    out: list[RawChunk] = []
    with fitz.open(path) as doc:
        for i, pg in enumerate(doc, start=1):
            for piece in re.split(r"\n\s*\n", pg.get_text()):
                piece = piece.strip()
                if piece:
                    out += [RawChunk(i, "", p) for p in _split_long(piece)]
    return out


def chunk_file(path: Path) -> list[RawChunk]:
    if path.suffix.lower() == ".pdf":
        return chunk_pdf(path)
    return chunk_markdown(path.read_text(encoding="utf-8"))
