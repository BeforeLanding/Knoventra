from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from sqlalchemy import func, select

from fakes import ScriptedModel, session_factory_for
from knoventra.agent.graph import AgentDeps, build_graph
from knoventra.api.chat import ChatIn, get_graph
from knoventra.api.deps import get_db
from knoventra.auth.security import issue_token
from knoventra.config import Settings, settings
from knoventra.db.models import Conversation, Message
from knoventra.main import app


@pytest.mark.parametrize('value', ['', ' \t\n　', '字' * 5, '🙂' * 5, '\x00', None, 123, {}])
def test_invalid_chat_body_does_not_create_records_or_advance_graph(db, users, monkeypatch, value):
    monkeypatch.setattr(settings, 'chat_max_chars', 4)
    graph = Mock()
    graph.get_state.side_effect = AssertionError('invalid request must not advance/read a checkpoint')
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_graph] = lambda: graph
    before = [db.scalar(select(func.count()).select_from(model)) for model in (Conversation, Message)]
    try:
        with TestClient(app) as client:
            headers = {'Authorization': 'Bearer ' + issue_token(users['sg-ops'].id)}
            assert client.get('/api/v1/me', headers=headers).json()['chat_max_chars'] == 4
            response = client.post('/api/v1/chat', headers=headers, json={'message': value})
            assert response.status_code == 422
        assert [db.scalar(select(func.count()).select_from(model)) for model in (Conversation, Message)] == before
        graph.get_state.assert_not_called()
        graph.stream.assert_not_called()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize('message', ['四个汉字', '🙂🙂🙂🙂'])
def test_exact_character_limit_is_accepted_and_stored_without_truncation(db, enc, users, monkeypatch, message):
    monkeypatch.setattr(settings, 'chat_max_chars', 4)
    graph = build_graph(AgentDeps(session_factory_for(db), enc, None, ScriptedModel(script=lambda msgs: AIMessage('收到')), InMemorySaver()))
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_graph] = lambda: graph
    try:
        with TestClient(app) as client:
            response = client.post('/api/v1/chat', headers={'Authorization': 'Bearer ' + issue_token(users['sg-ops'].id)},
                                   json={'message': message})
        assert response.status_code == 200 and 'event: done' in response.text
        stored = db.scalars(select(Message).where(Message.role == 'user')).all()
        assert [row.content for row in stored] == [message]
    finally:
        app.dependency_overrides.clear()


def test_validation_preserves_valid_api_whitespace_and_rejects_oversized_raw_input(monkeypatch):
    monkeypatch.setattr(settings, 'chat_max_chars', 4)
    assert ChatIn(message=' 字 ').message == ' 字 '
    with pytest.raises(ValidationError):
        ChatIn(message=' 字字字 ')


@pytest.mark.parametrize('limit', [0, -1])
def test_question_limit_configuration_must_be_positive(limit):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chat_max_chars=limit)
