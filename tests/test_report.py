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
from test_demo_scenarios import profile_a
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


PANGYO = amounts_of(
    A1=(36_000_000, 'salary'), A2=(1_963_100, 'remaining'), A3=(590_000, 'other'),
    A5=(2_001_100, 'remaining'), A9=(30_000_000, 'salary'),
)  # fmt: skip


@pytest.mark.parametrize(
    ('draft', 'text'),
    [
        ('매달 {{A2}}이 남아요', '매달 196만 3,100원이 남아요'),
        ('{{ A2 | 약 }}', '약 196만원'),
        ('{{A5-A2}} 더 남아요', '3만 8,000원 더 남아요'),
        ('{{A2-A5|약}}', '약 4만원'),
        ('연봉 차이 {{A1-A9}}', '연봉 차이 600만원'),
        (
            '{{A3}}를 쓰고 {{A2}}가 남고, {{A5}}는 {{A3}}와',
            '59만원을 쓰고 196만 3,100원이 남고, 200만 1,100원은 59만원과',
        ),
        ('{{A3}}가량', '59만원가량'),  # 조사가 아닌 말은 그대로
    ],
)
def test_render_fills_exact_amounts(draft, text):
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
    assert out['report'] == '지금 집에서 다니면 매달 196만 3,100원이 남아요.'  # 코드가 채운 금액
    assert out['report_check']['status'] == 'verified'
    assert out['report_check']['attempts'] == 2
    # 두 번째 쓰기는 앞 초안과 문제를 받았다
    previous, problems = writer.calls[1]
    assert previous == '매달 999만원이 남아요.'
    assert "'999만'처럼 금액을 숫자로 직접 썼어요" in problems[0]
    assert reviewer.calls == 1  # 금액이 틀린 초안은 검토자에게 보내지 않는다 (비용)
    assert reviewer.reports == ['지금 집에서 다니면 매달 196만 3,100원이 남아요.']
    assert '리포트 검증 실패 (1번째)' in caplog.text  # 이슈 완료 조건: 재작성이 로그로 확인


def test_reviewer_problem_also_triggers_rewrite():
    writer = FakeWriter('첫 초안', '고친 초안')
    reviewer = FakeReviewer(['청년 지원금을 남는 돈에 더했어요'], [])
    out = ReportAgent(writer, reviewer).run(pangyo_state())
    assert out['report'] == '고친 초안'
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
    writer = FakeWriter(f'지금 집에서 다니면 매달 {ref}씩 모자라요.')
    out = ReportAgent(writer, FakeReviewer([])).run(state)
    assert out['report_check']['status'] == 'verified'
    assert out['report'] == f'지금 집에서 다니면 매달 {won(-remaining_of(state))}씩 모자라요.'


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


def test_claude_reviewer_uses_small_model_and_reads_json():
    messages = FakeMessages(reply('{"problems": ["출처가 빠졌어요"]}'))
    assert ClaudeReviewer(fake_client(messages))({}, '리포트') == ['출처가 빠졌어요']
    assert messages.calls[0]['model'] == REVIEW_MODEL == 'claude-haiku-5-5'


def test_claude_reviewer_asks_for_structured_output():
    messages = FakeMessages(reply('{"problems": []}'))
    assert ClaudeReviewer(fake_client(messages))({}, '리포트') == []
    fmt = messages.calls[0]['output_config']['format']
    assert fmt['type'] == 'json_schema'
    assert fmt['schema']['properties']['problems']['items'] == {'type': 'string'}


@pytest.mark.parametrize(
    'answer',
    ['문제 없어 보여요', '{"problems": "없음"}', '{"problems": null}', '{"problems": [1]}', '{}'],
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
    writer = FakeWriter('매달 999만원이 남아요.', '판교 회사를 비교한 리포트예요.')
    with _client(tmp_path, ReportAgent(writer, FakeReviewer([]))) as c:
        sid = c.post('/sessions', json={'message': scenario_text('near_pangyo')}).json()[
            'session_id'
        ]
        done = c.post(
            f'/sessions/{sid}/confirm', json={'approved': True, 'profile': _pangyo_profile()}
        ).json()
        assert done['status'] == 'completed'
        assert done['report'] == '판교 회사를 비교한 리포트예요.'
        assert done['report_check']['status'] == 'verified'
        assert done['report_check']['attempts'] == 2
        assert done['report_check']['problem_count'] == 0
        assert 'problems' not in done['report_check']  # 틀린 초안 조각은 응답에 싣지 않는다


def test_recalculate_rewrites_report_only_when_numbers_change(tmp_path):
    writer = FakeWriter('첫 리포트', '다시 계산한 리포트')
    with _client(tmp_path, ReportAgent(writer, FakeReviewer([], []))) as c:
        sid = c.post('/sessions', json={'message': scenario_text('near_pangyo')}).json()[
            'session_id'
        ]
        c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': _pangyo_profile()})
        # 아무것도 안 고치고 다시 계산 → 예전 리포트 그대로 (Claude 호출 없음)
        same = c.post(f'/sessions/{sid}/recalculate', json={'profile': _pangyo_profile()}).json()
        assert same['report'] == '첫 리포트'
        assert len(writer.calls) == 1
        # 고정비를 고치면 숫자가 바뀌므로 다시 쓴다
        changed = _pangyo_profile()
        changed['fixed_expenses'] = [{'name': '보험', 'monthly_krw': 100_000}]
        again = c.post(f'/sessions/{sid}/recalculate', json={'profile': changed}).json()
        assert again['status'] == 'completed'
        assert again['report'] == '다시 계산한 리포트'
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
