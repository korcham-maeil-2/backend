"""오래된 세션 지우기 — 사용자가 넣은 공고 글·생활비를 서버에 오래 두지 않는다.

세션(LangGraph thread)에는 붙여 넣은 공고 원문, 파일에서 읽은 글, 생활비 입력, 계산 결과가
들어 있다. AI에 보낼 때는 개인정보를 가리지만(privacy.py), 저장된 것은 원문이다.
로그인이 없으니 '세션이 끝났다'를 서버가 알 수 없어서, 마지막으로 바뀐 지
SESSION_TTL_HOURS가 지난 세션을 통째로 지운다.

- 지우는 것: checkpoints·writes 표의 그 세션 줄 전부 (LangGraph adelete_thread)
- 남기는 것: 공공데이터 캐시(월세·청년정책·정책 조건). 사용자 정보가 아닌 공개 자료다
- 언제: 서버가 켜질 때 한 번, 그 뒤 PURGE_INTERVAL_SECONDS마다
- 지운 세션으로 요청하면 404 SESSION_NOT_FOUND → 화면은 새 세션으로 다시 시작한다
  (frontend page.tsx SESSION_LOST)
- '마지막으로 바뀐 때'는 가장 최근 체크포인트의 ts. 결과를 보기만 하는 요청은 시각을 바꾸지 않는다
"""

import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger(__name__)

TTL_ENV = 'SESSION_TTL_HOURS'
DEFAULT_TTL_HOURS = 24
PURGE_INTERVAL_SECONDS = 60 * 60


def ttl_from_env() -> timedelta:
    raw = os.getenv(TTL_ENV, '').strip()
    try:
        hours = float(raw) if raw else DEFAULT_TTL_HOURS
    except ValueError as e:
        raise ValueError(f'{TTL_ENV}는 숫자(시간)여야 합니다 (받은 값: {raw!r})') from e
    if hours <= 0:
        raise ValueError(f'{TTL_ENV}는 0보다 커야 합니다')
    return timedelta(hours=hours)


def _thread_ids(db_path: str | Path) -> list[str]:
    try:
        with closing(sqlite3.connect(db_path, timeout=5)) as db:
            rows = db.execute('SELECT DISTINCT thread_id FROM checkpoints').fetchall()
    except sqlite3.OperationalError:  # 표가 아직 없음 (세션이 한 번도 없었음)
        return []
    return [row[0] for row in rows]


async def purge_old_sessions(
    saver: BaseCheckpointSaver,
    db_path: str | Path,
    ttl: timedelta,
    now: datetime | None = None,
) -> int:
    """마지막으로 바뀐 지 ttl이 지난 세션을 지운다. 지운 수를 돌려준다."""
    cutoff = (now or datetime.now(UTC)) - ttl
    # SQLite는 지운 줄을 빈칸으로 남겨 파일 안에 글이 남을 수 있다 → 지울 때 0으로 덮어쓰게 한다
    conn = getattr(saver, 'conn', None)
    if conn is not None:
        await conn.execute('PRAGMA secure_delete = ON')
    removed = 0
    for thread_id in await asyncio.to_thread(_thread_ids, db_path):
        latest = await saver.aget_tuple({'configurable': {'thread_id': thread_id}})
        if latest is None:
            continue
        try:
            changed = datetime.fromisoformat(latest.checkpoint['ts'])
        except (KeyError, TypeError, ValueError):
            continue  # 시각을 모르면 지우지 않는다
        if changed < cutoff:
            await saver.adelete_thread(thread_id)
            removed += 1
    if removed:
        logger.info('오래된 세션 %d개를 지웠습니다 (%s보다 오래됨)', removed, ttl)
    return removed


async def purge_forever(saver: BaseCheckpointSaver, db_path: str | Path, ttl: timedelta) -> None:
    """서버가 켜져 있는 동안 주기적으로 지운다 (lifespan에서 작업으로 띄운다)."""
    while True:
        try:
            await purge_old_sessions(saver, db_path, ttl)
        except Exception:  # 지우기가 실패해도 서버는 계속 돈다. 다음 주기에 다시 한다
            logger.exception('오래된 세션을 지우지 못했습니다')
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)
