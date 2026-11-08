"""Parse Markdown / PDF into chunks that keep page number and section path."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_CHARS = 700
_HEADING = re.compile(r"^(#{1,4})\s+(.*\S)\s*$")
_PAGE_MARK = re.compile(r"^<!--\s*page:\s*(\d+)\s*-->\s*$")
# Boundary heuristic only; identifier recognition remains in retrieval.identifiers.
_WORD = re.compile(r"[A-Za-z0-9Ａ-Ｚａ-ｚ０-９_]+(?:[ \t]*[-‐‑‒–—−﹘﹣－][ \t]*[A-Za-z0-9Ａ-Ｚａ-ｚ０-９_]+)*")


class DocumentParseError(ValueError):
    """A recognized unsupported or malformed source document."""


def _check_text(text: str) -> str:
    if '\x00' in text:
        raise DocumentParseError('document text must not contain NUL')
    return text


@dataclass
class RawChunk:
    page: int | None
    section_path: str
    text: str


def _bounded_parts(text: str):
    """Hard limit with short Latin/fullwidth words/codes kept across boundaries.

    Scan a bounded window instead of repeatedly copying the remaining long text.
    A token longer than MAX_CHARS must itself be split to keep the hard limit.
    """
    start = 0
    while len(text) - start > MAX_CHARS:
        end = start + MAX_CHARS
        for token in _WORD.finditer(text, start, min(len(text), end + MAX_CHARS)):
            if token.start() >= end:
                break
            if token.start() < end < token.end() and token.end() - token.start() <= MAX_CHARS:
                end = token.start()
                break
        yield text[start:end]
        start = end
    if start < len(text):
        yield text[start:]


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    parts, cur = [], ""
    for sentence in re.split(r"(?<=[。！？.!?\n])", text):
        for piece in _bounded_parts(sentence):
            if cur and len(cur) + len(piece) > MAX_CHARS:
                if cur.strip():
                    parts.append(cur.strip())
                cur = ""
            cur += piece
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
    import pymupdf

    out: list[RawChunk] = []
    # A failed native path-based open can retain the Windows file handle.
    # Own the read in Python, so malformed PDFs cannot block temp-file cleanup.
    data = path.read_bytes()
    try:
        doc = pymupdf.open(stream=data, filetype='pdf')
    except pymupdf.FileDataError:
        raise DocumentParseError('PDF is empty or cannot be opened as a valid document') from None
    with doc:
        if not doc.is_pdf:
            raise DocumentParseError('file must be a PDF document')
        if doc.needs_pass:
            raise DocumentParseError('password-protected PDFs are not supported; upload an unlocked copy')
        for i, pg in enumerate(doc, start=1):
            for piece in re.split(r"\n\s*\n", _check_text(pg.get_text())):
                piece = piece.strip()
                if piece:
                    out += [RawChunk(i, "", p) for p in _split_long(piece)]
    return out


def chunk_file(path: Path) -> list[RawChunk]:
    if path.suffix.lower() == ".pdf":
        return chunk_pdf(path)
    if path.suffix.lower() != ".md":
        raise DocumentParseError('only .md and .pdf documents are supported')
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise DocumentParseError('Markdown must use UTF-8 encoding') from None
    return chunk_markdown(_check_text(text))
