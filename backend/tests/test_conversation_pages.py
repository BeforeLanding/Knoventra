import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient

from knoventra.api.deps import get_db
from knoventra.auth.security import issue_token
from knoventra.db.models import Conversation
from knoventra.main import app


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def headers(user):
    return {"Authorization": "Bearer " + issue_token(user.id)}


def conversation(db, user, number, created):
    row = Conversation(id=f"{number:032x}", user_id=user.id, thread_id=uuid.uuid4().hex,
                       title=f"会话 {number}", created_at=created)
    db.add(row)
    db.flush()
    return row


def test_owned_cursor_pages_order_equal_times_and_keep_legacy_list(client, db, users):
    now = dt.datetime(2026, 11, 8, tzinfo=dt.UTC)
    rows = [conversation(db, users['sg-ops'], n, now - dt.timedelta(minutes=n // 3)) for n in range(1, 6)]
    foreign = conversation(db, users['hr'], 99, now)
    h = headers(users['sg-ops'])
    seen, cursor = [], None
    for _ in range(3):
        page = client.get('/api/v1/conversations/page', headers=h,
                          params={'limit': 2, **({'before': cursor} if cursor else {})}).json()
        seen.extend(row['id'] for row in page['items'])
        cursor = page['next_cursor']
    assert seen == [rows[i].id for i in (1, 0, 4, 3, 2)] and cursor is None
    assert len(set(seen)) == 5
    assert client.get('/api/v1/conversations/page', headers=h, params={'before': foreign.id}).status_code == 404
    assert client.get('/api/v1/conversations/page', headers=h, params={'before': 'unknown'}).status_code == 404
    assert client.get('/api/v1/conversations/page').status_code == 401
    legacy = client.get('/api/v1/conversations', headers=h).json()
    assert isinstance(legacy, list) and {r['id'] for r in legacy} == set(seen)


def test_newer_insertion_does_not_repeat_earlier_page(client, db, users):
    now = dt.datetime(2026, 11, 8, tzinfo=dt.UTC)
    for n in range(1, 5):
        conversation(db, users['sg-ops'], n, now + dt.timedelta(minutes=n))
    h = headers(users['sg-ops'])
    first = client.get('/api/v1/conversations/page', headers=h, params={'limit': 2}).json()
    newest = conversation(db, users['sg-ops'], 5, now + dt.timedelta(minutes=5))
    second = client.get('/api/v1/conversations/page', headers=h, params={'limit': 2, 'before': first['next_cursor']}).json()
    assert [r['id'] for r in second['items']] == [f'{n:032x}' for n in (2, 1)]
    assert second['next_cursor'] is None
    assert client.get('/api/v1/conversations/page', headers=h).json()['items'][0]['id'] == newest.id


def test_default_and_maximum_page_sizes_and_empty_list(client, db, users):
    h = headers(users['sg-ops'])
    assert client.get('/api/v1/conversations/page', headers=h).json() == {'items': [], 'next_cursor': None}
    now = dt.datetime(2026, 11, 8, tzinfo=dt.UTC)
    for n in range(105):
        conversation(db, users['sg-ops'], n + 1, now)
    assert len(client.get('/api/v1/conversations/page', headers=h).json()['items']) == 20
    assert len(client.get('/api/v1/conversations/page', headers=h, params={'limit': 100}).json()['items']) == 100
    assert len(client.get('/api/v1/conversations/page', headers=h, params={'limit': 1}).json()['items']) == 1


@pytest.mark.parametrize('limit', [0, -1, 101])
def test_invalid_page_limit_is_rejected(client, users, limit):
    assert client.get('/api/v1/conversations/page', headers=headers(users['sg-ops']), params={'limit': limit}).status_code == 422


def test_title_search_is_owned_case_insensitive_and_paged_with_filter_bound_cursor(client, db, users):
    now = dt.datetime(2026, 11, 8, tzinfo=dt.UTC)
    rows = [conversation(db, users['sg-ops'], n, now) for n in range(1, 4)]
    rows[0].title, rows[1].title, rows[2].title = 'E17 排查', 'e17 follow-up', '其他问题'
    foreign = conversation(db, users['hr'], 99, now)
    foreign.title = 'E17 HR 私有'
    db.flush()
    h = headers(users['sg-ops'])
    first = client.get('/api/v1/conversations/page', headers=h, params={'q': ' E17 ', 'limit': 1}).json()
    assert [r['id'] for r in first['items']] == [rows[1].id]
    second = client.get('/api/v1/conversations/page', headers=h,
                        params={'q': 'E17', 'before': first['next_cursor'], 'limit': 1}).json()
    assert [r['id'] for r in second['items']] == [rows[0].id] and second['next_cursor'] is None
    for cursor in [rows[2].id, foreign.id, 'missing']:
        assert client.get('/api/v1/conversations/page', headers=h, params={'q': 'E17', 'before': cursor}).status_code == 404
    assert client.get('/api/v1/conversations/page', headers=h, params={'q': '不存在'}).json() == {'items': [], 'next_cursor': None}
    assert len(client.get('/api/v1/conversations/page', headers=h, params={'q': '  '}).json()['items']) == 3


@pytest.mark.parametrize('symbol', ['%', '_', '/'])
def test_search_metacharacters_are_literal_not_wildcards(client, db, users, symbol):
    now = dt.datetime(2026, 11, 8, tzinfo=dt.UTC)
    match = conversation(db, users['sg-ops'], 1, now)
    match.title = f'字面{symbol}标题'
    conversation(db, users['sg-ops'], 2, now)
    db.flush()
    page = client.get('/api/v1/conversations/page', headers=headers(users['sg-ops']), params={'q': symbol}).json()
    assert [r['id'] for r in page['items']] == [match.id]


@pytest.mark.parametrize('query', ['x' * 129, '\x00'])
def test_invalid_title_queries_are_rejected(client, users, query):
    assert client.get('/api/v1/conversations/page', headers=headers(users['sg-ops']), params={'q': query}).status_code == 422
