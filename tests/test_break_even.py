"""손익분기 연봉 테스트 (#14).

시나리오: 가상 사용자 B (울산 남구 본가, data/demo/users/user_b.json)
  - up_seoul  : 서울 강서구, 연봉 4,000만원 (식대 포함) → 이사해야 함 (울산에서 통근 불가)
  - stay_ulsan: 울산 남구, 연봉 비공개 → 본가에서 통근 (통근버스), 이사는 고려 안 함
"울산은 얼마면 남는 돈이 비슷할까요?"에 답하는 것이 목표다.

[실행] backend 폴더에서 uv run pytest tests/test_break_even.py
"""

import json

import pytest
from conftest import ROOT, scenario_llm, scenario_text

from backend.calc import monthly_net
from backend.calc.break_even import (
    MAX_ANNUAL_KRW,
    RECHECK_STEPS,
    STEP_KRW,
    break_even,
    pick_baseline,
)
from backend.calc.remaining import UserProfile, monthly_remaining
from backend.calc.summary import calculate_all
from backend.normalize import build_rows

USERS = ROOT / 'data' / 'demo' / 'users'


def row_of(pid: str, row_id: str):
    row = build_rows(scenario_llm(pid), scenario_text(pid), None)[0]
    row.id = row_id
    return row


def user_b_profile() -> dict:
    u = json.loads((USERS / 'user_b.json').read_text(encoding='utf-8'))
    home = u['scenario_inputs']['stay_ulsan']
    return {
        # 본가에서 쓰는 돈 = 부모님께 30만 + 본인 식비 25만 (질문 카드 답)
        'current_housing': {
            'value': home['to_parents_monthly_krw'] + home['own_food_monthly_krw'],
            'source': '본가 생활비 + 식비',
        },
        'fixed_expenses': [],
        'postings': {
            'seoul': {
                'commute_possible': False,  # 울산 → 서울 통근 불가
                'move_housing': {
                    'value': 500_000,
                    'status': 'estimated',
                    'source': '강서구 원룸급 월세 중앙값 (data/demo/README.md)',
                },
                'move_transport': {'value': 70_000},
            },
            'ulsan': {
                'commute_transport': {'value': 0, 'source': '통근버스 (무료)'},
                'move_possible': False,
            },
        },
    }


def rows():
    return [row_of('up_seoul', 'seoul'), row_of('stay_ulsan', 'ulsan')]


def remaining_at(row, profile, annual, case):
    """테스트용: 연봉 annual일 때 해당 경우의 월 잔여액을 직접 계산."""
    copy = row.model_copy(deep=True)
    copy.annual_salary_krw.value = annual
    return next(
        s.monthly_remaining for s in monthly_remaining(copy, profile).scenarios if s.case == case
    )


def assert_is_break_even(row, profile, annual, target, case='commute'):
    """annual이 손익분기 연봉인지: 만 원 적으면 모자라고, 그 위 300만 원까지는 늘 기준 이상."""
    assert remaining_at(row, profile, annual - STEP_KRW, case) < target
    for k in range(RECHECK_STEPS + 1):
        y = annual + k * STEP_KRW
        assert remaining_at(row, profile, y, case) >= target, y


# ── 1. 울산 시나리오: 손익분기 연봉 ─────────────────────────────────


def test_ulsan_break_even_against_seoul():
    profile = UserProfile.model_validate(user_b_profile())
    seoul, ulsan = rows()
    results = [monthly_remaining(r, profile) for r in (seoul, ulsan)]

    baseline, notes = pick_baseline(results, profile)
    assert notes == []
    assert baseline.kind == 'posting'
    assert baseline.posting_id == 'seoul'
    assert baseline.case == 'move'
    # 서울 세후 월급 − 월세 50만 − 교통비 7만
    seoul_net = monthly_net(40_000_000 // 12, 200_000).net
    assert baseline.monthly_remaining == seoul_net - 500_000 - 70_000
    assert baseline.status == 'estimated'  # 월세가 추정값

    be = break_even(ulsan, profile, baseline)
    commute, move = be.cases
    assert move.annual_salary_krw is None
    assert move.reason == '해당 없는 경우'

    annual = commute.annual_salary_krw
    assert annual % STEP_KRW == 0
    assert_is_break_even(ulsan, profile, annual, baseline.monthly_remaining)
    assert commute.monthly_remaining == remaining_at(ulsan, profile, annual, 'commute')
    # 본가 생활비(55만)는 서울 월세+교통비(57만)보다 2만 원 싸지만,
    # 서울은 식대 20만 원이 비과세이고 울산은 식사를 현물로 줘 비과세 식대가 없다.
    # 세금·보험료가 그만큼 더 나가서 울산은 4,000만 원보다 조금 더 받아야 같은 돈이 남는다.
    assert 40_000_000 < annual < 41_000_000
    assert commute.status == 'estimated'  # 기준(서울 월세 추정)의 신뢰도를 따른다


def test_calculate_all_includes_break_even_only_for_undisclosed():
    out = calculate_all([r.model_dump(mode='json') for r in rows()], user_b_profile())
    assert out['errors'] == []
    [be] = out['break_even']
    assert be['posting_id'] == 'ulsan'
    assert be['baseline']['posting_id'] == 'seoul'
    assert be['cases'][0]['annual_salary_krw'] is not None
    json.dumps(out)


# ── 2. 기준 고르기 ────────────────────────────────────────────────


def test_baseline_posting_id_is_respected_and_bad_id_falls_back():
    profile = user_b_profile()
    table = [r.model_dump(mode='json') for r in rows()]

    profile['baseline_posting_id'] = 'seoul'
    assert calculate_all(table, profile)['break_even'][0]['baseline']['posting_id'] == 'seoul'

    profile['baseline_posting_id'] = 'ulsan'  # 비공개라 잔여액이 없음 → 가장 큰 공고로
    out = calculate_all(table, profile)
    assert out['break_even'][0]['baseline']['posting_id'] == 'seoul'
    assert 'ulsan' in out['break_even_note']


def test_unavailable_baseline_case_is_named_in_note():
    """서울은 통근 불가 → '서울 · 통근'을 기준으로 고르면 그 경우가 없다고 알리고 이사와 비교."""
    profile = {**user_b_profile(), 'baseline_posting_id': 'seoul', 'baseline_case': 'commute'}
    out = calculate_all([r.model_dump(mode='json') for r in rows()], profile)
    assert out['break_even'][0]['baseline']['case'] == 'move'
    assert '공고 seoul · 지금 집에서 통근' in out['break_even_note']


def test_baseline_case_alone_filters_automatic_choice():
    from test_demo_scenarios import profile_a
    from test_demo_scenarios import row_of as demo_row

    table = [demo_row(p).model_dump(mode='json') for p in ('near_pangyo', 'down_naju')]
    out = calculate_all(table, {**profile_a(), 'baseline_case': 'commute'})
    assert out['break_even'][0]['baseline']['case'] == 'commute'


def test_housing_support_note_only_on_move_case():
    """주거지원비 안내는 이사 경우에만 붙는다 (통근 경우 계산에는 들어가지 않음)."""
    profile = user_b_profile()
    profile['target_monthly_remaining'] = 1_500_000
    profile['postings']['ulsan'] = {
        'commute_transport': {'value': 0},
        'move_housing': {'value': 500_000},
        'move_transport': {'value': 50_000},
    }
    _, ulsan = rows()
    ulsan.housing_allowance_monthly_krw.value = 200_000
    ulsan.housing_allowance_months.value = 12
    user = UserProfile.model_validate(profile)
    commute, move = break_even(ulsan, user, pick_baseline([], user)[0]).cases
    assert not any('주거지원비' in n for n in commute.notes)
    assert commute.during_support_annual_salary_krw is None
    assert any('주거지원비' in n for n in move.notes)


def test_baseline_case_can_be_chosen():
    """기준 공고에서 '이사'가 아니라 '통근'을 기준으로 고를 수도 있다 (사용자 A 판교)."""
    from test_demo_scenarios import profile_a
    from test_demo_scenarios import row_of as demo_row

    table = [demo_row(p).model_dump(mode='json') for p in ('near_pangyo', 'down_naju')]
    for case in ('commute', 'move'):
        profile = {**profile_a(), 'baseline_posting_id': 'near_pangyo', 'baseline_case': case}
        assert calculate_all(table, profile)['break_even'][0]['baseline']['case'] == case


def test_no_baseline_explains_what_to_ask():
    """공개 공고·지금 회사·목표 금액이 다 없으면 계산하지 않고 무엇이 필요한지 알려 준다."""
    out = calculate_all([rows()[1].model_dump(mode='json')], user_b_profile())
    assert out['break_even'] == []
    assert '지금 연봉' in out['break_even_note']


def test_incomplete_current_job_says_what_is_missing():
    """지금 연봉은 줬는데 교통비가 없으면, '연봉을 알려 달라'가 아니라 교통비가 없다고 알린다."""
    profile = user_c_profile()
    del profile['current_job']['transport']
    out = calculate_all([row_of('across_gwangju', 'gwangju').model_dump(mode='json')], profile)
    assert out['break_even'] == []
    assert '지금 회사까지 교통비' in out['break_even_note']
    assert '지금 연봉이나' not in out['break_even_note']


def test_incomplete_current_job_is_reported_even_with_posting_baseline():
    """공개 공고가 기준이 되더라도, 지금 회사를 못 쓴 이유는 알린다 (조용히 빠지지 않게)."""
    profile = user_b_profile()
    profile['current_job'] = {'annual_salary_krw': 90_000_000}  # 교통비 없음
    out = calculate_all([r.model_dump(mode='json') for r in rows()], profile)
    assert out['break_even'][0]['baseline']['kind'] == 'posting'
    assert '지금 회사까지 교통비' in out['break_even_note']


def test_no_profile_explains_why_no_break_even():
    out = calculate_all([r.model_dump(mode='json') for r in rows()], None)
    assert out['break_even'] == []
    assert '사용자 정보' in out['break_even_note']


# ── 2-1. 두 공고 모두 비공개일 때: 지금 회사 / 목표 금액이 기준 ────────


def user_c_profile() -> dict:
    """가상 사용자 C (울산 북구 원룸 → 광주 이직). 지금 연봉은 급여명세서 값."""
    u = json.loads((USERS / 'user_c.json').read_text(encoding='utf-8'))
    slip = u['expected_from_files']['payslip']
    return {
        'current_housing': {'value': u['current_residence']['monthly_rent_krw']},  # 400,000
        'fixed_expenses': [],
        'current_job': {
            'annual_salary_krw': slip['fixed_annual_krw'],  # 37,999,920 (식대 포함)
            'meal_nontaxable_monthly_krw': slip['meal_nontaxable'],  # 200,000
            'transport': {'value': 60_000},
            'source': '2026년 10월 급여명세서',
        },
        'default_costs': {
            'commute_possible': False,  # 울산 → 광주 통근 불가
            'move_housing': {'value': 420_000, 'status': 'estimated'},  # 광산구 중앙값
            'move_transport': {'value': 50_000},
        },
    }


def test_current_job_is_baseline_when_postings_are_undisclosed():
    gwangju = row_of('across_gwangju', 'gwangju')
    stay = row_of('stay_ulsan', 'ulsan')
    assert gwangju.salary_type.value == 'to_propose'
    profile = user_c_profile()
    table = [gwangju.model_dump(mode='json'), stay.model_dump(mode='json')]
    out = calculate_all(table, profile)

    assert out['break_even_note'] is None
    assert len(out['break_even']) == 2  # 둘 다 비공개 → 둘 다 손익분기 연봉
    be = out['break_even'][0]
    assert be['baseline']['kind'] == 'current_job'
    # 지금 세후 월급 − 월세 40만 − 교통비 6만
    now = monthly_net(37_999_920 // 12, 200_000).net - 400_000 - 60_000
    assert be['baseline']['monthly_remaining'] == now
    move = be['cases'][1]
    user = UserProfile.model_validate(profile)
    assert_is_break_even(gwangju, user, move['annual_salary_krw'], now, case='move')


def test_target_amount_is_used_first():
    profile = {**user_b_profile(), 'target_monthly_remaining': 1_500_000}
    out = calculate_all([r.model_dump(mode='json') for r in rows()], profile)
    baseline = out['break_even'][0]['baseline']
    assert baseline['kind'] == 'target'
    assert baseline['monthly_remaining'] == 1_500_000


# ── 3. 못 구하는 경우 ─────────────────────────────────────────────


def test_missing_costs_are_reported():
    profile = UserProfile.model_validate({**user_b_profile(), 'current_housing': None})
    seoul, ulsan = rows()
    baseline, _ = pick_baseline([monthly_remaining(seoul, profile)], profile)
    commute = break_even(ulsan, profile, baseline).cases[0]
    assert commute.annual_salary_krw is None
    assert commute.missing == ['주거비 (지금 집)']
    assert commute.status == 'missing'


def test_unreachable_target():
    profile = UserProfile.model_validate(user_b_profile())
    seoul, ulsan = rows()
    seoul.annual_salary_krw.value = 50 * MAX_ANNUAL_KRW  # 기준이 비현실적으로 큼
    baseline, _ = pick_baseline([monthly_remaining(seoul, profile)], profile)
    commute = break_even(ulsan, profile, baseline).cases[0]
    assert commute.annual_salary_krw is None
    assert '억 원으로도' in commute.reason


def test_original_row_is_not_modified():
    profile = UserProfile.model_validate(user_b_profile())
    seoul, ulsan = rows()
    before = ulsan.model_dump()
    break_even(ulsan, profile, pick_baseline([monthly_remaining(seoul, profile)], profile)[0])
    assert ulsan.model_dump() == before


# ── 4. 지원 기간·최저임금·신뢰도 ────────────────────────────────────


def test_temporary_housing_support_gives_after_support_answer():
    """주거지원비가 6개월만 나오면, 기본 답은 지원이 끝난 뒤에도 기준만큼 남는 연봉이다."""
    profile = UserProfile.model_validate(user_b_profile())
    _, ulsan = rows()
    ulsan.housing_allowance_monthly_krw.value = 300_000
    ulsan.housing_allowance_months.value = 6
    p = {**user_b_profile(), 'target_monthly_remaining': 2_000_000}
    p['postings']['ulsan'] = {
        'commute_possible': False,
        'move_housing': {'value': 500_000},
        'move_transport': {'value': 50_000},
    }
    profile = UserProfile.model_validate(p)
    baseline, _ = pick_baseline([], profile)
    move = break_even(ulsan, profile, baseline).cases[1]
    assert move.during_support_annual_salary_krw < move.annual_salary_krw
    assert move.support_excluded is True
    assert move.floor is None
    assert any('6개월 뒤 끝나므로' in n for n in move.notes)
    # 지원을 뺀 행에서 손익분기 조건이 참이다
    plain = ulsan.model_copy(deep=True)
    plain.housing_allowance_monthly_krw.value = None
    assert_is_break_even(plain, profile, move.annual_salary_krw, 2_000_000, case='move')


def test_any_salary_enough_is_raised_to_minimum_wage():
    """기준이 너무 낮아 어떤 연봉이든 충분하면, '최소 120만 원' 대신 최저임금 연봉을 보여 준다."""
    profile = UserProfile.model_validate({**user_b_profile(), 'target_monthly_remaining': 0})
    _, ulsan = rows()
    baseline, _ = pick_baseline([], profile)
    commute = break_even(ulsan, profile, baseline).cases[0]
    assert commute.annual_salary_krw == 25_890_000  # 최저임금 연 25,882,560원을 만 원 단위로 올림
    assert commute.floor == 'minimum_wage'  # 화면이 '최저임금 수준만 받아도…'로 쓴다


def test_status_includes_estimated_allowance():
    """연봉과 관계없는 수당이 '추정'이면 손익분기 연봉도 '추정'이다."""
    profile = UserProfile.model_validate(
        {**user_b_profile(), 'target_monthly_remaining': 2_000_000}
    )
    _, ulsan = rows()
    ulsan.transport_allowance_monthly_krw.value = 300_000
    ulsan.transport_allowance_monthly_krw.status = 'estimated'
    baseline, _ = pick_baseline([], profile)
    assert break_even(ulsan, profile, baseline).cases[0].status == 'estimated'


# ── 5. 세금이 뛰는 구간 ─────────────────────────────────────────────


def test_tax_jump_does_not_fool_the_search():
    """월급 376만 원에서 세금이 7,360원 뛰어, 연봉이 1만 원 올라도 실수령액이 줄어든다.

    기준을 '연봉 4,511만 원일 때 남는 돈'으로 잡으면 4,511만 원은 기준을 채우지만
    4,512만 원(월 376만 원)은 모자란다. 이분 탐색만 하면 4,511만 원을 답으로 낼 수 있는데,
    그러면 '이 금액 이상이면 늘 같은 돈이 남는다'가 거짓이 된다.
    """
    assert monthly_net(45_120_000 // 12).net < monthly_net(45_110_000 // 12).net
    _, ulsan = rows()
    base = UserProfile.model_validate(user_b_profile())
    target = remaining_at(ulsan, base, 45_110_000, 'commute')
    profile = UserProfile.model_validate({**user_b_profile(), 'target_monthly_remaining': target})
    baseline, _ = pick_baseline([], profile)
    annual = break_even(ulsan, profile, baseline).cases[0].annual_salary_krw
    assert annual > 45_120_000
    assert_is_break_even(ulsan, profile, annual, target)


ULSAN_COSTS = 550_000  # 사용자 B 울산 통근: 본가 55만 + 교통비 0 + 고정비 0


@pytest.mark.parametrize('dip_annual', [45_110_000, 70_090_000, 119_990_000, 140_000_000])
def test_matches_full_scan_up_to_200m(dip_annual):
    """알고리즘과 다른 방법(2억 원까지 만 원씩 전부 계산)으로 답이 같은지 확인한다.

    울산 통근은 식대·수당이 없어 월 잔여액 = 세후 월급(연봉 ÷ 12) − 55만 원이다.
    기준을 세금이 뛰는 곳 바로 앞(월 376만·584만·1,000만 원)과 그 밖 한 곳에 둔다.
    답 = '모자라는 가장 높은 연봉 + 1만 원'.
    """
    _, ulsan = rows()
    target = monthly_net(dip_annual // 12).net - ULSAN_COSTS
    profile = UserProfile.model_validate({**user_b_profile(), 'target_monthly_remaining': target})
    assert remaining_at(ulsan, profile, dip_annual, 'commute') == target  # 위 식이 맞는지

    short = [
        a
        for a in range(12_000_000, 200_000_001, STEP_KRW)
        if monthly_net(a // 12).net - ULSAN_COSTS < target
    ]
    expected = max(short) + STEP_KRW
    baseline, _ = pick_baseline([], profile)
    assert break_even(ulsan, profile, baseline).cases[0].annual_salary_krw == expected


def _max_recovery_steps(nets: list[int]) -> int:
    """각 연봉에서 '그보다 높은데 실수령액이 적은 연봉'까지의 가장 먼 거리(칸 수)."""
    suffix_min = nets[:]
    for j in range(len(nets) - 2, -1, -1):
        suffix_min[j] = min(nets[j], suffix_min[j + 1])
    worst = 0
    for i, n in enumerate(nets):
        # suffix_min은 오른쪽으로 갈수록 커진다 → net[j] < n인 가장 오른쪽 j를 이분 탐색
        lo, hi = i, len(nets)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if suffix_min[mid] < n:
                lo = mid
            else:
                hi = mid
        worst = max(worst, lo - i)
    return worst


@pytest.mark.parametrize(('family', 'meal'), [(1, 0), (1, 200_000), (2, 0), (3, 0), (11, 0)])
def test_recheck_window_covers_every_dip_in_tax_table(family, meal):
    """세금표 전체에서 줄어든 실수령액이 회복되는 거리가 다시 확인 범위(300만 원)보다 짧다.

    세금표를 새 해 것으로 바꿔 이 거리가 길어지면 이 테스트가 먼저 실패한다.
    """
    nets = [
        monthly_net(a // 12, min(meal, a // 12), family).net
        for a in range(12_000_000, 300_000_001, STEP_KRW)
    ]
    assert _max_recovery_steps(nets) < RECHECK_STEPS
