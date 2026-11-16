"""월 잔여액 계산 테스트 (#8).

공고는 data/demo의 가상 공고(fixture LLM 응답 → build_rows), 사용자는 data/demo/users의
가상 사용자 값을 쓴다. 세후 월급 자체의 정확성은 test_calc.py가 확인하므로,
여기서는 '세후 월급 + 수당 − 주거비 − 교통비 − 고정비'를 손으로 계산한 값과 비교한다.

[실행] backend 폴더에서 uv run pytest tests/test_remaining.py
"""

import json

import pytest
from conftest import ROOT, FakeExtractor, scenario_llm, scenario_text
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from backend.calc import monthly_net, monthly_net_from_annual
from backend.calc.remaining import (
    UserProfile,
    monthly_remaining,
    weakest,
)
from backend.calc.summary import calculate_all
from backend.graph.builder import build_graph
from backend.normalize import build_rows
from backend.normalize.schema import ComparisonRow

USERS = ROOT / 'data' / 'demo' / 'users'

# 판교: 연봉 3,600만 ÷ 12 + 식대 20만(별도, 비과세) + 통신비 5만(과세 수당)
PANGYO_NET = monthly_net(3_000_000 + 200_000 + 50_000, nontaxable_monthly=200_000).net


def row_of(pid: str) -> ComparisonRow:
    """가상 공고 하나를 비교표 한 행으로 (normalize 노드와 같은 경로, LLM은 fixture)."""
    text = scenario_text(pid)
    return build_rows(scenario_llm(pid), text, None)[0]


def user_a_profile() -> dict:
    """가상 사용자 A(서울 관악구 원룸)의 json을 UserProfile 모양으로 옮긴다."""
    u = json.loads((USERS / 'user_a.json').read_text(encoding='utf-8'))
    home = u['current_residence']
    pangyo = u['scenario_inputs']['near_pangyo']
    return {
        'current_housing': {
            'value': home['monthly_rent_krw'] + home['maintenance_fee_krw'],  # 520,000 + 70,000
            'source': '관악구 원룸 월세 + 관리비',
        },
        'fixed_expenses': [
            {'name': k, 'monthly_krw': v} for k, v in u['fixed_expenses_monthly_krw'].items()
        ],  # 150,000 + 55,000
        'postings': {
            'posting-1': {
                'commute_transport': {
                    'value': pangyo['commute_from_current_home_transport_monthly_krw']  # 110,000
                },
                # 판교 근처 월세는 실거래가 추정값이라고 가정한 값 (월세 API는 다른 이슈)
                'move_housing': {
                    'value': 600_000,
                    'status': 'estimated',
                    'source': '성남시 분당구 실거래가 중앙값(가정)',
                },
                'move_transport': {'value': u['current_transport_monthly_krw']},  # 62,000
            }
        },
    }


def by_case(result) -> dict:
    return {s.case: s for s in result.scenarios}


# ── 1. 판교(연봉 공개) + 사용자 A: 두 경우 모두 계산 ───────────────


def test_pangyo_user_a_both_cases():
    r = monthly_remaining(row_of('near_pangyo'), UserProfile.model_validate(user_a_profile()))
    net = PANGYO_NET  # 세금 계산 자체는 test_calc.py가 검증
    assert r.net_income.value == net
    assert r.payslip['gross'] == 3_250_000
    assert r.net_income.status == 'confirmed'
    assert r.allowances.value == 50_000  # 통신비
    assert r.fixed_expenses.value == 205_000

    cases = by_case(r)
    commute, move = cases['commute'], cases['move']
    assert commute.monthly_remaining == net - 590_000 - 110_000 - 205_000
    assert move.monthly_remaining == net - 600_000 - 62_000 - 205_000
    assert commute.missing == move.missing == []
    # 신뢰도: 통근은 사용자 입력이 가장 약하고, 이사는 월세 추정값 때문에 '추정'
    assert commute.status == 'user_input'
    assert move.status == 'estimated'
    assert move.housing.source == '성남시 분당구 실거래가 중앙값(가정)'


# ── 2. 연봉 비공개(나주) → 잔여액은 null, 없는 값을 알려 줌 ─────────


def test_undisclosed_salary_gives_null_and_missing():
    r = monthly_remaining(row_of('down_naju'), UserProfile.model_validate(user_a_profile()))
    assert r.net_income.value is None
    assert r.net_income.status == 'missing'
    for s in r.scenarios:
        assert s.monthly_remaining is None
        assert s.status == 'missing'
        assert '세후 월급' in s.missing


def test_naju_with_user_salary_subtracts_housing_allowance():
    """사용자가 확인 화면에서 연봉 칸만 채워도 계산된다 (연봉 종류는 '비공개' 그대로).

    이사 주거비에서 주거지원비 20만을 빼고, 서울→나주 통근은 해당 없음으로 둔다.
    """
    row = row_of('down_naju')
    assert row.salary_type.value == 'undisclosed'
    row.annual_salary_krw.value, row.annual_salary_krw.status = 38_000_000, 'user_input'
    profile = UserProfile.model_validate(
        {
            'current_housing': {'value': 590_000},
            'fixed_expenses': [],
            'default_costs': {
                'commute_possible': False,
                'move_housing': {'value': 450_000, 'status': 'estimated'},
                'move_transport': {'value': 100_000},  # 월 1회 서울 왕복
            },
        }
    )
    r = monthly_remaining(row, profile)
    net = monthly_net_from_annual(38_000_000).net
    commute, move = by_case(r)['commute'], by_case(r)['move']
    assert commute.applicable is False
    assert commute.monthly_remaining is None  # 통근 불가 → 숫자를 만들지 않음
    assert move.applicable is True
    assert move.housing.value == 250_000  # 450,000 − 200,000
    assert move.monthly_remaining == net - 250_000 - 100_000
    assert r.net_income.status == 'user_input'
    assert any('24개월' in n for n in r.notes)  # 지원 기간이 끝난다는 안내
    assert any('90%' in n for n in r.notes)  # 수습 급여 90%


def test_unknown_housing_period_is_counted_as_continuing_and_noted():
    """지원 기간을 모르면 남는 돈은 계속 받는 것으로 계산하고, 끝날 수 있다고 알린다."""
    row = row_of('down_naju')
    row.annual_salary_krw.value = 38_000_000
    row.housing_allowance_months.value = None
    profile = UserProfile.model_validate({'default_costs': {'move_housing': {'value': 450_000}}})
    r = monthly_remaining(row, profile)
    assert by_case(r)['move'].housing.value == 250_000  # 450,000 − 200,000
    assert any('지원 기간을 몰라 계속 받는 것으로' in n for n in r.notes)


@pytest.mark.parametrize(
    'profile',
    [
        None,  # 사용자 정보 없음
        {
            'default_costs': {'move_possible': False, 'move_housing': {'value': 450_000}}
        },  # 이사 안 함
        {'default_costs': {}},  # 이사할 집 주거비 없음
    ],
)
def test_housing_aid_note_only_when_aid_was_subtracted(profile):
    """남는 돈에서 주거지원비를 실제로 뺐을 때만 '계속 받는 것으로 계산했어요'라고 알린다."""
    row = row_of('down_naju')
    row.annual_salary_krw.value = 38_000_000
    row.housing_allowance_months.value = None
    user = UserProfile.model_validate(profile) if profile is not None else None
    assert not any('주거지원비' in n for n in monthly_remaining(row, user).notes)


def test_housing_allowance_never_makes_housing_negative():
    row = row_of('down_naju')
    profile = UserProfile.model_validate({'default_costs': {'move_housing': {'value': 150_000}}})
    assert by_case(monthly_remaining(row, profile))['move'].housing.value == 0


# ── 3. 사용자 정보가 없거나 일부만 있을 때 ──────────────────────────


def test_no_profile_keeps_net_income_but_no_remaining():
    r = monthly_remaining(row_of('near_pangyo'), None)
    assert r.net_income.value is not None
    commute = by_case(r)['commute']
    assert commute.monthly_remaining is None
    assert set(commute.missing) == {'고정비', '주거비 (지금 집)', '교통비 (통근)'}


def test_missing_values_are_never_filled_with_zero():
    """주거비만 있고 교통비가 없으면 0원으로 치지 않고 null."""
    profile = UserProfile.model_validate({'current_housing': {'value': 500_000}})
    commute = by_case(monthly_remaining(row_of('near_pangyo'), profile))['commute']
    assert commute.monthly_remaining is None
    assert commute.missing == ['고정비', '교통비 (통근)']
    assert commute.status == 'missing'


def test_empty_fixed_expenses_list_means_zero():
    """고정비를 물어봐서 '없다'고 답했으면([]) 0원, 아직 모르면(null) 계산하지 않는다."""
    asked = UserProfile.model_validate({'fixed_expenses': []})
    assert monthly_remaining(row_of('near_pangyo'), asked).fixed_expenses.value == 0
    unknown = UserProfile()
    assert monthly_remaining(row_of('near_pangyo'), unknown).fixed_expenses.value is None


def test_company_housing_without_cost_is_missing():
    row = row_of('near_pangyo')
    row.company_housing.value = True
    move = by_case(monthly_remaining(row, UserProfile()))['move']
    assert move.housing.value is None
    assert '사택' in move.housing.source


def test_posting_costs_fall_back_to_default_per_field():
    profile = UserProfile.model_validate(
        {
            'default_costs': {'move_transport': {'value': 50_000}},
            'postings': {'posting-1': {'move_housing': {'value': 400_000}}},
        }
    )
    costs = profile.costs_for('posting-1')
    assert costs.move_housing.value == 400_000
    assert costs.move_transport.value == 50_000  # 공고별 값이 없으면 기본값
    assert profile.costs_for('posting-9').move_housing is None


def test_remaining_can_be_negative():
    profile = UserProfile.model_validate(
        {
            'current_housing': {'value': 5_000_000},
            'fixed_expenses': [],
            'default_costs': {'commute_transport': {'value': 0}},
        }
    )
    commute = by_case(monthly_remaining(row_of('near_pangyo'), profile))['commute']
    assert commute.monthly_remaining < 0


# ── 4. 월급형 공고, 신뢰도 합치기 ──────────────────────────────────


def test_monthly_salary_posting():
    row = ComparisonRow.model_validate(
        {
            'id': 'posting-1',
            'salary_type': {'value': 'monthly', 'status': 'confirmed'},
            'monthly_salary_krw': {'value': 2_500_000, 'status': 'estimated'},
        }
    )
    r = monthly_remaining(row, None)
    assert r.net_income.value == r.payslip['net']
    assert r.payslip['gross'] == 2_500_000
    assert r.net_income.status == 'estimated'  # 월급 칸이 추정이면 결과도 추정


def test_meals_provided_removes_meal_exemption():
    """식사를 현물로 주면 현금 식대는 과세 (across_gwangju expected.json 메모와 같은 규칙)."""
    row = row_of('near_pangyo')
    row.meals_provided.value = True
    r = monthly_remaining(row, None)
    assert r.payslip['nontaxable'] == 0
    assert r.net_income.value == monthly_net(3_250_000, 0).net
    assert r.net_income.value < PANGYO_NET


def test_zero_salary_is_treated_as_missing():
    row = row_of('near_pangyo')
    row.annual_salary_krw.value = 0
    assert monthly_remaining(row, None).net_income.value is None


def test_zero_months_housing_allowance_is_not_subtracted():
    row = row_of('down_naju')
    row.housing_allowance_months.value = 0
    profile = UserProfile.model_validate({'default_costs': {'move_housing': {'value': 450_000}}})
    assert by_case(monthly_remaining(row, profile))['move'].housing.value == 450_000


def test_no_profile_net_income_is_at_most_estimated():
    """가족 수를 몰라 1인 가구로 계산했으니 '확인'으로 표시하지 않는다."""
    assert monthly_remaining(row_of('near_pangyo'), None).net_income.status == 'estimated'


def test_weakest():
    assert weakest([]) == 'confirmed'
    assert weakest(['confirmed', 'user_input']) == 'user_input'
    assert weakest(['user_input', 'estimated', 'confirmed']) == 'estimated'
    assert weakest(['estimated', 'missing']) == 'missing'


@pytest.mark.parametrize(
    'bad',
    [
        {'current_housing': {'value': -1}},  # 음수 금액
        {'current_housing': {'value': 0, 'status': 'missing'}},  # 값 없음인데 0원으로 계산될 뻔
        {'family': 1, 'children_8_to_20': 1},  # 자녀 수 ≥ 가족 수 → 세액표에 없음
    ],
)
def test_invalid_profile_is_rejected(bad):
    with pytest.raises(ValueError):
        UserProfile.model_validate(bad)


# ── 5. calculate_all: 잘못된 입력이 와도 멈추지 않음 ──────────────


def test_calculate_all_skips_bad_parts():
    good = row_of('near_pangyo').model_dump(mode='json')
    out = calculate_all([good, {'id': 'posting-2', 'annual_salary_krw': 'abc'}], {'family': 0})
    assert out['rows'] == 2
    assert out['has_user_profile'] is False  # family=0은 잘못된 값 → 사용 안 함
    assert len(out['postings']) == 1
    assert len(out['errors']) == 2
    json.dumps(out)  # 체크포인터에 저장할 수 있는 평범한 dict


def test_calculate_all_handles_empty_table():
    assert calculate_all(None, None)['postings'] == []
    assert calculate_all([], None)['rows'] == 0


# ── 6. 완료 조건: 그래프를 끝까지 돌리면 공고별·경우별 잔여액이 결과에 있음 ──


def test_graph_end_to_end_has_remaining_per_posting_and_case():
    graph = build_graph(InMemorySaver(), FakeExtractor())
    config = {'configurable': {'thread_id': 't1'}}
    graph.invoke({'messages': [scenario_text('near_pangyo')]}, config)
    # 사용자 정보는 비교표 확인 때 함께 보낸다 (API의 /confirm과 같은 모양)
    state = graph.invoke(Command(resume={'approved': True, 'profile': user_a_profile()}), config)

    result = state['result']
    assert result['has_user_profile'] is True
    [posting] = result['postings']
    assert posting['company'] == '(가상) 이음데이터랩(주)'
    remaining = {s['case']: s['monthly_remaining'] for s in posting['scenarios']}
    net = PANGYO_NET
    assert remaining == {
        'commute': net - 590_000 - 110_000 - 205_000,
        'move': net - 600_000 - 62_000 - 205_000,
    }


# ── 7. 2차 검수에서 찾은 경계 사례 ─────────────────────────────────


def test_value_with_default_missing_status_is_estimated():
    """금액은 있는데 status가 기본값(missing)이면 '추정'으로 본다 (숫자에 missing이 붙지 않게)."""
    row = ComparisonRow.model_validate(
        {'id': 'posting-1', 'annual_salary_krw': {'value': 36_000_000}}
    )
    profile = UserProfile.model_validate(
        {
            'current_housing': {'value': 500_000},
            'fixed_expenses': [],
            'default_costs': {'commute_transport': {'value': 50_000}},
        }
    )
    r = monthly_remaining(row, profile)
    commute = by_case(r)['commute']
    assert r.net_income.status == 'estimated'
    assert commute.monthly_remaining is not None
    assert commute.status == 'estimated'


@pytest.mark.parametrize('annual', [5, 1_199_999])  # 월 기본급 10만원 미만
def test_tiny_salary_is_not_calculated(annual):
    row = row_of('near_pangyo')
    row.annual_salary_krw.value = annual
    r = monthly_remaining(row, None)
    assert r.net_income.value is None
    assert '단위' in r.net_income.source


def test_estimated_meals_provided_lowers_confidence():
    row = row_of('near_pangyo')
    row.meals_provided.value, row.meals_provided.status = True, 'estimated'
    assert monthly_remaining(row, UserProfile()).net_income.status == 'estimated'


def test_calculation_error_shows_fixed_friendly_text(monkeypatch):
    """계산 중 예외가 나도 개발자용 예외 글(합쇼체·원 단위 숫자)은 화면으로 나가지 않는다."""
    import backend.calc.remaining as remaining

    def broken(*args, **kwargs):
        raise ValueError('간이세액표에서 구간을 찾지 못했습니다: 123456789')

    monkeypatch.setattr(remaining, 'monthly_net', broken)
    r = monthly_remaining(row_of('near_pangyo'), None)
    assert r.net_income.value is None
    shown = [r.net_income.source, *r.notes]
    assert not any('습니다' in text or '123456789' in text for text in shown)
    assert '세후 월급을 계산하지 못했어요' in r.net_income.source
