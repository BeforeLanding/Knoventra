from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from knoventra.config import ModelConfigurationError, Settings, settings
from knoventra.ingestion import encoder as module
from knoventra.retrieval import rerank


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    monkeypatch.setattr(module, '_cached', None)
    monkeypatch.setattr(module, '_cached_spec', None)
    rerank._load_reranker.cache_clear()
    yield
    rerank._load_reranker.cache_clear()


@pytest.mark.parametrize('options', [
    {'encoder': 'bge_m3'}, {'encoder': ''}, {'encoder': 'HASH'}, {'rerank': 'on'}, {'rerank': ''},
    {'dense_dim': 0}, {'dense_dim': -1}, {'sparse_dim': 0}, {'sparse_dim': -1},
    {'rerank': 'bge', 'rerank_model': ''}, {'rerank': 'bge', 'rerank_model': ' \t '},
])
def test_invalid_configuration_fails_at_construction(options):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **options)


def test_explicit_modes_and_disabled_unused_model_are_allowed():
    assert Settings(_env_file=None, encoder='hash', rerank='off', rerank_model='').rerank == 'off'
    assert Settings(_env_file=None, encoder='bge-m3', rerank='bge', rerank_model='local/model').encoder == 'bge-m3'


@pytest.mark.parametrize('mode', ['typo', '', None])
def test_unknown_encoder_never_falls_back_to_hash_or_returns_cached_model(monkeypatch, mode):
    monkeypatch.setattr(module, '_cached', object())
    hash_loader, bge_loader = Mock(), Mock()
    monkeypatch.setattr(module, 'HashEncoder', hash_loader)
    monkeypatch.setattr(module, 'BgeM3Encoder', bge_loader)
    monkeypatch.setattr(settings, 'encoder', mode)
    with pytest.raises(ModelConfigurationError):
        module.get_encoder()
    hash_loader.assert_not_called()
    bge_loader.assert_not_called()


@pytest.mark.parametrize('field,value', [('encoder', 'bge-m3'), ('dense_dim', 2048), ('sparse_dim', 100)])
def test_cached_encoder_cannot_be_relabelled_after_configuration_change(monkeypatch, field, value):
    monkeypatch.setattr(settings, 'encoder', 'hash')
    loader = Mock()
    monkeypatch.setattr(module, 'HashEncoder', loader)
    first = module.get_encoder()
    assert module.get_encoder() is first
    monkeypatch.setattr(settings, field, value)
    with pytest.raises(ModelConfigurationError, match='restart'):
        module.get_encoder()
    loader.assert_called_once()


@pytest.mark.parametrize('kind', ['encoder', 'reranker'])
def test_concurrent_cold_requests_construct_one_model(monkeypatch, kind):
    entered, release = threading.Event(), threading.Event()
    model = object()
    def construct(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return model
    loader = Mock(side_effect=construct)
    if kind == 'encoder':
        monkeypatch.setattr(settings, 'encoder', 'bge-m3')
        monkeypatch.setattr(module, 'BgeM3Encoder', loader)
        get = module.get_encoder
    else:
        monkeypatch.setattr(settings, 'rerank', 'bge')
        monkeypatch.setattr(settings, 'rerank_model', 'test/model')
        monkeypatch.setattr(rerank, 'BgeReranker', loader)
        get = rerank.get_reranker
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(get)
        assert entered.wait(5)
        second = pool.submit(get)
        release.set()
        assert first.result(timeout=10) is model and second.result(timeout=10) is model
    loader.assert_called_once()


def test_failed_real_model_load_does_not_fall_back_and_does_not_cache_failure(monkeypatch):
    monkeypatch.setattr(settings, 'encoder', 'bge-m3')
    loader = Mock(side_effect=RuntimeError('load failed'))
    hash_loader = Mock()
    monkeypatch.setattr(module, 'BgeM3Encoder', loader)
    monkeypatch.setattr(module, 'HashEncoder', hash_loader)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='load failed'):
            module.get_encoder()
    assert module._cached is None and loader.call_count == 2
    hash_loader.assert_not_called()


def test_configuration_changed_inside_constructor_is_not_published_as_cached(monkeypatch):
    monkeypatch.setattr(settings, 'encoder', 'hash')
    def construct(**kwargs):
        monkeypatch.setattr(settings, 'sparse_dim', settings.sparse_dim + 1)
        return object()
    monkeypatch.setattr(module, 'HashEncoder', construct)
    with pytest.raises(ModelConfigurationError, match='during loading'):
        module.get_encoder()
    assert module._cached is None


@pytest.mark.parametrize('mode,name', [('typo', 'model'), ('bge', ''), ('bge', ' '), ('bge', None)])
def test_invalid_runtime_rerank_config_never_silently_disables_or_loads(monkeypatch, mode, name):
    monkeypatch.setattr(settings, 'rerank', mode)
    monkeypatch.setattr(settings, 'rerank_model', name)
    loader = Mock()
    monkeypatch.setattr(rerank, '_load_reranker', loader)
    with pytest.raises(ModelConfigurationError):
        rerank.get_reranker()
    loader.assert_not_called()
