"""시연 시나리오 전체 테스트 (#8, #14) — 가상 사용자 3명 × 공고 5개를 시연 흐름 그대로 계산한다.

    사용자 A (서울 관악구 원룸): 판교(연봉 공개, 통근/이사) + 나주(비공개, 이사)
    사용자 B (울산 본가)       : 서울(연봉 공개, 이사)       + 울산(비공개, 본가 통근)
    사용자 C (울산 원룸, 재직) : 광주(희망 연봉 질문, 이사) — "지금처럼 살려면 얼마를 불러야 해?"

공고는 fixture LLM 응답 → build_rows (normalize 노드와 같은 경로), 사용자 값은 data/demo/users.
공고·사용자 파일에 없는 값(이사할 집 월세 등)은 README의 실거래가 중앙값이나 가정값을 쓰고,
가정값은 status='estimated'로 표시한다.

모든 결과에 공통 규칙을 확인한다.
  - 월 잔여액 = 세후 월급 − 주거비 − 교통비 − 기타 고정비 (값이 있을 때)
  - 손익분기 연봉: 만 원 적으면 모자라고, 그 이상(300만 원까지 확인)이면 늘 기준만큼 남는다

[실행] backend 폴더에서 uv run pytest tests/test_demo_scenarios.py
"""

import json

import pytest
from conftest import ROOT, FakeExtractor, scenario_llm, scenario_text
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from backend.calc.break_even import RECHECK_STEPS, STEP_KRW, long_term
from backend.calc.remaining import UserProfile, monthly_remaining
from backend.calc.summary import calculate_all
from backend.graph.builder import build_graph
from backend.normalize import build_rows

USERS = ROOT / 'data' / 'demo' / 'users'


def load_user(uid: str) -> dict:
    return json.loads((USERS / f'{uid}.json').read_text(encoding='utf-8'))


def row_of(pid: str):
    row = build_rows(scenario_llm(pid), scenario_text(pid), None)[0]
    row.id = pid  # 시나리오 이름을 공고 ID로 써서 profile.postings와 맞춘다
    return row


# ── 사용자 정보 (data/demo/users/*.json → UserProfile 모양) ─────────


def profile_a() -> dict:
    u = load_user('user_a')
    home = u['current_residence']
    naju = u['scenario_inputs']['down_naju']
    return {
        'current_housing': {
            'value': home['monthly_rent_krw'] + home['maintenance_fee_krw'],
            'source': '관악구 원룸 월세 + 관리비',
        },
        'fixed_expenses': [
            {'name': k, 'monthly_krw': v} for k, v in u['fixed_expenses_monthly_krw'].items()
        ],
        'postings': {
            'near_pangyo': {
                'commute_transport': {
                    'value': u['scenario_inputs']['near_pangyo'][
                        'commute_from_current_home_transport_monthly_krw'
                    ]
                },
                'move_housing': {'value': 600_000, 'status': 'estimated', 'source': '가정값'},
                'move_transport': {'value': u['current_transport_monthly_krw']},
            },
            'down_naju': {
                'commute_possible': False,  # 서울 → 나주 통근 불가
                'move_housing': {'value': 400_000, 'status': 'estimated', 'source': '가정값'},
                # 월 1회 서울 왕복 (사용자 입력값)
                'move_transport': {
                    'value': naju['trips_home_per_month'] * naju['round_trip_cost_krw']
                },
            },
        },
    }


def profile_b() -> dict:
    u = load_user('user_b')
    home = u['scenario_inputs']['stay_ulsan']
    return {
        'current_housing': {
            'value': home['to_parents_monthly_krw'] + home['own_food_monthly_krw'],
            'source': '본가 생활비 + 식비',
        },
        'fixed_expenses': [],
        'postings': {
            'up_seoul': {
                'commute_possible': False,
                'move_housing': {
                    'value': 500_000,
                    'status': 'estimated',
                    'source': '강서구 원룸급 월세 중앙값 (README)',
                },
                'move_transport': {'value': 70_000, 'status': 'estimated', 'source': '가정값'},
            },
            'stay_ulsan': {
                'commute_transport': {'value': 0, 'source': '통근버스'},
                'move_possible': False,
            },
        },
    }


def profile_c() -> dict:
    u = load_user('user_c')
    slip = u['expected_from_files']['payslip']
    return {
        'current_housing': {'value': u['current_residence']['monthly_rent_krw']},
        'fixed_expenses': [],
        'current_job': {
            'annual_salary_krw': slip['fixed_annual_krw'],
            'meal_nontaxable_monthly_krw': slip['meal_nontaxable'],
            'transport': {'value': 60_000, 'status': 'estimated', 'source': '가정값'},
            'source': '2026년 10월 급여명세서',
        },
        'postings': {
            'across_gwangju': {
                'commute_possible': False,
                'move_housing': {
                    'value': 420_000,
                    'status': 'estimated',
                    'source': '광산구 원룸급 월세 중앙값 (README)',
                },
                'move_transport': {'value': 50_000, 'status': 'estimated', 'source': '가정값'},
            }
        },
    }


# (사용자, 공고들, 사용자 정보)
FLOWS = [
    ('user_a', ['near_pangyo', 'down_naju'], profile_a),
    ('user_b', ['up_seoul', 'stay_ulsan'], profile_b),
    ('user_c', ['across_gwangju'], profile_c),
]
POSTING_USER = {pid: make for _, pids, make in FLOWS for pid in pids}


def run_flow(pids, make_profile):
    profile = make_profile()
    rows = [row_of(p) for p in pids]
    out = calculate_all([r.model_dump(mode='json') for r in rows], profile)
    return out, {r.id: r for r in rows}, UserProfile.model_validate(profile)


def remaining_at(row, profile, annual, case):
    copy = row.model_copy(deep=True)
    copy.annual_salary_krw.value = annual
    s = next(s for s in monthly_remaining(copy, profile).scenarios if s.case == case)
    return s.monthly_remaining


# ── 1. 공통 규칙: 모든 시나리오 ────────────────────────────────────


@pytest.mark.parametrize(('uid', 'pids', 'make'), FLOWS, ids=[f[0] for f in FLOWS])
def test_flow_result_is_consistent(uid, pids, make):
    out, _, _ = run_flow(pids, make)
    assert out['errors'] == []
    assert out['has_user_profile'] is True
    assert [p['posting_id'] for p in out['postings']] == pids
    json.dumps(out)

    for p in out['postings']:
        net, fixed = p['net_income'], p['fixed_expenses']
        assert [s['case'] for s in p['scenarios']] == ['commute', 'move']
        for s in p['scenarios']:
            if not s['applicable']:
                assert s['monthly_remaining'] is None
                continue
            if s['monthly_remaining'] is None:
                assert s['missing'], f'{p["posting_id"]} {s["case"]}: null인데 이유가 없음'
                assert s['status'] == 'missing'
                continue
            expected = (
                net['value'] - s['housing']['value'] - s['transport']['value'] - fixed['value']
            )
            assert s['monthly_remaining'] == expected
            assert s['missing'] == []
            assert s['status'] != 'missing'


@pytest.mark.parametrize(('uid', 'pids', 'make'), FLOWS, ids=[f[0] for f in FLOWS])
def test_flow_break_even_guarantee(uid, pids, make):
    """비공개 공고마다 손익분기 연봉이 있고, '그 이상이면 늘 기준만큼 남는다'가 참이다."""
    out, rows, profile = run_flow(pids, make)
    undisclosed = [p['posting_id'] for p in out['postings'] if p['net_income']['value'] is None]
    assert undisclosed, '시나리오마다 비공개 공고가 하나씩 있다'
    assert [b['posting_id'] for b in out['break_even']] == undisclosed
    assert out['break_even_note'] is None

    for be in out['break_even']:
        target = be['baseline']['monthly_remaining']
        row, _ = long_term(rows[be['posting_id']])  # 기본 답은 지원이 끝난 뒤 기준
        found = [c for c in be['cases'] if c['annual_salary_krw'] is not None]
        assert found, f'{be["posting_id"]}: 손익분기 연봉을 하나도 못 구함'
        for c in found:
            annual = c['annual_salary_krw']
            assert annual % STEP_KRW == 0
            assert remaining_at(row, profile, annual - STEP_KRW, c['case']) < target
            for k in range(RECHECK_STEPS + 1):
                assert remaining_at(row, profile, annual + k * STEP_KRW, c['case']) >= target


# ── 2. 시나리오별 기대 결과 ────────────────────────────────────────


def by_id(out):
    return {p['posting_id']: p for p in out['postings']}, {
        b['posting_id']: b for b in out['break_even']
    }


def test_user_a_pangyo_vs_naju():
    out, _, _ = run_flow(*FLOWS[0][1:])
    postings, bes = by_id(out)
    pangyo = {s['case']: s for s in postings['near_pangyo']['scenarios']}
    assert pangyo['commute']['monthly_remaining'] is not None
    assert pangyo['move']['monthly_remaining'] is not None
    naju = bes['down_naju']
    # 기준 = 판교 두 경우 중 더 많이 남는 쪽
    best = max(s['monthly_remaining'] for s in pangyo.values())
    assert naju['baseline']['posting_id'] == 'near_pangyo'
    assert naju['baseline']['monthly_remaining'] == best
    commute, move = naju['cases']
    assert commute['reason'] == '해당 없는 경우'
    # 주거지원비 20만 원(24개월)을 받는 동안은 판교(3,600만)보다 적게 받아도 되지만,
    # 지원이 끝난 뒤에도 같은 돈이 남으려면 판교보다 더 받아야 한다
    assert move['during_support_annual_salary_krw'] < 36_000_000 < move['annual_salary_krw']
    assert any('24개월 뒤 끝나므로' in n for n in move['notes'])


def test_user_b_seoul_vs_ulsan():
    out, _, _ = run_flow(*FLOWS[1][1:])
    postings, bes = by_id(out)
    seoul = {s['case']: s for s in postings['up_seoul']['scenarios']}
    assert seoul['commute']['applicable'] is False
    ulsan = bes['stay_ulsan']
    assert ulsan['baseline']['posting_id'] == 'up_seoul'
    assert ulsan['baseline']['case'] == 'move'
    commute, move = ulsan['cases']
    assert move['reason'] == '해당 없는 경우'
    # 울산은 식사를 현물로 줘 비과세 식대가 없어 4,000만 원보다 조금 더 받아야 한다
    assert 40_000_000 < commute['annual_salary_krw'] < 41_000_000


def test_user_c_gwangju_keeps_current_life():
    """ "지금처럼 살려면 얼마를 불러야 해?" → 지금 회사 그대로가 기준."""
    out, _, _ = run_flow(*FLOWS[2][1:])
    _, bes = by_id(out)
    gwangju = bes['across_gwangju']
    assert gwangju['baseline']['kind'] == 'current_job'
    move = gwangju['cases'][1]
    # 월세가 40만 → 42만으로 오르고 식대 비과세가 없어지니 지금 연봉(약 3,800만)보다 높아야 한다
    assert move['annual_salary_krw'] > 38_000_000
    assert move['status'] == 'estimated'  # 광주 월세가 추정값


# ── 3. 그래프 끝까지: 공고 5개 각각 ───────────────────────────────


@pytest.mark.parametrize('pid', list(POSTING_USER))
def test_graph_end_to_end_each_posting(pid):
    graph = build_graph(InMemorySaver(), FakeExtractor())
    config = {'configurable': {'thread_id': pid}}
    profile = POSTING_USER[pid]()
    # 그래프 안에서 공고 ID는 posting-1이라 이 공고의 비용을 그 ID로 옮긴다
    profile['postings'] = {'posting-1': profile['postings'][pid]}
    graph.invoke({'messages': [scenario_text(pid)]}, config)
    state = graph.invoke(Command(resume={'approved': True, 'profile': profile}), config)

    result = state['result']
    assert result['errors'] == []
    [posting] = result['postings']
    assert len(posting['scenarios']) == 2
    applicable = [s for s in posting['scenarios'] if s['applicable']]
    assert applicable, '적어도 한 경우는 해당한다'
    if posting['net_income']['value'] is not None:
        assert all(s['monthly_remaining'] is not None for s in applicable)
