"""월세 실거래가 테스트 (get_rent, #15) — 실제 공공데이터포털은 부르지 않는다.

    - 응답 읽기: 실제 응답과 같은 모양의 XML (docs api/국토부_전월세_실거래가.md 5·6절)
    - 추정: 원룸급(20~40㎡) 신규 월세만 골라 중앙값
    - 캐시: 한 번 받은 달은 다시 묻지 않음
    - 계산: 이사할 집 월세를 비우면 추정값을 '추정'으로 씀, 사용자 값이 있으면 그 값
    - API: 확인 → get_rent → 계산, 다시 계산 때 재조회 없음, GET /rent/{코드}

[실행] backend 폴더에서 uv run pytest tests/test_rent.py
"""

import asyncio
import sqlite3
from datetime import date

import pytest
from conftest import FakeExtractor, scenario_text
from fastapi.testclient import TestClient
from test_demo_scenarios import profile_a

from backend.calc.summary import calculate_all
from backend.main import create_app
from backend.public_data.rent import (
    Deal,
    RentApiError,
    RentCache,
    RentEstimator,
    parse_page,
    recent_months,
    summarize,
)

NAJU = '12170'
TODAY = date(2026, 11, 15)
MONTHS = recent_months(TODAY)

# 실제 응답에서 주소·건물명을 지운 모양 (보증금 쉼표, 빈 값 공백 한 칸)
PAGE = """<?xml version="1.0" encoding="utf-8" standalone="yes"?><response><header>
<resultCode>000</resultCode><resultMsg>OK</resultMsg></header><body><items>
<item><contractType> </contractType><deposit>200</deposit><houseType>다가구</houseType>
<monthlyRent>29</monthlyRent><sggCd>12170</sggCd><totalFloorAr>27</totalFloorAr></item>
<item><contractType>갱신</contractType><deposit>2,000</deposit><excluUseAr>30.77</excluUseAr>
<monthlyRent>42</monthlyRent><sggCd>12170</sggCd></item>
<item><contractType>신규</contractType><deposit>1,000</deposit><excluUseAr>64.85</excluUseAr>
<monthlyRent>60</monthlyRent><sggCd>12170</sggCd></item>
</items><numOfRows>1000</numOfRows><pageNo>1</pageNo><totalCount>3</totalCount></body></response>"""

KEY_ERROR = """<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>
<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg><returnReasonCode>30
</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"""


def deal(rent: int, area: float = 30, deposit: int = 500, renewal: bool = False) -> Deal:
    return Deal(rent_man=rent, deposit_man=deposit, area=area, renewal=renewal)


class FakeFetcher:
    """유형·달마다 정해 둔 거래를 돌려주고, 몇 번 불렸는지 센다."""

    def __init__(self, deals: list[Deal] | None = None, fail: bool = False) -> None:
        self.deals = deals if deals is not None else [deal(40), deal(45), deal(50)]
        self.fail = fail
        self.calls = 0

    async def __call__(self, kind, lawd_cd, ymd):
        await asyncio.sleep(0)  # 실제 조회처럼 이벤트 루프에 차례를 넘긴다
        self.calls += 1
        if self.fail:
            raise RentApiError('오류 응답: SERVICE ERROR')
        return list(self.deals) if kind == 'OffiRent' else []


# ── 응답 읽기 ────────────────────────────────────────────────────


def test_parse_page_reads_money_area_and_renewal():
    deals, total = parse_page(PAGE)
    assert total == 3
    assert deals == [
        Deal(rent_man=29, deposit_man=200, area=27.0, renewal=False),  # 단독·다가구: 계약면적
        Deal(rent_man=42, deposit_man=2000, area=30.77, renewal=True),  # '2,000' 쉼표
        Deal(rent_man=60, deposit_man=1000, area=64.85, renewal=False),
    ]


def test_parse_page_key_error_is_not_silent():
    """키 오류는 HTTP 200에 다른 모양으로 온다 → 빈 결과로 넘기지 않고 오류로."""
    with pytest.raises(RentApiError):
        parse_page(KEY_ERROR)
    with pytest.raises(RentApiError):
        parse_page('<html>점검 중</html')


def test_recent_months_skips_current_month_and_wraps_year():
    assert MONTHS == ['202605', '202606', '202607', '202608', '202609', '202610']
    assert recent_months(date(2027, 2, 3), 3) == ['202611', '202612', '202701']


# ── 추정 ─────────────────────────────────────────────────────────


def test_summarize_uses_small_new_monthly_rents_median():
    deals = [
        deal(40),
        deal(44, deposit=300),
        deal(100),  # 비싼 한 건이 있어도 중앙값은 덜 흔들린다
        deal(30, renewal=True),  # 갱신 → 뺌
        deal(0, deposit=9000),  # 전세 → 뺌
        deal(90, area=84),  # 큰 집 → 뺌
        deal(20, area=12),  # 원룸텔급 → 뺌
    ]
    e = summarize(NAJU, MONTHS, deals)
    assert e.monthly_rent_krw == 440_000
    assert e.deposit_krw == 5_000_000
    assert e.sample_size == 3
    assert e.status == 'estimated'
    assert e.region == '전남광주통합특별시 나주시'
    assert e.period == '2026년 5~10월'
    assert '국토교통부 전월세 실거래가' in e.source and '3건' in e.source
    assert '관리비 별도' in e.source


def test_summarize_without_usable_deals_is_missing_not_zero():
    e = summarize(NAJU, MONTHS, [deal(0), deal(50, area=80)])
    assert e.monthly_rent_krw is None
    assert e.status == 'missing'
    assert e.reason


def test_estimator_caches_months(tmp_path):
    fetcher = FakeFetcher()
    cache = RentCache(str(tmp_path / 'c.sqlite'))
    est = RentEstimator(fetcher, cache, today=lambda: TODAY)
    first = asyncio.run(est.estimate(NAJU))
    assert fetcher.calls == 18  # 3유형 × 6개월
    assert first.monthly_rent_krw == 450_000
    assert first.sample_size == 18  # 오피스텔 3건 × 6개월
    again = asyncio.run(est.estimate(NAJU))
    assert fetcher.calls == 18  # 캐시에서 읽음
    assert again == first


def test_cache_refreshes_recent_months_after_a_day(tmp_path):
    path = str(tmp_path / 'c.sqlite')
    cache = RentCache(path)
    cache.write(NAJU, {('OffiRent', '202610'): [deal(40)], ('OffiRent', '202605'): [deal(40)]})
    with sqlite3.connect(path) as db:
        db.execute("UPDATE rent_cache SET fetched_at='2026-01-01T00:00:00+00:00'")
    got = cache.read(NAJU, MONTHS, fresh_since='202609')
    assert ('OffiRent', '202610') not in got  # 최근 달 → 다시 받는다
    assert got[('OffiRent', '202605')] == [deal(40)]  # 지난 달은 계속 쓴다


def test_broken_cache_does_not_stop_estimate(tmp_path, monkeypatch):
    """캐시를 읽지도 쓰지도 못해도(잠김 등) 조회해서 시세를 낸다."""

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(RentCache, 'read', locked)
    monkeypatch.setattr(RentCache, 'write', locked)
    est = RentEstimator(FakeFetcher(), RentCache(str(tmp_path / 'c.sqlite')), today=lambda: TODAY)
    assert asyncio.run(est.estimate(NAJU)).monthly_rent_krw == 450_000


def test_estimate_many_skips_codes_that_need_a_district():
    """성남시(41130)처럼 구를 골라야 하는 코드는 실거래가가 0건이라 조회하지 않는다."""
    fetcher = FakeFetcher()
    est = RentEstimator(fetcher, today=lambda: TODAY)
    assert asyncio.run(est.estimate_many(['41130', '99999'])) == {}
    assert fetcher.calls == 0


def test_estimator_api_error_gives_missing_not_crash():
    est = RentEstimator(FakeFetcher(fail=True), today=lambda: TODAY)
    e = asyncio.run(est.estimate(NAJU))
    assert e.status == 'unavailable'  # '거래 없음'(missing)과 구분 → 다시 계산할 때 재조회
    assert e.monthly_rent_krw is None
    assert e.reason


# ── 계산에 반영 ──────────────────────────────────────────────────


def _naju_row() -> dict:
    from conftest import scenario_llm

    from backend.normalize import build_rows

    [row] = build_rows(scenario_llm('down_naju'), scenario_text('down_naju'))
    return row.model_dump(mode='json')


def _rents(value: int | None = 450_000) -> dict:
    e = summarize(NAJU, MONTHS, [deal(value // 10_000)] if value else [])
    return {NAJU: e.model_dump(mode='json')}


def _move(out: dict) -> dict:
    [posting] = out['postings']
    return next(s for s in posting['scenarios'] if s['case'] == 'move')


def _naju_profile(**move) -> dict:
    p = profile_a()
    p['postings'] = {'posting-1': {**p['postings']['down_naju'], **move}}
    return p


def test_empty_move_housing_uses_rent_estimate():
    profile = _naju_profile()
    del profile['postings']['posting-1']['move_housing']
    out = calculate_all([_naju_row()], profile, _rents())
    housing = _move(out)['housing']
    # 시세 45만원 − 회사 주거지원비 20만원
    assert housing['value'] == 250_000
    assert housing['status'] == 'estimated'
    assert '국토교통부 전월세 실거래가' in housing['source']
    # 나주는 연봉 비공개라 남는 돈 대신 '빠진 값'에 주거비가 없어야 한다
    assert '주거비 (이사)' not in _move(out)['missing']
    assert out['rent_estimates'][0]['lawd_cd'] == NAJU


def test_user_move_housing_wins_over_estimate():
    profile = _naju_profile(move_housing={'value': 380_000})
    out = calculate_all([_naju_row()], profile, _rents())
    assert _move(out)['housing']['value'] == 380_000 - 200_000
    assert _move(out)['housing']['status'] == 'user_input'


def test_estimate_not_used_when_not_moving_or_missing():
    profile = _naju_profile(move_possible=False)
    del profile['postings']['posting-1']['move_housing']
    out = calculate_all([_naju_row()], profile, _rents())
    assert _move(out)['applicable'] is False

    profile = _naju_profile()
    del profile['postings']['posting-1']['move_housing']
    out = calculate_all([_naju_row()], profile, _rents(None))  # 시세를 못 구함
    assert _move(out)['housing']['value'] is None
    assert _move(out)['monthly_remaining'] is None


# ── API (확인 → get_rent → 계산) ─────────────────────────────────


def _client(tmp_path, fetcher) -> TestClient:
    app = create_app(str(tmp_path / 'cp.sqlite'), extractor=FakeExtractor(), rent_fetcher=fetcher)
    return TestClient(app)


def test_confirm_runs_get_rent_and_recalculate_reuses_it(tmp_path):
    fetcher = FakeFetcher()
    profile = _naju_profile()
    del profile['postings']['posting-1']['move_housing']
    with _client(tmp_path, fetcher) as c:
        sid = c.post('/sessions', json={'message': scenario_text('down_naju')}).json()['session_id']
        done = c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': profile})
        assert done.status_code == 200
        move = _move(done.json()['result'])
        assert move['housing']['status'] == 'estimated'
        assert move['housing']['value'] == 450_000 - 200_000
        calls = fetcher.calls
        assert calls > 0

        again = c.post(f'/sessions/{sid}/recalculate', json={'profile': profile})
        assert again.status_code == 200
        assert fetcher.calls == calls  # 이미 구한 지역은 다시 묻지 않는다
        assert _move(again.json()['result'])['housing']['value'] == 250_000


def test_cache_write_failure_still_confirms(tmp_path, monkeypatch):
    """캐시에 못 써도 확인은 200 (세션이 '실행 중'에 갇히지 않는다)."""

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(RentCache, 'write', locked)
    profile = _naju_profile()
    del profile['postings']['posting-1']['move_housing']
    with _client(tmp_path, FakeFetcher()) as c:
        sid = c.post('/sessions', json={'message': scenario_text('down_naju')}).json()['session_id']
        done = c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': profile})
        assert done.status_code == 200
        assert done.json()['status'] == 'completed'
        assert _move(done.json()['result'])['housing']['status'] == 'estimated'


def test_recalculate_retries_regions_that_failed(tmp_path):
    """확인 때 불러오지 못한 지역은 다시 계산할 때 다시 조회한다."""
    fetcher = FakeFetcher(fail=True)
    profile = _naju_profile()
    del profile['postings']['posting-1']['move_housing']
    with _client(tmp_path, fetcher) as c:
        sid = c.post('/sessions', json={'message': scenario_text('down_naju')}).json()['session_id']
        done = c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': profile})
        assert done.json()['result']['rent_estimates'][0]['status'] == 'unavailable'
        assert _move(done.json()['result'])['housing']['value'] is None

        fetcher.fail = False
        again = c.post(f'/sessions/{sid}/recalculate', json={'profile': profile}).json()
        assert again['result']['rent_estimates'][0]['status'] == 'estimated'
        assert _move(again['result'])['housing']['value'] == 450_000 - 200_000


def test_rent_endpoint(tmp_path):
    with _client(tmp_path, FakeFetcher()) as c:
        r = c.get(f'/rent/{NAJU}')
        assert r.status_code == 200
        assert r.json()['monthly_rent_krw'] == 450_000
        assert r.json()['status'] == 'estimated'
        # 구를 더 골라야 하는 시(성남시)·없는 코드 → 404
        for code in ('41130', '99999'):
            r = c.get(f'/rent/{code}')
            assert r.status_code == 404
            assert r.json()['code'] == 'REGION_NOT_FOUND'


def test_rent_endpoint_without_key_says_why(tmp_path):
    with TestClient(create_app(str(tmp_path / 'cp.sqlite'), extractor=FakeExtractor())) as c:
        body = c.get(f'/rent/{NAJU}').json()
        assert body['status'] == 'missing'
        assert body['monthly_rent_krw'] is None
        assert '인증키' in body['reason']
