import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select

from knoventra.api.deps import get_db
from knoventra.api.routes import list_documents
from knoventra.auth.security import issue_token
from knoventra.db.models import Chunk, Document, KnowledgeBase, Revision
from knoventra.main import app


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def add_doc(db, kb, title, versions):
    doc = Document(kb_id=kb.id, title=title, source_lang='en')
    db.add(doc)
    db.flush()
    published = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    for version, status in versions:
        db.add(Revision(document_id=doc.id, version=version, status=status,
                        content_hash=str(version) * 64,
                        published_at=None if status == 'DRAFT' else published))
    db.flush()
    return doc


def test_catalog_preserves_document_identity_version_order_and_summary_shape(client, db, users):
    first, second, empty = [KnowledgeBase(name=name) for name in ('catalog-a', 'catalog-b', 'catalog-empty')]
    db.add_all([first, second, empty])
    db.flush()
    add_doc(db, first, 'Same title', [(3, 'DRAFT'), (1, 'RETIRED'), (2, 'PUBLISHED')])
    add_doc(db, second, 'Same title', [(1, 'DRAFT')])
    add_doc(db, first, 'Without versions', [])
    response = client.get('/api/v1/admin/documents', headers={
        'Authorization': 'Bearer ' + issue_token(users['admin'].id)})
    assert response.status_code == 200
    body = response.json()
    assert body['kbs'] == sorted(body['kbs']) and 'catalog-empty' in body['kbs']
    docs = [d for d in body['documents'] if d['kb'].startswith('catalog-')]
    assert [(d['kb'], d['title']) for d in docs] == [
        ('catalog-a', 'Same title'), ('catalog-a', 'Without versions'), ('catalog-b', 'Same title')]
    assert docs[1]['revisions'] == []
    assert [r['version'] for r in docs[0]['revisions']] == [1, 2, 3]
    assert [r['status'] for r in docs[0]['revisions']] == ['RETIRED', 'PUBLISHED', 'DRAFT']
    assert docs[0]['revisions'][0]['published_at'] is not None
    assert docs[0]['revisions'][2]['published_at'] is None
    assert [r['version'] for r in docs[2]['revisions']] == [1]
    assert all(set(d) == {'kb', 'title', 'lang', 'revisions'} for d in docs)
    assert all(set(r) == {'id', 'version', 'status', 'published_at'} for d in docs for r in d['revisions'])


@pytest.mark.parametrize('extra_documents', [0, 1, 40])
def test_catalog_read_count_stays_constant_as_document_count_grows(db, extra_documents):
    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == 'it-public'))
    for index in range(extra_documents):
        add_doc(db, kb, f'Catalog growth {index:03}', [(1, 'RETIRED'), (2, 'DRAFT')])
    reads = []
    connection = db.connection()
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            reads.append(statement)
    event.listen(connection, 'before_cursor_execute', capture)
    try:
        body = list_documents(None, db)
    finally:
        event.remove(connection, 'before_cursor_execute', capture)
    assert len(reads) == 2
    assert len([d for d in body['documents'] if d['title'].startswith('Catalog growth')]) == extra_documents
    assert all('knv_chunk' not in statement and 'content_hash' not in statement for statement in reads)


def test_catalog_without_documents_keeps_kbs_and_upload_rules(db):
    for model in (Chunk, Revision, Document):
        db.execute(delete(model))
    db.flush()
    body = list_documents(None, db)
    assert body['documents'] == [] and body['kbs']
    assert body['upload_max_bytes'] > 0
    assert body['upload_rules']['languages'] == ['zh-Hans', 'zh-Hant', 'en']


@pytest.mark.parametrize('who,status', [(None, 401), ('sg-ops', 403)])
def test_catalog_still_requires_admin_before_reading_documents(client, db, users, who, status):
    headers = {} if who is None else {'Authorization': 'Bearer ' + issue_token(users[who].id)}
    reads = []
    connection = db.connection()
    def capture(conn, cursor, statement, parameters, context, executemany):
        reads.append(statement)
    event.listen(connection, 'before_cursor_execute', capture)
    try:
        response = client.get('/api/v1/admin/documents', headers=headers)
    finally:
        event.remove(connection, 'before_cursor_execute', capture)
    assert response.status_code == status
    assert all('knv_document' not in statement and 'knv_revision' not in statement for statement in reads)
