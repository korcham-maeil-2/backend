"""온통청년 청년정책 API (getPlcy) 호출 + SQLite 캐시.

명세와 실제 호출로 확인한 주의점은 ../docs/api/온통청년_청년정책.md 에 있다. 여기서 지키는 것:
  - 시군구 코드(zipCd)로 그 지역 정책을 모두 받는다 (전국 정책도 함께 온다). 한 번에 1,000건씩
  - 오류는 HTTP 상태 코드로 온다 (키 틀림 403 JSON, 키 없음 400 HTML) → 먼저 상태를 본다
  - 응답에 공무원 담당자 실명(sprvsnInstPicNm·operInstPicNm)이 있다 → 저장하기 전에 버린다
  - 정책은 계속 바뀐다 → 하루 지난 캐시는 다시 받는다

이 파일은 받아 오기만 한다. 고르기(지역·나이·분류·신청 기간)는 policies.py가 한다.
"""

import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol

import httpx

from backend.public_data import http as public_http

logger = logging.getLogger(__name__)
# httpx는 INFO 로그에 요청 주소를 그대로 찍는다 (주소에 인증키가 들어 있다)
logging.getLogger('httpx').setLevel(logging.WARNING)

URL = 'https://www.youthcenter.go.kr/go/ythip/getPlcy'
API_KEY_ENV = 'YOUTHCENTER_API_KEY'
CACHE_TTL_SECONDS = 24 * 60 * 60  # 하루
PAGE_SIZE = 1000  # 한 번에 1,000건까지 오는 것을 확인함 (2026-11-11)
MAX_PAGES = 10  # 무한 반복 방지 (지역 하나는 1,000건 안팎)
# 연결 3초, 응답 8초 (한 쪽 1,000건은 보통 1~3초). 느리면 계산 결과가 그만큼 늦어지므로 짧게
TIMEOUT = httpx.Timeout(8.0, connect=3.0)
FAILURE_COOLDOWN_SECONDS = 120  # 조회에 실패한 지역은 이 동안 다시 묻지 않는다 (매번 기다리지 않게)
# 캐시에 담는 모양(KEEP_FIELDS 등)이 바뀌면 올린다 → 예전 모양으로 저장된 줄은 쓰지 않고 새로 받는다
CACHE_VERSION = 3

# 고르기·화면에 쓰는 필드만 남긴다 (담당자 실명 등 나머지는 저장하지 않는다)
KEEP_FIELDS = (
    'plcyNo',
    'plcyNm',
    'plcyKywdNm',
    'plcySprtCn',
    'lclsfNm',
    'mclsfNm',
    'sprtTrgtMinAge',
    'sprtTrgtMaxAge',
    'earnCndSeCd',
    'earnEtcCn',
    'addAplyQlfcCndCn',
    'aplyYmd',
    'aplyPrdSeCd',
    'aplyUrlAddr',
    'refUrlAddr1',
    'sprvsnInstCdNm',  # 주관 기관 (화면 표시, 지역 거르기)
    'rgtrUpInstCdNm',  # 등록 기관의 상위 기관 (지역 거르기: 시군구 이름)
    'rgtrHghrkInstCdNm',  # 등록 기관의 최상위 기관 (지역 거르기: 시도·중앙부처)
    'pvsnInstGroupCd',  # 기관 구분 0054001 중앙부처 / 0054002 지자체 (지역 거르기)
    'jobCd',  # 취업·학력·특화 요건 코드 (policies.py _fit: 다른 대상 전용이면 아래로)
    'schoolCd',
    'sbizCd',
)


class SupportError(Exception):
    """정책을 받아 오지 못함. message는 화면에 그대로 보여 줄 수 있는 해요체 문장."""


class PolicySource(Protocol):
    """시군구 코드 하나의 청년정책 목록(필드는 KEEP_FIELDS)을 돌려준다. 실패하면 SupportError."""

    def policies_for(self, zip_cd: str) -> list[dict[str, Any]]: ...


def _slim(policies: Iterable[Any]) -> list[dict[str, Any]]:
    """KEEP_FIELDS만 남긴다. 정책 모양(dict)이 아닌 항목은 건너뛴다.

    대상 지역 코드 목록(zipCd)은 전국 정책이면 250여 개라 크다 → 개수(zipCdCount)만 남긴다.
    (지역 거르기에서 '근무지를 콕 집은 정책'인지 '전국 대상'인지 가르는 데 쓴다)
    """
    return [
        {**{k: p.get(k) for k in KEEP_FIELDS}, 'zipCdCount': _count_codes(p.get('zipCd'))}
        for p in policies
        if isinstance(p, dict)
    ]


def _cache_key(zip_cd: str) -> str:
    return f'v{CACHE_VERSION}:{zip_cd}'


def _count_codes(codes: Any) -> int:
    return len([c for c in str(codes or '').split(',') if c.strip()])


class YouthCenterClient:
    """실제 API를 부르는 정책 목록 공급자. 결과는 SQLite에 하루 동안 저장해 두고 다시 쓴다.

    cache_path: 체크포인트와 같은 SQLite 파일 (youth_policy_cache 표). CLAUDE.md: SQLite 파일 하나.

    동기 함수다. 이벤트 루프 위에서 부르면 안 된다 — 체크포인터가 쓰기 잠금을 잡은 채 루프가
    돌기를 기다리는 동안 이쪽이 루프를 막으면 서로 멈춘다(database is locked, #41에서 겪음).
    그래프는 동기 노드를 스레드에서 돌리고, /recalculate는 asyncio.to_thread로 부른다.
    캐시는 덤이라, 캐시를 읽거나 쓰다 실패해도 조회는 계속한다.
    반대로 API가 실패하면 하루 지난 캐시라도 돌려준다 (정책은 하루 사이에 거의 안 바뀐다.
    빈 목록·오류보다 어제 목록이 낫다).
    """

    def __init__(
        self,
        api_key: str,
        cache_path: str | Path,
        *,
        http: httpx.Client | None = None,
        ttl_seconds: int = CACHE_TTL_SECONDS,
        clock=time.time,
        keep: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        """keep: 저장·반환할 정책만 고르는 함수 (예: 주거·금융복지문화 분류만). 없으면 전부."""
        self._api_key = api_key
        self._cache_path = Path(cache_path)
        self._http = http or public_http.client(TIMEOUT)
        # 시군구 코드 → (마지막으로 조회에 실패한 시각, 그때의 이유)
        self._failed: dict[str, tuple[float, str]] = {}
        self._keep = keep
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                'CREATE TABLE IF NOT EXISTS youth_policy_cache '
                '(zip_cd TEXT PRIMARY KEY, fetched_at REAL NOT NULL, body TEXT NOT NULL)'
            )

    def _connect(self) -> sqlite3.Connection:
        # 그래프 노드는 여러 스레드에서 돌 수 있어, 부를 때마다 연결을 새로 연다 (파일 하나, 가벼움)
        return sqlite3.connect(self._cache_path, timeout=5)

    def policies_for(self, zip_cd: str) -> list[dict[str, Any]]:
        cached = self._read_cache(zip_cd)
        if cached is not None and self._clock() - cached[0] <= self._ttl:
            return cached[1]
        try:
            failed_at, reason = self._failed.get(zip_cd, (float('-inf'), ''))
            if self._clock() - failed_at < FAILURE_COOLDOWN_SECONDS:
                # 방금 실패한 지역: 기다리지 않고 같은 이유로 바로 실패 (키 오류면 그대로 알린다)
                raise SupportError(reason)
            try:
                fetched = _slim(self._fetch(zip_cd))
            except SupportError as e:
                self._failed[zip_cd] = (self._clock(), str(e))
                raise
        except SupportError:
            if cached is None:
                raise
            logger.warning('온통청년 조회 실패 → 지난번에 받아 둔 목록을 씁니다 (%s)', zip_cd)
            return cached[1]
        self._failed.pop(zip_cd, None)
        # 쓸 정책만 남겨 저장한다 (관련 분류는 전체의 약 4분의 1 → 캐시도 그만큼 작다)
        policies = [p for p in fetched if self._keep is None or self._keep(p)]
        if fetched:  # 응답이 통째로 비면 잠깐의 오류일 수 있어 하루 동안 붙잡아 두지 않는다
            self._write_cache(zip_cd, policies)
        return policies

    def _read_cache(self, zip_cd: str) -> tuple[float, list[dict[str, Any]]] | None:
        """(받은 시각, 목록). 없거나 캐시가 고장이면 None. 오래됐는지는 부르는 쪽이 본다."""
        try:
            with closing(self._connect()) as db:
                row = db.execute(
                    'SELECT fetched_at, body FROM youth_policy_cache WHERE zip_cd = ?',
                    (_cache_key(zip_cd),),
                ).fetchone()
            return None if row is None else (row[0], json.loads(row[1]))
        except (sqlite3.Error, ValueError):
            logger.warning('청년정책 캐시를 읽지 못해 새로 받습니다 (%s)', zip_cd, exc_info=True)
            return None

    def _write_cache(self, zip_cd: str, policies: list[dict[str, Any]]) -> None:
        """실패해도 넘어간다 (다음에 다시 받을 뿐)."""
        body = json.dumps(policies, ensure_ascii=False)
        try:
            with closing(self._connect()) as db, db:
                db.execute(
                    'INSERT OR REPLACE INTO youth_policy_cache VALUES (?, ?, ?)',
                    (_cache_key(zip_cd), self._clock(), body),
                )
        except sqlite3.Error:
            logger.warning('청년정책 캐시에 쓰지 못했습니다 (%s)', zip_cd, exc_info=True)

    def _fetch(self, zip_cd: str) -> list[dict[str, Any]]:
        policies: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            params = {
                'apiKeyNm': self._api_key,
                'rtnType': 'json',
                'zipCd': zip_cd,
                'pageNum': page,
                'pageSize': PAGE_SIZE,
            }
            try:
                response = self._http.get(URL, params=params)
            except httpx.HTTPError as e:
                # 예외 글·연결 정보에 요청 주소(인증키 포함)가 들어갈 수 있어 종류만 남긴다
                logger.warning('온통청년 요청 실패 (%s): %s', zip_cd, type(e).__name__)
                raise SupportError(
                    '온통청년에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.'
                ) from None
            if response.status_code == 403:
                raise SupportError('온통청년 인증키가 맞지 않아요. 관리자에게 알려 주세요.')
            if response.status_code != 200:
                raise SupportError(
                    f'온통청년 응답이 올바르지 않아요 (HTTP {response.status_code}).'
                )
            try:
                result = response.json()['result']
                items = result['youthPolicyList'] or []
                total = int(result['pagging']['totCount'])  # 철자가 pagging (g 두 개)
            except (ValueError, KeyError, TypeError) as e:
                raise SupportError('온통청년 응답 모양이 예상과 달라요.') from e
            policies += items
            if not items or len(policies) >= total:
                return policies
        logger.warning('온통청년 목록이 %d쪽을 넘어 일부만 받았습니다 (%s)', MAX_PAGES, zip_cd)
        return policies


def source_from_env(
    cache_path: str | Path, keep: Callable[[dict[str, Any]], bool] | None = None
) -> PolicySource | None:
    """인증키(YOUTHCENTER_API_KEY)가 있으면 실제 API 공급자, 없으면 None (조회하지 않음)."""
    key = os.getenv(API_KEY_ENV, '').strip()
    return YouthCenterClient(key, cache_path, keep=keep) if key else None
