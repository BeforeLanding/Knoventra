import importlib
from unittest.mock import Mock

import pytest
from pgvector import SparseVector, Vector

from knoventra.access.scope import AccessScope, NoAccess
from knoventra.config import settings
from knoventra.ingestion.encoder import EncoderOutputError, HashEncoder

module = importlib.import_module('knoventra.retrieval.search')
SCOPE = AccessScope('user', frozenset({'kb'}))


def pair(value=1.0, sparse=None):
    return ([value] + [0.0] * (settings.dense_dim - 1), {1: 0.25} if sparse is None else sparse)


def empty_db():
    db = Mock()
    db.execute.return_value.all.return_value = []
    return db


@pytest.mark.parametrize('query,opencc,expected', [
    ('设备校正', True, '设备校正'),
    ('设备校正', False, '设备校正'),
    ('設備校正', False, '設備校正'),
    ('s5設備報錯誤碼19', True, 'S5-19'),
])
def test_identical_channel_texts_are_encoded_once(query, opencc, expected):
    enc, db = Mock(), empty_db()
    enc.encode.return_value = [pair()]
    module.search(db, enc, SCOPE, query, use_opencc=opencc)
    enc.encode.assert_called_once_with([expected])
    assert db.execute.call_count == 3  # session setting and both recall channels


def test_distinct_scripts_share_one_validated_batch_and_keep_channel_identity():
    enc, db = Mock(), empty_db()
    original, simplified = pair(1.0, {1: 0.25}), pair(2.0, {2: 0.75})
    enc.encode.return_value = [original, simplified]
    module.search(db, enc, SCOPE, '設備校正')
    enc.encode.assert_called_once_with(['設備校正', '设备校正'])
    dense_call, sparse_call = db.execute.call_args_list[1:]
    assert dense_call.args[1]['q'] == Vector(original[0]).to_text()
    assert sparse_call.args[1]['q'] == SparseVector(simplified[1], settings.sparse_dim).to_text()


@pytest.mark.parametrize('opencc', [True, False])
def test_dense_only_never_converts_or_encodes_unused_sparse_text(monkeypatch, opencc):
    convert = Mock(side_effect=AssertionError('unused conversion'))
    monkeypatch.setattr(module, 'to_simplified', convert)
    enc, db = Mock(), empty_db()
    enc.encode.return_value = [pair()]
    result = module.search(db, enc, SCOPE, '設備校正', use_sparse=False, use_opencc=opencc)
    enc.encode.assert_called_once_with(['設備校正'])
    convert.assert_not_called()
    assert db.execute.call_count == 2 and 'sparse' not in result.stages


@pytest.mark.parametrize('bad', ['missing', 'extra', 'bad_first', 'bad_second'])
def test_entire_distinct_batch_is_checked_before_any_sql(bad):
    rows = [pair(), pair()]
    if bad == 'missing':
        rows.pop()
    elif bad == 'extra':
        rows.append(pair())
    else:
        rows[0 if bad == 'bad_first' else 1] = pair(float('nan'))
    enc, db = Mock(), empty_db()
    enc.encode.return_value = rows
    with pytest.raises(EncoderOutputError):
        module.search(db, enc, SCOPE, '設備校正')
    enc.encode.assert_called_once_with(['設備校正', '设备校正'])
    db.execute.assert_not_called()


def test_batching_never_encodes_before_access_check(monkeypatch):
    enc, db, convert = Mock(), empty_db(), Mock()
    monkeypatch.setattr(module, 'to_simplified', convert)
    with pytest.raises(NoAccess):
        module.search(db, enc, AccessScope('user', frozenset()), '設備校正')
    enc.encode.assert_not_called()
    convert.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.parametrize('query', ['设备校正', '設備校正'])
def test_offline_channel_vectors_match_previous_separate_call_policy(query):
    enc, db = HashEncoder(), empty_db()
    dense = enc.encode([query])[0][0]
    sparse = enc.encode([module.to_simplified(query)])[0][1]
    module.search(db, enc, SCOPE, query)
    assert db.execute.call_args_list[1].args[1]['q'] == Vector(dense).to_text()
    assert db.execute.call_args_list[2].args[1]['q'] == SparseVector(sparse, settings.sparse_dim).to_text()
