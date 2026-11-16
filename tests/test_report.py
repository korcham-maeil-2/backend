"""리포트 테스트 (#19) — 실제 Claude는 부르지 않는다 (가짜 작성자·검토자·클라이언트).

    - 금액 읽기와 검사: 리포트 속 '…원'을 모두 찾아 계산 결과와 대조 (반올림은 쓴 자리까지)
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
from backend.report.facts import amounts_in, build_facts, check_amounts


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


# ── 금액 읽기·검사 ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ('text', 'value'),
    [
        ('매달 196만 3,100원이 남아요', 1_963_100),
        ('최소 3,712만원 이상', 37_120_000),
        ('1억 2,000만원', 120_000_000),
        ('월세 650,000원', 650_000),
        ('약 20만원', 200_000),
        ('0원', 0),
        ('차이는 1만 2천원 정도예요', 12_000),
        ('5천원', 5_000),
    ],
)
def test_amounts_in(text, value):
    assert [v for _, v in amounts_in(text)] == [value]


def test_amounts_in_skips_words_with_won():
    """'지원'·'원룸'처럼 숫자 없는 '원'은 금액이 아니다."""
    assert amounts_in('청년 지원 정책과 원룸 시세, 2,112건') == []


def test_check_amounts_allows_rounding_to_written_place():
    allowed = {1_963_100}
    assert check_amounts('약 196만원이 남아요', allowed) == []  # 만 원 단위 반올림
    assert check_amounts('196만 3,100원', allowed) == []
    assert check_amounts('196만 3,000원', allowed) == []  # 천 원 단위 반올림
    assert check_amounts('196만 5,000원', allowed)  # 천 원 단위까지 썼는데 틀림
    assert check_amounts('약 200만원', allowed) == ["'200만원'는 계산 결과에 없는 금액이에요"]


def test_differences_between_remaining_are_allowed():
    state = pangyo_state()
    _, allowed = build_facts(state['result'], None, state['table'])
    gap = remaining_of(state, 'commute') - remaining_of(state, 'move')
    assert abs(gap) in allowed


# ── 사실 정리: 개인정보를 넘기지 않는다 ─────────────────────────


def test_facts_leave_out_personal_details():
    state = pangyo_state()
    state['user_profile']['fixed_expenses'] = [{'name': '홍길동 학자금', 'monthly_krw': 150_000}]
    state['result'] = calculate_all(state['table'], state['user_profile'])
    facts, _ = build_facts(state['result'], None, state['table'])
    text = json.dumps(facts, ensure_ascii=False)
    assert '홍길동' not in text  # 고정비는 합계만
    assert '15만원' in text
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

    def __call__(self, facts, report):
        self.calls += 1
        return self.answers.pop(0)


def test_wrong_amount_is_rewritten_with_feedback(caplog):
    state = pangyo_state()
    right = f'지금 집에서 다니면 매달 약 {remaining_of(state) // 10_000}만원이 남아요.'
    writer = FakeWriter('매달 999만원이 남아요.', right)
    reviewer = FakeReviewer([])
    with caplog.at_level(logging.WARNING):
        out = ReportAgent(writer, reviewer).run(state)
    assert out['report'] == right
    assert out['report_check']['status'] == 'verified'
    assert out['report_check']['attempts'] == 2
    # 두 번째 쓰기는 앞 초안과 문제를 받았다
    assert writer.calls[1] == (
        '매달 999만원이 남아요.',
        ["'999만원'는 계산 결과에 없는 금액이에요"],
    )
    assert reviewer.calls == 1  # 금액이 틀린 초안은 검토자에게 보내지 않는다 (비용)
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
    assert out['report_check']['problems']
    assert len(writer.calls) == MAX_ATTEMPTS


def test_claude_failure_does_not_block_result():
    writer = FakeWriter(error=ReportError('리포트를 쓰지 못했어요.'))
    out = ReportAgent(writer, FakeReviewer()).run(pangyo_state())
    assert out['report'] is None
    assert out['report_check'] == {
        'status': 'unavailable',
        'attempts': 1,
        'problems': [],
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
    assert first['messages'] == [
        {'role': 'user', 'content': json.dumps(facts, ensure_ascii=False, indent=1)}
    ]
    assert second['messages'][1] == {'role': 'assistant', 'content': '리포트 글'}
    assert '숫자 틀림' in second['messages'][2]['content']


def test_claude_reviewer_uses_small_model_and_reads_json():
    messages = FakeMessages(reply('{"problems": ["출처가 빠졌어요"]}'))
    assert ClaudeReviewer(fake_client(messages))({}, '리포트') == ['출처가 빠졌어요']
    assert messages.calls[0]['model'] == REVIEW_MODEL == 'claude-haiku-5-5'


def test_claude_reviewer_bad_answer_is_an_error_not_a_pass():
    messages = FakeMessages(reply('문제 없어 보여요'))
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


def test_recalculate_rewrites_report(tmp_path):
    writer = FakeWriter('첫 리포트', '다시 계산한 리포트')
    with _client(tmp_path, ReportAgent(writer, FakeReviewer([], []))) as c:
        sid = c.post('/sessions', json={'message': scenario_text('near_pangyo')}).json()[
            'session_id'
        ]
        c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': _pangyo_profile()})
        again = c.post(f'/sessions/{sid}/recalculate', json={'profile': _pangyo_profile()}).json()
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
