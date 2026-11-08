import base64
import json
import re

import pytest

from answer_review import make_snapshot, prepare_packet
from render_review_html import render


def sample():
    source = {"alias": "S1", "chunk_id": "chunk", "document_id": "doc", "document_title": "<script>title</script>",
              "revision": 1, "page": 2, "section_path": "Section", "text": "safe <script>alert(1)</script> text",
              "lang": "en"}
    snap = make_snapshot("Question", "Claim <script>alert(2)</script> [S1].", {"S1": source}, [source], None)
    report = {"mode": "agent", "cases": [{"id": "case-1", "answer": snap["answer"], "review_snapshot": snap}]}
    return prepare_packet(report), report


def test_review_page_escapes_source_text_and_embeds_exact_unreviewed_packet():
    packet, report = sample()
    html = render(packet, report)
    assert "&lt;script&gt;title&lt;/script&gt;" in html
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    encoded = re.search(r'atob\("([A-Za-z0-9+/=]+)"\)', html).group(1)
    assert json.loads(base64.b64decode(encoded)) == packet


def test_review_page_refuses_to_drop_existing_annotations():
    packet, report = sample()
    claim = packet["cases"][0]["claims"][0]
    claim.update(verdict="unsupported", reviewer="reviewer", reviewer_kind="agent",
                 notes="No factual support", supports=[])
    with pytest.raises(ValueError, match="unreviewed packet"):
        render(packet, report)
