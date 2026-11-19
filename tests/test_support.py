"""청년 지원 정책 테스트 (get_local_support, #16) — 실제 온통청년 API는 부르지 않는다.

    - 받아 오기: 쪽 넘기기, 오류(키 틀림 403·키 없음 400 HTML·연결 실패·모양 다름), 캐시, 실명 빼기
    - 고르기: 분류·지역(다른 지자체 정책 빼기)·나이·신청 기간, 해마다 다시 등록한 사업 하나로, 순서
    - 시연 지역: 실제 응답을 저장한 fixture로 서울 강서구·나주시 목록 확인
    - API: 확인 → get_local_support, 다시 계산 때 나이 반영, 키가 없으면 null

[fixture] tests/fixtures/support/{시군구 코드}.json
  2026-11-15 실제 API 응답(관련 분류만)을 youthcenter.py의 KEEP_FIELDS로 줄여 저장했다.
  담당자 실명 등 나머지 필드는 들어 있지 않다.

[실행] backend 폴더에서 uv run pytest tests/test_support.py
"""

import json
import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest
from conftest import FakeExtractor, scenario_llm, scenario_text
from fastapi.testclient import TestClient
from test_demo_scenarios import profile_a

from backend.calc.remaining import UserProfile
from backend.main import create_app
from backend.normalize import build_rows
from backend.normalize.checks import region_by_code
from backend.support import SupportError, YouthCenterClient, find_local_support
from backend.support.policies import FIRST_JOB_MAX, MAX_POLICIES, select_policies
from backend.support.youthcenter import URL

FIXTURES = Path(__file__).parent / 'fixtures' / 'support'
TODAY = date(2026, 11, 15)
NAJU, GANGSEO = '12170', '11500'


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    """API를 거치는 테스트도 '오늘'을 TODAY로 (해가 바뀌어도 정책이 끝남으로 바뀌지 않게)."""
    monkeypatch.setattr('backend.support.policies.today_kst', lambda: TODAY)


def pol(**kw) -> dict:
    """정책 한 건 (기본: 나주시가 등록한, 신청 기간 중인 주거 정책, 19~34세, 소득 무관)."""
    return {
        'plcyNo': 'P1',
        'plcyNm': '나주 청년 월세 지원',
        'plcySprtCn': '월 20만원',
        'lclsfNm': '주거',
        'sprtTrgtMinAge': '19',
        'sprtTrgtMaxAge': '34',
        'earnCndSeCd': '0043001',
        'earnEtcCn': '',
        'addAplyQlfcCndCn': '',
        'aplyPrdSeCd': '0057001',
        'aplyYmd': '20260101 ~ 20261231',
        'aplyUrlAddr': 'https://www.naju.go.kr',
        'refUrlAddr1': '',
        'sprvsnInstCdNm': '전남광주통합특별시 나주시 청년정책과',
        'rgtrUpInstCdNm': '전남광주통합특별시',
        'rgtrHghrkInstCdNm': '전남광주통합특별시',
        'pvsnInstGroupCd': '0054002',  # 지자체
        'zipCdCount': 1,  # 대상 지역 수 (나주만)
        **kw,
    }


NATIONAL = {
    'pvsnInstGroupCd': '0054001',  # 중앙부처
    'rgtrHghrkInstCdNm': '국토교통부',
    'rgtrUpInstCdNm': '국토교통부',
    'sprvsnInstCdNm': '국토교통부 주택기금과',
    'zipCdCount': 256,
}


def agency(name: str, **kw) -> dict:
    """등록·주관 기관 이름을 모두 name으로 (기본값의 '나주시'가 섞이지 않게)."""
    return {'rgtrHghrkInstCdNm': name, 'rgtrUpInstCdNm': name, 'sprvsnInstCdNm': name, **kw}


def pick(*policies: dict, age: int | None = 27, lawd_cd: str = NAJU):
    return select_policies(list(policies), region_by_code(lawd_cd), age, TODAY)


def names(found) -> list[str]:
    return [p.name for p in found]


# ── 받아 오기 (youthcenter.py) ─────────────────────────────────────


def page_body(items: list[dict], total: int) -> dict:
    return {'resultCode': 200, 'result': {'pagging': {'totCount': total}, 'youthPolicyList': items}}


def client_with(handler, tmp_path, **kw) -> YouthCenterClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return YouthCenterClient('test-key', tmp_path / 'cache.sqlite', http=http, **kw)


def test_fetch_reads_every_page_and_drops_officials_names(tmp_path):
    pages = {
        '1': [pol(plcyNo='A', sprvsnInstPicNm='홍길동'), pol(plcyNo='B')],
        '2': [pol(plcyNo='C', operInstPicNm='김철수')],
    }
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(URL)
        assert request.url.params['zipCd'] == NAJU
        asked.append(request.url.params['pageNum'])
        return httpx.Response(200, json=page_body(pages[request.url.params['pageNum']], 3))

    got = client_with(handler, tmp_path).policies_for(NAJU)
    assert [p['plcyNo'] for p in got] == ['A', 'B', 'C']
    assert asked == ['1', '2']
    # 담당 공무원 실명은 받자마자 버린다 (저장·화면 모두)
    assert all('PicNm' not in key for p in got for key in p)
    with sqlite3.connect(tmp_path / 'cache.sqlite') as db:
        [(body,)] = db.execute('SELECT body FROM youth_policy_cache').fetchall()
    assert '홍길동' not in body and '김철수' not in body


def test_cache_is_reused_for_a_day_then_refreshed(tmp_path):
    now = [1_000_000.0]
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=page_body([pol()], 1))

    client = client_with(handler, tmp_path, clock=lambda: now[0])
    client.policies_for(NAJU)
    client.policies_for(NAJU)
    assert len(calls) == 1
    now[0] += 24 * 60 * 60 + 1
    client.policies_for(NAJU)
    assert len(calls) == 2


def test_broken_cache_does_not_stop_the_lookup(tmp_path, monkeypatch):
    """캐시는 덤이다: SQLite가 잠기거나 고장 나도 API로 받아 온다 (#41의 database is locked)."""
    client = client_with(lambda r: httpx.Response(200, json=page_body([pol()], 1)), tmp_path)

    def locked():
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(client, '_connect', locked)
    assert [p['plcyNo'] for p in client.policies_for(NAJU)] == ['P1']


def test_api_failure_falls_back_to_yesterdays_list(tmp_path):
    """API가 실패하면 하루 지난 캐시라도 쓴다 (다시 계산 때 멀쩡한 목록이 오류로 덮이지 않게)."""
    now = [1_000_000.0]
    up = [True]

    def handler(request):
        if up[0]:
            return httpx.Response(200, json=page_body([pol()], 1))
        raise httpx.ConnectTimeout('down', request=request)

    client = client_with(handler, tmp_path, clock=lambda: now[0])
    client.policies_for(NAJU)
    now[0] += 2 * 24 * 60 * 60
    up[0] = False
    assert [p['plcyNo'] for p in client.policies_for(NAJU)] == ['P1']
    # 받아 둔 목록이 없으면 그대로 실패를 알린다
    with pytest.raises(SupportError):
        client.policies_for(GANGSEO)


def test_failed_region_is_not_retried_for_a_while(tmp_path):
    """방금 실패한 지역은 잠시 다시 묻지 않는다 (생활비만 고쳐 다시 계산할 때마다 기다리지 않게)."""
    now = [1_000_000.0]
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectTimeout('down', request=request)

    client = client_with(handler, tmp_path, clock=lambda: now[0])
    with pytest.raises(SupportError, match='연결하지 못했어요'):
        client.policies_for(NAJU)
    with pytest.raises(SupportError, match='연결하지 못했어요'):  # 같은 이유로 바로 실패
        client.policies_for(NAJU)
    assert len(calls) == 1
    now[0] += 121
    with pytest.raises(SupportError):
        client.policies_for(NAJU)
    assert len(calls) == 2


def test_cache_rows_from_an_older_shape_are_not_used(tmp_path):
    """캐시 모양이 바뀌면(CACHE_VERSION) 예전 줄은 쓰지 않고 새로 받는다."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=page_body([pol()], 1))

    client = client_with(handler, tmp_path)
    with sqlite3.connect(tmp_path / 'cache.sqlite') as db:
        db.execute(
            'INSERT INTO youth_policy_cache VALUES (?, ?, ?)', (NAJU, 9e12, '[{"plcyNo": "OLD"}]')
        )
    assert [p['plcyNo'] for p in client.policies_for(NAJU)] == ['P1']
    assert len(calls) == 1


def test_empty_list_is_not_cached(tmp_path):
    """빈 목록은 잠깐의 오류일 수 있어 하루 동안 붙잡아 두지 않는다."""
    lists = [[], [pol()]]
    client = client_with(lambda r: httpx.Response(200, json=page_body(lists.pop(0), 1)), tmp_path)
    assert client.policies_for(NAJU) == []
    assert [p['plcyNo'] for p in client.policies_for(NAJU)] == ['P1']


def test_items_that_are_not_policies_are_skipped(tmp_path):
    body = page_body([pol(), 'not a policy', None], 3)
    got = client_with(lambda r: httpx.Response(200, json=body), tmp_path).policies_for(NAJU)
    assert [p['plcyNo'] for p in got] == ['P1']


@pytest.mark.parametrize(
    ('response', 'message'),
    [
        (httpx.Response(403, json={'resultCode': 403, 'resultMessage': '인증키 오류'}), '인증키'),
        (httpx.Response(400, text='<html>Bad Request</html>'), 'HTTP 400'),
        (httpx.Response(200, text='<html>점검 중</html>'), '모양'),
        (httpx.Response(200, json={'resultCode': 200, 'result': None}), '모양'),
    ],
)
def test_api_errors_become_support_error(tmp_path, response, message):
    client = client_with(lambda r: response, tmp_path)
    with pytest.raises(SupportError, match=message):
        client.policies_for(NAJU)


def test_connection_error_does_not_leak_the_key(tmp_path):
    def handler(request):
        raise httpx.ConnectTimeout(f'timed out: {request.url}', request=request)

    with pytest.raises(SupportError, match='연결하지 못했어요') as e:
        client_with(handler, tmp_path).policies_for(NAJU)
    # 원인 예외(요청 주소에 키가 들어 있음)를 달고 다니지 않는다
    assert e.value.__cause__ is None
    assert 'test-key' not in str(e.value)


# ── 고르기 (policies.py) ──────────────────────────────────────────


def test_only_categories_that_affect_monthly_money():
    found = pick(
        pol(plcyNo='1', plcyNm='주거', lclsfNm='주거,주거'),
        pol(plcyNo='2', plcyNm='복지 응답이름', lclsfNm='금융･복지･문화'),
        pol(plcyNo='3', plcyNm='복지 코드이름', lclsfNm='복지문화'),
        pol(plcyNo='4', plcyNm='일자리', lclsfNm='일자리,일자리'),
        pol(plcyNo='5', plcyNm='교육', lclsfNm='교육･직업훈련'),
    )
    assert sorted(names(found)) == ['복지 응답이름', '복지 코드이름', '주거']


@pytest.mark.parametrize(
    ('owner', 'expected'),
    [
        # 기관 구분 코드가 중앙부처 → 전국 정책
        (NATIONAL, 'national'),
        # 코드는 '지자체'인데 기관이 중앙 기관 (문화체육관광부 사업에 실제로 있다) → 전국 정책
        (agency('정부산하기관및위원회', zipCdCount=256), 'national'),
        # 근무지 시도·시군구의 정책
        ({}, 'local'),
        ({'rgtrHghrkInstCdNm': '전라남도', 'rgtrUpInstCdNm': '전라남도'}, 'local'),  # 옛 이름
        (agency('', sprvsnInstCdNm='전남광주통합특별시 나주시'), 'local'),  # 등록 기관이 빔
        # 어느 지자체인지 모르지만 대상 지역이 좁다 (API가 근무지가 대상인 것만 준다) → 남긴다
        (agency('', sprvsnInstCdNm='교통정책과', zipCdCount=3), 'local'),
        # 다른 지역 지자체 정책: 대상 지역을 전국으로 적어 둬도 뺀다
        ({'rgtrHghrkInstCdNm': '경상북도', 'rgtrUpInstCdNm': '경상북도 의성군'}, None),
        (agency('전남광주통합특별시', rgtrUpInstCdNm='전남광주통합특별시 화순군'), None),
        (agency('화순군청'), None),  # '화순군청' → 화순군
        (agency('경기도교육청'), None),  # '경기도교육청' → 경기도
        (agency('', sprvsnInstCdNm='서울특별시 주택실'), None),
        # 기관은 시도만 적었지만 정책 이름에 다른 시군구가 있다 ('남구 백운광장 청년축제')
        ({'plcyNm': '남구 청년축제', 'sprvsnInstCdNm': '전남광주통합특별시'}, None),
        ({'plcyNm': '나주시 청년의 날', 'sprvsnInstCdNm': '전남광주통합특별시'}, 'local'),
        # 실제 응답: 등록 기관은 '광주시청'인데 상위 기관이 전남광주 → 전남광주 시도 정책
        (
            {
                'rgtrHghrkInstCdNm': '광주시청',
                'rgtrUpInstCdNm': '전남광주통합특별시',
                'sprvsnInstCdNm': '광주시청',
                'zipCdCount': 27,
            },
            'local',
        ),
        # 부서 이름만 적혔어도 대상 지역이 좁으면 지역 정책 ('…실'이라고 전국으로 보지 않는다)
        (agency('', sprvsnInstCdNm='주택실', zipCdCount=3), 'local'),
        # 어느 지자체인지 모르고 대상이 전국이면 '우리 지역으로 오세요' 정책일 수 있어 뺀다
        (agency('', sprvsnInstCdNm='교통정책과', zipCdCount=256), None),
    ],
)
def test_region_scope(owner, expected):
    found = pick(pol(**owner))
    assert [p.scope for p in found] == ([expected] if expected else [])


def test_research_contracts_are_not_support():
    """온통청년에 올라온 연구용역은 사람이 신청하는 지원이 아니라 뺀다."""
    found = pick(
        pol(plcyNo='1', plcyNm='청년 월세 지원'),
        pol(
            plcyNo='2',
            plcyNm='청년자산형성 연계 강화',
            plcySprtCn='5종 지원사업 간 연계 강화 연구용역',
        ),
    )
    assert names(found) == ['청년 월세 지원']


def test_age_filter():
    policies = [
        pol(plcyNo='1', plcyNm='19~34', sprtTrgtMinAge='19', sprtTrgtMaxAge='34'),
        pol(plcyNo='2', plcyNm='제한 없음', sprtTrgtMinAge='0', sprtTrgtMaxAge='0'),
        pol(plcyNo='3', plcyNm='사실상 없음', sprtTrgtMinAge='1', sprtTrgtMaxAge='99'),
        pol(plcyNo='4', plcyNm='39세 이하', sprtTrgtMinAge='0', sprtTrgtMaxAge='39'),
    ]
    assert sorted(names(pick(*policies, age=40))) == ['사실상 없음', '제한 없음']
    assert len(pick(*policies, age=27)) == 4
    # 나이를 모르면 거르지 않는다
    assert len(pick(*policies, age=None)) == 4
    texts = {p.name: p.age_range for p in pick(*policies)}
    assert texts == {
        '19~34': '19~34세',
        '제한 없음': '나이 제한 없음',
        '사실상 없음': '나이 제한 없음',
        '39세 이하': '39세 이하',
    }


def test_apply_period():
    national = NATIONAL
    found = pick(
        pol(plcyNo='1', plcyNm='기간 중', aplyYmd='20261101 ~ 20261130'),
        pol(plcyNo='2', plcyNm='상시', aplyPrdSeCd='0057002', aplyYmd=''),
        pol(plcyNo='3', plcyNm='곧 시작', aplyYmd='20270101 ~ 20270131'),
        pol(plcyNo='4', plcyNm='기간 모름', aplyYmd=''),
        # 신청이 끝난 것은 뺀다 (다시 열릴지, 조건이 같을지 장담할 수 없다)
        pol(plcyNo='5', plcyNm='올해 끝남', aplyYmd='20261019 ~ 20261030'),
        pol(plcyNo='6', plcyNm='마감 코드', aplyPrdSeCd='0057003', aplyYmd='20270101 ~ 20271231'),
        pol(plcyNo='7', plcyNm='작년 초 끝남', aplyYmd='20250101 ~ 20250131'),
        pol(plcyNo='8', plcyNm='언제 끝났는지 모름', aplyPrdSeCd='0057003', aplyYmd=''),
        pol(plcyNo='9', plcyNm='끝난 전국 정책', aplyYmd='20261001 ~ 20261031', **national),
    )
    status = {p.name: (p.apply_status, p.apply_period) for p in found}
    assert status == {
        '기간 중': ('open', '2026-11-01 ~ 2026-11-30'),
        '상시': ('always', '상시'),
        '곧 시작': ('upcoming', '2027-01-01 ~ 2027-01-31'),
        '기간 모름': ('unknown', None),
    }


@pytest.mark.parametrize(
    ('today', 'expected'),
    [
        # 신청 기간이 1월·7월 두 번: 사이에는 '곧 시작'(다음 기간), 안에서는 '신청 중'
        (date(2026, 3, 15), ('upcoming', '2026-07-01 ~ 2026-07-31')),
        (date(2026, 7, 10), ('open', '2026-07-01 ~ 2026-07-31')),
        (date(2026, 1, 10), ('open', '2026-01-01 ~ 2026-01-31')),
        (date(2026, 11, 15), None),  # 두 기간 모두 끝남 → 뺀다
    ],
)
def test_several_application_windows(today, expected):
    policy = pol(aplyYmd='20260101 ~ 20260131, 20260701 ~ 20260731')
    found = select_policies([policy], region_by_code(NAJU), 27, today)
    assert [(p.apply_status, p.apply_period) for p in found] == ([expected] if expected else [])


def test_same_program_registered_every_year_is_shown_once():
    found = pick(
        pol(plcyNo='1', plcyNm='청년 이사비 지원사업', aplyYmd='20260401 ~ 20260414'),
        pol(plcyNo='2', plcyNm='2026 청년 이사비 지원사업', aplyYmd='20260818 ~ 20260831'),
        pol(plcyNo='3', plcyNm='(2차) 청년 이사비 지원사업', aplyYmd='20261101 ~ 20261130'),
        pol(plcyNo='4', plcyNm='청년 이사비 지원 상담', aplyYmd='20261101 ~ 20261130'),
        pol(plcyNo='5', plcyNm='(국토부) 26년 청년 월세', aplyYmd='20260330 ~ 20260529'),
        pol(plcyNo='6', plcyNm='청년 월세', aplyYmd='20261101 ~ 20261130'),
        pol(plcyNo='7', plcyNm='1인 가구 월세', aplyYmd='20261101 ~ 20261130'),
        pol(plcyNo='8', plcyNm='2인 가구 월세', aplyYmd='20261101 ~ 20261130'),
    )
    # 신청할 수 있는 것을 남긴다. 연도·차수 말고 다른 숫자가 다르면 다른 사업
    assert sorted(p.id for p in found) == ['3', '4', '6', '7', '8']


def test_programs_that_differ_only_in_trailing_brackets_or_numbers_are_kept():
    found = pick(
        pol(plcyNo='1', plcyNm='공적 임대주택 공급(전세임대)'),
        pol(plcyNo='2', plcyNm='공적 임대주택 공급(매입임대)'),
        pol(plcyNo='3', plcyNm='10년 공공임대'),
        pol(plcyNo='4', plcyNm='20년 공공임대'),
    )
    assert sorted(p.id for p in found) == ['1', '2', '3', '4']


def test_same_name_from_city_and_district_are_different_programs():
    """서울시와 강서구가 같은 이름으로 낸 사업은 서로 다른 사업이다 (합치지 않는다)."""
    city = {'rgtrHghrkInstCdNm': '서울특별시', 'sprvsnInstCdNm': '서울특별시 주택정책과'}
    district = {'rgtrHghrkInstCdNm': '서울특별시', 'rgtrUpInstCdNm': '서울특별시 강서구'}
    found = pick(
        pol(plcyNo='1', plcyNm='청년 월세 지원', **city),
        pol(plcyNo='2', plcyNm='청년 월세 지원', **district),
        lawd_cd=GANGSEO,
    )
    assert sorted(p.id for p in found) == ['1', '2']


@pytest.mark.parametrize(
    ('older', 'newer'),
    [
        # 신청 중 > 상시 > 곧 시작 > 기간 모름
        ({'aplyYmd': ''}, {'aplyYmd': '20260101 ~ 20261231'}),  # 기간 모름 < 신청 중
        ({'aplyYmd': '20270101 ~ 20270131'}, {'aplyYmd': '20261101 ~ 20261130'}),  # 곧 < 신청 중
        ({'aplyPrdSeCd': '0057002', 'aplyYmd': ''}, {'aplyYmd': '20261101 ~ 20261130'}),
        ({'aplyYmd': '20261001 ~ 20261031'}, {'aplyYmd': '20270101 ~ 20270131'}),  # 끝난 것은 빠짐
    ],
)
def test_the_edition_you_can_apply_to_now_is_kept(older, newer):
    found = pick(
        pol(plcyNo='old', plcyNm='청년 월세 지원', **older),
        pol(plcyNo='new', plcyNm='2026 청년 월세 지원', **newer),
    )
    assert [p.id for p in found] == ['new']


@pytest.mark.parametrize(
    ('name', 'category', 'family', 'expected'),
    [
        ('청년 월세 지원', '금융･복지･문화', 1, 'moving'),  # 월세
        ('청년 전세보증금 이자 지원', '금융･복지･문화', 1, 'moving'),
        ('청년안심주택 공급', '주거', 1, 'moving'),  # 주거 분류
        ('학자금대출 신용유의자 신용회복', '금융･복지･문화', 1, 'general'),  # '대출'만으로는 아님
        ('청년 마음건강센터', '금융･복지･문화', 1, 'general'),
        ('자립준비청년 자립 수당', '금융･복지･문화', 1, 'targeted'),
        ('대학생 연합생활관', '주거', 1, 'targeted'),  # 주거여도 대상이 정해져 있으면
        ('청년 창업 공간 지원', '금융･복지･문화', 1, 'targeted'),
        ('청년부부 결혼축하금', '금융･복지･문화', 1, 'targeted'),  # 혼자면 신혼·결혼은 아래로
        ('청년부부 결혼축하금', '금융･복지･문화', 2, 'general'),  # 가족이 있으면 그대로
        ('신혼부부 전세자금', '주거', 2, 'moving'),
        # 넓은 낱말에 걸리지 않는다
        ('서울 전역 청년 월세 지원', '금융･복지･문화', 1, 'moving'),
        ('청년·대학생 햇살론', '금융･복지･문화', 1, 'general'),
        ('중소기업 청년 기숙사 임차비', '금융･복지･문화', 1, 'moving'),
        ('전역예정 장병 지원', '금융･복지･문화', 1, 'targeted'),
        ('군복무 청년 상해보험 지원', '금융･복지･문화', 1, 'targeted'),
        ('근로능력 있는 수급자의 탈수급 지원', '금융･복지･문화', 1, 'targeted'),
    ],
)
def test_fit_puts_what_a_new_mover_can_use_first(name, category, family, expected):
    [p] = select_policies(
        [pol(plcyNm=name, lclsfNm=category)], region_by_code(NAJU), 27, TODAY, single=family == 1
    )
    assert p.fit == expected


def test_targeted_policies_go_below_general_ones_but_stay():
    found = pick(
        pol(plcyNo='1', plcyNm='자립준비청년 수당', lclsfNm='복지문화'),
        pol(plcyNo='2', plcyNm='청년 마음건강', lclsfNm='복지문화'),
        pol(plcyNo='3', plcyNm='청년 월세', lclsfNm='복지문화'),
    )
    assert names(found) == ['청년 월세', '청년 마음건강', '자립준비청년 수당']


def test_family_size_comes_from_the_profile():
    source = FakeSource({NAJU: [pol(plcyNm='청년부부 결혼축하금', lclsfNm='복지문화')]})
    table = scenario_table('down_naju')
    alone = find_local_support(table, {'age': 27}, source, TODAY)
    married = find_local_support(table, {'age': 27, 'family': 2}, source, TODAY)
    assert alone['regions'][0]['policies'][0]['fit'] == 'targeted'
    assert married['regions'][0]['policies'][0]['fit'] == 'general'


def test_order_housing_first_then_deadline():
    found = pick(
        pol(plcyNo='1', plcyNm='복지 상시', lclsfNm='복지문화', aplyPrdSeCd='0057002'),
        pol(plcyNo='2', plcyNm='주거 내년 시작', aplyYmd='20270101 ~ 20270131'),
        pol(plcyNo='4', plcyNm='주거 상시', aplyPrdSeCd='0057002'),
        pol(plcyNo='5', plcyNm='주거 이번 달 마감', aplyYmd='20261101 ~ 20261130'),
    )
    # 주거(이사하는 사람에게 바로 쓰임) 먼저, 그 안에서 마감 가까운 순, 상시는 뒤
    assert names(found) == ['주거 이번 달 마감', '주거 내년 시작', '주거 상시', '복지 상시']


def test_display_fields():
    [p] = pick(
        pol(
            aplyUrlAddr='해당 기관 홈페이지 참조',
            refUrlAddr1='www.naju.go.kr/youth',
            earnCndSeCd='0043003',
            earnEtcCn='중위소득 150% 이하',
            addAplyQlfcCndCn='가' * 400,
        )
    )
    # 주소가 아닌 글은 건너뛰고, https://가 빠진 주소에는 붙인다
    assert p.url == 'https://www.naju.go.kr/youth'
    assert p.income == '중위소득 150% 이하'
    assert len(p.conditions) == 300 and p.conditions.endswith('…')
    assert p.agency == '전남광주통합특별시 나주시 청년정책과'


@pytest.mark.parametrize(
    ('apply_url', 'ref_url', 'expected'),
    [
        ('https://a.kr/view?x=1&amp;y=2', '', 'https://a.kr/view?x=1&y=2'),  # HTML로 바꾼 글자
        ('HTTP://A.KR', '', 'HTTP://A.KR'),  # 대문자
        ('-', 'www.naju.go.kr', 'https://www.naju.go.kr'),  # 주소가 아닌 값은 건너뜀
        ('-', '', None),
    ],
)
def test_url(apply_url, ref_url, expected):
    [p] = pick(pol(aplyUrlAddr=apply_url, refUrlAddr1=ref_url))
    assert p.url == expected


def test_names_with_html_entities_are_readable():
    [p] = pick(pol(plcyNm='&quot;노벨 문학도시&quot; 청년 월세'))
    assert p.name == '"노벨 문학도시" 청년 월세'


# ── 지역마다 찾기 (find_local_support) ────────────────────────────


def scenario_table(*pids: str) -> list[dict]:
    """시연 공고의 비교표 (공고 ID를 posting-1, posting-2 …로 다시 붙인다)."""
    rows = []
    for pid in pids:
        rows += build_rows(scenario_llm(pid), scenario_text(pid), None)
    return [{**r.model_dump(mode='json'), 'id': f'posting-{i}'} for i, r in enumerate(rows, 1)]


class FakeSource:
    """시군구 코드마다 정해 둔 정책 목록. failing 코드는 SupportError."""

    def __init__(self, by_code: dict[str, list[dict]], failing: set[str] = frozenset()) -> None:
        self.by_code, self.failing, self.calls = by_code, failing, []

    def policies_for(self, zip_cd: str) -> list[dict]:
        self.calls.append(zip_cd)
        if zip_cd in self.failing:
            raise SupportError('온통청년에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.')
        return self.by_code.get(zip_cd, [])


def fixture_source() -> FakeSource:
    return FakeSource(
        {c: json.loads((FIXTURES / f'{c}.json').read_text('utf-8')) for c in (NAJU, GANGSEO)}
    )


def test_demo_regions_list_local_support_with_sources():
    """이슈 #16 완료 조건: 시연 두 지역(서울 강서구·전남 나주)의 지원 목록이 출처와 함께 나온다."""
    out = find_local_support(
        scenario_table('up_seoul', 'down_naju'), {'age': 27}, fixture_source(), TODAY
    )
    by_region = {r['region']: r for r in out['regions']}
    assert set(by_region) == {'서울특별시 강서구', '전남광주통합특별시 나주시'}
    for r in by_region.values():
        assert r['error'] is None
        assert 0 < len(r['policies']) <= MAX_POLICIES <= r['matched']
        assert all(p['scope'] == 'local' and p['url'] for p in r['policies'])
        # 신청할 수 있거나 곧 열리는 것만 (끝난 정책은 보여 주지 않는다)
        assert {p['apply_status'] for p in r['policies']} <= {
            'open',
            'always',
            'upcoming',
            'unknown',
        }
        # 다른 지역 지자체 정책(화순군·의성군 등)은 섞이지 않는다
        agencies = ' '.join(p['agency'] or '' for p in r['policies'])
        assert not any(w in agencies for w in ('화순', '의성', '장흥'))
    # 나주: 올해 신청이 끝난 '나주형 청년 취업자 주거비'(10/30 마감)는 보여 주지 않는다
    assert '2026년 나주형 청년 취업자 주거비 지원사업' not in names_in(
        by_region['전남광주통합특별시 나주시']
    )
    assert '2026 청년 월세 지원' in names_in(by_region['서울특별시 강서구'])
    # 전국 정책은 지역마다 되풀이하지 않고 한 번만, 신청할 수 있는 것만
    assert out['national'] and out['national_matched'] >= len(out['national'])
    assert all(p['scope'] == 'national' for p in out['national'])
    assert len({p['id'] for p in out['national']}) == len(out['national'])
    # 보여 줄 10개에 못 든 '입사하면 챙겨 볼 것'(첫 취업 누구나)도 정착 플랜용으로 함께 온다
    extra = [p['name'] for p in out['national'][MAX_POLICIES:]]
    assert '직장인 든든한 한끼' in extra and len(extra) <= FIRST_JOB_MAX
    assert out['source_url'] == 'https://www.youthcenter.go.kr'
    assert out['checked_on'] == '2026-11-15'
    assert out['age'] == 27 and out['notes'] == []


def names_in(region_support: dict) -> list[str]:
    return [p['name'] for p in region_support['policies']]


def test_one_region_failing_does_not_stop_the_others():
    source = fixture_source()
    source.failing = {GANGSEO}
    out = find_local_support(scenario_table('up_seoul', 'down_naju'), None, source, TODAY)
    by_code = {r['lawd_cd']: r for r in out['regions']}
    assert by_code[GANGSEO]['policies'] == [] and '연결하지 못했어요' in by_code[GANGSEO]['error']
    assert by_code[NAJU]['policies'] and by_code[NAJU]['error'] is None
    assert out['age'] is None
    assert out['notes'] == ['나이를 알려 주면 나이 조건에 맞는 정책만 골라 드려요.']


def test_unexpected_error_in_one_region_does_not_stop_the_graph():
    """예상 못 한 오류도 그 지역만 비운다 (노드가 죽으면 세션이 '실행 중'에 갇힌다)."""

    class Broken(FakeSource):
        def policies_for(self, zip_cd):
            if zip_cd == GANGSEO:
                raise RuntimeError('boom')
            return super().policies_for(zip_cd)

    source = Broken(fixture_source().by_code)
    out = find_local_support(scenario_table('up_seoul', 'down_naju'), None, source, TODAY)
    by_code = {r['lawd_cd']: r for r in out['regions']}
    assert '문제가 생겼어요' in by_code[GANGSEO]['error']
    assert 'boom' not in by_code[GANGSEO]['error']  # 내부 오류 글은 화면에 보내지 않는다
    assert by_code[NAJU]['policies']


def test_region_name():
    assert region_by_code(NAJU).name == '전남광주통합특별시 나주시'
    assert region_by_code('36110').name == '세종특별자치시'  # 시군구가 따로 없는 시도
    assert region_by_code('99999') is None


def test_postings_in_the_same_region_share_one_lookup_and_unknown_location_is_noted():
    table = scenario_table('down_naju', 'down_naju', 'up_seoul')
    table[2]['location']['value'] = None  # 근무지를 정하지 못한 공고
    source = fixture_source()
    out = find_local_support(table, {'age': 27}, source, TODAY)
    assert source.calls == [NAJU]
    [naju] = out['regions']
    assert naju['posting_ids'] == ['posting-1', 'posting-2']
    assert out['notes'] == ['근무지 시·군·구를 정하지 못한 공고는 지원 정책을 찾지 못했어요.']


def test_age_must_be_reasonable():
    assert UserProfile.model_validate({'age': 27}).age == 27
    for bad in (14, 100):
        with pytest.raises(ValueError):
            UserProfile.model_validate({'age': bad})


# ── API (확인 → get_local_support, 다시 계산) ──────────────────────


def api(tmp_path, source=None) -> TestClient:
    app = create_app(str(tmp_path / 'cp.sqlite'), extractor=FakeExtractor(), support_source=source)
    return TestClient(app)


def naju_profile(age: int | None) -> dict:
    p = profile_a()
    p['postings'] = {'posting-1': p['postings']['down_naju']}
    return {**p, 'age': age}


def test_confirm_finds_support_and_recalculate_applies_age(tmp_path):
    youth_only = pol(plcyNo='Y', plcyNm='34세까지', sprtTrgtMaxAge='34')
    source = FakeSource(
        {
            NAJU: [
                pol(plcyNo='A', plcyNm='누구나', sprtTrgtMinAge='0', sprtTrgtMaxAge='0'),
                youth_only,
            ]
        }
    )
    with api(tmp_path, source) as c:
        sid = c.post('/sessions', json={'message': scenario_text('down_naju')}).json()['session_id']
        assert c.get(f'/sessions/{sid}').json()['local_support'] is None  # 확인 전
        done = c.post(
            f'/sessions/{sid}/confirm', json={'approved': True, 'profile': naju_profile(27)}
        ).json()
        assert done['status'] == 'completed'
        [naju] = done['local_support']['regions']
        assert naju['posting_ids'] == ['posting-1']
        assert {p['name'] for p in naju['policies']} == {'누구나', '34세까지'}
        assert done['result'] is not None  # 계산과 함께 끝난다 (두 노드가 동시에 돈다)

        older = c.post(f'/sessions/{sid}/recalculate', json={'profile': naju_profile(40)}).json()
        [naju] = older['local_support']['regions']
        assert [p['name'] for p in naju['policies']] == ['누구나']
        assert older['local_support']['age'] == 40
        assert c.get(f'/sessions/{sid}').json()['local_support'] == older['local_support']

        # 공고를 새로 넣으면 이전 표 기준의 지원 목록은 지운다
        again = c.post(f'/sessions/{sid}/messages', json={'message': scenario_text('up_seoul')})
        assert again.json()['local_support'] is None


def test_real_client_shares_the_checkpoint_file_without_locking(tmp_path):
    """실제 YouthCenterClient가 체크포인트와 같은 SQLite 파일에 캐시를 써도 잠기지 않는다.

    #41(월세)에서 이벤트 루프 위의 동기 sqlite가 체크포인터와 서로 기다려 'database is locked'로
    500이 났다. 여기서는 조회가 스레드에서 돌아 그러지 않는다는 것을 실제 흐름으로 확인한다.
    """
    db = tmp_path / 'cp.sqlite'
    body = page_body([pol()], 1)
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    client = YouthCenterClient('test-key', db, http=http)
    app = create_app(str(db), extractor=FakeExtractor(), support_source=client)
    with TestClient(app) as c:
        for _ in range(3):
            message = {'message': scenario_text('down_naju')}
            sid = c.post('/sessions', json=message).json()['session_id']
            done = c.post(
                f'/sessions/{sid}/confirm', json={'approved': True, 'profile': naju_profile(27)}
            )
            assert done.status_code == 200
            assert done.json()['local_support']['regions'][0]['policies']
            again = c.post(f'/sessions/{sid}/recalculate', json={'profile': naju_profile(27)})
            assert again.status_code == 200


def test_without_key_local_support_is_null(tmp_path):
    with api(tmp_path) as c:
        sid = c.post('/sessions', json={'message': scenario_text('down_naju')}).json()['session_id']
        done = c.post(
            f'/sessions/{sid}/confirm', json={'approved': True, 'profile': naju_profile(27)}
        ).json()
        assert done['status'] == 'completed'
        assert done['local_support'] is None


def test_city_hall_name_is_read_as_its_city_not_a_province_alias():
    """'광주시청'은 경기도 광주시다 ('광주' 줄임말로 읽으면 전남광주가 된다)."""
    policy = pol(**agency('광주시청'))
    [p] = select_policies([policy], region_by_code('41610'), 27, TODAY)  # 경기도 광주시
    assert p.scope == 'local'
    assert select_policies([policy], region_by_code(NAJU), 27, TODAY) == []


def test_health_says_whether_local_support_is_available(tmp_path):
    """화면은 이 값으로 나이 칸을 보일지 정한다 (나이는 청년 지원을 고르는 데만 쓴다)."""
    with api(tmp_path, FakeSource({})) as c:
        assert c.get('/health').json()['local_support'] is True
    with api(tmp_path / 'nokey') as c:
        assert c.get('/health').json()['local_support'] is False


def test_local_support_endpoint_picks_again_for_today(tmp_path, monkeypatch):
    """결과 화면을 다시 열면 오늘 날짜로 다시 고른다 (저장된 '신청 중'이 며칠 뒤 낡지 않게)."""
    ends_soon = pol(plcyNo='S', plcyNm='이번 달 마감', aplyYmd='20261101 ~ 20261130')
    source = FakeSource({NAJU: [ends_soon]})
    with api(tmp_path, source) as c:
        sid = c.post('/sessions', json={'message': scenario_text('down_naju')}).json()['session_id']
        assert c.get(f'/sessions/{sid}/local-support').json() is None  # 계산 전
        c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': naju_profile(27)})
        [now] = c.get(f'/sessions/{sid}/local-support').json()['regions'][0]['policies']
        assert now['apply_status'] == 'open'

        monkeypatch.setattr('backend.support.policies.today_kst', lambda: date(2026, 12, 5))
        region = c.get(f'/sessions/{sid}/local-support').json()['regions'][0]
        assert region['policies'] == []  # 그사이 신청이 끝났다
        # 저장된 목록(계산할 때 기준)은 그대로다 — 이 주소는 보기만 한다
        stored = c.get(f'/sessions/{sid}').json()['local_support']['regions'][0]
        assert stored['policies'][0]['apply_status'] == 'open'
        assert c.get('/sessions/nope/local-support').status_code == 404


def test_client_keeps_only_what_it_is_told_to(tmp_path):
    """캐시에는 고를 대상(주거·금융복지문화)만 저장한다."""
    from backend.support import is_relevant

    body = page_body([pol(plcyNo='H'), pol(plcyNo='J', lclsfNm='일자리')], 2)
    client = client_with(lambda r: httpx.Response(200, json=body), tmp_path, keep=is_relevant)
    assert [p['plcyNo'] for p in client.policies_for(NAJU)] == ['H']
    with sqlite3.connect(tmp_path / 'cache.sqlite') as db:
        [(stored,)] = db.execute('SELECT body FROM youth_policy_cache').fetchall()
    assert '"J"' not in stored


@pytest.mark.parametrize(
    ('codes', 'expected'),
    [
        ({'jobCd': '0013003'}, 'targeted'),  # 미취업자 전용
        ({'jobCd': '0013001,0013003'}, 'general'),  # 재직자도 된다
        ({'schoolCd': '0049005'}, 'targeted'),  # 대학 재학 전용
        ({'schoolCd': '0049005,0049006'}, 'general'),  # 대졸 예정도 된다
        ({'sbizCd': '0014005'}, 'targeted'),  # 장애인 전용
        ({'sbizCd': '0014001,0014009'}, 'general'),  # 중소기업 재직자도 된다
        ({'jobCd': '0013010', 'schoolCd': '0049010', 'sbizCd': '0014010'}, 'general'),
        ({'jobCd': ''}, 'general'),  # 코드가 없으면 이름만 본다
    ],
)
def test_requirement_codes_for_someone_else_go_below(codes, expected):
    """요건 코드가 막 취업한 사람이 아닌 대상 전용이면 '특정 대상'으로 아래에 둔다."""
    [p] = select_policies(
        [pol(plcyNm='청년 마음건강', lclsfNm='금융･복지･문화', **codes)],
        region_by_code(NAJU),
        27,
        TODAY,
    )
    assert p.fit == expected
