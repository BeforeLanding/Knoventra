"""Conservative lexical checks for explicit fault codes and part numbers.

Evidence uses explicit hyphenated identifiers. Queries additionally support a
bounded Chinese model + labelled two-digit fault-code grammar. This checks
evidence presence, not whether the evidence answers the question.
"""
from __future__ import annotations

import re
import unicodedata

_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−﹘﹣－"})
_IDENTIFIER = re.compile(r"(?<![A-Z0-9_-])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+(?![A-Z0-9_-])", re.ASCII | re.IGNORECASE)
IDENTIFIER_POLICY = "explicit-and-split-v2"
_SPLIT_CODE = re.compile(
    r"(?<![A-Z0-9_-])(?P<model>[A-Z]{1,8}[0-9]{1,4})[ \t]*"
    r"(?:(?:设备|設備)[ \t]*)?(?:报|報)?[ \t]*"
    r"(?:故障码|故障碼|错误码|錯誤碼|代码|代碼)[ \t]*:?[ \t]*"
    r"(?P<suffix>[0-9]{2})(?![A-Z0-9_.-])"
    r"(?![ \t]*(?:%|°|度|摄氏度|攝氏度|分钟|分鐘|毫秒|秒|小时|小時|毫米|米|公斤|千克|元|台|次))"
    r"(?![ \t]*(?:[/、,，和及或与與~～]|到|至)[ \t]*[0-9])",
    re.ASCII | re.IGNORECASE,
)


def normalize_identifiers(text: str) -> str:
    """Canonicalise code spelling for recall as well as matching; preserve prose case."""
    normal = unicodedata.normalize("NFKC", text).translate(_DASHES)
    normal = re.sub(r"[ \t]*-[ \t]*", "-", normal)
    return _IDENTIFIER.sub(lambda m: m.group().upper() if any(c.isdigit() for c in m.group()) else m.group(), normal)


def extract_identifiers(text: str) -> frozenset[str]:
    return frozenset(m.group() for m in _IDENTIFIER.finditer(normalize_identifiers(text))
                     if any(c.isdigit() for c in m.group()))


def normalize_query_identifiers(text: str) -> str:
    """Join only unambiguous labelled query codes, without changing source text.

    Supports ASCII-letter/digit models, adjacent Chinese labels and exactly two
    numeric fault digits. Does not infer bare numbers, distant references,
    numeric alternatives/ranges, units, or one/three-digit suffixes.
    """
    normal = normalize_identifiers(text)
    joined = _SPLIT_CODE.sub(lambda m: f"{m['model'].upper()}-{m['suffix']}", normal)
    return joined if extract_identifiers(joined) else text


def extract_query_identifiers(text: str) -> frozenset[str]:
    return extract_identifiers(normalize_query_identifiers(text))


def evidence_identifiers(section_path: str, text: str) -> frozenset[str]:
    # A Markdown chunk can inherit its code from the heading. Document titles
    # alone are too broad: a manual can contain several unrelated code sections.
    return extract_identifiers(section_path) | extract_identifiers(text)
