from unittest.mock import MagicMock, Mock

import pymupdf
import pytest
from fastapi.testclient import TestClient

from knoventra.api import routes
from knoventra.api.deps import get_db
from knoventra.auth.security import issue_token
from knoventra.ingestion.chunking import DocumentParseError, chunk_file
from knoventra.ingestion.service import add_revision
from knoventra.main import app


def source(tmp_path, kind):
    suffix = '.md' if kind in {'encoding', 'nul'} else '.txt' if kind == 'extension' else '.pdf'
    path = tmp_path / ('source' + suffix)
    if kind == 'encrypted':
        with pymupdf.open() as doc:
            doc.new_page().insert_text((20, 30), 'Private manual')
            doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='reader')
    else:
        path.write_bytes({'encoding': b'# Manual\n\xff', 'nul': b'# Manual\x00\nBody',
                          'empty': b'', 'broken': b'%PDF-1.7\nbroken',
                          'extension': b'# Manual\nBody'}[kind])
    return path


@pytest.mark.parametrize('kind', ['encoding', 'nul', 'empty', 'broken', 'encrypted', 'extension'])
def test_recognized_bad_document_is_rejected_before_encoding_or_writes(tmp_path, kind):
    path = source(tmp_path, kind)
    db, enc = MagicMock(), Mock()
    db.scalar.return_value = None
    with pytest.raises(DocumentParseError):
        add_revision(db, enc, kb_id='kb', title='Manual', lang='en', path=path)
    enc.encode.assert_not_called()
    db.execute.assert_not_called()
    db.add.assert_not_called()
    db.flush.assert_not_called()


@pytest.mark.parametrize('kind', ['encoding', 'nul', 'broken', 'encrypted'])
def test_api_returns_parse_rejection_keeps_catalog_and_cleans_temp_copy(db, users, tmp_path, monkeypatch, kind):
    path = source(tmp_path, kind)
    enc = Mock()
    monkeypatch.setattr(routes, 'get_encoder', lambda: enc)
    copied = []
    original_copy = routes.copy_upload
    def record_copy(stream, destination, limit):
        copied.append(destination)
        return original_copy(stream, destination, limit)
    monkeypatch.setattr(routes, 'copy_upload', record_copy)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            headers = {'Authorization': 'Bearer ' + issue_token(users['admin'].id)}
            before = client.get('/api/v1/admin/documents', headers=headers).json()
            result = client.post('/api/v1/admin/documents', headers=headers,
                                 data={'kb': 'it-public', 'title': 'VPN 使用说明', 'lang': 'zh-Hans'},
                                 files={'file': (path.name, path.read_bytes(), 'application/octet-stream')})
            assert result.status_code == 422
            assert result.json()['detail']['code'] == 'DOCUMENT_PARSE_INVALID'
            assert str(path) not in result.text
            assert client.get('/api/v1/admin/documents', headers=headers).json() == before
    finally:
        app.dependency_overrides.clear()
    enc.encode.assert_not_called()
    assert copied and all(not path.exists() for path in copied)


def test_valid_uppercase_markdown_and_pdf_still_preserve_body_and_page(tmp_path):
    md = tmp_path / 'manual.MD'
    md.write_text('# 手册\n## 操作\n设备校正。', encoding='utf-8')
    assert [(c.page, c.section_path, c.text) for c in chunk_file(md)] == [(1, '手册 > 操作', '设备校正。')]
    pdf = tmp_path / 'manual.PDF'
    with pymupdf.open() as doc:
        doc.new_page().insert_text((20, 30), 'Valid manual')
        doc.save(pdf)
    assert [(c.page, c.text) for c in chunk_file(pdf)] == [(1, 'Valid manual')]


@pytest.mark.parametrize('error', [PermissionError('disk unavailable'), RuntimeError('unexpected parser failure')])
def test_unclassified_pdf_failures_are_not_misreported_as_document_validation(tmp_path, monkeypatch, error):
    path = tmp_path / 'manual.pdf'
    path.write_bytes(b'%PDF-1.7\nfixture')
    monkeypatch.setattr(pymupdf, 'open', Mock(side_effect=error))
    with pytest.raises(type(error), match=str(error)):
        chunk_file(path)


@pytest.mark.parametrize('suffix', ['.md', '.pdf'])
def test_missing_source_remains_an_operational_failure(tmp_path, suffix):
    with pytest.raises((FileNotFoundError, pymupdf.FileNotFoundError)):
        chunk_file(tmp_path / ('missing' + suffix))
