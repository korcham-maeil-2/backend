"""정착 플랜 참고 정보 (이슈 #18): 은행 적금(금감원)·재직자 강의(고용24). 실제 API는 안 부른다."""

from datetime import date

import pytest
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

    def __call__(self, kind, sido, sigungu, today, weekend, ncs=None):
        self.calls.append((kind, sido, sigungu, weekend))
        if self.fail:
            raise TrainingError('신청하신 OpenApi 서비스가 존재하지 않습니다')
        found = self.by_key.get((kind, sigungu or sido), [])
        if ncs:  # srchNcs1: 그 직무 과정만
            found = [c for c in found if c.get('ncsCd', '').startswith(ncs)]
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


# ── 리뷰 반영 (backend#49 리뷰) ──


class PartlyBroken(FakeWork24):
    """컨소시엄만 실패 (키 오류 등)."""

    def __call__(self, kind, sido, sigungu, today, weekend, ncs=None):
        if kind == 'consortium':
            self.calls.append((kind, sido, sigungu, weekend))
            raise TrainingError('신청하신 OpenApi 서비스가 존재하지 않습니다')
        return super().__call__(kind, sido, sigungu, today, weekend, ncs)


def test_one_kind_failing_keeps_the_other():
    """컨소시엄 키 하나만 틀려도 내일배움카드 과정은 보인다. 둘 다 실패할 때만 unavailable."""
    fake = PartlyBroken({('card', PANGYO): [raw_course('카드 과정', '2026-12-01')]})
    found = TrainingSource(fake).get(PANGYO, TODAY)
    assert found.status == 'ok' and [c.title for c in found.courses] == ['카드 과정']
    both = TrainingSource(FakeWork24({}, fail=True)).get(PANGYO, TODAY)
    assert both.status == 'unavailable'


def test_widens_when_sigungu_has_only_courses_starting_today():
    """시군구에 '오늘 시작' 과정만 있으면 날짜로 거른 뒤 비었으니 시도로 넓힌다."""
    fake = FakeWork24(
        {
            ('card', PANGYO): [raw_course('오늘 시작', TODAY.isoformat())],
            ('card', '41'): [raw_course('경기 과정', '2026-12-01', area='41131')],
        }
    )
    found = TrainingSource(fake, kinds=('card',)).get(PANGYO, TODAY)
    assert found.scope == 'sido' and [c.title for c in found.courses] == ['경기 과정']


def test_failure_is_remembered_for_a_while():
    """실패한 지역은 잠깐(쿨다운) 다시 묻지 않는다 (열 때마다 느린 조회를 되풀이하지 않게)."""
    from backend.public_data.training import FAILURE_COOLDOWN_SECONDS

    now = [0.0]
    fake = FakeWork24({}, fail=True)
    source = TrainingSource(fake, kinds=('card',), clock=lambda: now[0])
    source.get(PANGYO, TODAY)
    calls = len(fake.calls)
    assert source.get(PANGYO, TODAY).status == 'unavailable'
    assert len(fake.calls) == calls
    now[0] = FAILURE_COOLDOWN_SECONDS + 1
    source.get(PANGYO, TODAY)
    assert len(fake.calls) > calls


def test_cache_is_per_day():
    """같은 날에는 캐시, 날짜가 바뀌면 다시 묻는다 (전날 목록이 남지 않게)."""
    fake = FakeWork24({('card', PANGYO): [raw_course('과정', '2026-12-01')]})
    source = TrainingSource(fake, kinds=('card',))
    source.get(PANGYO, TODAY)
    calls = len(fake.calls)
    source.get(PANGYO, TODAY)
    assert len(fake.calls) == calls
    source.get(PANGYO, date(2026, 11, 18))
    assert len(fake.calls) > calls


def test_courses_without_institute_are_not_grouped():
    from backend.public_data.training import PER_INSTITUTE, _course

    many = [_course(raw_course(f'과정 {i}', '2026-12-01', subTitle=''), 'card') for i in range(5)]
    assert len(pick_courses(many, TODAY)) == 5 > PER_INSTITUTE


def test_odd_training_rows_are_skipped():
    """응답 행 모양이 이상하면(제목이 숫자 등) 그 행만 건너뛴다 (500이 나지 않게)."""
    fake = FakeWork24(
        {
            ('card', PANGYO): [
                raw_course('정상 과정', '2026-12-01'),
                {**raw_course('x', '2026-12-01'), 'title': 3},
            ]
        }
    )
    found = TrainingSource(fake, kinds=('card',)).get(PANGYO, TODAY)
    assert [c.title for c in found.courses] == ['정상 과정']


def test_odd_savings_answer_is_unavailable_not_500():
    """금감원 응답 모양이 예상과 다르면 500 대신 '불러오지 못함'. 실패도 잠깐 기억한다."""
    calls = []

    def odd():
        calls.append(1)
        return {
            'base': [{'fin_prdt_nm': 3, 'join_deny': '1', 'fin_co_no': 'B', 'fin_prdt_cd': 'A'}],
            'options': [{'fin_co_no': 'B', 'fin_prdt_cd': 'A', 'save_trm': '12', 'intr_rate': 3}],
        }

    source = SavingsSource(odd)
    assert source.get().status == 'unavailable'
    source.get()
    assert len(calls) == 1  # 쿨다운 동안 다시 묻지 않는다


# ── 후속 수정 ──


def test_widens_only_when_every_kind_is_empty_in_sigungu():
    """한 종류라도 시군구에 과정이 있으면 넓히지 않는다: 시군구·시도 과정이 섞이면
    화면의 범위 안내('시군구에는 과정이 없어 시도 과정')가 틀린다."""
    fake = FakeWork24(
        {
            ('card', PANGYO): [raw_course('판교 카드 과정', '2026-12-01')],
            ('consortium', '41'): [raw_course('경기 컨소시엄', '2026-12-01', area='41131')],
        }
    )
    found = TrainingSource(fake).get(PANGYO, TODAY)
    assert found.scope == 'sigungu'
    assert [c.title for c in found.courses] == ['판교 카드 과정']
    assert all(sigungu == PANGYO for _, _, sigungu, _ in fake.calls)  # 시도로 묻지 않았다


def test_widening_skips_the_kind_that_failed():
    """시군구에서 실패한 종류는 시도에서 다시 묻지 않는다
    (같은 키 오류를 한 번 더 기다리지 않게)."""
    fake = PartlyBroken(
        {('card', '41'): [raw_course('경기 카드 과정', '2026-12-01', area='41131')]}
    )
    found = TrainingSource(fake).get(PANGYO, TODAY)
    assert found.status == 'ok' and found.scope == 'sido'
    assert [c.title for c in found.courses] == ['경기 카드 과정']
    assert [call for call in fake.calls if call[0] == 'consortium' and call[2] is None] == []


def test_partial_failure_is_kept_only_for_the_cooldown():
    """한 종류만 실패한 결과도 잠깐(쿨다운) 둔다: 열 때마다 실패한 쪽을 다시 기다리지 않게."""
    from backend.public_data.training import FAILURE_COOLDOWN_SECONDS

    now = [0.0]
    fake = PartlyBroken({('card', PANGYO): [raw_course('카드 과정', '2026-12-01')]})
    source = TrainingSource(fake, clock=lambda: now[0])
    source.get(PANGYO, TODAY)
    calls = len(fake.calls)
    assert [c.title for c in source.get(PANGYO, TODAY).courses] == ['카드 과정']
    assert len(fake.calls) == calls
    now[0] = FAILURE_COOLDOWN_SECONDS + 1
    source.get(PANGYO, TODAY)
    assert len(fake.calls) > calls


def test_old_days_are_dropped_from_the_cache():
    fake = FakeWork24({('card', PANGYO): [raw_course('과정', '2026-12-01')]})
    source = TrainingSource(fake, kinds=('card',))
    source.get(PANGYO, TODAY)
    source.get(PANGYO, date(2026, 11, 18))
    assert [key[1] for key in source._cache] == [date(2026, 11, 18)]


# ── 직무에 맞는 강의 · 무료 과정 자리 ──


@pytest.mark.parametrize(
    ('job', 'ncs'),
    [
        ('백엔드 개발자', '20'),
        ('전력설비 엔지니어', '19'),
        ('기술영업', '10'),
        ('공정관리', None),  # 모르면 직무로 고르지 않는다
        ('웹디자이너', '08'),  # 디자인 먼저
        ('콘텐츠 마케터', '02'),
        ('전자상거래 MD', '10'),  # '전자'가 들어 있어도 전기·전자가 아니다
        ('간호사', '06'),
        # 틀리게 고르느니 모름: 겹치는 낱말
        ('연구개발(R&D)', None),
        ('인사이트 분석가', None),
        ('데이터 입력 사무', '02'),
        ('홀서버', None),
        ('보안요원', None),
        # 하는 일이 업종보다 먼저 (Fable 리뷰)
        ('자동차 영업', '10'),
        ('반도체 장비 영업', '10'),
        ('건설 회계', '02'),
        ('전자 회사 인사 담당', '02'),
        # 영문 약어는 단어로만
        ('RETAIL 매니저', None),
        ('MAINTENANCE 엔지니어', None),
        ('AI 엔지니어', '20'),
        ('영상의학과 간호사', '06'),
        ('앱 마케터', '02'),
        (None, None),
    ],
)
def test_ncs_for_job(job, ncs):
    from backend.public_data.training import ncs_for_job

    assert ncs_for_job(job) == ncs


def test_job_matching_courses_first_and_free_courses_kept():
    """직무(NCS)에 맞는 과정이 주말반보다 먼저, 무료 컨소시엄(평일)도 주말반에 밀리지 않는다."""
    fake = FakeWork24(
        {
            ('card', PANGYO): [
                raw_course(
                    f'주말 {i}', '2026-12-01', weekend='1', subTitle=f'학원 {i}', ncsCd='12010101'
                )
                for i in range(6)
            ]
            + [raw_course('파이썬 평일', '2026-12-05', subTitle='코딩학원', ncsCd='20010202')],
            ('consortium', PANGYO): [
                raw_course('AI 실무', '2026-12-03', subTitle='SK', ncsCd='20010702'),
                raw_course('리더십', '2026-12-04', subTitle='상의', ncsCd='02020101'),
            ],
        }
    )
    source = TrainingSource(fake)
    titles = [c.title for c in source.get(PANGYO, TODAY, ncs='20').courses]
    assert titles[:2] == ['AI 실무', '파이썬 평일']  # 직무에 맞는 것 먼저 (시작일 순)
    assert '리더십' in titles and len(titles) == 5  # 무료 과정 자리
    calls = len(fake.calls)
    no_job = [c.title for c in source.get(PANGYO, TODAY).courses]
    assert len(fake.calls) == calls  # 직무만 달라지면 다시 묻지 않는다
    assert no_job[0] == '주말 0' and {'AI 실무', '리더십'} <= set(no_job)


def test_api_training_ncs_param(tmp_path):
    app = create_app(
        str(tmp_path / 'cp.sqlite'),
        extractor=FakeExtractor(),
        savings_source=SavingsSource(lambda: SAVINGS),
        training_source=TrainingSource(
            FakeWork24({('card', PANGYO): [raw_course('과정', '2027-01-05', ncsCd='20010202')]})
        ),
    )
    with TestClient(app) as c:
        found = c.get(f'/plan/training/{PANGYO}?ncs=20').json()
        assert found['courses'][0]['ncs'] == '20010202'
        assert c.get(f'/plan/training/{PANGYO}?ncs=abc').status_code == 422


def test_later_cohort_of_the_same_course_is_dropped():
    from backend.public_data.training import _course

    first = _course(raw_course('자바 주말반', '2026-12-05'), 'card')
    later = _course(raw_course('자바 주말반', '2026-12-12'), 'card')
    assert [c.start for c in pick_courses([later, first], TODAY)] == ['2026-12-05']


def test_free_slots_do_not_push_out_job_matching_courses():
    """직무에 맞는 과정이 충분하면 직무와 무관한 무료 과정에 자리를 내주지 않는다."""
    fake = FakeWork24(
        {
            ('card', PANGYO): [
                raw_course(f'개발 {i}', '2026-12-01', subTitle=f'학원 {i}', ncsCd='20010202')
                for i in range(6)
            ],
            ('consortium', PANGYO): [raw_course('리더십', '2026-12-01', subTitle='상의')],
        }
    )
    titles = [c.title for c in TrainingSource(fake).get(PANGYO, TODAY, ncs='20').courses]
    assert '리더십' not in titles and len(titles) == 5


class FirstThree(FakeWork24):
    """고용24처럼 시작일 순 앞부분만 준다 (직무 조건이 없을 때)."""

    def __call__(self, kind, sido, sigungu, today, weekend, ncs=None):
        found = super().__call__(kind, sido, sigungu, today, weekend, ncs)
        return found if ncs else found[:3]


def test_job_courses_are_asked_with_the_job_filter():
    """시작일 순 앞부분에 없는 직무 과정도 직무 조건(srchNcs1)으로 따로 물어 찾는다.
    시군구에 과정이 있으면 직무 과정도 시군구에서만 (시도로 넓히지 않는다)."""
    fake = FirstThree(
        {
            ('card', PANGYO): [
                raw_course(f'요리 {i}', f'2026-12-0{i + 1}', subTitle=f'학원 {i}', ncsCd='13010101')
                for i in range(3)
            ]
            + [raw_course('자바', '2027-01-20', subTitle='코딩학원', ncsCd='20010202')],
            ('card', '41'): [
                raw_course('경기 파이썬', '2026-12-01', area='41131', ncsCd='20010202')
            ],
        }
    )
    source = TrainingSource(fake, kinds=('card',))
    found = source.get(PANGYO, TODAY, ncs='20')
    assert found.scope == 'sigungu' and found.courses[0].title == '자바'
    assert '경기 파이썬' not in [c.title for c in found.courses]
    assert '자바' not in [c.title for c in source.get(PANGYO, TODAY).courses]  # 직무 없으면 그대로


def test_job_lookup_failure_falls_back_and_follows_the_base_scope():
    """직무 조회가 실패하면 기본 목록 그대로.
    기본 목록이 시도로 넓혔으면 직무 과정도 시도에서 묻는다."""

    class JobFails(FakeWork24):
        def __call__(self, kind, sido, sigungu, today, weekend, ncs=None):
            if ncs:
                raise TrainingError('직무 조회 실패')
            return super().__call__(kind, sido, sigungu, today, weekend, ncs)

    base = {('card', PANGYO): [raw_course('과정', '2026-12-01')]}
    found = TrainingSource(JobFails(base), kinds=('card',)).get(PANGYO, TODAY, ncs='20')
    assert found.status == 'ok' and [c.title for c in found.courses] == ['과정']

    wide = FakeWork24(
        {('card', '41'): [raw_course('경기 자바', '2026-12-01', area='41131', ncsCd='20')]}
    )
    found = TrainingSource(wide, kinds=('card',)).get(PANGYO, TODAY, ncs='20')
    assert found.scope == 'sido' and found.courses[0].title == '경기 자바'
    assert ('card', '41', None, True) in wide.calls  # 직무 과정도 시도에서
