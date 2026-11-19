"""지역 월세 시세 — 국토교통부 전월세 실거래가로 '회사 근처 원룸 월세'를 추정한다.

명세: docs 저장소 api/국토부_전월세_실거래가.md (실제 호출로 확인한 값만 씀)
  - 세 API(단독·다가구 / 연립다세대 / 오피스텔), 한 번 호출 = 시군구 하나 × 한 달
  - XML 응답. 금액 단위는 만원, 보증금은 '1,000'처럼 쉼표가 들어 있고 빈 값은 공백 한 칸
  - 키 오류는 HTTP 200에 다른 모양(OpenAPI_ServiceResponse)으로 온다 → 본문으로 판단

[추정 방법]
1) 최근 6개월(이번 달 제외, 다 끝난 달만) × 세 유형을 조회한다 (지역 하나당 18번 + 페이지)
2) 월세 계약만 남긴다 (월세 0 = 전세는 뺌)
3) 갱신 계약은 뺀다. 갱신은 인상 상한(5%)이 있어 새로 이사하는 사람이 내는 값보다 낮다.
   신규와 '계약 구분 없음'(빈 값이 많다)은 남긴다.
4) 1인 가구가 구하는 크기만 남긴다: 면적 RENT_MIN_AREA ~ RENT_MAX_AREA ㎡
   (연립다세대·오피스텔은 전용면적, 단독·다가구는 계약면적)
5) 월세 '중앙값'을 쓴다 (평균은 비싼 몇 건에 끌려간다). 보증금 중앙값과 표본 수도 함께 준다.

[캐시]
응답은 체크포인트와 같은 SQLite 파일의 rent_cache 표에 둔다 (CLAUDE.md: SQLite 파일 하나).
신고는 계약 후 30일 안에 들어오므로, 최근 2개월 치는 하루가 지나면 다시 받고 그 전 달은 계속 쓴다.

[한계] (화면·발표에 함께 말한다)
- 관리비는 실거래가에 없다 → 월세만의 값이다.
- 아주 싼 원룸(보증금 6천만 원 이하이면서 월세 30만 원 이하)은 신고 대상이 아니라 덜 잡힌다.
- 고시원·원룸텔은 주택이 아니라 없다.

숫자만 다룬다 (LLM 없음). 개별 거래의 주소(지번 등)는 저장하지도 화면에 보내지도 않는다.
"""

import asyncio
import json
import logging
import sqlite3
import statistics
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, Protocol

import httpx
from defusedxml import ElementTree
from pydantic import BaseModel, ConfigDict, Field

from backend.normalize.checks import region_by_code
from backend.public_data import http

logger = logging.getLogger(__name__)

Kind = Literal['SHRent', 'RHRent', 'OffiRent']
KIND_LABELS: dict[Kind, str] = {
    'SHRent': '단독·다가구',
    'RHRent': '연립다세대',
    'OffiRent': '오피스텔',
}
URL = 'https://apis.data.go.kr/1613000/RTMSDataSvc{kind}/getRTMSDataSvc{kind}'
PAGE_SIZE = 1000  # 기본 10건이라 꼭 지정한다 (명세 4절). 단독·다가구는 1000건을 넘는 달도 있다
MONTHS = 6
RENT_MIN_AREA = 20.0  # ㎡. 이보다 작으면 원룸텔급
RENT_MAX_AREA = 40.0  # ㎡. 원룸·작은 투룸까지 (약 12평)
FRESH_MONTHS = 2  # 이 달 수만큼 최근 달은 신고가 더 들어올 수 있어 캐시를 하루만 쓴다
CONCURRENCY = 6  # 공공데이터포털에 한꺼번에 보내는 요청 수
SOURCE_NAME = '국토교통부 전월세 실거래가'


class RentApiError(Exception):
    """공공데이터포털이 정상 응답을 주지 않음 (키 오류, 서버 오류 등)."""


@dataclass(frozen=True)
class Deal:
    """거래 한 건에서 계산에 쓰는 값만. 주소·건물명은 담지 않는다."""

    rent_man: int  # 월세(만원), 0이면 전세
    deposit_man: int  # 보증금(만원)
    area: float | None  # ㎡
    renewal: bool  # 갱신 계약인지


# ── 1) 조회 ──────────────────────────────────────────────────────


class RentFetcher(Protocol):
    async def __call__(self, kind: Kind, lawd_cd: str, ymd: str) -> list[Deal]:
        """한 유형 × 시군구 하나 × 한 달의 거래 전부 (페이지를 다 돈다)."""
        ...


def _text(item, tag: str) -> str:
    node = item.find(tag)
    return (node.text or '').strip() if node is not None else ''


def _int(value: str) -> int:
    return int(value.replace(',', '') or 0)


def parse_page(xml_text: str) -> tuple[list[Deal], int]:
    """응답 한 쪽 → (거래 목록, 전체 건수). 오류 모양이면 RentApiError."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as e:
        raise RentApiError('XML이 아닌 응답') from e
    if root.tag != 'response':
        # 키 오류는 <OpenAPI_ServiceResponse>…<errMsg>…</errMsg> (명세 6절)
        raise RentApiError(f'오류 응답: {_text(root, "cmmMsgHeader/errMsg") or root.tag}')
    code = _text(root, 'header/resultCode')
    if code not in ('000', '00'):
        raise RentApiError(f'오류 코드 {code}: {_text(root, "header/resultMsg")}')
    deals = []
    for item in root.iterfind('body/items/item'):
        area = _text(item, 'excluUseAr') or _text(item, 'totalFloorAr')
        deals.append(
            Deal(
                rent_man=_int(_text(item, 'monthlyRent')),
                deposit_man=_int(_text(item, 'deposit')),
                area=float(area) if area else None,
                renewal=_text(item, 'contractType') == '갱신',
            )
        )
    total = _int(_text(root, 'body/totalCount'))
    return deals, total


class HttpRentFetcher:
    """실제 공공데이터포털 호출. 키는 Decoding 키 (httpx가 인코딩한다, 명세 2절)."""

    def __init__(self, service_key: str) -> None:
        self._key = service_key
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _client_for_loop(self) -> httpx.AsyncClient:
        """연결을 여러 요청이 같이 쓴다 (요청마다 새로 열면 동시 접속이 몰려 시간 초과가 났다).
        httpx 연결은 이벤트 루프에 묶이므로, 루프가 바뀌면(동기로 돌린 테스트 등) 새로 만든다."""
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client = http.async_client(
                httpx.Timeout(15, connect=10), httpx.Limits(max_connections=CONCURRENCY * 2)
            )
            self._loop = loop
        return self._client

    async def __call__(self, kind: Kind, lawd_cd: str, ymd: str) -> list[Deal]:
        return await self._fetch_all(self._client_for_loop(), kind, lawd_cd, ymd)

    async def _fetch_all(
        self, client: httpx.AsyncClient, kind: Kind, lawd_cd: str, ymd: str
    ) -> list[Deal]:
        deals: list[Deal] = []
        page = 1
        while True:
            params = {
                'serviceKey': self._key,
                'LAWD_CD': lawd_cd,
                'DEAL_YMD': ymd,
                'numOfRows': PAGE_SIZE,
                'pageNo': page,
            }
            r = await self._get(client, URL.format(kind=kind), params)
            got, total = parse_page(r.text)
            deals += got
            if not got or page * PAGE_SIZE >= total:
                return deals
            page += 1

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
        """한 번 실패하면(연결 지연 등) 한 번만 다시 시도한다."""
        for attempt in (1, 2):
            try:
                r = await client.get(url, params=params)
                r.raise_for_status()
                return r
            except httpx.HTTPError as e:
                if attempt == 2:
                    # 예외 글에 요청 주소(키 포함)가 들어갈 수 있어 종류만 남긴다
                    raise RentApiError(f'요청 실패: {type(e).__name__}') from None
        raise AssertionError('도달하지 않음')


# ── 2) 캐시 (SQLite) ─────────────────────────────────────────────


class RentCache:
    """시군구 하나의 (유형, 달) → 거래 목록. 최근 FRESH_MONTHS 달은 하루가 지나면 다시 받는다.

    체크포인터(AsyncSqliteSaver)와 같은 파일을 쓰므로, 이 동기 함수들은 반드시 이벤트 루프 밖
    (asyncio.to_thread)에서 부른다. 루프 위에서 부르면, 체크포인터가 쓰기 잠금을 잡고 루프가
    돌기를 기다리는 동안 이쪽은 잠금이 풀리기를 기다려 서로 멈춘다 (database is locked).
    지역 하나를 한 번에 읽고(SELECT 1번) 한 번에 쓴다(executemany 1번) → 잠금이 겹칠 틈을 줄인다.
    """

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        with self._connect() as db:
            db.execute(
                'CREATE TABLE IF NOT EXISTS rent_cache ('
                ' kind TEXT, lawd_cd TEXT, ymd TEXT, fetched_at TEXT, deals TEXT,'
                ' PRIMARY KEY (kind, lawd_cd, ymd))'
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5)

    def read(
        self, lawd_cd: str, months: list[str], fresh_since: str
    ) -> dict[tuple[Kind, str], list[Deal]]:
        """이 지역의 쓸 수 있는 캐시 전부. 최근 달인데 하루가 지난 것은 빼서 다시 받게 한다."""
        stale_before = datetime.now(UTC) - timedelta(days=1)
        with self._connect() as db:
            rows = db.execute(
                'SELECT kind, ymd, fetched_at, deals FROM rent_cache WHERE lawd_cd=?',
                (lawd_cd,),
            ).fetchall()
        out: dict[tuple[Kind, str], list[Deal]] = {}
        for kind, ymd, fetched_at, raw in rows:
            if ymd not in months:
                continue
            if ymd >= fresh_since and datetime.fromisoformat(fetched_at) < stale_before:
                continue
            out[(kind, ymd)] = [Deal(**d) for d in json.loads(raw)]
        return out

    def write(self, lawd_cd: str, found: dict[tuple[Kind, str], list[Deal]]) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.executemany(
                'INSERT OR REPLACE INTO rent_cache VALUES (?, ?, ?, ?, ?)',
                [
                    (kind, lawd_cd, ymd, now, json.dumps([asdict(d) for d in deals]))
                    for (kind, ymd), deals in found.items()
                ],
            )


# ── 3) 추정 ──────────────────────────────────────────────────────


class RentEstimate(BaseModel):
    """지역 하나의 원룸급 월세 추정. 결과 화면과 계산(이사 경우 주거비)에 쓴다."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    lawd_cd: str
    region: str = Field(description='화면 표시용 지역 이름 (예: 전남광주통합특별시 나주시)')
    monthly_rent_krw: int | None = Field(description='월세 중앙값(원). 못 구하면 null')
    deposit_krw: int | None = Field(default=None, description='같은 거래들의 보증금 중앙값(원)')
    sample_size: int = Field(default=0, description='중앙값을 낸 거래 수')
    period: str = Field(description='조회한 계약 기간 (예: 2026년 5~10월)')
    source: str = Field(description='출처 문장 (데이터명·기간·조건·표본 수)')
    status: Literal['estimated', 'missing', 'unavailable'] = Field(
        default='estimated',
        description=(
            'estimated=시세를 구함 / missing=조건에 맞는 거래가 없음 / '
            'unavailable=불러오지 못함(잠시 후 다시 조회)'
        ),
    )
    reason: str | None = Field(default=None, description='못 구한 이유')


def recent_months(today: date, n: int = MONTHS) -> list[str]:
    """이번 달을 뺀 최근 n개월 (오래된 달부터). 11월 15일 → ['202605', …, '202610']"""
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f'{y}{m:02d}')
    return out[::-1]


def period_text(months: list[str]) -> str:
    a, b = months[0], months[-1]
    if a[:4] == b[:4]:
        return f'{a[:4]}년 {int(a[4:])}~{int(b[4:])}월'
    return f'{a[:4]}년 {int(a[4:])}월~{b[:4]}년 {int(b[4:])}월'


def _region_name(lawd_cd: str) -> str | None:
    region = region_by_code(lawd_cd)
    return region.name if region else None


def usable(deals: Iterable[Deal]) -> list[Deal]:
    """원룸급 신규(또는 구분 없음) 월세 계약만."""
    return [
        d
        for d in deals
        if d.rent_man > 0
        and not d.renewal
        and d.area is not None
        and RENT_MIN_AREA <= d.area <= RENT_MAX_AREA
    ]


def _man_to_won(value: float) -> int:
    return int(round(value * 10_000))


def summarize(lawd_cd: str, months: list[str], deals: list[Deal]) -> RentEstimate:
    region = _region_name(lawd_cd) or lawd_cd
    period = period_text(months)
    picked = usable(deals)
    condition = f'{int(RENT_MIN_AREA)}~{int(RENT_MAX_AREA)}㎡ 월세(갱신 제외)'
    if not picked:
        return RentEstimate(
            lawd_cd=lawd_cd,
            region=region,
            monthly_rent_krw=None,
            period=period,
            source=f'{SOURCE_NAME} {period}, {condition}',
            status='missing',
            reason='조건에 맞는 월세 거래가 없어요',
        )
    rent = _man_to_won(statistics.median(d.rent_man for d in picked))
    deposit = _man_to_won(statistics.median(d.deposit_man for d in picked))
    sigungu = region.split(' ', 1)[-1]
    return RentEstimate(
        lawd_cd=lawd_cd,
        region=region,
        monthly_rent_krw=rent,
        deposit_krw=deposit,
        sample_size=len(picked),
        period=period,
        source=(
            f'{SOURCE_NAME} 중앙값 ({sigungu} {condition} {len(picked):,}건, {period}, 관리비 별도)'
        ),
    )


class RentEstimator:
    """시군구 코드 → RentEstimate. 캐시를 먼저 보고, 없는 달만 공공데이터포털에 묻는다."""

    def __init__(
        self,
        fetcher: RentFetcher,
        cache: RentCache | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._fetch = fetcher
        self._cache = cache
        self._today = today

    async def estimate(self, lawd_cd: str) -> RentEstimate:
        months = recent_months(self._today())
        fresh_since = months[-FRESH_MONTHS]
        cached = await self._read_cache(lawd_cd, months, fresh_since)
        sem = asyncio.Semaphore(CONCURRENCY)

        async def fetch(kind: Kind, ymd: str) -> list[Deal]:
            async with sem:
                return await self._fetch(kind, lawd_cd, ymd)

        todo = [(kind, ymd) for kind in KIND_LABELS for ymd in months if (kind, ymd) not in cached]
        try:
            fetched = await asyncio.gather(*(fetch(kind, ymd) for kind, ymd in todo))
        except RentApiError as e:
            logger.warning('월세 실거래가 조회 실패 (%s): %s', lawd_cd, e)
            return RentEstimate(
                lawd_cd=lawd_cd,
                region=_region_name(lawd_cd) or lawd_cd,
                monthly_rent_krw=None,
                period=period_text(months),
                source=SOURCE_NAME,
                status='unavailable',
                reason='실거래가를 불러오지 못했어요. 잠시 후 다시 계산하면 다시 불러와요',
            )
        found = dict(zip(todo, fetched, strict=True))
        if found:
            await self._write_cache(lawd_cd, found)
        deals = [d for got in (*cached.values(), *found.values()) for d in got]
        return summarize(lawd_cd, months, deals)

    async def _read_cache(
        self, lawd_cd: str, months: list[str], fresh_since: str
    ) -> dict[tuple[Kind, str], list[Deal]]:
        """캐시를 읽는다 (루프 밖 스레드에서). 캐시가 고장 나면 캐시 없이 조회한다."""
        if self._cache is None:
            return {}
        try:
            return await asyncio.to_thread(self._cache.read, lawd_cd, months, fresh_since)
        except sqlite3.Error:
            logger.warning('월세 캐시를 읽지 못해 새로 조회합니다 (%s)', lawd_cd, exc_info=True)
            return {}

    async def _write_cache(self, lawd_cd: str, found: dict[tuple[Kind, str], list[Deal]]) -> None:
        """캐시에 쓴다 (루프 밖 스레드에서). 실패해도 계산은 계속한다 (다음에 다시 받을 뿐)."""
        if self._cache is None:
            return
        try:
            await asyncio.to_thread(self._cache.write, lawd_cd, found)
        except sqlite3.Error:
            logger.warning('월세 캐시에 쓰지 못했습니다 (%s)', lawd_cd, exc_info=True)

    async def estimate_many(self, lawd_cds: Iterable[str]) -> dict[str, RentEstimate]:
        """여러 지역을 한꺼번에. 조회할 수 없는 코드(구를 골라야 하는 시 등)는 건너뛴다."""
        codes = sorted({c for c in lawd_cds if rentable(c)})
        found = await asyncio.gather(*(self.estimate(c) for c in codes))
        return dict(zip(codes, found, strict=True))


def rentable(lawd_cd: str) -> bool:
    """실거래가를 조회할 수 있는 시군구 코드인지 (코드표에 있고, 구를 더 고를 필요가 없음).

    예: 성남시 41130은 False (실거래가는 분당구 41135처럼 구 코드로만 나온다).
    """
    region = region_by_code(lawd_cd)
    return region is not None and region.selectable
