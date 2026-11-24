"""Turn parsed journal rows into deduplicated, redacted field cases.

Pipeline per import: redact -> resolve machine model -> exact dedup on
normalised text -> SimHash near dedup (same models only) -> rule-based case
fields -> merge into stored cases. Originals are never stored; only redacted
text and per-pattern counts (found / still present after replacement).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from knoventra.db.models import IngestAudit, JournalCase
from knoventra.ingestion.journal import JournalRecord
from knoventra.ingestion.normalize import to_simplified
from knoventra.retrieval.identifiers import normalize_identifiers

# ---------------------------------------------------------------- redaction

_REDACTIONS: list[tuple[str, re.Pattern[str], str]] = [
    ("password", re.compile(r"(口令|密码|密碼|password|passwd|pwd)(\s*(?:[:：=]|为|為|是)\s*|\s+)([^\s，,；;。）)]+)",
                            re.IGNORECASE),
     r"\1\2[PASSWORD]"),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    ("phone", re.compile(r"\+\d{1,3}[\s-]?\d{3,4}[\s-]?\d{3,4}(?:[\s-]?\d{1,4})?(?!\d)"), "[PHONE]"),
    ("phone", re.compile(r"(?<![\d.])1[3-9]\d[\s-]?\d{4}[\s-]?\d{4}(?![\d.])"), "[PHONE]"),
    ("phone", re.compile(r"(?<![\d.])09\d{2}-?\d{3}-?\d{3}(?![\d.])"), "[PHONE]"),
]


def redact(text: str) -> tuple[str, Counter]:
    """Replace sensitive strings. Returns (redacted text, {kind: matches replaced})."""
    found: Counter = Counter()
    for kind, pattern, repl in _REDACTIONS:
        text, n = pattern.subn(repl, text)
        found[kind] += n
    return text, +found


def residual(text: str) -> Counter:
    """Matches still present after redaction; the audit expects all zero."""
    left: Counter = Counter()
    for kind, pattern, _ in _REDACTIONS:
        left[kind] += sum(1 for m in pattern.finditer(text) if "[PASSWORD]" not in m.group())
    return +left


# ---------------------------------------------------------------- machine model

MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    "WX-38": ("WX-38", "WX38", "单面机"),
    "WX-P": ("WX-P", "WXP", "双面机"),
    "ST-250": ("ST-250", "ST250"),
}
FAMILY_ALIASES: dict[str, tuple[str, ...]] = {"WX 系列": ("WX-38", "WX-P")}


def _alias_pattern(alias: str) -> re.Pattern[str]:
    core = re.escape(alias).replace(r"\-", r"[\s-]?").replace(r"\ ", r"\s*")
    return re.compile(rf"(?<![A-Za-z0-9]){core}(?![A-Za-z0-9])", re.IGNORECASE)


_MODEL_PATTERNS = [(m, _alias_pattern(a)) for m, aliases in MODEL_ALIASES.items() for a in aliases]
_FAMILY_PATTERNS = [(ms, _alias_pattern(a)) for a, ms in FAMILY_ALIASES.items()]


def models_from_text(text: str) -> list[str]:
    norm = to_simplified(normalize_identifiers(text))
    hits = {m for m, p in _MODEL_PATTERNS if p.search(norm)}
    for models, p in _FAMILY_PATTERNS:
        if p.search(norm):
            hits.update(models)
    return sorted(hits)


def strip_models(text: str) -> str:
    """The text without any machine-model alias (for building one query per model)."""
    for _, pattern in _MODEL_PATTERNS:
        text = pattern.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class Machine:
    serial: str
    model: str
    region: str | None = None
    company: str | None = None


def load_registry(path: Path) -> dict[str, Machine]:
    items = json.loads(path.read_text(encoding="utf-8"))
    return {m["serial"]: Machine(m["serial"], m["model"], m.get("region"), m.get("company")) for m in items}


def resolve_models(machine: str, text: str, registry: dict[str, Machine]) -> tuple[list[str], str]:
    """Registry first, then the alias dictionary; never guess. Returns (models, how)."""
    if machine in registry:
        return [registry[machine].model], "registry"
    models = models_from_text(text)
    return models, ("dictionary" if models else "unknown")


# ---------------------------------------------------------------- dedup

_PUNCT = re.compile(r"[\W_]+", re.UNICODE)


def normalize_for_dedup(text: str) -> str:
    text = to_simplified(unicodedata.normalize("NFKC", text)).casefold()
    return _PUNCT.sub("", text)


def text_key(topic: str, content: str, notes: str, models: list[str]) -> str:
    blob = "\x1f".join([normalize_for_dedup(topic), normalize_for_dedup(content), normalize_for_dedup(notes),
                        ",".join(models)])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def simhash64(text: str, n: int = 3) -> int:
    """64-bit SimHash over character n-grams of the normalised text (signed for BIGINT)."""
    norm = normalize_for_dedup(text)
    grams = [norm[i:i + n] for i in range(max(1, len(norm) - n + 1))] or [""]
    weights = [0] * 64
    for gram, count in Counter(grams).items():
        h = int.from_bytes(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += count if h >> bit & 1 else -count
    value = sum(1 << bit for bit in range(64) if weights[bit] > 0)
    return value - (1 << 64) if value >= 1 << 63 else value


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()


# Demo calibration: same-procedure variants sit at distance <= 8 / Jaccard >= 0.79,
# different procedures at >= 22 / <= 0.13. SimHash proposes, Jaccard confirms,
# because 64-bit SimHash is noisy on short journal rows.
NEAR_DUP_MAX_DISTANCE = 10
NEAR_DUP_MIN_JACCARD = 0.6


def shingles(text: str, n: int = 3) -> set[str]:
    norm = normalize_for_dedup(text)
    return {norm[i:i + n] for i in range(max(1, len(norm) - n + 1))}


def near_duplicate(a_text: str, a_hash: int, b_text: str, b_hash: int) -> bool:
    if hamming(a_hash, b_hash) > NEAR_DUP_MAX_DISTANCE:
        return False
    a, b = shingles(a_text), shingles(b_text)
    return len(a & b) / max(1, len(a | b)) >= NEAR_DUP_MIN_JACCARD


# ---------------------------------------------------------------- case fields

_STEP_SPLIT = re.compile(r"(?:^|[；;\n])\s*\d{1,2}\s*[.、．)]\s*")
_UNRESOLVED = re.compile(r"待跟踪|待原厂|待回复|未解决|待确认|继续跟进|pending|tbd|follow[\s-]?up", re.IGNORECASE)
_TROUBLE = re.compile(r"alarm|报警|告警|异常|异响|漏液|漏水|trouble\s*\w*ting|故障|不转|偏移|error|fail", re.IGNORECASE)
_CAUSE = re.compile(r"原因|确认.{0,30}为|发现|怀疑|means|found|caused", re.IGNORECASE)
_FIX = re.compile(r"更换|重新|调整|reset|复位|按\s*sop|清洁|插紧|replace|reseat|recover|恢复|正常", re.IGNORECASE)
_ALARM = re.compile(r"(?<![A-Za-z0-9-])([A-Z]-\d{4})(?![\d-])|(?:alarm|报警|告警)[ \t]*([A-Z]{1,3}-?\d{3,5})(?![\d-])")


def alarm_codes(text: str) -> list[str]:
    """Alarm codes only: single-letter four-digit codes or codes right after alarm/报警/告警."""
    simplified = normalize_identifiers(to_simplified(text))
    return sorted({a or b for a, b in _ALARM.findall(simplified)} - set(MODEL_ALIASES))


@dataclass(frozen=True)
class CaseFields:
    category: str  # troubleshooting | procedure
    symptom: str
    alarm_codes: list[str]
    cause: str
    solution: str
    resolved: bool | None


def steps(content: str) -> list[str]:
    parts = [p.strip(" ；;。") for p in _STEP_SPLIT.split(content)]
    return [p for p in parts if p]


def extract_case_fields(topic: str, content: str, notes: str) -> CaseFields:
    simp_all = to_simplified(f"{topic}\n{content}\n{notes}")
    codes = alarm_codes(simp_all)
    # Procedures often mention "alarm 记录" or "无异响" in passing; only the topic or a
    # concrete alarm code marks a row as troubleshooting.
    trouble = bool(_TROUBLE.search(to_simplified(topic)) or codes)
    unresolved = bool(_UNRESOLVED.search(simp_all))
    items = steps(content)
    simp_items = [to_simplified(s) for s in items]
    cause_at = next((i for i, s in enumerate(simp_items) if _CAUSE.search(s)), None)
    cause = items[cause_at] if cause_at is not None else ""
    after = range((cause_at + 1) if cause_at is not None else 0, len(items))
    fixes = [items[i] for i in after if _FIX.search(simp_items[i])]
    if not trouble:
        return CaseFields("procedure", "", codes, "", "", False if unresolved else None)
    resolved = False if unresolved else (True if fixes else None)
    return CaseFields("troubleshooting", topic, codes, cause, "；".join(fixes) if resolved else "", resolved)


# ---------------------------------------------------------------- build and store


def detect_lang(text: str) -> str:
    if not re.search(r"[一-鿿]", text):
        return "en"
    return "zh-Hant" if to_simplified(text) != text else "zh-Hans"


@dataclass
class Occurrence:
    record: JournalRecord
    topic: str
    content: str
    notes: str


@dataclass
class CaseDraft:
    key: str
    simhash: int
    models: list[str]
    model_source: str
    members: list[Occurrence] = field(default_factory=list)

    @property
    def text(self) -> str:
        rep = self.rep
        return "\n".join((rep.topic, rep.content, rep.notes))

    @property
    def rep(self) -> Occurrence:
        return min(self.members, key=lambda o: (o.record.date, o.record.source.file, o.record.source.row))


@dataclass
class BuildStats:
    journal_rows: int = 0
    exact_merged: int = 0
    near_merged: int = 0
    unknown_model: int = 0
    redactions_found: Counter = field(default_factory=Counter)
    redactions_residual: Counter = field(default_factory=Counter)


def build_cases(records: list[JournalRecord], registry: dict[str, Machine]) -> tuple[list[CaseDraft], BuildStats]:
    stats = BuildStats(journal_rows=len(records))
    by_key: dict[str, CaseDraft] = {}
    for r in records:
        red = []
        for value in (r.topic, r.content, r.notes):
            text, found = redact(value)
            stats.redactions_found += found
            stats.redactions_residual += residual(text)
            red.append(text)
        topic, content, notes = red
        models, how = resolve_models(r.machine, f"{topic}\n{content}\n{notes}", registry)
        if how == "unknown":
            stats.unknown_model += 1
        key = text_key(topic, content, notes, models)
        occ = Occurrence(r, topic, content, notes)
        if key in by_key:
            by_key[key].members.append(occ)
            stats.exact_merged += 1
        else:
            by_key[key] = CaseDraft(key, simhash64(f"{topic}\n{content}\n{notes}"), models, how, [occ])
    drafts: list[CaseDraft] = []
    for d in sorted(by_key.values(), key=lambda d: (d.rep.record.date, d.key)):
        target = next((t for t in drafts if t.models == d.models and d.models
                       and near_duplicate(t.text, t.simhash, d.text, d.simhash)), None)
        if target is None:
            drafts.append(d)
        else:
            target.members += d.members
            stats.near_merged += len(d.members)
    return drafts, stats


def _iso_dates(members: list[Occurrence]) -> list[str]:
    return sorted({o.record.date.isoformat() for o in members})


def _sources(members: list[Occurrence]) -> list[dict]:
    return sorted(({"file": o.record.source.file, "sheet": o.record.source.sheet, "row": o.record.source.row}
                   for o in members), key=lambda s: (s["file"], s["sheet"], s["row"]))


def _merge_list(old: list | None, new: list) -> list:
    out = list(old or [])
    for item in new:
        if item not in out:
            out.append(item)
    return sorted(out, key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))


def store_cases(db: Session, drafts: list[CaseDraft], registry: dict[str, Machine]) -> tuple[int, int]:
    """Merge drafts into knv_journal_case. Idempotent for re-imports. Returns (created, updated)."""
    created = updated = 0
    for d in drafts:
        existing = db.scalar(select(JournalCase).where(JournalCase.text_key == d.key))
        if existing is None and d.models:
            candidates = db.scalars(select(JournalCase).where(JournalCase.models_key == ",".join(d.models))).all()
            existing = next((c for c in candidates
                             if near_duplicate("\n".join((c.topic, c.content, c.notes)), c.simhash,
                                               d.text, d.simhash)),
                            None)
        machines = sorted({o.record.machine for o in d.members})
        regions = sorted({registry[m].region for m in machines if m in registry and registry[m].region})
        if existing is None:
            rep = d.rep
            fields = extract_case_fields(rep.topic, rep.content, rep.notes)
            existing = JournalCase(
                text_key=d.key, simhash=d.simhash, models=d.models, models_key=",".join(d.models),
                model_source=d.model_source, topic=rep.topic, content=rep.content, notes=rep.notes,
                lang=detect_lang(f"{rep.topic}{rep.content}"), category=fields.category, symptom=fields.symptom,
                alarm_codes=fields.alarm_codes, cause=fields.cause, solution=fields.solution,
                resolved=fields.resolved, machine_ids=[], engineers=[], companies=[], regions=[], dates=[],
                sources=[],
            )
            db.add(existing)
            created += 1
        else:
            updated += 1
        existing.machine_ids = _merge_list(existing.machine_ids, machines)
        existing.engineers = _merge_list(existing.engineers, sorted({o.record.engineer for o in d.members}))
        existing.companies = _merge_list(existing.companies,
                                         sorted({o.record.company for o in d.members if o.record.company}))
        existing.regions = _merge_list(existing.regions, regions)
        existing.dates = _merge_list(existing.dates, _iso_dates(d.members))
        existing.sources = _merge_list(existing.sources, _sources(d.members))
        existing.first_date = dt.date.fromisoformat(existing.dates[0])
        existing.last_date = dt.date.fromisoformat(existing.dates[-1])
        existing.occurrences = len(existing.sources)
        db.flush()
    return created, updated


def write_audit(db: Session, *, files: list[str], counts: dict, stats: BuildStats) -> IngestAudit:
    audit = IngestAudit(kind="journal", files=files, counts=counts,
                        redactions={"found": dict(stats.redactions_found),
                                    "residual": dict(stats.redactions_residual)})
    db.add(audit)
    db.flush()
    return audit
