"""리포트 테스트 (#19) — 실제 Claude는 부르지 않는다 (가짜 작성자·검토자·클라이언트).

    - 자리표시: AI는 금액을 {{A3}}로만 쓰고 코드가 채운다. 숫자로 직접 쓴 금액·부호 틀림은 거절
    - 사실 정리: 고정비 이름·나이 같은 사용자 정보는 Claude에 넘기지 않는다
    - 쓰기 → 검증 → 다시 쓰기: 틀린 금액·검토 문제가 있으면 문제를 알려 주고 다시 쓴다,
      끝까지 못 고치면 리포트를 숨긴다, Claude 실패는 계산 결과를 막지 않는다
    - 그래프·API: 확인하면 리포트가 붙고, 다시 계산하면 다시 쓴다

[실행] backend 폴더에서 uv run pytest tests/test_report.py
"""

import json
import logging

import pytest
from conftest import FakeExtractor, scenario_llm, scenario_text
from fastapi.testclient import TestClient
from test_demo_scenarios import FLOWS, profile_a, run_flow
from test_normalize import FakeMessages, fake_client, reply

from backend.calc.summary import calculate_all
from backend.calc.text import won
from backend.claude import MODEL, REVIEW_MODEL
from backend.main import create_app
from backend.normalize import build_rows
from backend.report import (
    MAX_ATTEMPTS,
    ClaudeReviewer,
    ClaudeWriter,
    ReportAgent,
    ReportError,
)
from backend.report.agent import (
    CONTENT_RULES,
    NO_ESTIMATE,
    REVIEWER_SYSTEM,
    RULES,
    WRITER_SYSTEM,
    Finding,
)
from backend.report.facts import MAX_TEXT, Amounts, build_facts, check_draft, render


def pangyo_state() -> dict:
    """판교 공고 + 사용자 A로 계산한 그래프 상태 (리포트 노드가 받는 모양)."""
    [row] = build_rows(scenario_llm('near_pangyo'), scenario_text('near_pangyo'))
    table = [row.model_dump(mode='json')]
    profile = profile_a()
    profile['postings'] = {'posting-1': profile['postings']['near_pangyo']}
    profile['age'] = 26
    return {'table': table, 'user_profile': profile, 'result': calculate_all(table, profile)}


def remaining_of(state: dict, case: str = 'commute') -> int:
    [posting] = state['result']['postings']
    return next(s['monthly_remaining'] for s in posting['scenarios'] if s['case'] == case)


# ── 자리표시 검사·채우기 ─────────────────────────────────────────


def ref_of(state: dict, case: str = 'commute') -> str:
    """그 경우의 '매달 남는 돈' 자리표시 ('{{A2}}')."""
    _, amounts = build_facts(state['result'], state.get('local_support'), state['table'])
    value = remaining_of(state, case)
    key = next(k for k, (v, kind) in amounts.values.items() if v == value and kind == 'remaining')
    return '{{' + key + '}}'


def amounts_of(**values: tuple[int, str]) -> Amounts:
    return Amounts(dict(values))


def shown(value: int) -> str:
    """리포트에 들어가는 금액 글자 (화면과 같은 반올림: '약 196만원')."""
    return render('{{A1}}', amounts_of(A1=(value, 'other')))


PANGYO = amounts_of(
    A1=(36_000_000, 'salary'), A2=(1_963_100, 'remaining'), A3=(590_000, 'other'),
    A5=(2_001_100, 'remaining'), A9=(30_000_000, 'salary'),
)  # fmt: skip


@pytest.mark.parametrize(
    ('draft', 'text'),
    [
        # 화면과 같게: 만 원으로 떨어지지 않으면 반올림해 '약' (AI가 '|약'을 썼든 안 썼든)
        ('매달 {{A2}}이 남아요', '매달 약 196만원이 남아요'),
        ('{{ A2 | 약 }}', '약 196만원'),
        ('{{A5-A2}} 더 남아요', '약 4만원 더 남아요'),
        ('약 {{A2}} 남아요', '약 196만원 남아요'),  # '약'을 따로 써도 겹치지 않게
        ('월세 절약 {{A2}}', '월세 절약 약 196만원'),  # 낱말 끝 '약'은 그대로
        ('{{A2-A5|약}}', '약 4만원'),
        ('연봉 차이 {{A1-A9}}', '연봉 차이 600만원'),
        (
            '{{A3}}를 쓰고 {{A2}}가 남고, {{A5}}는 {{A3}}와',
            '59만원을 쓰고 약 196만원이 남고, 약 200만원은 59만원과',
        ),
        ('{{A3}}가량', '59만원가량'),  # 조사가 아닌 말은 그대로
        ('{{A3}}로 잡았어요', '59만원으로 잡았어요'),
        ('{{A3}}로도 충분해요', '59만원으로도 충분해요'),
        ('{{A3}}로서', '59만원로서'),  # 조사가 아닌 말은 그대로
    ],
)
def test_render_fills_amounts_like_the_screen(draft, text):
    assert check_draft(draft, PANGYO) == []
    assert render(draft, PANGYO) == text


@pytest.mark.parametrize(
    'draft',
    [
        '매달 500만 남아요',  # '원' 없음
        '연봉 5천만원',  # 천만
        '1.5억원',  # 소수
        '1,970,000원',
        '196만 3,100원이 남아요',  # 맞는 금액이라도 숫자로 직접 쓰면 안 된다
        '월세는 50~60만원',  # 범위
        '1963100',  # 단위 없는 큰 수
        '연봉 3,600만 원',  # '만' 뒤 띄어쓰기
    ],
)
def test_literal_amounts_are_rejected(draft):
    assert check_draft(draft, PANGYO)


@pytest.mark.parametrize(
    'draft',
    [
        '2026년 5~10월 거래 기준이에요.',
        '지원은 6개월 동안이에요.',
        '20~40㎡ 원룸, 거래 2,112건',
        '청년 지원 정책과 원룸 시세',
        '2020\n원룸 시세를 봤어요.',  # 줄 끝 숫자 + 다음 줄 '원룸'
        '최대 3번 다시 써요.',
        '출처: https://www.data.go.kr/data/15126474/openapi.do',  # 주소 속 숫자
    ],
)
def test_non_money_numbers_pass(draft):
    assert check_draft(draft, PANGYO) == []


@pytest.mark.parametrize(
    ('draft', 'problem'),
    [
        ('{{A99}}', 'JSON에 없는 자리표시'),
        ('{{A1-A2}}', '연봉끼리만'),  # 연봉과 매달 남는 돈의 차이
        ('{{A3-A2}}', '연봉끼리만'),  # 'other'끼리는 차이를 못 쓴다
        ('{{뭔가}}', '알 수 없는 자리표시'),
        ('{{A2 남아요', '괄호'),
        ('매달 {{A2}} 적자예요', '흑자'),
    ],
)
def test_bad_placeholders_are_rejected(draft, problem):
    problems = check_draft(draft, PANGYO)
    assert any(problem in p for p in problems), problems


def test_negative_remaining_sign():
    """마이너스는 부호를 지킨다: 크기만 쓰면 '모자라요'가 있어야 하고, '남아요'라고 하면 안 된다."""
    deficit = amounts_of(A1=(-200_000, 'remaining'))
    ok = '매달 {{A1|절대}}씩 모자라요.'
    assert check_draft(ok, deficit) == []
    assert render(ok, deficit) == '매달 20만원씩 모자라요.'
    assert render('{{A1}}', deficit) == '−20만원'
    assert check_draft('매달 {{A1|절대}}이 남아요.', deficit)
    assert check_draft('매달 {{A1}}이 남아요.', deficit)
    assert check_draft('매달 남는 돈은 {{A1}}이에요.', deficit) == []


def test_zero_won_passes_only_when_facts_have_zero():
    """'0원으로 계산했어요'는 틀릴 숫자가 없다 → 자료에 0원이 있으면 숫자로 써도 된다."""
    zero = amounts_of(A1=(0, 'other'), A2=(1_963_100, 'remaining'))
    assert check_draft('교통비는 0원으로 계산했어요.', zero) == []
    assert check_draft('교통비는 0 원이에요.', zero) == []
    assert check_draft('교통비는 0원이에요.', PANGYO)  # 자료에 0원이 없다
    assert check_draft('10원', zero) and check_draft('1,0원', zero)  # 0으로 끝나는 다른 수


def test_break_even_facts_carry_confidence():
    """최소 연봉이 가정값 주거비에 기대면 '추정'이라고 넘긴다 (작성·검토 규칙 2의 근거)."""
    out, rows, _ = run_flow(*FLOWS[0][1:])
    table = [r.model_dump(mode='json') for r in rows.values()]
    facts, _ = build_facts(out, None, table)
    [naju] = facts['연봉 비공개 공고의 최소 연봉']
    assert naju['기준']['신뢰도'] == '추정'
    [move] = naju['경우별']  # 해당 없는 통근은 공고 쪽 '경우별'에 이유와 함께만 있다
    assert move['최소 연봉'] and move['신뢰도'] == '추정'
    # 만원 단위로 올린 연봉이라 기준과 몇백 원 차이 나는 '최소 연봉일 때 남는 돈'은 넘기지 않는다
    assert not any(k.startswith('최소 연봉일 때 매달 남는 돈') for k in move)


def test_net_income_must_follow_minimum_salary_not_support_salary():
    """최소 연봉일 때 세후 월급을 지원 받는 동안 연봉 뒤에 붙이면 코드가 막는다.

    첫 초안 9개 중 7개가 이렇게 틀렸고, 검토(AI)는 두 모델 다 안정적으로 못 잡았다.
    """
    out, rows, _ = run_flow(*FLOWS[0][1:])
    table = [r.model_dump(mode='json') for r in rows.values()]
    facts, amounts = build_facts(out, None, table)
    [move] = facts['연봉 비공개 공고의 최소 연봉'][0]['경우별']

    def ref(text: str) -> str:
        return text[: text.index('}}') + 2]

    least, net = ref(move['최소 연봉']), ref(move['최소 연봉일 때 세후 월급'])
    during = ref(move['지원 받는 동안 최소 연봉 (이때의 세후 월급은 따로 없음)'])
    ok = f'최소 {least}이고 이때 세후 월급은 {net}이에요. 지원 받는 동안은 {during}이면 돼요.'
    bad = f'최소 {least}이에요. 지원 받는 동안은 {during}이면 되고, 이때 세후 월급은 {net}이에요.'
    # 지원 중 연봉 뒤라도 '최소 연봉일 때'라고 다시 밝히면 맞게 쓴 것이다
    restated = f'지원 받는 동안은 {during}이면 돼요. 최소 연봉일 때 세후 월급은 {net}이에요.'
    assert check_draft(ok, amounts) == []
    assert check_draft(restated, amounts) == []
    assert any('최소 연봉일 때의 세후 월급' in p for p in check_draft(bad, amounts))


def test_same_company_postings_get_distinct_names():
    """같은 회사 공고 둘: 공고 ID를 넘기지 않으니 몇 번째 공고인지 붙여 구분한다."""
    state = pangyo_state()
    posting = state['result']['postings'][0]
    twin = json.loads(json.dumps(posting)) | {'posting_id': 'posting-2'}
    state['result']['postings'].append(twin)
    facts, _ = build_facts(state['result'], None, state['table'])
    names = [p['회사'] for p in facts['공고별 매달 남는 돈']]
    company = posting['company']
    assert names == [f'{company} (1번째 공고)', f'{company} (2번째 공고)']


def test_writer_and_reviewer_share_content_rules():
    """규칙을 따로 두면 한쪽만 바뀌어, 규칙대로 쓴 리포트를 검토가 떨어뜨린다."""
    assert CONTENT_RULES in WRITER_SYSTEM and CONTENT_RULES in REVIEWER_SYSTEM


def test_every_amount_in_facts_has_a_placeholder():
    """자료 속 금액은 모두 자리표시가 붙어 있다 → AI가 자료의 어떤 금액이든 쓸 수 있다."""
    state = pangyo_state()
    state['result']['postings'][0]['notes'].append('가정값 60만원 − 주거지원비 20만원으로 봤어요.')
    facts, amounts = build_facts(state['result'], None, state['table'])
    text = json.dumps(facts, ensure_ascii=False)
    for key, (value, _) in amounts.values.items():
        text = text.replace('{{' + key + '}} = ' + won(value), '')
    assert check_draft(text, amounts) == []
    assert '{{' not in text


def test_facts_text_is_cut_and_has_no_none():
    state = pangyo_state()
    state['table'][0]['company'] = {'value': '가' * 500}
    state['result']['postings'][0]['company'] = '가' * 500
    state['result']['break_even'] = [{'company': 'B', 'baseline': {}, 'cases': []}]
    facts, _ = build_facts(state['result'], None, state['table'])
    assert len(facts['공고별 매달 남는 돈'][0]['회사']) == MAX_TEXT
    assert facts['연봉 비공개 공고의 최소 연봉'][0]['기준'] is None
    assert 'None' not in json.dumps(facts, ensure_ascii=False)


# ── 사실 정리: 개인정보를 넘기지 않는다 ─────────────────────────


def test_facts_leave_out_personal_details():
    state = pangyo_state()
    state['user_profile']['fixed_expenses'] = [{'name': '홍길동 학자금', 'monthly_krw': 150_000}]
    state['result'] = calculate_all(state['table'], state['user_profile'])
    facts, _ = build_facts(state['result'], None, state['table'])
    text = json.dumps(facts, ensure_ascii=False)
    assert '홍길동' not in text  # 고정비는 합계만
    assert '15만원' in text  # 합계는 넘긴다
    assert '26' not in text and '나이' not in text  # 나이는 넘기지 않는다
    assert '(가상) 이음데이터랩(주)' in text  # 공고에 공개된 회사 이름은 넘긴다


# ── 쓰기 → 검증 → 다시 쓰기 ──────────────────────────────────────


class FakeWriter:
    """정해 둔 초안을 차례로 내고, 받은 문제 목록을 모아 둔다."""

    def __init__(self, *drafts: str, error: Exception | None = None):
        self.drafts, self.error = list(drafts), error
        self.calls: list[tuple[str | None, list[str]]] = []

    def __call__(self, facts, previous, problems):
        self.calls.append((previous, list(problems)))
        if self.error:
            raise self.error
        return self.drafts.pop(0)


class FakeReviewer:
    def __init__(self, *answers: list[str]):
        self.answers = list(answers)
        self.calls = 0

        self.reports: list[str] = []

    def __call__(self, facts, report):
        self.calls += 1
        self.reports.append(report)
        return self.answers.pop(0)


def test_literal_amount_is_rewritten_with_feedback(caplog):
    state = pangyo_state()
    right = f'지금 집에서 다니면 매달 {ref_of(state)}이 남아요.'
    writer = FakeWriter('매달 999만원이 남아요.', right)
    reviewer = FakeReviewer([])
    with caplog.at_level(logging.WARNING):
        out = ReportAgent(writer, reviewer).run(state)
    assert out['report'] == '지금 집에서 다니면 매달 약 196만원이 남아요.'  # 코드가 채운 금액
    assert out['report_check']['status'] == 'verified'
    assert out['report_check']['attempts'] == 2
    # 두 번째 쓰기는 앞 초안과 문제를 받았다
    previous, problems = writer.calls[1]
    assert previous == '매달 999만원이 남아요.'
    assert "'999만'처럼 금액을 숫자로 직접 썼어요" in problems[0]
    assert reviewer.calls == 1  # 금액이 틀린 초안은 검토자에게 보내지 않는다 (비용)
    assert reviewer.reports == ['지금 집에서 다니면 매달 약 196만원이 남아요.']
    assert '리포트 검증 실패 (1번째)' in caplog.text  # 이슈 완료 조건: 재작성이 로그로 확인


def test_reviewer_problem_also_triggers_rewrite():
    writer = FakeWriter('첫 초안 (추정)', '고친 초안 (추정)')
    reviewer = FakeReviewer(['청년 지원금을 남는 돈에 더했어요'], [])
    out = ReportAgent(writer, reviewer).run(pangyo_state())
    assert out['report'] == '고친 초안 (추정)'
    assert writer.calls[1][1] == ['청년 지원금을 남는 돈에 더했어요']


def test_never_fixed_report_is_hidden():
    writer = FakeWriter(*['1,000만원이 남아요.'] * MAX_ATTEMPTS)
    out = ReportAgent(writer, FakeReviewer()).run(pangyo_state())
    assert out['report'] is None  # 틀린 숫자를 보여 주지 않는다
    assert out['report_check']['status'] == 'unverified'
    assert out['report_check']['attempts'] == MAX_ATTEMPTS
    assert out['report_check']['problem_count'] == 1
    assert len(writer.calls) == MAX_ATTEMPTS


def test_negative_remaining_user_gets_report():
    """남는 돈이 마이너스여도 리포트를 받는다 (부호를 지켜 '모자라요'로 쓰면 통과)."""
    state = pangyo_state()
    state['user_profile']['fixed_expenses'] = [{'name': '학자금', 'monthly_krw': 3_000_000}]
    state['result'] = calculate_all(state['table'], state['user_profile'])
    assert remaining_of(state) < 0
    ref = ref_of(state).replace('}}', '|절대}}')
    writer = FakeWriter(f'지금 집에서 다니면 매달 {ref}씩 모자라요. 이사 경우는 추정이에요.')
    out = ReportAgent(writer, FakeReviewer([])).run(state)
    assert out['report_check']['status'] == 'verified'
    assert out['report'] == (
        f'지금 집에서 다니면 매달 {shown(-remaining_of(state))}씩 모자라요. 이사 경우는 추정이에요.'
    )


def test_empty_draft_is_not_verified():
    out = ReportAgent(FakeWriter('  \n '), FakeReviewer([])).run(pangyo_state())
    assert out['report'] is None
    assert out['report_check']['status'] == 'unavailable'


def test_claude_failure_does_not_block_result():
    writer = FakeWriter(error=ReportError('리포트를 쓰지 못했어요.'))
    out = ReportAgent(writer, FakeReviewer()).run(pangyo_state())
    assert out['report'] is None
    assert out['report_check'] == {
        'status': 'unavailable',
        'attempts': 1,
        'problem_count': 0,
        'reason': '리포트를 쓰지 못했어요.',
    }


# ── Claude 요청 모양 (가짜 클라이언트) ──────────────────────────


def test_claude_writer_request_and_rewrite_messages():
    messages = FakeMessages(reply('리포트 글'), reply('고친 글'))
    writer = ClaudeWriter(fake_client(messages))
    facts = {'공고별 매달 남는 돈': []}
    assert writer(facts, None, []) == '리포트 글'
    assert writer(facts, '리포트 글', ['숫자 틀림']) == '고친 글'
    first, second = messages.calls
    assert first['model'] == MODEL
    content = first['messages'][0]['content']
    assert content.startswith('<계산결과>') and content.endswith('</계산결과>')
    assert json.dumps(facts, ensure_ascii=False, indent=1) in content
    assert second['messages'][1] == {'role': 'assistant', 'content': '리포트 글'}
    assert '숫자 틀림' in second['messages'][2]['content']


def check(rule='없는 사실', quote='거리가 멀어서', why='이유가 JSON에 없어요', verdict='위반'):
    return {'rule': rule, 'quote': quote, 'why': why, 'verdict': verdict}


def review_reply(*checks: dict):
    return reply(json.dumps({'checks': list(checks)}, ensure_ascii=False))


def test_claude_reviewer_uses_small_model_and_reads_json():
    messages = FakeMessages(review_reply(check()))
    report = '나주는 거리가 멀어서 통근하지 않아요.'
    [finding] = ClaudeReviewer(fake_client(messages))({}, report)
    assert finding == Finding('없는 사실', '거리가 멀어서', '이유가 JSON에 없어요')
    assert finding.blocking
    assert str(finding) == "[없는 사실] '거리가 멀어서' → 이유가 JSON에 없어요"
    assert messages.calls[0]['model'] == REVIEW_MODEL == 'claude-haiku-5-5'


def test_claude_reviewer_drops_non_violations_and_made_up_quotes(caplog):
    """'문제 아님' 항목과, 리포트에 없는 글을 인용한 가벼운 지적은 다시 쓰게 하지 않는다."""
    report = '- 판교는 매달 약 200만원(추정)이\n  남아요.'
    messages = FakeMessages(
        review_reply(
            check(verdict='문제 아님', quote='약 200만원'),  # 스스로 문제가 아니라고 함
            check(rule='빠진 값 안내', quote='세후 월급을 넣으면'),  # 리포트에 없는 글
            # 띄어쓰기·줄바꿈·따옴표·목록 기호·줄임표 차이는 봐준다
            check(rule='추정 표시', quote='"판교는 매달 약200만원 (추정)이 … 남아요"'),
        )
    )
    with caplog.at_level(logging.WARNING):
        findings = ClaudeReviewer(fake_client(messages))({}, report)
    assert [f.rule for f in findings] == ['추정 표시']
    assert '인용이 리포트에 없음' in caplog.text


def test_claude_reviewer_keeps_blocking_finding_with_misquoted_text():
    """막는 위반은 인용을 조금 틀리게 옮겨도 남긴다 (틀린 금액 설명을 통과시키지 않는다)."""
    messages = FakeMessages(review_reply(check(rule='금액 귀속', quote='나주는 매달 200만원')))
    [finding] = ClaudeReviewer(fake_client(messages))({}, '판교는 매달 200만원이 남아요.')
    assert finding.blocking


def test_claude_reviewer_asks_for_structured_output():
    messages = FakeMessages(review_reply())
    assert ClaudeReviewer(fake_client(messages))({}, '리포트') == []
    fmt = messages.calls[0]['output_config']['format']
    assert fmt['type'] == 'json_schema'
    item = fmt['schema']['properties']['checks']['items']
    assert item['properties']['rule']['enum'] == list(RULES)
    assert item['required'] == ['rule', 'quote', 'why', 'verdict']


def test_light_problems_get_one_rewrite_then_show():
    """추정 표시·빠진 값 안내만 남으면 한 번 고치게 하고, 두 번째부터는 보여 준다 (숨기지 않음)."""
    light = Finding('빠진 값 안내', '고친 초안', '세후 월급도 빠졌다고 알려야 해요')
    writer = FakeWriter('첫 초안 (추정)', '고친 초안 (추정)')
    out = ReportAgent(writer, FakeReviewer([light], [light])).run(pangyo_state())
    assert out['report'] == '고친 초안 (추정)'
    assert out['report_check'] == {
        'status': 'verified',
        'attempts': 2,
        'problem_count': 1,
        'reason': None,
    }
    assert writer.calls[1][1] == [str(light)]


def test_blocking_problem_left_at_the_end_hides_report():
    wrong = Finding('금액 귀속', '고친 초안', '판교 금액을 나주에 붙였어요')
    writer = FakeWriter(*['초안 (추정)'] * MAX_ATTEMPTS)
    out = ReportAgent(writer, FakeReviewer(*[[wrong]] * MAX_ATTEMPTS)).run(pangyo_state())
    assert out['report'] is None
    assert out['report_check']['status'] == 'unverified'


def test_report_without_estimate_word_is_asked_to_say_so():
    """추정인 금액(판교 이사)을 썼는데 '추정'이라는 말이 한 번도 없으면 코드가 먼저 알린다."""
    state = pangyo_state()
    move = ref_of(state, 'move')
    writer = FakeWriter(f'이사하면 매달 {move}이 남아요.', f'이사하면 매달 {move}이 남아요 (추정).')
    out = ReportAgent(writer, FakeReviewer([], [])).run(state)
    assert out['report'] == f'이사하면 매달 {shown(remaining_of(state, "move"))}이 남아요 (추정).'
    assert writer.calls[1][1] == [NO_ESTIMATE]


def test_estimate_word_not_needed_when_no_estimated_amount_is_used():
    """추정인 금액을 쓰지 않았으면 '추정'이라는 말을 억지로 넣게 하지 않는다."""
    state = pangyo_state()
    writer = FakeWriter(f'지금 집에서 다니면 매달 {ref_of(state)}이 남아요.')
    out = ReportAgent(writer, FakeReviewer([])).run(state)
    assert out['report_check']['attempts'] == 1


def test_facts_give_reason_for_not_applicable_case_and_skip_empty_support():
    out, rows, _ = run_flow(*FLOWS[0][1:])
    table = [r.model_dump(mode='json') for r in rows.values()]
    facts, _ = build_facts(out, None, table)
    naju = facts['공고별 매달 남는 돈'][1]
    commute = naju['경우별'][0]
    # 이유가 없으면 AI가 '거리가 멀어서'처럼 지어낸다 → 사용자가 고른 것이라고 적는다
    assert commute == {
        '경우': '지금 집에서 통근',
        '해당': False,
        '이유': '사용자가 지금 집에서 통근할 수 없다고 입력함',
    }
    assert '청년 지원' not in ' '.join(facts)  # 찾지 않았으면 칸을 뺀다
    assert naju['주거지원비 (회사가 주는 돈, 청년 지원과 다름)']
    # 내부 공고 ID는 넘기지 않는다 (리포트 10편 중 3편에 'posting-1'이 그대로 나왔다)
    text = json.dumps(facts, ensure_ascii=False)
    assert 'near_pangyo' not in text and 'down_naju' not in text


@pytest.mark.parametrize(
    'answer',
    [
        '문제 없어 보여요',
        '{"problems": []}',  # 예전 모양
        '{"checks": "없음"}',
        '{"checks": null}',
        '{"checks": [1]}',
        '{"checks": [{"rule": "없는 사실"}]}',
        '{}',
    ],
)
def test_claude_reviewer_bad_answer_is_an_error_not_a_pass(answer):
    messages = FakeMessages(reply(answer))
    with pytest.raises(ReportError):
        ClaudeReviewer(fake_client(messages))({}, '리포트')


# ── 그래프·API ───────────────────────────────────────────────────


def _client(tmp_path, agent) -> TestClient:
    app = create_app(str(tmp_path / 'cp.sqlite'), extractor=FakeExtractor(), report_agent=agent)
    return TestClient(app)


def _pangyo_profile() -> dict:
    p = profile_a()
    p['postings'] = {'posting-1': p['postings']['near_pangyo']}
    return p


def test_confirm_writes_verified_report_through_graph_loop(tmp_path):
    """확인하면 write_report → verify_report → (틀림) write_report → verify_report 순으로 돈다."""
    writer = FakeWriter('매달 999만원이 남아요.', '판교 회사를 비교한 리포트예요 (추정 포함).')
    with _client(tmp_path, ReportAgent(writer, FakeReviewer([]))) as c:
        sid = c.post('/sessions', json={'message': scenario_text('near_pangyo')}).json()[
            'session_id'
        ]
        done = c.post(
            f'/sessions/{sid}/confirm', json={'approved': True, 'profile': _pangyo_profile()}
        ).json()
        assert done['status'] == 'completed'
        assert done['report'] == '판교 회사를 비교한 리포트예요 (추정 포함).'
        assert done['report_check']['status'] == 'verified'
        assert done['report_check']['attempts'] == 2
        assert done['report_check']['problem_count'] == 0
        assert 'problems' not in done['report_check']  # 틀린 초안 조각은 응답에 싣지 않는다


def test_recalculate_rewrites_report_only_when_numbers_change(tmp_path):
    writer = FakeWriter('첫 리포트 (추정)', '다시 계산한 리포트 (추정)')
    with _client(tmp_path, ReportAgent(writer, FakeReviewer([], []))) as c:
        sid = c.post('/sessions', json={'message': scenario_text('near_pangyo')}).json()[
            'session_id'
        ]
        c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': _pangyo_profile()})
        # 아무것도 안 고치고 다시 계산 → 예전 리포트 그대로 (Claude 호출 없음)
        same = c.post(f'/sessions/{sid}/recalculate', json={'profile': _pangyo_profile()}).json()
        assert same['report'] == '첫 리포트 (추정)'
        assert len(writer.calls) == 1
        # 고정비를 고치면 숫자가 바뀌므로 다시 쓴다
        changed = _pangyo_profile()
        changed['fixed_expenses'] = [{'name': '보험', 'monthly_krw': 100_000}]
        again = c.post(f'/sessions/{sid}/recalculate', json={'profile': changed}).json()
        assert again['status'] == 'completed'
        assert again['report'] == '다시 계산한 리포트 (추정)'
        assert len(writer.calls) == 2


def test_report_failure_keeps_session_completed(tmp_path):
    """작성자가 예상 못 한 오류를 내도 세션은 끝나고 계산 결과는 그대로다."""
    writer = FakeWriter(error=RuntimeError('버그'))
    with _client(tmp_path, ReportAgent(writer, FakeReviewer())) as c:
        sid = c.post('/sessions', json={'message': scenario_text('near_pangyo')}).json()[
            'session_id'
        ]
        done = c.post(
            f'/sessions/{sid}/confirm', json={'approved': True, 'profile': _pangyo_profile()}
        ).json()
        assert done['status'] == 'completed'
        assert done['result']['postings']
        assert done['report'] is None
        assert done['report_check']['status'] == 'unavailable'
