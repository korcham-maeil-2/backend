"""오래된 세션 지우기 (retention.py)."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from backend.main import create_app
from backend.retention import purge_old_sessions, ttl_from_env

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


async def _put(saver: AsyncSqliteSaver, thread_id: str, when: datetime) -> None:
    checkpoint = empty_checkpoint()
    checkpoint['ts'] = when.isoformat()
    checkpoint['channel_values'] = {'messages': [f'{thread_id} 공고 원문 (담당자 010-1234-5678)']}
    config = {'configurable': {'thread_id': thread_id, 'checkpoint_ns': ''}}
    saved = await saver.aput(config, checkpoint, {'source': 'input', 'step': -1}, {})
    await saver.aput_writes(saved, [('messages', ['더 보낸 글'])], task_id='t1')


def test_old_sessions_are_deleted_and_recent_ones_kept(tmp_path):
    db = tmp_path / 'ck.sqlite'

    async def run():
        async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
            await _put(saver, 'old', NOW - timedelta(hours=30))
            await _put(saver, 'recent', NOW - timedelta(hours=2))
            removed = await purge_old_sessions(saver, db, timedelta(hours=24), now=NOW)
            old = await saver.aget_tuple({'configurable': {'thread_id': 'old'}})
            recent = await saver.aget_tuple({'configurable': {'thread_id': 'recent'}})
            return removed, old, recent

    removed, old, recent = asyncio.run(run())
    assert removed == 1
    assert old is None  # 공고 원문·생활비가 든 세션이 통째로 사라짐
    assert recent is not None

    with sqlite3.connect(db) as conn:  # 쓰기 기록(writes)까지 남지 않는다
        left = {row[0] for row in conn.execute('SELECT thread_id FROM writes')}
    assert left == {'recent'}
    # 파일 안에도 지운 세션의 글이 남지 않는다 (secure_delete)
    assert 'old 공고 원문'.encode() not in db.read_bytes()
    assert 'recent 공고 원문'.encode() in db.read_bytes()


def test_latest_checkpoint_decides(tmp_path):
    # 처음 만든 지는 오래됐어도 최근에 다시 바뀐 세션은 남긴다
    db = tmp_path / 'ck.sqlite'

    async def run():
        async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
            await _put(saver, 's', NOW - timedelta(hours=40))
            await _put(saver, 's', NOW - timedelta(hours=1))
            return await purge_old_sessions(saver, db, timedelta(hours=24), now=NOW)

    assert asyncio.run(run()) == 0


def test_empty_database_is_fine(tmp_path):
    db = tmp_path / 'empty.sqlite'

    async def run():
        async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
            return await purge_old_sessions(saver, db, timedelta(hours=24), now=NOW)

    assert asyncio.run(run()) == 0


def test_ttl_from_env(monkeypatch):
    monkeypatch.delenv('SESSION_TTL_HOURS', raising=False)
    assert ttl_from_env() == timedelta(hours=24)
    monkeypatch.setenv('SESSION_TTL_HOURS', '6')
    assert ttl_from_env() == timedelta(hours=6)
    for bad in ('0', '-1', 'abc'):
        monkeypatch.setenv('SESSION_TTL_HOURS', bad)
        with pytest.raises(ValueError):
            ttl_from_env()


def test_app_starts_and_stops_with_purge_task(tmp_path):
    # 서버를 켜고 끌 때 지우기 작업이 함께 시작·정리된다 (끄는 데 멈추지 않음)
    app = create_app(db_path=str(tmp_path / 'db.sqlite'))
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
