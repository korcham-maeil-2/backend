"""정착 플랜 참고 정보 (이슈 #18): 은행 적금(금감원)·재직자 강의(고용24). 실제 API는 안 부른다."""

from datetime import date

from conftest import FakeExtractor
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.public_data.finlife import (
    CACHE_SECONDS,
    FinlifeError,
    SavingsSource,
    pick_savings,
)
from backend.public_data.training import TrainingError, TrainingSource, pick_courses

TODAY = date(2026, 11, 17)
PANGYO, NAJU = '41135', '12170'


def product(code: str, name: str, deny: str = '1', limit=None) -> dict:
    return {
        'fin_co_no': 'B1',
        'fin_prdt_cd': code,
        'kor_co_nm': '주식회사 가나은행',
        'fin_prdt_nm': name,
        'join_deny': deny,
        'max_limit': limit,
        'join_way': '스마트폰',
        'dcls_month': '202610',
    }


def option(code: str, rate, term='12', max_rate=None, kind='자유적립식') -> dict:
    return {
        'fin_co_no': 'B1',
        'fin_prdt_cd': code,
        'save_trm': term,
        'intr_rate': rate,
        'intr_rate2': max_rate,
        'rsrv_type_nm': kind,
    }


SAVINGS = {
    'base': [
        product('A', '가나 자유적금', limit=3_000_000),
        product('B', '가나 정기적금'),
        product('C', '가나 기업적금'),  # 법인용 → 뺀다
        product('D', '서민 전용 적금', deny='2'),  # 가입 제한 → 뺀다
        product('E', '긴 이름\n적금'),
    ],
    'options': [
        option('A', 3.5, max_rate=4.0),
        option('A', 3.9, term='24'),  # 12개월만 본다
        option('A', 3.6),  # 같은 상품은 가장 높은 12개월 옵션 하나
        option('B', 3),  # 정수로 오기도 한다
        option('C', 9.9),
        option('D', 9.9),
        option('E', 2.5),
    ],
    'month': '202610',
}


def test_pick_savings_by_base_rate():
    picked = pick_savings(SAVINGS)
    assert [(p.name, p.rate) for p in picked] == [
        ('가나 자유적금', 3.6),
        ('가나 정기적금', 3.0),
        ('긴 이름 적금', 2.5),
    ]
    assert picked[0].bank == '가나은행'  # '주식회사' 뗌
    assert picked[0].limit_krw == 3_000_000 and picked[1].limit_krw is None


def test_savings_source_caches_in_memory_and_reports_failure():
    calls, now = [], [0.0]

    def fetch():
        calls.append(1)
        return SAVINGS

    source = SavingsSource(fetch, clock=lambda: now[0])
    first = source.get()
    assert first.status == 'ok' and first.disclosed_month == '202610'
    assert '금융감독원' in first.source
    source.get()
    assert len(calls) == 1  # 한 시간 안에는 다시 부르지 않는다
    now[0] = CACHE_SECONDS + 1
    source.get()
    assert len(calls) == 2

    def broken():
        raise FinlifeError('err_cd 020')

    failed = SavingsSource(broken).get()
    assert failed.status == 'unavailable' and failed.products == [] and failed.reason


def test_savings_off_without_key():
    off = SavingsSource(None).get()
    assert off.status == 'off' and '인증키' in off.reason


def raw_course(title, start, area=PANGYO, weekend='3', fee='100000', **kw) -> dict:
    return {
        'title': title,
        'subTitle': '가나훈련원',
        'traStartDate': start,
        'traEndDate': '2027-01-31',
        'trngAreaCd': area,
        'wkendSe': weekend,
        'courseMan': fee,
        'titleLink': 'https://www.work24.go.kr/x',
        **kw,
    }


class FakeWork24:
    """(종류, 시도, 시군구) → 과정 목록. 주말반만 조회하면 그중 주말반만. calls에 조건을 남긴다."""

    def __init__(self, by_key: dict, fail: bool = False) -> None:
        self.by_key, self.fail, self.calls = by_key, fail, []

    def __call__(self, kind, sido, sigungu, today, weekend):
        self.calls.append((kind, sido, sigungu, weekend))
        if self.fail:
            raise TrainingError('신청하신 OpenApi 서비스가 존재하지 않습니다')
        found = self.by_key.get((kind, sigungu or sido), [])
        return [c for c in found if c['wkendSe'] == '1'] if weekend else found


def test_training_weekend_first_then_start_date():
    fake = FakeWork24(
        {
            ('card', PANGYO): [
                raw_course('파이썬 기초', '2026-12-01'),
                raw_course('엑셀 주말반', '2027-01-10', weekend='1'),
                raw_course('지난 과정', '2026-11-01'),  # 이미 시작 → 뺀다
                raw_course('오늘 시작', '2026-11-17'),  # 오늘 시작도 신청하기 어렵다 → 뺀다
                raw_course('다른 지역', '2026-11-20', area='41131'),  # trngAreaCd로 거른다
            ],
            ('consortium', PANGYO): [
                raw_course('데이터 분석', '2026-11-20', fee='0', subTitle='대한상의 기술교육센터')
            ],
        }
    )
    found = TrainingSource(fake).get(PANGYO, TODAY)
    assert found.status == 'ok' and found.scope == 'sigungu'
    assert [c.title for c in found.courses] == ['엑셀 주말반', '데이터 분석', '파이썬 기초']
    assert found.courses[0].weekend and found.courses[1].fee_krw == 0
    assert found.courses[1].kind_label.startswith('중소기업')
    assert found.region == '경기도 성남시 분당구'


def test_training_widens_to_sido_when_sigungu_is_empty():
    fake = FakeWork24({('card', '12'): [raw_course('나주 근처 과정', '2026-12-01', area='12110')]})
    found = TrainingSource(fake, kinds=('card',)).get(NAJU, TODAY)
    assert found.scope == 'sido'
    assert [c.title for c in found.courses] == ['나주 근처 과정']
    assert fake.calls == [
        ('card', '12', NAJU, True),
        ('card', '12', NAJU, False),
        ('card', '12', None, True),
        ('card', '12', None, False),
    ]


def test_training_failure_and_off():
    failed = TrainingSource(FakeWork24({}, fail=True)).get(PANGYO, TODAY)
    assert failed.status == 'unavailable' and failed.courses == []
    off = TrainingSource(None).get(PANGYO, TODAY)
    assert off.status == 'off' and '인증키' in off.reason


def test_pick_courses_limits_one_institute():
    from backend.public_data.training import PER_INSTITUTE, _course

    many = [_course(raw_course(f'과정 {i}', '2026-12-01'), 'card') for i in range(5)]
    assert len(pick_courses(many, TODAY)) == PER_INSTITUTE


def test_pick_courses_drops_duplicates():
    from backend.public_data.training import _course

    a = _course(raw_course('같은 과정', '2026-12-01'), 'card')
    b = _course(raw_course('같은 과정', '2026-12-01'), 'card')
    assert len(pick_courses([a, b], TODAY)) == 1


def test_api_routes(tmp_path):
    app = create_app(
        str(tmp_path / 'cp.sqlite'),
        extractor=FakeExtractor(),
        savings_source=SavingsSource(lambda: SAVINGS),
        training_source=TrainingSource(
            FakeWork24({('card', PANGYO): [raw_course('과정', '2027-01-05')]})
        ),
    )
    with TestClient(app) as c:
        savings = c.get('/plan/savings').json()
        assert savings['status'] == 'ok' and len(savings['products']) == 3
        training = c.get(f'/plan/training/{PANGYO}').json()
        assert training['status'] == 'ok' and training['courses'][0]['title'] == '과정'
        assert c.get('/plan/training/99999').status_code == 404
