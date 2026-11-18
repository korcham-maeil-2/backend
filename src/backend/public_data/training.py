"""근무지 근처 재직자 오프라인 강의 (고용24 훈련과정 API) — 정착 플랜 (이슈 #18).

명세: docs/api/고용24_훈련과정.md (실제 호출로 확인한 값)
  - 국민내일배움카드  …/callOpenApiSvcInfo310L01.do  crseTracseSe=C0061I (재직자)
  - 국가인적자원개발 컨소시엄 …/callOpenApiSvcInfo312L01.do  crseTracseSe=C0042
    (중소기업 재직자, 무료)
  - 서비스마다 키가 따로다: WORK24_TRAINING_CARD_KEY, WORK24_TRAINING_CONSORTIUM_KEY
  - 공통 조건: returnType=JSON, outType=1(목록), pageSize=100(101 이상이면 빈 결과),
    srchTraStDt~srchTraEndDt = 훈련 '시작일' 범위(오늘~3개월), sort=ASC, sortCol=2(시작일),
    srchTraGbn=M1001(오프라인 일반), srchTraArea1=lawd_cd 앞 2자리, srchTraArea2=lawd_cd
  - 지역은 응답의 trngAreaCd로 판단한다 (address는 기관 주소일 수 있다, 명세 7절)
  - 오류: {"error": "..."} (정상 응답에는 error 키가 없다)
  - 시군구에 과정이 없으면 시도로 넓히고, 화면에 '근무지 인근(시도)'으로 범위를 밝힌다 (명세 8절)

고르는 법: 주말반(wkendSe=1) 먼저, 그다음 시작일 빠른 순으로 TRAINING_MAX개.
  한 기관은 PER_INSTITUTE개까지 (판교 주말반 앞 5개가 모두 한 학원이었다).
  한 번에 100개까지만 오고 시작일 순이라, 사람 많은 지역은 앞 100개가 며칠 안에 시작하는
  주중반뿐이다 (판교 확인). 그래서 주말반은 wkendSe=1로 따로 한 번 더 조회한다.
신청 자격(재직 여부, 내일배움카드 발급, 컨소시엄 참여기업 소속 등)은 판단하지 않는다
→ 상세 링크에서 확인.
결과는 지역마다 하루 메모리에 둔다 (약관: 1일 1만 쿼리 미만).
"""

import logging
import os
import time
from collections import Counter
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, Literal

import httpx
from pydantic import Field

from backend.calc.remaining import OutputModel
from backend.normalize.checks import region_by_code

logger = logging.getLogger(__name__)

BASE = 'https://www.work24.go.kr/cm/openApi/call/hr'
KINDS: dict[str, tuple[str, str, str]] = {
    # 종류: (주소, 훈련유형 코드, 키 환경 변수)
    'card': (f'{BASE}/callOpenApiSvcInfo310L01.do', 'C0061I', 'WORK24_TRAINING_CARD_KEY'),
    'consortium': (
        f'{BASE}/callOpenApiSvcInfo312L01.do',
        'C0042',
        'WORK24_TRAINING_CONSORTIUM_KEY',
    ),
}
KIND_LABEL = {'card': '국민내일배움카드', 'consortium': '중소기업 재직자 대상(컨소시엄)'}
SEARCH_DAYS = 92  # 시작일 범위: 오늘~3개월
TRAINING_MAX = 5
PER_INSTITUTE = 2
CACHE_SECONDS = 24 * 60 * 60  # 같은 날 안에서만 쓴다 (캐시 키에 날짜가 들어 있다)
# 고용24가 느려도 화면이 오래 기다리지 않게 짧게. 실패한 지역은 이 동안 다시 묻지 않는다
# (youthcenter.py와 같은 방식: 페이지를 열 때마다 느린 조회를 되풀이하지 않게)
TIMEOUT = httpx.Timeout(8.0, connect=3.0)
FAILURE_COOLDOWN_SECONDS = 120
SOURCE = '고용24 훈련과정 (work24.go.kr)'
WEEKEND = '1'

Kind = Literal['card', 'consortium']


class Course(OutputModel):
    title: str
    institute: str | None
    kind: Kind
    kind_label: str
    start: str = Field(description='훈련 시작일 (YYYY-MM-DD)')
    end: str | None
    weekend: bool = Field(description='주말반')
    fee_krw: int | None = Field(description='수강비 (원). 컨소시엄은 0(무료)')
    url: str | None = Field(description='고용24 과정 상세 (신청·장소·자격 확인)')


class TrainingList(OutputModel):
    """GET /plan/training/{lawd_cd} 응답."""

    lawd_cd: str
    region: str | None
    scope: Literal['sigungu', 'sido'] = Field(
        description='sigungu=근무지 시군구 과정, sido=시군구에 없어 시도로 넓힘'
    )
    courses: list[Course]
    status: str = Field(description='ok / unavailable(불러오지 못함) / off(키 없음)')
    reason: str | None = None
    source: str = SOURCE


class TrainingError(Exception):
    pass


# (종류, 시도 코드, 시군구 코드 또는 None, 오늘, 주말반만) → 과정 원본 목록
Fetcher = Callable[[Kind, str, str | None, date, bool], list[dict[str, Any]]]


def http_fetcher(keys: dict[Kind, str]) -> Fetcher:
    def fetch(
        kind: Kind, sido: str, sigungu: str | None, today: date, weekend: bool
    ) -> list[dict[str, Any]]:
        url, course_type, _ = KINDS[kind]
        params = {
            'authKey': keys[kind],
            'returnType': 'JSON',
            'outType': '1',
            'pageNum': '1',
            'pageSize': '100',
            # 내일부터 시작하는 과정만 (오늘 시작은 이미 신청하기 어렵다)
            'srchTraStDt': f'{today + timedelta(days=1):%Y%m%d}',
            'srchTraEndDt': f'{today + timedelta(days=SEARCH_DAYS):%Y%m%d}',
            'sort': 'ASC',
            'sortCol': '2',
            'srchTraArea1': sido,
            'crseTracseSe': course_type,
            'srchTraGbn': 'M1001',
        }
        if sigungu:
            params['srchTraArea2'] = sigungu
        if weekend:
            params['wkendSe'] = WEEKEND
        try:
            body = httpx.get(url, params=params, timeout=TIMEOUT).json()
        except (httpx.HTTPError, ValueError) as e:
            raise TrainingError(type(e).__name__) from e
        # 응답 모양이 다르면(목록이 아님 등) 500 대신 '불러오지 못함'으로
        if not isinstance(body, dict):
            raise TrainingError('응답이 객체가 아님')
        if 'error' in body:
            raise TrainingError(str(body['error'])[:80])
        found = body.get('srchList') or []
        if not isinstance(found, list):
            raise TrainingError('srchList가 목록이 아님')
        return [raw for raw in found if isinstance(raw, dict)]

    return fetch


def _int(value: Any) -> int | None:
    try:
        return int(str(value).replace(',', '').strip())
    except ValueError:
        return None


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ''


def _course(raw: dict[str, Any], kind: Kind) -> Course | None:
    start = _str(raw.get('traStartDate'))
    if not _str(raw.get('title')) or len(start) != 10:
        return None
    fee = _int(raw.get('courseMan'))
    return Course(
        title=' '.join(str(raw['title']).split()),
        institute=_str(raw.get('subTitle')) or None,
        kind=kind,
        kind_label=KIND_LABEL[kind],
        start=start,
        end=_str(raw.get('traEndDate')) or None,
        weekend=str(raw.get('wkendSe')) == WEEKEND,
        fee_krw=0 if kind == 'consortium' else fee,
        url=_str(raw.get('titleLink')) or None,
    )


def pick_courses(found: list[Course], today: date, n: int = TRAINING_MAX) -> list[Course]:
    """내일부터 시작하는 과정만 (오늘 시작은 이미 신청하기 어렵다), 주말반 먼저, 시작일 빠른 순.
    같은 과정(이름·기관·시작일)은 한 번, 한 기관은 PER_INSTITUTE개까지 (기관 이름이 없는 과정은
    서로 다른 기관일 수 있어 묶지 않는다)."""
    seen: set = set()
    per: Counter = Counter()
    out: list[Course] = []
    for c in sorted(found, key=lambda c: (not c.weekend, c.start, c.title)):
        key = (c.title, c.institute, c.start)
        crowded = c.institute is not None and per[c.institute] >= PER_INSTITUTE
        if c.start <= today.isoformat() or key in seen or crowded:
            continue
        seen.add(key)
        if c.institute is not None:
            per[c.institute] += 1
        out.append(c)
    return out[:n]


class TrainingSource:
    def __init__(
        self,
        fetch: Fetcher | None,
        kinds: tuple[Kind, ...] = ('card', 'consortium'),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch, self._kinds, self._clock = fetch, kinds, clock
        # (시군구, 날짜) → (저장 시각, 결과). 날짜가 바뀌면 새로 묻는다
        self._cache: dict[tuple[str, date], tuple[float, TrainingList]] = {}
        self._failed: dict[str, tuple[float, TrainingList]] = {}  # 실패한 지역 (잠깐 기억)

    def _kind(self, kind: Kind, sido: str, sigungu: str | None, today: date) -> list[Course]:
        """한 종류(내일배움카드·컨소시엄)의 과정. 주말반은 따로 한 번 더 묻는다 (모듈 설명)."""
        raws = self._fetch(kind, sido, sigungu, today, True)  # 주말반
        raws += self._fetch(kind, sido, sigungu, today, False)
        courses = []
        for raw in raws:
            # 지역은 trngAreaCd로 판단 (시군구로 찾을 때만 걸러낸다)
            if sigungu and str(raw.get('trngAreaCd') or '') not in ('', sigungu):
                continue
            if (c := _course(raw, kind)) is not None:
                courses.append(c)
        return courses

    def _collect(self, lawd_cd: str, today: date) -> tuple[list[Course], str, list[str]]:
        """종류마다 따로 조회한다: 한 종류가 실패해도(키 오류 등) 다른 종류는 보여 준다.
        시군구에 '내일부터 시작하는' 과정이 없으면 그 종류는 시도로 넓힌다.
        돌려주는 값: (과정, 범위, 실패한 종류)"""
        courses: list[Course] = []
        scope = 'sigungu'
        failed: list[str] = []
        for kind in self._kinds:
            try:
                found = pick_courses(self._kind(kind, lawd_cd[:2], lawd_cd, today), today)
                if not found:  # 날짜로 거른 뒤에도 없으면 시도로 (명세 8절)
                    found = pick_courses(self._kind(kind, lawd_cd[:2], None, today), today)
                    scope = 'sido' if found else scope
            except (TrainingError, TypeError, AttributeError, KeyError, ValueError) as e:
                logger.warning('고용24 %s 조회 실패 (%s): %s', kind, lawd_cd, e)
                failed.append(kind)
                continue
            courses += found
        return courses, scope, failed

    def get(self, lawd_cd: str, today: date) -> TrainingList:
        region = region_by_code(lawd_cd)
        name = region.name if region else None
        if self._fetch is None:
            return TrainingList(
                lawd_cd=lawd_cd,
                region=name,
                scope='sigungu',
                courses=[],
                status='off',
                reason='재직자 강의를 불러오지 않아요 (서버에 고용24 인증키 없음)',
            )
        now = self._clock()
        cached = self._cache.get((lawd_cd, today))
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
        failed_before = self._failed.get(lawd_cd)
        if failed_before and now - failed_before[0] < FAILURE_COOLDOWN_SECONDS:
            return failed_before[1]
        courses, scope, failed = self._collect(lawd_cd, today)
        if failed and len(failed) == len(self._kinds):  # 모든 종류가 실패했을 때만 '불러오지 못함'
            result = TrainingList(
                lawd_cd=lawd_cd,
                region=name,
                scope='sigungu',
                courses=[],
                status='unavailable',
                reason='재직자 강의를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.',
            )
            self._failed[lawd_cd] = (now, result)
            return result
        found = TrainingList(
            lawd_cd=lawd_cd,
            region=name,
            scope=scope,  # type: ignore[arg-type]
            courses=pick_courses(courses, today),
            status='ok',
        )
        if not failed:  # 일부만 실패했으면 캐시하지 않는다 (다음에 다시 묻는다)
            self._cache[(lawd_cd, today)] = (now, found)
        return found


def training_from_env() -> TrainingSource:
    """키가 있는 종류만 조회한다. 하나도 없으면 꺼짐."""
    keys: dict[Kind, str] = {}
    for kind, (_, _, env) in KINDS.items():
        if value := os.getenv(env, '').strip():
            keys[kind] = value  # type: ignore[index]
    if not keys:
        return TrainingSource(None)
    return TrainingSource(http_fetcher(keys), kinds=tuple(keys))  # type: ignore[arg-type]
