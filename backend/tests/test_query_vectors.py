import math
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from knoventra.access.scope import AccessScope, NoAccess
from knoventra.config import settings
from knoventra.db.models import Chunk, Document, KnowledgeBase, Revision
from knoventra.ingestion.service import _sparse
from knoventra.retrieval.search import search


def encoder_for(dense, sparse):
    encoder = Mock()
    encoder.encode.return_value = [(dense, sparse)]
    return encoder


@pytest.mark.parametrize('value', [0.0, -0.0, 1e-50])
@pytest.mark.parametrize('sparse', [{}, {1: 0.0}, {1: 1e-50}])
def test_zero_float32_query_without_sparse_signal_does_not_issue_sql(value, sparse):
    db = Mock(side_effect=AssertionError('no usable channel'))
    db.execute.side_effect = AssertionError('no candidate or session SQL expected')
    result = search(db, encoder_for([value] * settings.dense_dim, sparse), AccessScope('user', frozenset({'kb'})), '???')
    assert result.evidence == [] and result.stages == {'dense': [], 'fused': [], 'selected': []}
    db.execute.assert_not_called()


def test_empty_access_scope_is_rejected_before_encoding_even_with_zero_vectors():
    db, encoder = Mock(), Mock()
    with pytest.raises(NoAccess):
        search(db, encoder, AccessScope('user', frozenset()), '???')
    encoder.encode.assert_not_called()
    db.execute.assert_not_called()


@pytest.fixture
def toy_vectors(db):
    kb = KnowledgeBase(name='query-vector-test')
    db.add(kb)
    db.flush()
    doc = Document(kb_id=kb.id, title='Numeric fixture', source_lang='en')
    db.add(doc)
    db.flush()
    revision = Revision(document_id=doc.id, version=1, status='PUBLISHED', content_hash='a' * 64)
    db.add(revision)
    db.flush()
    rows = []
    for index in range(4):
        vector = [0.0] * settings.dense_dim
        if index < 2:
            vector[index] = 1.0
        chunk = Chunk(kb_id=kb.id, revision_id=revision.id, page=1, section_path='',
                      text=f'Fixture {index}', text_norm=f'Fixture {index}', dense=None if index == 3 else vector,
                      sparse=_sparse({7: 1.0} if index == 2 else {8: 1.0} if index == 3 else {}))
        db.add(chunk)
        rows.append(chunk)
    db.flush()
    return AccessScope('fixture-user', frozenset({kb.id})), rows


def test_tiny_nonzero_float32_query_retains_direction_and_ignores_unscorable_stored_dense(db, toy_vectors):
    scope, rows = toy_vectors
    vector = [0.0] * settings.dense_dim
    vector[0] = 1e-9
    result = search(db, encoder_for(vector, {}), scope, 'fixture', use_sparse=False)
    assert result.evidence[0].chunk_id == rows[0].id
    assert result.evidence[0].dense_sim == pytest.approx(1.0)
    assert all(math.isfinite(row.dense_sim) for row in result.evidence)
    # An index scan may omit zero vectors itself; either way they must not be fused.
    assert rows[2].id not in result.stages['fused']
    assert rows[3].id not in result.stages['fused']


@pytest.mark.parametrize('token,index', [(7, 2), (8, 3)])
def test_zero_dense_query_can_use_an_authorized_sparse_match(db, toy_vectors, token, index):
    scope, rows = toy_vectors
    result = search(db, encoder_for([0.0] * settings.dense_dim, {token: 1.0}), scope, 'fixture')
    assert result.stages['dense'] == []
    assert [row.chunk_id for row in result.evidence] == [rows[index].id]
    assert result.evidence[0].channels == {'sparse': 1}
    assert result.evidence[0].dense_sim is None and result.evidence[0].sparse_ip == pytest.approx(1.0)


def test_disabling_sparse_does_not_restore_a_zero_dense_channel(db, toy_vectors):
    scope, _ = toy_vectors
    result = search(db, encoder_for([0.0] * settings.dense_dim, {7: 1.0}), scope, 'fixture', use_sparse=False)
    assert result.evidence == [] and 'sparse' not in result.stages


@pytest.mark.parametrize('invalid', [None, float('nan'), float('inf'), -float('inf')])
def test_nonfinite_distances_are_excluded_per_channel_without_discarding_valid_other_channel(invalid):
    def row(cid, distance):
        return SimpleNamespace(id=cid, dist=distance, doc_id='doc', title='Title', version=1, page=1,
                               section_path='', text='Body', source_lang='en')
    db = Mock()
    dense = Mock(); dense.all.return_value = [row('a', invalid), row('b', 0.2)]
    sparse = Mock(); sparse.all.return_value = [row('a', -1.0), row('c', invalid)]
    db.execute.side_effect = [None, dense, sparse]
    result = search(db, encoder_for([1.0] * settings.dense_dim, {7: 1.0}), AccessScope('user', frozenset({'kb'})), 'fixture')
    assert result.stages['dense'] == ['b'] and result.stages['sparse'] == ['a']
    assert result.stages['dense_unscorable'] == ['a'] and result.stages['sparse_unscorable'] == ['c']
    assert {r.chunk_id for r in result.evidence} == {'a', 'b'}
    assert next(r for r in result.evidence if r.chunk_id == 'a').dense_sim is None
    assert all(math.isfinite(r.score) for r in result.evidence)
