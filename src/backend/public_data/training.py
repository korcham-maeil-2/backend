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
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, Literal

import httpx
from pydantic import Field

from backend.calc.remaining import OutputModel
from backend.normalize.checks import region_by_code
from backend.public_data import http

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
FREE_SLOTS = 2  # 무료(컨소시엄) 과정 자리: 평일반이 많아 주말반 카드 과정에 밀려 안 보이지 않게
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
    ncs: str | None = Field(default=None, description='직무 분류 코드 (NCS, 앞 2자리가 대분류)')


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


# (종류, 시도 코드, 시군구 코드 또는 None, 오늘, 주말반만, 직무 NCS 대분류 또는 None) → 과정 목록
Fetcher = Callable[[Kind, str, str | None, date, bool, str | None], list[dict[str, Any]]]


def http_fetcher(keys: dict[Kind, str]) -> Fetcher:
    def fetch(
        kind: Kind,
        sido: str,
        sigungu: str | None,
        today: date,
        weekend: bool,
        ncs: str | None = None,
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
        if ncs:  # 직무(NCS 대분류)로 좁힌다 ('20' → 정보통신 과정만, 2026-11-18 확인)
            params['srchNcs1'] = ncs
        try:
            with http.client(TIMEOUT) as client:
                body = client.get(url, params=params).json()
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
        ncs=_str(raw.get('ncsCd')) or None,
    )


# 공고 직무 → NCS 대분류 (ncsCd 앞 2자리), 모르면 None. 틀리면 '모름'보다 나빠서
# 하는 일을 업종보다 먼저 보고('자동차 영업'은 영업), 겹치는 낱말은 좁힌다 (테스트에 예시)
_JOB_NCS = (
    ('08', re.compile(r'디자인|디자이너|영상(?!의학)|방송(?!통신)')),
    (
        '20',
        re.compile(
            r'개발자|웹 ?개발|앱 ?개발|프로그래|소프트웨어|백엔드|프론트|풀스택|데이터(?! ?입력)|'
            r'인공지능|머신러닝|정보보안|클라우드|네트워크|DevOps|(?<![A-Za-z])(?:AI|SW)(?![A-Za-z])'
        ),
    ),
    ('10', re.compile(r'영업|판매|(?<![A-Za-z])MD(?![A-Za-z])')),
    ('02', re.compile(r'사무|회계|경리|인사(?!이트)|총무|재무|마케팅|마케터')),
    ('06', re.compile(r'간호|의료|임상')),
    ('13', re.compile(r'조리|요리|주방|제과|제빵')),
    ('09', re.compile(r'물류|운송|배송')),
    ('03', re.compile(r'금융|은행|보험|증권')),
    ('19', re.compile(r'전기|전자(?!상거래)|반도체|전력')),
    ('15', re.compile(r'기계|설비|자동차')),
    ('14', re.compile(r'건축|토목|건설')),
    ('17', re.compile(r'화학|바이오|제약')),
    ('23', re.compile(r'안전관리|환경관리|환경공학')),
)


def ncs_for_job(job: str | None) -> str | None:
    """'백엔드 개발자' → '20'(정보통신). 먼저 맞는 분류 하나."""
    return next((code for code, words in _JOB_NCS if job and words.search(job)), None)


def pick_courses(
    found: list[Course], today: date, n: int = TRAINING_MAX, ncs: str | None = None
) -> list[Course]:
    """내일부터 시작하는 과정만 (오늘 시작은 이미 신청하기 어렵다). 직무(ncs 대분류)에 맞는 것,
    주말반, 시작일 빠른 순. 무료(컨소시엄) 과정은 있으면 FREE_SLOTS개까지 자리를 둔다.
    같은 과정(이름·기관)은 먼저 고른 기수 한 번, 한 기관은 PER_INSTITUTE개까지
    (기관 이름이 없는 과정은 서로 다른 기관일 수 있어 묶지 않는다)."""

    def order(c: Course) -> tuple:
        return (not (ncs and (c.ncs or '').startswith(ncs)), not c.weekend, c.start, c.title)

    seen: set = set()
    per: Counter = Counter()
    out: list[Course] = []
    for c in sorted(found, key=order):
        key = (c.title, c.institute)  # 같은 과정의 다음 기수는 자리만 차지한다
        crowded = c.institute is not None and per[c.institute] >= PER_INSTITUTE
        if c.start <= today.isoformat() or key in seen or crowded:
            continue
        seen.add(key)
        if c.institute is not None:
            per[c.institute] += 1
        out.append(c)
    # 직무에 맞는 과정부터 채우고, 단계마다 무료 과정에 먼저 자리를 준다 (합쳐 FREE_SLOTS개까지)
    picked: list[Course] = []
    for tier in (False, True):
        group = [c for c in out if order(c)[0] == tier]
        room = max(FREE_SLOTS - sum(c.kind == 'consortium' for c in picked), 0)
        free = [c for c in group if c.kind == 'consortium'][:room]
        picked += (free + [c for c in group if c not in free])[: n - len(picked)]
    return sorted(picked, key=order)


class TrainingSource:
    def __init__(
        self,
        fetch: Fetcher | None,
        kinds: tuple[Kind, ...] = ('card', 'consortium'),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch, self._kinds, self._clock = fetch, kinds, clock
        # (시군구, 날짜, 직무, 범위) → (만료 시각, 결과). 한 종류라도 실패하면 쿨다운만큼만 둔다
        self._cache: dict[tuple[str, date, str | None, str], tuple[float, TrainingList]] = {}
        # 지역마다 잠금: 같은 지역은 한 번만 조회하고, 다른 지역은 기다리지 않는다
        self._locks: dict[str, threading.Lock] = {}

    def _kind(
        self, kind: Kind, sido: str, sigungu: str | None, today: date, ncs: str | None = None
    ) -> list[Course]:
        """한 종류(내일배움카드·컨소시엄)의 과정. 주말반은 따로 한 번 더 묻는다 (모듈 설명)."""
        raws = self._fetch(kind, sido, sigungu, today, True, ncs)  # 주말반
        raws += self._fetch(kind, sido, sigungu, today, False, ncs)
        courses = []
        for raw in raws:
            # 지역은 trngAreaCd로 판단 (시군구로 찾을 때만 걸러낸다)
            if sigungu and str(raw.get('trngAreaCd') or '') not in ('', sigungu):
                continue
            if (c := _course(raw, kind)) is not None:
                courses.append(c)
        return courses

    def _gather(
        self,
        kinds: tuple[Kind, ...],
        lawd_cd: str,
        sigungu: str | None,
        today: date,
        ncs: str | None = None,
    ) -> tuple[list[Course], list[Kind]]:
        """종류마다 따로 조회한다: 한 종류가 실패해도(키 오류 등) 다른 종류는 보여 준다.
        돌려주는 값: (내일부터 시작하는 과정, 실패한 종류)"""
        tomorrow = (today + timedelta(days=1)).isoformat()
        courses: list[Course] = []
        failed: list[Kind] = []
        for kind in kinds:
            try:
                found = self._kind(kind, lawd_cd[:2], sigungu, today, ncs)
                courses += [c for c in found if c.start >= tomorrow]
            except (TrainingError, TypeError, AttributeError, KeyError, ValueError) as e:
                logger.warning('고용24 %s 조회 실패 (%s): %s', kind, lawd_cd, e)
                failed.append(kind)
        return courses, failed

    def _collect(
        self, lawd_cd: str, today: date
    ) -> tuple[list[Course], Literal['sigungu', 'sido'], list[Kind]]:
        """시군구에서 찾고, 모든 종류가 비었을 때만 시도로 넓힌다 (명세 8절).
        종류마다 넓히면 시군구·시도 과정이 섞여 화면의 범위 안내가 틀린다.
        돌려주는 값: (과정, 범위, 실패한 종류)"""
        courses, failed = self._gather(self._kinds, lawd_cd, lawd_cd, today)
        if courses:
            return courses, 'sigungu', failed
        ok = tuple(k for k in self._kinds if k not in failed)
        if not ok:
            return [], 'sigungu', failed
        wide, wide_failed = self._gather(ok, lawd_cd, None, today)
        return wide, ('sido' if wide else 'sigungu'), failed + wide_failed

    def get(self, lawd_cd: str, today: date, ncs: str | None = None) -> TrainingList:
        """근무지의 과정 TRAINING_MAX개. ncs(직무 대분류)를 주면 그 직무 과정을 먼저 고른다.
        조회 결과는 지역·날짜(·직무)마다 담아 두고, 고르기만 요청마다 한다."""
        base = self._load(lawd_cd, today)
        if base.status != 'ok':
            return base
        courses = base.courses
        if ncs:
            # 시작일 순 100개 안에는 직무 과정이 적어(판교 정보통신 62개 중 7개, 2026-11-18)
            # 직무 조건으로 한 번 더 묻는다. 범위는 기본 목록과 같게
            courses = self._load(lawd_cd, today, ncs, base.scope).courses + courses
        return base.model_copy(update={'courses': pick_courses(courses, today, ncs=ncs)})

    def _load(
        self,
        lawd_cd: str,
        today: date,
        ncs: str | None = None,
        scope: Literal['sigungu', 'sido'] = 'sigungu',
    ) -> TrainingList:
        with self._locks.setdefault(lawd_cd, threading.Lock()):
            return self._load_unlocked(lawd_cd, today, ncs, scope)

    def _load_unlocked(
        self, lawd_cd: str, today: date, ncs: str | None, scope: Literal['sigungu', 'sido']
    ) -> TrainingList:
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
        key = (lawd_cd, today, ncs, scope)  # 직무 과정은 어느 범위로 물었는지까지
        cached = self._cache.get(key)
        if cached and now < cached[0]:
            return cached[1]
        if ncs:  # 직무 과정: 기본 목록과 같은 범위에서만 (넓히지 않는다)
            sigungu = lawd_cd if scope == 'sigungu' else None
            courses, failed = self._gather(self._kinds, lawd_cd, sigungu, today, ncs)
        else:
            courses, scope, failed = self._collect(lawd_cd, today)
        if set(failed) == set(self._kinds):  # 모든 종류가 실패했을 때만 '불러오지 못함'
            result = TrainingList(
                lawd_cd=lawd_cd,
                region=name,
                scope='sigungu',
                courses=[],
                status='unavailable',
                reason='재직자 강의를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.',
            )
        else:
            result = TrainingList(
                lawd_cd=lawd_cd,
                region=name,
                scope=scope,
                courses=courses,  # 고르기 전 전부 (get이 고른다)
                status='ok',
            )
        # 지난 날짜는 지운다 (다른 지역 잠금과 겹칠 수 있어 목록을 떠 두고 pop)
        for old in [k for k in list(self._cache) if k[1] != today]:
            self._cache.pop(old, None)
        ttl = FAILURE_COOLDOWN_SECONDS if failed else CACHE_SECONDS
        self._cache[key] = (now + ttl, result)
        return result


def training_from_env() -> TrainingSource:
    """키가 있는 종류만 조회한다. 하나도 없으면 꺼짐."""
    keys: dict[Kind, str] = {}
    for kind, (_, _, env) in KINDS.items():
        if value := os.getenv(env, '').strip():
            keys[kind] = value  # type: ignore[index]
    if not keys:
        return TrainingSource(None)
    return TrainingSource(http_fetcher(keys), kinds=tuple(keys))  # type: ignore[arg-type]
