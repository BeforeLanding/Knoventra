"""Conservative lexical checks for explicit fault codes and part numbers.

Only hyphenated ASCII identifiers containing a digit are recognised. This is
an evidence-presence check, not a test that the evidence answers the question.
"""
from __future__ import annotations

import re
import unicodedata

_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−﹘﹣－"})
_IDENTIFIER = re.compile(r"(?<![A-Z0-9_-])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+(?![A-Z0-9_-])", re.ASCII | re.IGNORECASE)


def normalize_identifiers(text: str) -> str:
    """Canonicalise code spelling for recall as well as matching; preserve prose case."""
    normal = unicodedata.normalize("NFKC", text).translate(_DASHES)
    normal = re.sub(r"[ \t]*-[ \t]*", "-", normal)
    return _IDENTIFIER.sub(lambda m: m.group().upper() if any(c.isdigit() for c in m.group()) else m.group(), normal)


def extract_identifiers(text: str) -> frozenset[str]:
    return frozenset(m.group() for m in _IDENTIFIER.finditer(normalize_identifiers(text))
                     if any(c.isdigit() for c in m.group()))


def evidence_identifiers(section_path: str, text: str) -> frozenset[str]:
    # A Markdown chunk can inherit its code from the heading. Document titles
    # alone are too broad: a manual can contain several unrelated code sections.
    return extract_identifiers(section_path) | extract_identifiers(text)
