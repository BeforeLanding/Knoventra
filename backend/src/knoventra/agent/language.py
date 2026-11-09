"""Match unambiguous Chinese script in final answers; keep evidence and machine literals intact."""
from functools import lru_cache
import re

from knoventra.ingestion.normalize import to_simplified

ANSWER_SCRIPT_POLICY = "unambiguous-question-opencc-v1"
_LITERALS = re.compile(r"```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)|`[^`\n]*`|https?://[^\s<>()]+")


@lru_cache(maxsize=1)
def _s2t():
    from opencc import OpenCC
    return OpenCC("s2t")


def answer_script(question: str) -> str | None:
    text = _LITERALS.sub("", question)
    traditional = to_simplified(text) != text
    simplified = _s2t().convert(text) != text
    if traditional and not simplified:
        return "zh-Hant"
    if simplified and not traditional:
        return "zh-Hans"
    return None  # Shared characters, non-Chinese or mixed/ambiguous script: do not guess.


def format_answer(question: str, answer: str) -> str:
    script = answer_script(question)
    if script is None:
        return answer
    convert = to_simplified if script == "zh-Hans" else _s2t().convert
    parts = []
    offset = 0
    for literal in _LITERALS.finditer(answer):
        parts.extend((convert(answer[offset:literal.start()]), literal.group()))
        offset = literal.end()
    parts.append(convert(answer[offset:]))
    return "".join(parts)
