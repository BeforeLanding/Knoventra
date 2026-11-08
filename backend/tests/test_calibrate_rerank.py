from sqlalchemy import select

from calibrate_rerank import DEFAULT_CASES, load_cases, score_split, select_threshold
from knoventra.db.models import Chunk, Document, Revision


def test_frozen_threshold_labels_point_to_published_source_text(db):
    cases, digest = load_cases(DEFAULT_CASES)
    assert len(cases) == 24 and len(digest) == 64
    assert sum(c["answerable"] for c in cases) == 12
    rows = db.execute(select(Document.title, Revision.version, Chunk.page, Chunk.text)
                      .join(Revision, Revision.document_id == Document.id)
                      .join(Chunk, Chunk.revision_id == Revision.id)
                      .where(Revision.status == "PUBLISHED")).all()
    for case in cases:
        if case["answerable"]:
            expected = case["expect"]
            assert any(title == expected["title"] and page == expected["page"]
                       and expected["support_text"] in text for title, _, page, text in rows), case["id"]


def test_threshold_selection_uses_calibration_only_and_counts_gate_errors():
    calibration = [
        {"top_score": 0.8, "answerable": True, "target_found_at_5": True, "pre_gate_reason": None},
        {"top_score": 0.7, "answerable": True, "target_found_at_5": True, "pre_gate_reason": None},
        {"top_score": 0.6, "answerable": False, "target_found_at_5": None, "pre_gate_reason": None},
        {"top_score": 0.2, "answerable": False, "target_found_at_5": None, "pre_gate_reason": None},
    ]
    threshold, selected = select_threshold(calibration)
    assert 0.6 < threshold <= 0.7
    assert selected["balanced_accuracy"] == 1.0
    validation = [
        {"top_score": 0.9, "answerable": False, "target_found_at_5": None, "pre_gate_reason": None},
        {"top_score": 0.4, "answerable": True, "target_found_at_5": True, "pre_gate_reason": None},
    ]
    assert score_split(validation, threshold)["false_accept"] == 1
    assert score_split(validation, threshold)["false_reject_or_miss"] == 1
