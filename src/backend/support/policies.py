"""받을 수 있는 청년 지원 고르기 — 공고 근무지(지역)·나이·분류·신청 기간으로 거른다.

    공고마다 근무지 시군구 코드(lawd_cd) → 온통청년에서 그 지역 정책 → 아래 조건으로 거르기
      1) 분류: 매달 남는 돈에 영향을 주는 것만 (주거, 금융·복지·문화)
      2) 지역: 다른 지역 지자체의 정책은 뺀다 (아래 '지역 거르기')
      3) 나이: 사용자 나이가 정책 나이 범위 안 (나이를 모르면 거르지 않고 안내만)
      4) 신청 기간: 지금 신청할 수 있거나 곧 열리는 것만 (끝난 정책은 보여 주지 않는다)

[지역 정책과 전국 정책]
회사를 고르는 데 도움이 되는 것은 근무지마다 다른 '지역 정책'(시도·시군구)이다.
  - 지역 정책: 지역마다 따로 보여 준다.
  - 전국 정책(중앙부처): 어느 회사를 골라도 같으므로 지역마다 되풀이하지 않고 한 번만 따로.

[신청 기간]
  - 신청 중·상시·곧 시작·기간 모름만 담는다. 신청이 끝난 정책은 담지 않는다 — 다시 열릴지,
    열려도 조건이 같을지 아무도 장담할 수 없어서다 (화면은 '조건과 일정은 바뀔 수 있으니 신청 전에
    확인해 주세요'로 안내한다).
  - 같은 사업이 해마다 따로 등록돼 있어('2025 …'·'2026 …') 연도를 뺀 이름이 같으면 하나만 남긴다.
    신청 중 > 상시 > 곧 시작 > 기간 모름 순으로 고르고, 같으면 마감이 늦은 것.

[나에게 맞는 것 먼저]
정책 이름·키워드로 세 묶음으로 나눠 순서를 정한다 (빼지는 않는다, 자격은 신청 페이지에서 확인).
  moving   이사하는 사회초년생에게 바로 쓰이는 것 (주거 분류, 월세·전세·임대·보증금·이사 등)
  general  그 밖의 청년 정책
  targeted 특정 대상 (자립준비청년·장애·한부모·가족돌봄·대학생·창업·농어업 등).
           가족 수가 1명(혼자)이면 신혼·출산·자녀 정책도 여기로
순서: moving → general → targeted, 그 안에서 주거 먼저, 마감 가까운 순 (상시·기간 모름은 뒤).

[지역 거르기]
API의 지역 코드(zipCd)만으로는 부족하다. '의성군으로 이사 오면 지원'처럼 다른 지자체가
대상 지역을 전국(시군구 코드 250여 개)으로 적어 둔 정책이 많아서, 나주를 조회해도
의성군·화순군·서울시 정책이 함께 온다 (2026-11-15 나주 조회: 관련 분류 231건 중 약 40건).
  1) 기관 구분 코드(pvsnInstGroupCd): 0054001 중앙부처 → 전국 정책. 그 밖(지자체·없음) → 2)로.
  2) 지자체 정책은 기관 이름(등록 최상위·상위 기관, 주관 기관)에서 시도·시군구를 찾는다.
     '경기도교육청' → 경기도, '화순군청' → 화순군. 근무지 시도와 같아야 하고, 시군구가 적혀
     있으면(기관 이름이나 정책 이름에) 그것도 같아야 한다.
  3) 이름으로 지역을 못 찾으면: 대상 지역 수(zipCdCount)가 좁으면(근무지를 콕 집은 정책)
     지역 정책. 전국 대상이면 중앙 기관 이름(부·처·청·위원회)일 때만 전국 정책, 아니면
     '우리 지역으로 오세요'처럼 남의 지역 정책일 수 있어 뺀다.

소득·자격 조건은 대부분 자유 서술이라 코드로 판단하지 않는다 → 원문을 보여 주고
'신청 페이지에서 확인'으로 안내한다 (docs/api/온통청년_청년정책.md 7절).
숫자 계산은 하지 않는다 (지원금이 잔여액에 더해지지 않음). 목록과 출처만 준다.
"""

import html
import logging
import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from functools import cache
from typing import Any, Literal

from pydantic import Field, ValidationError

from backend.calc.remaining import OutputModel, UserProfile
from backend.normalize.checks import (
    SIDO_ALIASES,
    Region,
    load_regions,
    normalize_sido,
    region_by_code,
)
from backend.normalize.schema import ComparisonRow
from backend.support.youthcenter import PolicySource, SupportError

logger = logging.getLogger(__name__)

MAX_POLICIES = 10
# 첫 취업이면 누구나 해당하는 전국 상시 지원 (정착 플랜의 '입사하면 챙겨 볼 것').
# 이름에서만 찾고 구직자용은 뺀다. 보여 줄 목록(MAX_POLICIES)에 못 들어도 이만큼은 함께 보낸다
_FIRST_JOB_WORDS = re.compile(r'국민연금|학자금|청약|자산형성|직장인|재직')
_NOT_FIRST_JOB_WORDS = re.compile(r'구직|취업준비|면접|일경험|인턴|창업|상담')
FIRST_JOB_MAX = 5
LOOKUP_THREADS = 4  # 지역 여러 곳을 동시에 조회 (캐시가 없으면 지역마다 1~3초)
KST = timezone(timedelta(hours=9))

# 대분류 이름이 두 벌 섞여 온다 (코드정의서 이름 / 응답 이름).
# 응답 이름의 가운뎃점은 반각 '･'(U+FF65)
RELEVANT_CATEGORIES = {'주거', '금융･복지･문화', '복지문화'}
HOUSING = '주거'  # 이 분류를 먼저 보여 준다 (매달 남는 돈에 가장 크게 영향)

# 코드정의서 (docs/api/온통청년_청년정책.md 5절). 기관 구분은 2026-11-15 응답으로 확인
APPLY_ALWAYS = '0057002'
APPLY_CLOSED = '0057003'
INCOME_ANY = '0043001'
CENTRAL_GOV = '0054001'  # 기관 구분: 0054001 중앙부처, 0054002 지자체

# 지역을 찾지 못한 기관 가운데 중앙 기관으로 보는 이름 끝 (국토교통부, 국세청, 금융위원회 …)
_CENTRAL_NAME = re.compile(r'(부|처|청|실|위원회|정부산하기관및위원회)$')
# 대상 지역(zipCd)이 이만큼 이상이면 '전국 대상'으로 본다 (전국 시군구는 250여 개)
NATIONWIDE_ZIPS = 200

SOURCE_NAME = '온통청년 청년정책 (youthcenter.go.kr)'
SOURCE_URL = 'https://www.youthcenter.go.kr'

ApplyStatus = Literal['open', 'always', 'upcoming', 'unknown']
Scope = Literal['local', 'national']

# 나에게 맞는 것 먼저 (모듈 설명): 정책 이름·키워드에서 찾는 말
# ('대출'·'이자'는 넣지 않는다: '학자금대출 신용회복'처럼 이사와 상관없는 것이 많다)
_MOVING_WORDS = re.compile(r'월세|전세|임대|임차|보증금|주거비|이사|중개보수')
# 낱말을 좁게 잡는다: '서울 전역'·'청년·대학생 햇살론'·'중소기업 청년 기숙사'는 일반 정책이다
_TARGETED_WORDS = re.compile(
    r'자립준비|보호종료|장애|한부모|가족돌봄|대학원|대학기숙사|공공기숙사|연합생활관|'
    r'농업|농촌|귀농|어업|어촌|귀어|군인|장병|제대|전역예정|전역자|북한이탈|다문화|'
    r'예술지원|예술인|창작자|예술가|자살|금연|창업|중장년|군복무|수급자'
)
# 요건 코드 (docs/api/온통청년_청년정책.md 코드정의, 쉼표로 여럿): 이 중 하나도 없으면 막 취업한
# 사람이 아닌 대상 전용이다 (미취업자·재학생·장애인·군인 전용 등). 코드가 비어 있으면 보지 않는다
_FOR_NEW_WORKER = {
    'jobCd': {'0013001', '0013009', '0013010'},  # 재직자·기타·제한없음
    # 고교 졸업·대졸 예정·대학 졸업·석박사·기타·제한없음
    'schoolCd': {'0049004', '0049006', '0049007', '0049008', '0049009', '0049010'},
    'sbizCd': {'0014001', '0014010'},  # 중소기업·제한없음
}
_FAMILY_WORDS = re.compile(r'신혼|결혼|부부|출산|임신|난임|다자녀|자녀|육아|출생')

Fit = Literal['moving', 'general', 'targeted']
_FIT_RANK: dict[Fit, int] = {'moving': 0, 'general': 1, 'targeted': 2}

# 같은 사업의 여러 해 치 가운데 남길 것 (큰 값이 먼저)
_STATUS_RANK: dict[ApplyStatus, int] = {
    'open': 4,
    'always': 3,
    'upcoming': 2,
    'unknown': 1,
}


class SupportPolicy(OutputModel):
    """받을 수 있는 정책 하나. 화면에 그대로 보여 준다."""

    id: str = Field(description='온통청년 정책 번호')
    name: str
    category: str = Field(description='대분류 (주거, 금융･복지･문화 등)')
    support: str | None = Field(description='지원 내용 (원문, 길면 줄임)')
    agency: str | None = Field(description='주관 기관')
    age_range: str = Field(description="나이 조건 (예: '19~39세', '나이 제한 없음')")
    income: str = Field(description="소득 조건 (예: '소득 무관', 원문 서술)")
    conditions: str | None = Field(description='추가 신청 자격 (원문, 길면 줄임)')
    scope: Scope = Field(description='local=근무지 지자체 정책, national=전국 정책')
    fit: Fit = Field(
        description=(
            'moving=이사하는 사회초년생에게 바로 쓰이는 것(월세·전세 등), general=그 밖, '
            'targeted=특정 대상(자립준비청년·장애·대학생·창업 등, 혼자면 신혼·출산도) → 아래로'
        )
    )
    apply_status: ApplyStatus = Field(
        description=(
            'open=신청 기간 중, always=상시, upcoming=곧 시작, unknown=기간을 모름 '
            '(끝난 정책은 담지 않는다)'
        )
    )
    apply_period: str | None = Field(description="신청 기간 (예: '2026-04-01 ~ 2026-11-30')")
    url: str | None = Field(description='신청·안내 페이지 (https 붙임). 자격은 여기서 확인')


class RegionSupport(OutputModel):
    """지역(시군구) 하나의 지역 정책. 전국 정책은 LocalSupport.national에 한 번만 담는다."""

    lawd_cd: str
    region: str = Field(description='시도 시군구 (예: 서울특별시 강서구)')
    posting_ids: list[str] = Field(description='이 지역에 근무지가 있는 공고')
    policies: list[SupportPolicy] = Field(description='지금·곧 신청할 수 있는 지역 정책')
    matched: int = Field(description='신청할 수 있는 지역 정책 수 (보여 준 것보다 많을 수 있음)')
    error: str | None = Field(default=None, description='조회하지 못한 이유 (해요체)')


class LocalSupport(OutputModel):
    """get_local_support 노드의 결과 = 세션 응답의 local_support 칸."""

    regions: list[RegionSupport]
    national: list[SupportPolicy] = Field(
        description='전국 정책 (어느 근무지든 같다). 신청할 수 있는 것만'
    )
    national_matched: int = Field(description='신청할 수 있는 전국 정책 수')
    age: int | None = Field(description='거르는 데 쓴 나이. 없으면 나이로 거르지 않음')
    notes: list[str] = Field(default_factory=list, description='화면에 함께 보여 줄 안내')
    source: str = Field(default=SOURCE_NAME, description='출처')
    source_url: str = Field(default=SOURCE_URL)
    checked_on: str = Field(description='조회한 날 (YYYY-MM-DD, 한국 시간)')


def today_kst() -> date:
    return datetime.now(KST).date()


# ── 정책 한 건 읽기 ────────────────────────────────────────────────


def _text(policy: dict[str, Any], key: str) -> str:
    # 응답 글자에 '&amp;'·'&quot;'처럼 HTML로 바꿔 쓴 글자가 섞여 온다
    return html.unescape(str(policy.get(key) or '')).strip()


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except ValueError:
        return 0


def _categories(policy: dict[str, Any]) -> list[str]:
    """관련 분류만, 겹친 것은 한 번 ('일자리,일자리'처럼 같은 값이 겹쳐 오기도 한다)."""
    found = (c.strip() for c in _text(policy, 'lclsfNm').split(','))
    return list(dict.fromkeys(c for c in found if c in RELEVANT_CATEGORIES))


def is_relevant(policy: dict[str, Any]) -> bool:
    """고를 대상인 분류(주거, 금융·복지·문화)인지. 캐시에 이것만 저장한다 (main.py)."""
    return bool(_categories(policy))


def _dates(text: str) -> list[date]:
    """'20260401 ~ 20261130' 안의 날짜들. 달력에 없는 날짜는 건너뛴다."""
    found = []
    for d in re.findall(r'\d{8}', text):
        try:
            found.append(datetime.strptime(d, '%Y%m%d').date())
        except ValueError:
            continue
    return found


def _windows(dates: list[date]) -> list[tuple[date, date]]:
    """날짜를 두 개씩 (시작, 끝) 신청 기간으로. 짝이 없는 마지막 날짜는 그날 하루. 시작 순."""
    pairs = [
        (dates[i], dates[i + 1] if i + 1 < len(dates) else dates[i])
        for i in range(0, len(dates), 2)
    ]
    return sorted(pairs)


def _apply(policy: dict[str, Any], today: date) -> tuple[ApplyStatus, str | None] | None:
    """(신청 상태, 기간 글자). 신청이 끝났으면 None → 뺀다.

    기간이 여러 번이면('20260101 ~ 20260131, 20260701 ~ 20260731') 오늘이 든 기간,
    없으면 다음 기간을 본다 (사이 기간에 '신청 중'이 되지 않게).
    """
    code = _text(policy, 'aplyPrdSeCd')
    if code == APPLY_ALWAYS:
        return 'always', '상시'
    if code == APPLY_CLOSED:
        return None
    dates = _dates(_text(policy, 'aplyYmd'))
    if not dates:
        return 'unknown', None
    # 오늘이 든 기간, 없으면 다음 기간. 다 지났으면 끝난 정책
    upcoming = [w for w in _windows(dates) if w[1] >= today]
    if not upcoming:
        return None
    start, end = upcoming[0]
    return ('upcoming' if start > today else 'open'), f'{start:%Y-%m-%d} ~ {end:%Y-%m-%d}'


def _age_range(policy: dict[str, Any]) -> tuple[int, int]:
    """(최소, 최대) 나이. 0은 제한 없음.

    '0' 말고도 1세·99세처럼 사실상 제한이 없다는 뜻으로 적은 값이 있어 0으로 맞춘다.
    연령 제한 여부 플래그(sprtTrgtAgeLmtYn)는 실제 값과 어긋나서 쓰지 않는다.
    """
    low, high = _int(policy.get('sprtTrgtMinAge')), _int(policy.get('sprtTrgtMaxAge'))
    return (low if low > 1 else 0), (high if high < 99 else 0)


def _age_ok(age: int | None, low: int, high: int) -> bool:
    return age is None or ((not low or age >= low) and (not high or age <= high))


def _age_text(low: int, high: int) -> str:
    if not low and not high:
        return '나이 제한 없음'
    if not high:
        return f'{low}세 이상'
    return f'{low}~{high}세' if low else f'{high}세 이하'


def _income_text(policy: dict[str, Any]) -> str:
    if _text(policy, 'earnCndSeCd') == INCOME_ANY:
        return '소득 무관'
    return _text(policy, 'earnEtcCn') or '소득 조건 있음 (신청 페이지에서 확인)'


def _url(policy: dict[str, Any]) -> str | None:
    """신청 주소, 없으면 참고 주소. 주소가 아닌 값('-', '해당 기관 홈페이지 참조')은 건너뛴다."""
    for key in ('aplyUrlAddr', 'refUrlAddr1'):
        raw = _text(policy, key)
        if not raw or ' ' in raw or '.' not in raw:
            continue
        # 'www.dgyouth.kr'처럼 https://가 빠진 값, 'HTTP://'처럼 대문자인 값이 온다
        scheme = re.match(r'https?://', raw, re.IGNORECASE)
        return raw if scheme else f'https://{raw}'
    return None


def _shorten(text: str, limit: int = 300) -> str | None:
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + '…'


def _base_name(name: str) -> str:
    """해마다 다시 등록한 같은 사업을 알아보는 이름: 앞머리 괄호·연도·차수·띄어쓰기를 뺀다.

    '2026 청년 부동산 중개보수 지원사업' == '청년 부동산 중개보수 지원사업'
    '(국토부) 26년 청년월세 지원사업' == '[2차] 청년월세 지원사업' == '청년월세 지원사업'
    뒤쪽 괄호('임대주택 공급(전세임대)' vs '(매입임대)')와 다른 숫자('1인 가구',
    '10년 공공임대')는 남긴다 (서로 다른 사업이다).
    """
    name = re.sub(r'^(\s*[(\[][^)\]]*[)\]])+', '', name)  # 이름 맨 앞 괄호들
    # 이름 맨 앞 '26년'. 두 자리 연도는 21~29만 본다 ('10년 공공임대'·'20년 공공임대'는 기간)
    name = re.sub(r"^\s*'?2[1-9]년", '', name)
    name = re.sub(r'20\d\d년?|\(?\d+차\)?', '', name)  # 어디서든 '2026(년)', '2차'
    return re.sub(r'[^\w]', '', name)


# ── 지역 거르기 ───────────────────────────────────────────────────


@cache
def _sido_prefixes() -> tuple[str, ...]:
    """기관 이름 앞에 붙는 시도 이름 (정식 이름·줄임말), 긴 것부터 ('경기도교육청' → '경기도')."""
    names = {r.sido for r in load_regions()} | set(SIDO_ALIASES)
    return tuple(sorted(names, key=len, reverse=True))


@cache
def _sigungu_sidos() -> dict[str, frozenset[str]]:
    """시군구 이름(첫 낱말: '성남시 분당구' → '성남시') → 그 이름이 있는 시도들 ('중구'는 여럿)."""
    found: dict[str, set[str]] = {}
    for r in load_regions():
        if r.sigungu and r.sigungu != r.sido:
            found.setdefault(r.sigungu.split()[0], set()).add(r.sido)
    return {name: frozenset(sidos) for name, sidos in found.items()}


def _place(names: Iterable[str], sido: str | None = None) -> tuple[str | None, set[str]]:
    """기관 이름들에서 (시도, 시군구 이름들)을 찾는다. 못 찾으면 (None, 빈 집합).

    시군구 이름인 낱말('광주시청' → 광주시, '화순군청' → 화순군)은 시도 줄임말로 읽지 않는다.
    시도를 찾았으면(또는 sido로 받았으면) 그 시도에 실제로 있는 시군구 이름만 인정한다:
      '광주시청' + 상위 '전남광주통합특별시' → 전남광주 시도 정책 (경기 광주시가 아니다)
      '광주시청'만 있음                      → 경기도 광주시 정책
    """
    words = ' '.join(names).split()
    heads = {w.removesuffix('청') for w in words} & _sigungu_sidos().keys()
    sido = sido or next(
        (
            normalize_sido(p)
            for w in words
            if w.removesuffix('청') not in heads
            for p in _sido_prefixes()
            if w.startswith(p)
        ),
        None,
    )
    if sido is not None:
        heads = {h for h in heads if sido in _sigungu_sidos()[h]}
    return sido, heads


def _scope(policy: dict[str, Any], region: Region) -> tuple[Scope, str] | None:
    """(local·national, 정책을 낸 단계). 다른 지역 정책이거나 지역을 모르면 None.

    단계: 시군구 정책이면 그 시군구 이름, 시도·전국 정책이면 ''. 서울시와 강서구가 같은 이름으로
    낸 다른 사업을 하나로 합치지 않으려고 쓴다. (모듈 설명의 '지역 거르기')
    """
    group = _text(policy, 'pvsnInstGroupCd')
    top = _text(policy, 'rgtrHghrkInstCdNm')
    names = [top, _text(policy, 'rgtrUpInstCdNm'), _text(policy, 'sprvsnInstCdNm')]
    if group == CENTRAL_GOV:
        return 'national', ''
    sido, sigungu = _place(names)
    # 정책 이름에 적힌 시군구도 본다 ('남구 백운광장 청년축제'를 시도 이름으로만 등록한 경우).
    # 시도를 찾았으면 그 시도의 시군구만 ('광주시' 같은 이름이 다른 시도에도 있다)
    sigungu |= _place([_text(policy, 'plcyNm')], sido)[1]
    if sido is None and not sigungu:
        # 기관 이름으로 지역을 못 찾았다. 대상 지역이 좁으면 근무지를 콕 집은 정책이다
        # (API가 근무지가 대상인 정책만 준다. '주택실'처럼 부서 이름만 적힌 지역 정책도 여기)
        targets = _int(policy.get('zipCdCount'))
        if 0 < targets < NATIONWIDE_ZIPS:
            return 'local', ''
        # 전국 대상인데 중앙 기관 이름이면 전국 정책 (구분 코드가 '지자체'로 잘못 붙은
        # 문화체육관광부 사업도 있다: 2026-11-15 나주 조회). 아니면 남의 지역 정책일 수 있어 뺀다
        head = (top or _text(policy, 'sprvsnInstCdNm')).split()
        return ('national', '') if head and _CENTRAL_NAME.search(head[0]) else None
    home = region.sigungu.split()[0]
    if sido is not None and sido != region.sido:
        return None
    if sigungu:
        # 시군구 이름이 적혀 있으면 근무지 시군구여야 한다 (시도를 못 찾았으면 그 이름이 근무지
        # 시도에 있는지도 본다: '중구'는 여러 시도에 있다)
        if home not in sigungu or region.sido not in _sigungu_sidos()[home]:
            return None
        return 'local', home
    return ('local', '') if sido is not None else None


# ── 고르기 ────────────────────────────────────────────────────────


def _deadline(policy: SupportPolicy) -> date:
    """기간 글자('2026-04-01 ~ 2026-11-30')의 마감일. 상시·기간 없음은 가장 늦은 날로 본다."""
    try:
        return date.fromisoformat((policy.apply_period or '')[-10:])
    except ValueError:
        return date.max


def _fit(policy: dict[str, Any], single: bool) -> Fit:
    """나에게 맞는 정도 (모듈 설명의 '나에게 맞는 것 먼저'). 이름·키워드와 요건 코드를 본다."""
    for key, fits in _FOR_NEW_WORKER.items():
        codes = {c.strip() for c in _text(policy, key).split(',') if c.strip()}
        if codes and not codes & fits:
            return 'targeted'
    words = f'{_text(policy, "plcyNm")} {_text(policy, "plcyKywdNm")}'
    # '대학생'은 청년과 함께 적힌 것('청년·대학생')이 아닐 때만 대학생 전용으로 본다
    student_only = '대학생' in words and '청년' not in _text(policy, 'plcyNm')
    if _TARGETED_WORDS.search(words) or student_only or (single and _FAMILY_WORDS.search(words)):
        return 'targeted'
    moving = HOUSING in _categories(policy) or _MOVING_WORDS.search(words)
    return 'moving' if moving else 'general'


def is_first_job(policy: SupportPolicy) -> bool:
    """첫 취업이면 누구나 해당하는 전국 지원인지 (이름으로만 본다)."""
    return (
        policy.scope == 'national'
        and bool(_FIRST_JOB_WORDS.search(policy.name))
        and not _NOT_FIRST_JOB_WORDS.search(policy.name)
    )


def _order(policy: SupportPolicy) -> tuple[int, bool, int]:
    """보여 줄 순서: 나에게 맞는 것 → 주거 먼저 → 마감 가까운 순 (상시·기간 모름은 뒤)."""
    return _FIT_RANK[policy.fit], policy.category != HOUSING, _deadline(policy).toordinal()


def _newer(a: SupportPolicy, b: SupportPolicy) -> SupportPolicy:
    """같은 사업의 여러 해 치 가운데 남길 것: 신청 중 > 상시 > 곧 시작 > 기간 모름,
    같으면 마감이 늦은 것."""
    return max(a, b, key=lambda p: (_STATUS_RANK[p.apply_status], _deadline(p)))


def select_policies(
    raw: list[dict[str, Any]],
    region: Region,
    age: int | None,
    today: date,
    single: bool = True,
) -> list[SupportPolicy]:
    """근무지 region에서 받을 수 있는 정책을 골라 보여 줄 순서로 늘어놓는다.

    single: 혼자 사는지 (가족 수 1명). 그러면 신혼·출산·자녀 정책을 아래로 내린다.

    신청이 끝난 것은 뺀다. 같은 사업을 해마다 따로 등록한 것은 하나만 남긴다.
    """
    picked: dict[tuple[Scope, str, str], SupportPolicy] = {}
    for policy in raw:
        categories = _categories(policy)
        low, high = _age_range(policy)
        if not _text(policy, 'plcyNo') or not categories or not _age_ok(age, low, high):
            continue
        owner = _scope(policy, region)
        apply = _apply(policy, today)
        if owner is None or apply is None:
            continue
        (scope, level), (status, period) = owner, apply
        found = SupportPolicy(
            id=_text(policy, 'plcyNo'),
            name=_text(policy, 'plcyNm') or '(이름 없음)',
            category=HOUSING if HOUSING in categories else categories[0],
            support=_shorten(_text(policy, 'plcySprtCn')),
            agency=_text(policy, 'sprvsnInstCdNm') or None,
            age_range=_age_text(low, high),
            income=_income_text(policy),
            conditions=_shorten(_text(policy, 'addAplyQlfcCndCn')),
            scope=scope,
            fit=_fit(policy, single),
            apply_status=status,
            apply_period=period,
            url=_url(policy),
        )
        key = (scope, level, _base_name(found.name))
        picked[key] = _newer(picked[key], found) if key in picked else found
    return sorted(picked.values(), key=_order)


def _user(profile: dict | None) -> tuple[int | None, bool]:
    """(나이, 혼자인지). 사용자 정보가 없거나 깨졌으면 (None, 혼자) — UserProfile 기본값과 같다."""
    if not profile:
        return None, True
    try:
        user = UserProfile.model_validate(profile)
    except ValidationError:
        return None, True  # 형식이 깨진 사용자 정보는 계산 쪽(errors)에서 알린다
    return user.age, user.family == 1


def find_local_support(
    table: list[dict] | None,
    profile: dict | None,
    source: PolicySource,
    today: date | None = None,
) -> dict[str, Any]:
    """비교표의 근무지 지역마다 받을 수 있는 청년 지원을 찾는다. 그래프 노드·다시 계산이 부른다.

    지역 하나를 조회하지 못해도 나머지는 계속한다 (그 지역 error에 이유).
    돌려주는 값은 체크포인터(SQLite)에 저장되도록 평범한 dict다.
    """
    today = today or today_kst()
    age, single = _user(profile)

    postings: dict[str, list[str]] = {}  # 시군구 코드 → 그 지역에 근무지가 있는 공고 ID들
    without_region = 0
    for raw in table or []:
        try:
            row = ComparisonRow.model_validate(raw)
        except ValidationError:
            continue  # 형식이 깨진 행은 계산 쪽(errors)에서 알린다
        lawd_cd = row.location.value.lawd_cd if row.location.value else None
        if lawd_cd:
            postings.setdefault(lawd_cd, []).append(row.id)
        else:
            without_region += 1

    notes = []
    if age is None:
        notes.append('나이를 알려 주면 나이 조건에 맞는 정책만 골라 드려요.')
    if without_region:
        notes.append('근무지 시·군·구를 정하지 못한 공고는 지원 정책을 찾지 못했어요.')

    def lookup(lawd_cd: str) -> tuple[list[SupportPolicy], str | None]:
        """(이 지역에서 고른 정책, 조회하지 못한 이유)."""
        region = region_by_code(lawd_cd)
        if region is None:
            return [], '근무지 지역 코드를 알 수 없어 찾지 못했어요.'
        try:
            return select_policies(source.policies_for(lawd_cd), region, age, today, single), None
        except SupportError as e:
            return [], str(e)
        except Exception:
            # 예상 못 한 오류도 이 지역만 비운다 (그래프가 멈추면 세션이 '실행 중'에 갇힌다)
            logger.exception('청년 지원 정책을 고르지 못했습니다 (%s)', lawd_cd)
            return [], '청년 지원 정책을 불러오다 문제가 생겼어요. 잠시 후 다시 계산해 주세요.'

    with ThreadPoolExecutor(max_workers=LOOKUP_THREADS) as pool:
        looked_up = list(pool.map(lookup, postings))

    regions, national = [], {}
    for (lawd_cd, posting_ids), (found, error) in zip(postings.items(), looked_up, strict=True):
        local = [p for p in found if p.scope == 'local']
        national |= {p.id: p for p in found if p.scope == 'national'}
        region = region_by_code(lawd_cd)
        regions.append(
            RegionSupport(
                lawd_cd=lawd_cd,
                region=region.name if region else lawd_cd,
                posting_ids=posting_ids,
                policies=local[:MAX_POLICIES],
                matched=len(local),
                error=error,
            )
        )
    nationals = sorted(national.values(), key=_order)
    first_job = [p for p in nationals[MAX_POLICIES:] if is_first_job(p) and p.fit != 'targeted']
    return LocalSupport(
        regions=regions,
        national=nationals[:MAX_POLICIES] + first_job[:FIRST_JOB_MAX],
        national_matched=len(nationals),
        age=age,
        notes=notes,
        checked_on=today.isoformat(),
    ).model_dump(mode='json')
