from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from knoventra.api import routes
from knoventra.api.deps import get_db
from knoventra.auth.security import issue_token
from knoventra.db.models import KnowledgeBase
from knoventra.ingestion import service
from knoventra.main import app


@pytest.mark.parametrize('title,lang', [('', 'en'), (' \t　', 'zh-Hans'), ('x' * 256, 'en'),
                                     ('🙂' * 256, 'en'), ('x\x00y', 'en'), ('valid', 'zh'), ('valid', 'invalid-tag')])
def test_invalid_metadata_is_rejected_before_db_reads_file_parsing_or_encoding(title, lang):
    db, encoder = Mock(), Mock()
    db.scalar.side_effect = AssertionError('invalid metadata must not query DB')
    with pytest.raises(service.DocumentMetadataError):
        service.add_revision(db, encoder, kb_id='unused', title=title, lang=lang, path=Path('not-read.md'))
    db.scalar.assert_not_called()
    db.execute.assert_not_called()
    encoder.encode.assert_not_called()


@pytest.mark.parametrize('title,lang', [(' \t　', 'zh-Hans'), ('x' * 256, 'en'), ('🙂' * 256, 'en'),
                                     ('x\x00y', 'en'), ('valid', 'zh'), ('VPN 使用说明', 'zh-Hant')])
def test_api_preflight_rejects_before_copy_or_model_load_and_preserves_catalog(db, users, monkeypatch, title, lang):
    encoder, copier = Mock(side_effect=AssertionError('must not load')), Mock(side_effect=AssertionError('must not copy'))
    monkeypatch.setattr(routes, 'get_encoder', encoder)
    monkeypatch.setattr(routes, 'copy_upload', copier)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            headers = {'Authorization': 'Bearer ' + issue_token(users['admin'].id)}
            before = client.get('/api/v1/admin/documents', headers=headers).json()
            assert before['upload_rules'] == {'title_max_chars': 255, 'languages': ['zh-Hans', 'zh-Hant', 'en']}
            response = client.post('/api/v1/admin/documents', headers=headers,
                                   data={'kb': 'it-public', 'title': title, 'lang': lang},
                                   files={'file': ('test.md', b'# Manual\nUseful body.', 'text/markdown')})
            assert response.status_code == 422
            assert response.json()['detail']['code'] == 'INVALID_DOCUMENT_METADATA'
            assert client.get('/api/v1/admin/documents', headers=headers).json() == before
    finally:
        app.dependency_overrides.clear()
    encoder.assert_not_called()
    copier.assert_not_called()


@pytest.mark.parametrize('title,lang', [('字' * 255, 'zh-Hans'), ('🙂' * 255, 'en'), (' raw title ', 'zh-Hant')])
def test_valid_boundary_and_title_identity_are_preserved(db, enc, tmp_path, title, lang):
    from knoventra.db.models import Document
    path = tmp_path / 'valid.md'
    path.write_text('# Manual\nUseful body.', encoding='utf-8')
    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == 'it-public'))
    revision = service.add_revision(db, enc, kb_id=kb.id, title=title, lang=lang, path=path)
    doc = db.get(Document, revision.document_id)
    assert doc.title == title and doc.source_lang == lang and revision.status == 'DRAFT'


def test_existing_language_mismatch_stops_direct_ingestion_before_file_reads(db):
    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == 'it-public'))
    encoder = Mock(side_effect=AssertionError('must not encode'))
    with pytest.raises(service.DocumentMetadataError, match='language must match'):
        service.add_revision(db, encoder, kb_id=kb.id, title='VPN 使用说明', lang='en', path=Path('not-read.md'))
    encoder.encode.assert_not_called()


def test_preflight_does_not_autoflush_unrelated_pending_documents(db):
    from knoventra.db.models import Document
    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == 'it-public'))
    pending = Document(kb_id=kb.id, title='尚未提交的其他文档', source_lang='zh-Hans')
    db.add(pending)
    service.validate_revision_metadata(db, kb_id=kb.id, title='新文档', lang='zh-Hans')
    assert pending.id is None and pending in db.new
