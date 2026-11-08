from unittest.mock import Mock

import numpy as np
import pytest

from knoventra.access.scope import AccessScope
from knoventra.config import settings
from knoventra.ingestion.encoder import BgeM3Encoder, EncoderOutputError, encode_checked
from knoventra.retrieval.search import search


@pytest.fixture
def dimensions(monkeypatch):
    monkeypatch.setattr(settings, 'dense_dim', 3)
    monkeypatch.setattr(settings, 'sparse_dim', 10)


@pytest.mark.parametrize('rows', [
    None, [], [([1, 2, 3], {}, 'extra')], [([], {})], [([1, 2], {})],
    [([1, 2, float('nan')], {})], [([1, 2, float('inf')], {})], [([1, 2, 1e100], {})],
    [([1, 2, 10**400], {})], [([1, 2, True], {})], [([1, 2, '3'], {})],
    [(None, {})], [([1, 2, 3], [])], [([1, 2, 3], {-1: 1.0})], [([1, 2, 3], {10: 1.0})],
    [([1, 2, 3], {True: 1.0})], [([1, 2, 3], {'1': 1.0})], [([1, 2, 3], {1.5: 1.0})],
    [([1, 2, 3], {1: float('nan')})], [([1, 2, 3], {1: float('inf')})],
    [([1, 2, 3], {1: -0.1})], [([1, 2, 3], {1: 1e100})], [([1, 2, 3], {1: 10**400})],
    [([1, 2, 3], {1: True})], [([1, 2, 3], {1: '1.0'})],
])
def test_invalid_encoder_outputs_fail_explicitly(dimensions, rows):
    encoder = Mock()
    encoder.encode.return_value = rows
    with pytest.raises(EncoderOutputError):
        encode_checked(encoder, ['query'])
    encoder.encode.assert_called_once_with(['query'])


def test_valid_numpy_values_empty_sparse_and_zero_dense_are_not_rewritten(dimensions):
    rows = [(np.array([0.0, -0.2, 0.5], dtype=np.float32), {np.int64(9): np.float32(0.7)}),
            ([0, 0, 0], {})]
    encoder = Mock()
    encoder.encode.return_value = rows
    assert encode_checked(encoder, ['a', 'b']) is rows


@pytest.mark.parametrize('dense_count,sparse_count', [(0, 1), (1, 0), (1, 2), (2, 1)])
def test_bge_adapter_rejects_missing_and_surplus_members_without_downloading_models(dense_count, sparse_count):
    encoder = BgeM3Encoder.__new__(BgeM3Encoder)
    encoder.model = Mock()
    encoder.model.encode.return_value = {'dense_vecs': [[0.1]] * dense_count, 'lexical_weights': [{}] * sparse_count}
    with pytest.raises(EncoderOutputError, match='count does not match'):
        encoder.encode(['text'])


def test_bge_empty_batch_does_not_call_model():
    encoder = BgeM3Encoder.__new__(BgeM3Encoder)
    encoder.model = Mock()
    assert encoder.encode([]) == []
    encoder.model.encode.assert_not_called()


def test_invalid_query_embedding_never_reaches_candidate_sql(dimensions):
    db, encoder = Mock(), Mock()
    encoder.encode.return_value = [([float('nan'), 0, 1], {})]
    with pytest.raises(EncoderOutputError):
        search(db, encoder, AccessScope('user', frozenset({'kb'})), 'query')
    db.execute.assert_not_called()


@pytest.mark.parametrize('output', [{}, {'dense_vecs': None, 'lexical_weights': [{}]},
                                  {'dense_vecs': [[1]], 'lexical_weights': [{'not-an-index': 1}]}])
def test_bge_adapter_classifies_malformed_response_as_encoder_failure(output):
    encoder = BgeM3Encoder.__new__(BgeM3Encoder)
    encoder.model = Mock()
    encoder.model.encode.return_value = output
    with pytest.raises(EncoderOutputError):
        encoder.encode(['text'])


@pytest.mark.parametrize('bad_call', [1, 2])
def test_ingestion_rejects_invalid_vectors_before_creating_document_or_revision(db, enc, tmp_path, bad_call):
    from sqlalchemy import func, select
    from knoventra.db.models import Chunk, Document, KnowledgeBase, Revision
    from knoventra.ingestion.service import add_revision
    path = tmp_path / 'bad-vector.md'
    path.write_text('# Manual\nUseful body.', encoding='utf-8')
    counts = lambda: [db.scalar(select(func.count()).select_from(model)) for model in (Document, Revision, Chunk)]
    before = counts()
    calls = []
    class BrokenEncoder:
        def encode(self, texts):
            calls.append(1)
            rows = enc.encode(texts)
            if len(calls) == bad_call:
                rows[0] = (rows[0][0][:-1], rows[0][1]) if bad_call == 1 else (rows[0][0], {settings.sparse_dim: 1.0})
            return rows
    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == 'it-public'))
    with pytest.raises(EncoderOutputError):
        add_revision(db, BrokenEncoder(), kb_id=kb.id, title='坏编码结果', lang='en', path=path)
    assert counts() == before
    assert len(calls) == bad_call


def test_upload_reports_encoder_fault_as_service_failure_not_document_validation(db, users, monkeypatch):
    from fastapi.testclient import TestClient
    from knoventra.api import routes
    from knoventra.api.deps import get_db
    from knoventra.auth.security import issue_token
    from knoventra.main import app
    encoder = Mock()
    encoder.encode.return_value = [([float('nan')] * settings.dense_dim, {})]
    monkeypatch.setattr(routes, 'get_encoder', lambda: encoder)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            headers = {'Authorization': 'Bearer ' + issue_token(users['admin'].id)}
            before = client.get('/api/v1/admin/documents', headers=headers).json()
            response = client.post('/api/v1/admin/documents', headers=headers,
                                   data={'kb': 'it-public', 'title': '坏编码上传', 'lang': 'en'},
                                   files={'file': ('valid.md', b'# Manual\nUseful body.', 'text/markdown')})
            assert response.status_code == 503 and response.json()['detail'] == 'ENCODER_OUTPUT_INVALID'
            assert client.get('/api/v1/admin/documents', headers=headers).json() == before
    finally:
        app.dependency_overrides.clear()


def test_chat_query_encoder_fault_is_operational_error_without_evidence(db, users):
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver
    from fakes import ScriptedModel, call, session_factory_for
    from knoventra.agent.graph import AgentDeps, build_graph
    from knoventra.api.chat import get_graph
    from knoventra.api.deps import get_db
    from knoventra.auth.security import issue_token
    from knoventra.main import app
    encoder = Mock()
    encoder.encode.return_value = [([float('nan')] * settings.dense_dim, {})]
    graph = build_graph(AgentDeps(session_factory_for(db), encoder, None,
                                 ScriptedModel(script=lambda msgs: call('search_knowledge', {'query': 'E17-42'})), InMemorySaver()))
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_graph] = lambda: graph
    try:
        with TestClient(app) as client:
            response = client.post('/api/v1/chat', headers={'Authorization': 'Bearer ' + issue_token(users['sg-ops'].id)},
                                   json={'message': 'E17-42 排查'})
        assert 'RETRIEVAL_UNAVAILABLE' in response.text and 'INSUFFICIENT_EVIDENCE' not in response.text
        assert 'event: sources' not in response.text
        encoder.encode.assert_called_once()
    finally:
        app.dependency_overrides.clear()
