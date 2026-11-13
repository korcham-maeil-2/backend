"""normalize (#5) 테스트 — 시연 공고 5개를 넣었을 때 비교표가 정답(expected.json)대로 나오는지.

완료 조건: 시연 공고 텍스트를 넣으면 비교표 스키마에 맞는 결과가 나오고, 없는 값은 비어 있다.

[이 테스트가 확인하는 것 / 못 하는 것]
  - 확인함: 'Claude가 이렇게 답하면' 그 뒤의 코드(원문 대조, 금액 확인, 지역 코드 찾기,
           스키마 변환, 세션 API)가 정답대로 비교표를 만드는지.
  - 못 함 : 실제 Claude가 정말 이렇게 답하는지. 그건 API를 불러야 알 수 있다 (개발 중 금지).
"""

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from conftest import SCENARIOS, scenario_expected, scenario_llm, scenario_text
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.normalize import (
    ClaudeExtractor,
    ComparisonRow,
    ExtractionResult,
    NormalizeError,
    checks,
)
from backend.normalize.llm import MODEL
from backend.normalize.rows import REGION_QUESTION, build_rows

# expected.json은 '언급 없음'도 false로 적은 곳이 있다. 우리 스키마는 '언급 없음'을
# 지어내지 않고 missing(공고에 없음)으로 둔다. 그 차이를 여기에 이유와 함께 적어 둔다.
KNOWN_DIFFERENCES = {
    ('near_pangyo', 'shuttle_bus'): '공고에 통근버스 언급이 없다 (교통비 지원 없음과는 다른 정보)',
    ('up_seoul', 'shuttle_bus'): '메일에 통근버스 언급이 없다',
}


def row_for(pid: str) -> ComparisonRow:
    rows = build_rows(scenario_llm(pid), scenario_text(pid))
    assert len(rows) == 1
    return rows[0]


def expected_pairs(exp: dict) -> dict[str, object]:
    """expected.json → {비교표 칸 이름: 정답 값}. 정답 파일에 있는 칸만 비교한다."""
    pairs = {
        'company': exp['company'],
        'job_title': exp['job_title'],
        'employment_type': exp['employment_type'],
        'location.sido': exp['location']['sido'],
        'location.sigungu': exp['location']['sigungu'],
        'location.lawd_cd': exp['location']['lawd_cd'],
        'salary_type': exp['salary']['type'],
        'annual_salary_krw': exp['salary']['annual_krw'],
        'meal_allowance_monthly_krw': exp['nontaxable_meal_monthly_krw'],
        'transport_allowance_monthly_krw': exp['commute']['transport_allowance_monthly_krw'],
        'shuttle_bus': exp['commute']['shuttle_bus'],
    }
    if 'probation' in exp:
        pairs['probation_months'] = exp['probation']['months']
        pairs['probation_pay_rate'] = exp['probation']['pay_rate']
    housing = exp['housing']
    if 'allowance_monthly_krw' in housing:
        pairs['housing_allowance_monthly_krw'] = housing['allowance_monthly_krw']
        pairs['housing_allowance_months'] = housing['allowance_months']
    if 'company_housing' in housing:
        pairs['company_housing'] = housing['company_housing']
    return pairs


def actual(row: ComparisonRow, key: str):
    if key.startswith('location.'):
        loc = row.location.value
        return getattr(loc, key.split('.', 1)[1]) if loc else None
    return getattr(row, key).value


# ── 시나리오 5개 ────────────────────────────────────────────────


@pytest.mark.parametrize('pid', SCENARIOS)
def test_scenario_matches_expected(pid):
    """정답 파일(expected.json)의 값과 비교표 값이 같다."""
    row = row_for(pid)
    for key, want in expected_pairs(scenario_expected(pid)).items():
        if (pid, key) in KNOWN_DIFFERENCES:
            continue
        assert actual(row, key) == want, f'{pid}.{key}: {actual(row, key)!r} != {want!r}'


@pytest.mark.parametrize(('pid', 'key'), list(KNOWN_DIFFERENCES))
def test_unmentioned_is_missing_not_false(pid, key):
    """정답 파일과 다른 곳: 공고에 언급이 없으면 false가 아니라 '공고에 없음'으로 비워 둔다."""
    cell = getattr(row_for(pid), key)
    assert cell.value is None
    assert cell.status == 'missing'


@pytest.mark.parametrize('pid', SCENARIOS)
def test_every_cell_is_confirmed_or_empty(pid):
    """값이 있는 칸은 모두 원문에서 확인되고(confirmed), 없는 칸은 값이 비어 있다(missing).

    fixture는 원문을 그대로 인용했으므로 '추정'이 하나도 없어야 한다.
    """
    row = row_for(pid)
    cells = {
        k: getattr(row, k) for k in ComparisonRow.model_fields if hasattr(getattr(row, k), 'status')
    }
    cells |= {f'other.{a.name}': a.monthly_krw for a in row.other_allowances}
    for name, cell in cells.items():
        if cell.value is None:
            assert cell.status == 'missing', name
        else:
            assert cell.status == 'confirmed', name
            assert cell.source_text and checks.quote_in_text(cell.source_text, scenario_text(pid))


def test_undisclosed_salary_stays_empty():
    """연봉 비공개 공고(울산·나주)는 연봉을 지어내지 않는다.

    '동종업계 상위 수준'도 금액이 아니다.
    """
    for pid in ['stay_ulsan', 'down_naju']:
        row = row_for(pid)
        assert row.salary_type.value == 'undisclosed'
        assert row.salary_type.status == 'confirmed'
        assert row.annual_salary_krw.value is None
        assert row.annual_salary_krw.status == 'missing'


def test_scenario_details():
    """정답 파일에 칸이 없는 세부 값들."""
    pangyo = row_for('near_pangyo')
    assert [(a.name, a.monthly_krw.value) for a in pangyo.other_allowances] == [('통신비', 50000)]
    assert pangyo.meal_allowance_in_salary.value is False
    assert pangyo.housing_allowance_monthly_krw.value == 0  # '주거 지원: 없음' → 0원 (확인)
    assert pangyo.housing_allowance_monthly_krw.status == 'confirmed'

    seoul = row_for('up_seoul')
    assert seoul.meal_allowance_in_salary.value is True  # 식대가 연봉 4,000만원 안에 포함
    assert seoul.meals_provided.value is False
    assert seoul.housing_allowance_monthly_krw.status == 'missing'  # 메일에 주거 언급 없음

    gwangju = row_for('across_gwangju')
    # 사용자 본인의 월세(40)·이사 견적(70만 원)은 공고 조건이 아니라 칸에 들어가지 않는다.
    dumped = json.dumps(gwangju.model_dump(), ensure_ascii=False)
    assert '700000' not in dumped and '400000' not in dumped
    assert gwangju.location.value.raw == '광주 광산구 하남산단'


# ── 지어낸 값 막기 ───────────────────────────────────────────────


def _pangyo_with(**changes) -> ComparisonRow:
    llm = scenario_llm('near_pangyo')
    posting = llm.postings[0].model_copy(update=changes)
    return build_rows(ExtractionResult(postings=[posting]), scenario_text('near_pangyo'))[0]


def test_quote_not_in_text_is_estimated():
    """LLM이 원문에 없는 인용을 대면 '추정'이 되고, 그 인용은 근거로 쓰지 않는다."""
    from backend.normalize.schema import ExtractedMoney

    row = _pangyo_with(annual_salary_krw=ExtractedMoney(value=45000000, quote='연봉 4,500만원'))
    assert row.annual_salary_krw.status == 'estimated'
    assert row.annual_salary_krw.source_text is None


def test_wrong_amount_is_estimated():
    """인용은 맞는데 숫자를 잘못 옮기면(3,600만원 → 3,800만) '추정'이 된다."""
    from backend.normalize.schema import ExtractedMoney

    row = _pangyo_with(
        annual_salary_krw=ExtractedMoney(
            value=38000000, quote='3,600만원 (기본급 기준, 성과급 별도)'
        )
    )
    assert row.annual_salary_krw.status == 'estimated'
    assert row.annual_salary_krw.source_text == '3,600만원 (기본급 기준, 성과급 별도)'


def test_unknown_region_asks_user():
    """코드표에서 지역을 못 찾으면 원문만 담고, 사용자에게 물을 목록에 넣는다."""
    from backend.normalize.schema import ExtractedLocation, LocationGuess

    row = _pangyo_with(
        location=ExtractedLocation(
            value=LocationGuess(sido=None, sigungu='판교'), quote='판교테크노밸리'
        )
    )
    assert row.location.status == 'estimated'
    assert row.location.value.raw == '판교테크노밸리'
    assert row.location.value.lawd_cd is None
    assert REGION_QUESTION in row.questions_to_ask


# ── 원문 대조 도구 ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ('quote', 'amount'),
    [
        ('3,600만원 (기본급 기준)', 36_000_000),
        ('월 20만 원', 200_000),
        ('40,000,000원 (세전, 12개월 균등 분할 지급)', 40_000_000),
        ('월 200,000원 현금 지급', 200_000),
        ('1억 2,000만원', 120_000_000),
        ('연 1.5억', 150_000_000),
    ],
)
def test_amounts_krw(quote, amount):
    assert amount in checks.amounts_krw(quote)


def test_amounts_krw_ignores_non_money_numbers():
    assert checks.amounts_krw('12개월 균등 분할, 주 5일') == set()


def test_months_and_rates():
    assert checks.months_in('입사 후 2년간') == {24}
    assert checks.months_in('수습 3개월') == {3}
    assert checks.rates_in('수습 기간 급여 90%') == {0.9}


@pytest.mark.parametrize(
    ('sido', 'sigungu', 'lawd_cd'),
    [
        ('전남', '나주시', '12170'),
        ('광주', '광산구', '12330'),
        ('경기', '분당구', '41135'),
        ('울산', '남구', '31140'),
        (None, '나주시', '12170'),
    ],
)
def test_find_region(sido, sigungu, lawd_cd):
    assert checks.find_region(sido, sigungu).lawd_cd == lawd_cd


def test_find_region_ambiguous_returns_none():
    """시도 없이 '남구'만 있으면 여러 곳이라 정하지 않는다."""
    assert checks.find_region(None, '남구') is None


# ── Claude 호출부 (가짜 클라이언트로) ──────────────────────────────


class FakeMessages:
    def __init__(self, response=None, error: Exception | None = None):
        self.response, self.error, self.kwargs = response, error, None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def fake_client(messages: FakeMessages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def test_claude_extractor_request_and_result():
    """요청 모양(모델·구조화 출력·fallback)을 확인하고, parsed_output을 그대로 돌려준다."""
    llm = scenario_llm('near_pangyo')
    messages = FakeMessages(SimpleNamespace(stop_reason='end_turn', parsed_output=llm))
    text = scenario_text('near_pangyo')

    assert ClaudeExtractor(fake_client(messages))(text) == llm
    kw = messages.kwargs
    assert kw['model'] == MODEL == 'claude-sonnet-5-5'
    assert kw['output_format'] is ExtractionResult
    assert kw['fallbacks'] == 'default'
    assert kw['messages'] == [{'role': 'user', 'content': text}]


@pytest.mark.parametrize(
    'response',
    [
        SimpleNamespace(stop_reason='refusal', parsed_output=None),
        SimpleNamespace(stop_reason='max_tokens', parsed_output=None),
        SimpleNamespace(stop_reason='end_turn', parsed_output=None),
    ],
)
def test_claude_extractor_bad_stop(response):
    with pytest.raises(NormalizeError):
        ClaudeExtractor(fake_client(FakeMessages(response)))('공고')


def test_claude_extractor_connection_error():
    err = anthropic.APIConnectionError(request=httpx.Request('POST', 'https://example.invalid'))
    with pytest.raises(NormalizeError):
        ClaudeExtractor(fake_client(FakeMessages(error=err)))('공고')


def test_extraction_schema_is_accepted_by_sdk():
    """SDK가 ExtractionResult를 구조화 출력 JSON 스키마로 바꿀 수 있는지 (API 호출 없음)."""
    from anthropic.lib._parse._transform import transform_schema

    schema = transform_schema(ExtractionResult)
    assert 'postings' in schema['properties']


# ── 세션 API까지 ─────────────────────────────────────────────────


def test_session_api_returns_comparison_rows(tmp_path, fake_extractor):
    """세션 API로 판교 공고를 보내면 비교표 행이 스키마대로 나온다."""
    app = create_app(str(tmp_path / 'cp.sqlite'), extractor=fake_extractor)
    with TestClient(app) as c:
        s = c.post('/sessions', json={'message': scenario_text('near_pangyo')}).json()
        assert s['status'] == 'awaiting_confirmation'
        row = s['table'][0]
        assert row['annual_salary_krw'] == {
            'status': 'confirmed',
            'source_text': '3,600만원 (기본급 기준, 성과급 별도)',
            'value': 36000000,
        }
        assert row['location']['value']['lawd_cd'] == '41135'
        assert s['pending']['payload']['table'][0]['id'] == 'posting-1'


def test_openapi_exposes_comparison_row(tmp_path, fake_extractor):
    """프론트 pnpm gen:api가 비교표 타입을 만들 수 있게 OpenAPI에 실려 있다."""
    app = create_app(str(tmp_path / 'cp.sqlite'), extractor=fake_extractor)
    with TestClient(app) as c:
        spec = c.get('/openapi.json').json()
    schemas = spec['components']['schemas']
    for name in ['ComparisonRow', 'MoneyCell', 'LocationCell', 'TextCell', 'BoolCell']:
        assert name in schemas
    assert set(schemas['MoneyCell']['properties']['status']['enum']) == {
        'confirmed',
        'estimated',
        'user_input',
        'missing',
    }
    # LLM 전용 모양은 API에 나가지 않는다
    assert 'ExtractionResult' not in schemas


def test_normalize_failure_is_502(tmp_path):
    """Claude 호출이 실패하면 공통 에러 모양으로 502 NORMALIZE_FAILED."""

    def failing(text):
        raise NormalizeError('공고를 정리하는 중 문제가 생겼습니다.')

    with TestClient(create_app(str(tmp_path / 'cp.sqlite'), extractor=failing)) as c:
        r = c.post('/sessions', json={'message': '공고'})
    assert r.status_code == 502
    assert r.json() == {
        'code': 'NORMALIZE_FAILED',
        'message': '공고를 정리하는 중 문제가 생겼습니다.',
        'details': None,
    }


# ── 월급·단위 혼동·모순 막기 (실제 호출 후 Fable 검토로 추가) ───────────────


def _posting_with(text: str, **changes):
    """빈 공고 답에 원하는 칸만 채워 비교표 행을 만든다."""
    from backend.normalize.schema import ExtractedPosting

    blank = {'value': None, 'quote': None}
    base = {k: blank for k in ExtractedPosting.model_fields if k != 'other_allowances'}
    base['other_allowances'] = []
    posting = ExtractedPosting.model_validate(base).model_copy(update=changes)
    return build_rows(ExtractionResult(postings=[posting]), text)[0]


def test_monthly_salary_is_supported():
    """월급 공고: 월급 칸에 들어가고 확인된다. 연봉 칸은 비워 두고 월급×12도 하지 않는다."""
    from backend.normalize.schema import ExtractedMoney, ExtractedSalaryType

    text = '급여: 월급 250만원 (세전)'
    row = _posting_with(
        text,
        salary_type=ExtractedSalaryType(value='monthly', quote='월급 250만원 (세전)'),
        monthly_salary_krw=ExtractedMoney(value=2_500_000, quote='월급 250만원 (세전)'),
    )
    assert row.salary_type.value == 'monthly'
    assert row.monthly_salary_krw.value == 2_500_000
    assert row.monthly_salary_krw.status == 'confirmed'
    assert row.annual_salary_krw.status == 'missing'
    assert not any('연봉' in q for q in row.questions_to_ask)


def test_monthly_pay_in_annual_field_is_estimated():
    """'월급 250만원'을 연봉 칸에 250만원으로 넣으면 숫자는 맞아도 '추정'이 된다 (단위 혼동)."""
    from backend.normalize.schema import ExtractedMoney

    text = '급여: 월급 250만원 (세전)'
    row = _posting_with(
        text, annual_salary_krw=ExtractedMoney(value=2_500_000, quote='월급 250만원 (세전)')
    )
    assert row.annual_salary_krw.status == 'estimated'
    assert any('세전 연봉' in q for q in row.questions_to_ask)


def test_salary_range_is_not_confirmed():
    """연봉 범위는 금액 하나로 정할 수 없다: 금액 칸에 넣으면 추정, 비워 두면 원문과 함께 묻는다."""
    from backend.normalize.schema import ExtractedMoney, ExtractedSalaryType

    text = '연봉: 3,000~3,500만원 (경력에 따라 협의)'
    row = _posting_with(
        text, annual_salary_krw=ExtractedMoney(value=35_000_000, quote='3,000~3,500만원')
    )
    assert row.annual_salary_krw.status == 'estimated'

    row = _posting_with(text, salary_type=ExtractedSalaryType(value=None, quote='3,000~3,500만원'))
    assert row.salary_type.status == 'missing'
    assert '제시받은 연봉 (공고 표기: 3,000~3,500만원)' in row.questions_to_ask


def test_yearly_amount_in_monthly_field_is_estimated():
    """'교육비 연 100만원'을 월 수당으로 넣으면 추정."""
    from backend.normalize.schema import ExtractedAllowance

    text = '복리후생: 교육비 연 100만원, 통신비 월 5만원'
    row = _posting_with(
        text,
        other_allowances=[
            ExtractedAllowance(name='교육비', monthly_krw=1_000_000, quote='교육비 연 100만원'),
            ExtractedAllowance(name='통신비', monthly_krw=50_000, quote='통신비 월 5만원'),
        ],
    )
    status = {a.name: a.monthly_krw.status for a in row.other_allowances}
    assert status == {'교육비': 'estimated', '통신비': 'confirmed'}


def test_allowance_without_amount_is_dropped():
    """금액이 없는 수당(자격수당 등)은 비교표에 넣지 않는다."""
    from backend.normalize.schema import ExtractedAllowance

    row = _posting_with(
        '자격수당 지급',
        other_allowances=[ExtractedAllowance(name='자격수당', monthly_krw=None, quote='자격수당')],
    )
    assert row.other_allowances == []


def test_true_with_negative_quote_is_estimated():
    """'있음(true)'인데 인용이 '없대'면 모순이라 추정."""
    from backend.normalize.schema import ExtractedBool

    text = '기숙사랑 통근버스는 없대'
    row = _posting_with(text, shuttle_bus=ExtractedBool(value=True, quote=text))
    assert row.shuttle_bus.status == 'estimated'
    row = _posting_with(text, shuttle_bus=ExtractedBool(value=False, quote=text))
    assert row.shuttle_bus.status == 'confirmed'


@pytest.mark.parametrize(
    ('sido', 'sigungu', 'quote', 'lawd_cd', 'status'),
    [
        ('세종', None, '세종시 집현동', '36110', 'confirmed'),
        # 구가 없는 '성남시'는 조회에 못 쓰는 상위 코드 → 코드는 비우고 구를 묻는다
        ('경기', '성남시', '경기 성남시 소재', None, 'estimated'),
        # 원문에 '분당구'가 없는데 '판교'에서 추론한 경우 → 추정
        ('경기', '분당구', '판교테크노밸리', '41135', 'estimated'),
    ],
)
def test_location_edge_cases(sido, sigungu, quote, lawd_cd, status):
    from backend.normalize.schema import ExtractedLocation, LocationGuess

    row = _posting_with(
        f'근무지: {quote}',
        location=ExtractedLocation(value=LocationGuess(sido=sido, sigungu=sigungu), quote=quote),
    )
    assert row.location.value.lawd_cd == lawd_cd
    assert row.location.status == status
    assert (REGION_QUESTION in row.questions_to_ask) == (lawd_cd is None)


def test_questions_are_made_by_code():
    """물어볼 것은 코드가 빈칸을 보고 만든다 (LLM이 아님). 주거·통근 질문은 하지 않는다."""
    assert row_for('stay_ulsan').questions_to_ask == [
        '제시받은 연봉 (공고에 금액 없음)',
        '수습 기간 급여 비율 (공고에 없음)',
    ]
    assert row_for('near_pangyo').questions_to_ask == []
    assert row_for('down_naju').questions_to_ask == [
        '제시받은 연봉 (공고에 금액 없음)',
        '한 달 예상 연장근로 시간 (모르면 0시간으로 계산)',
    ]
    assert row_for('across_gwangju').questions_to_ask == [
        '한 달 예상 연장근로 시간 (모르면 0시간으로 계산)'
    ]


@pytest.mark.parametrize(
    ('quote', 'amount'),
    [
        ('4천만원', 40_000_000),
        ('3천6백만원', 36_000_000),
        ('1억 5천만원', 150_000_000),
        ('5천원', 5_000),
    ],
)
def test_amounts_krw_korean_units(quote, amount):
    assert amount in checks.amounts_krw(quote)


def test_months_year_and_month():
    assert 18 in checks.months_in('1년 6개월')


# ── Fable 재검토로 추가 ─────────────────────────────────────────


@pytest.mark.parametrize(
    'quote',
    [
        '연봉 3,000만원~3,500만원',
        '연봉 2,500만~3,000만원',
        '연봉 3,000만원 이상',
        '3000-3500만원',
    ],
)
def test_range_forms_are_estimated(quote):
    from backend.normalize.schema import ExtractedMoney

    low = checks.amounts_krw(quote)
    value = min(low) if low else 30_000_000
    row = _posting_with(quote, annual_salary_krw=ExtractedMoney(value=value, quote=quote))
    assert row.annual_salary_krw.status == 'estimated'


def test_date_hyphen_is_not_a_range():
    from backend.normalize.schema import ExtractedMoney

    quote = '연봉 4,200만원 (2026-01 기준)'
    row = _posting_with(quote, annual_salary_krw=ExtractedMoney(value=42_000_000, quote=quote))
    assert row.annual_salary_krw.status == 'confirmed'


def test_annual_salary_in_monthly_field_is_estimated():
    from backend.normalize.schema import ExtractedMoney

    quote = '연봉 3,000만원'
    row = _posting_with(quote, monthly_salary_krw=ExtractedMoney(value=30_000_000, quote=quote))
    assert row.monthly_salary_krw.status == 'estimated'


@pytest.mark.parametrize(
    ('quote', 'value'),
    [
        ('연봉 4,000만원(식대 월 20만원 포함)', 40_000_000),
        ('연봉 3,600만원 (월 300만원)', 36_000_000),
    ],
)
def test_annual_quote_with_monthly_breakdown_is_confirmed(quote, value):
    from backend.normalize.schema import ExtractedMoney

    row = _posting_with(quote, annual_salary_krw=ExtractedMoney(value=value, quote=quote))
    assert row.annual_salary_krw.status == 'confirmed'


@pytest.mark.parametrize('quote', ['통근버스 운행 (이용료 없음)', '기숙사 제공(본인 부담 없음)'])
def test_no_cost_is_not_a_contradiction(quote):
    from backend.normalize.schema import ExtractedBool

    row = _posting_with(quote, shuttle_bus=ExtractedBool(value=True, quote=quote))
    assert row.shuttle_bus.status == 'confirmed'


def test_missing_location_asks_user():
    from backend.normalize.rows import LOCATION_MISSING_QUESTION

    row = _posting_with('회사명: 가나다')
    assert row.location.status == 'missing'
    assert LOCATION_MISSING_QUESTION in row.questions_to_ask


def test_estimated_allowance_is_listed_in_questions():
    from backend.normalize.schema import ExtractedAllowance

    text = '교육비 연 100만원'
    row = _posting_with(
        text,
        other_allowances=[ExtractedAllowance(name='교육비', monthly_krw=1_000_000, quote=text)],
    )
    assert any('기타 수당(교육비)' in q for q in row.questions_to_ask)


def test_salary_copied_across_postings_is_estimated():
    """공고 2건: A의 연봉 인용을 B 행도 쓰면 (원문엔 한 번뿐) 둘 다 추정으로 내린다."""
    text = scenario_text('near_pangyo') + '\n\n' + scenario_text('down_naju')
    pangyo = scenario_llm('near_pangyo').postings[0]
    naju = scenario_llm('down_naju').postings[0]
    wrong_naju = naju.model_copy(update={'annual_salary_krw': pangyo.annual_salary_krw})
    rows = build_rows(ExtractionResult(postings=[pangyo, wrong_naju]), text)
    assert [r.annual_salary_krw.status for r in rows] == ['estimated', 'estimated']

    # 정상(각자 자기 인용)이면 그대로 확인
    rows = build_rows(ExtractionResult(postings=[pangyo, naju]), text)
    assert rows[0].annual_salary_krw.status == 'confirmed'
    assert rows[1].annual_salary_krw.status == 'missing'


def test_hiring_track():
    """채용 구분(신입/경력)은 고용형태와 따로 담는다."""
    assert {pid: row_for(pid).hiring_track.value for pid in SCENARIOS} == {
        'stay_ulsan': '신입',
        'up_seoul': '신입',
        'across_gwangju': '경력',
        'near_pangyo': '신입',
        'down_naju': '신입',
    }
    gwangju = row_for('across_gwangju')
    assert gwangju.hiring_track.status == 'confirmed'
    assert gwangju.employment_type.status == 'missing'  # '경력직'은 고용형태가 아니다
