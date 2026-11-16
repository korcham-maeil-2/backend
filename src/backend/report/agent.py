"""리포트 쓰기 → 검증 → (틀리면) 다시 쓰기.

    write  : Claude(Sonnet)가 계산 결과(facts.py)를 쉬운 말 리포트로 쓴다
    verify : 1) 코드가 리포트의 금액을 계산 결과와 하나하나 대조한다 (facts.check_amounts)
             2) 금액이 맞으면 Claude(Haiku)가 숫자 말고 다른 문제를 본다
                (데이터에 없는 주장, 추정값을 확정처럼 말함, 금융상품 권유,
                 청년 지원금을 남는 돈에 더함)
             문제가 있으면 그 목록을 알려 주고 다시 쓰게 한다 (MAX_ATTEMPTS번까지)
    끝까지 통과하지 못하면 리포트를 보여 주지 않는다 (틀린 숫자를 보여 주는 것보다 낫다).

설계 원칙: 숫자는 코드가 계산하고, 문장만 LLM이 쓴다. 그 문장 속 숫자도 코드가 다시 확인한다.
Claude 호출이 실패해도(키 없음·한도 초과 등) 계산 결과는 그대로 보여 준다 (status=unavailable).
"""

import json
import logging
from typing import Any, Literal, Protocol

import anthropic
from pydantic import Field

from backend.calc.remaining import OutputModel
from backend.claude import REVIEW_MODEL, FailureMessages, answer_text, create_message
from backend.report.facts import build_facts, check_amounts

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # 처음 쓰기 1번 + 다시 쓰기 2번

WRITER_SYSTEM = """\
너는 취업·이사를 고민하는 사회초년생에게 일자리 비교 결과를 설명하는 상담사다.
사용자가 준 JSON(계산 결과)만 보고, 결과 화면 위에 놓일 짧은 리포트를 쓴다.

규칙:
1. 숫자는 JSON에 있는 금액 글자를 그대로 쓰거나, 만 원 단위로 반올림해 '약 196만원'처럼 쓴다.
   새로 계산하지 않는다. 예외: '매달 남는 돈'끼리, 연봉끼리의 차이는 말해도 된다.
2. 신뢰도가 '추정'인 값(월세 시세 등)은 추정이라고 밝히고 출처를 짧게 적는다
   (예: 국토부 실거래가 추정).
3. '계산에 빠진 값'이 있어 매달 남는 돈을 못 구한 경우는, 무엇을 넣으면 계산되는지 알려 준다.
4. 연봉 비공개 공고는 '최소 연봉'(이 금액 이상이면 기준만큼 남는다)을 설명하고,
   연봉 협상 때 기준으로 쓸 수 있다고 알려 준다.
5. 청년 지원은 남는 돈 계산에 넣지 않았다. 지원금을 남는 돈에 더하지 말고,
   '신청 전 조건 확인'을 덧붙인다.
6. 금융상품 가입을 권하지 않는다. 어느 회사를 고르라고 정해 주지 않는다 (결정은 사용자가 한다).
7. 해요체, 쉬운 말. 전문 용어는 풀어 쓴다.
8. 형식: 제목 없이 바로 시작. 짧은 문단 3~5개 또는 '- '로 시작하는 줄. 모두 합쳐 700자 안팎.
   마크다운 굵게(**), 표, 제목(#)은 쓰지 않는다.
"""

REVIEWER_SYSTEM = """\
너는 리포트 검토자다. 계산 결과(JSON)와 그것을 설명한 리포트를 받는다.
금액이 맞는지는 이미 프로그램이 확인했다. 너는 아래만 본다:
1. JSON에 없는 사실·정책 이름·조건을 지어냈는가
2. 신뢰도가 '추정'인 값을 확정된 것처럼 말하거나 출처를 빠뜨렸는가
3. 청년 지원금을 매달 남는 돈에 더했거나, 받을 수 있다고 단정했는가
4. 금융상품 가입을 권하거나, 특정 회사를 고르라고 정해 줬는가
5. JSON과 반대되는 말을 했는가 (예: 더 적게 남는 쪽을 더 남는다고 함)

문제가 없으면 {"problems": []}, 있으면 {"problems": ["무엇이 왜 문제인지 한 문장", ...]}.
JSON 하나만 답한다. 사소한 문체는 문제로 보지 않는다.
"""

_FAILURES = FailureMessages(
    task='리포트',
    busy='리포트를 쓰는 요청이 많아요. 잠시 후 다시 계산해 주세요.',
    failed='리포트를 쓰지 못했어요. 계산 결과는 그대로 볼 수 있어요.',
    unreachable='리포트를 쓰는 서버에 연결하지 못했어요. 계산 결과는 그대로 볼 수 있어요.',
    refusal='리포트를 쓰지 못했어요. 계산 결과는 그대로 볼 수 있어요.',
    too_long='리포트가 너무 길어져 멈췄어요. 계산 결과는 그대로 볼 수 있어요.',
)

ReportStatus = Literal['verified', 'unverified', 'unavailable', 'skipped']


class ReportError(Exception):
    """리포트를 쓰거나 검토하지 못함. message는 화면에 보여 줄 수 있는 해요체 문장."""


class ReportCheck(OutputModel):
    """리포트 검증 결과 = 세션 응답의 report_check 칸."""

    status: ReportStatus = Field(
        description=(
            'verified=검증 통과, unverified=다시 써도 통과 못 해 보여 주지 않음, '
            'unavailable=쓰지 못함(Claude 오류), skipped=리포트 기능 꺼짐(키 없음·시연 모드)'
        )
    )
    attempts: int = Field(default=0, description='쓴 횟수 (1 = 한 번에 통과)')
    problems: list[str] = Field(default_factory=list, description='마지막 검증에서 찾은 문제')
    reason: str | None = Field(default=None, description='쓰지 못한 이유 (해요체)')


class Writer(Protocol):
    def __call__(self, facts: dict, previous: str | None, problems: list[str]) -> str:
        """리포트 글. previous·problems가 있으면 그 문제를 고쳐 다시 쓴다."""
        ...


class Reviewer(Protocol):
    def __call__(self, facts: dict, report: str) -> list[str]:
        """숫자 말고 다른 문제 목록 (없으면 [])."""
        ...


def _facts_text(facts: dict) -> str:
    return json.dumps(facts, ensure_ascii=False, indent=1)


class ClaudeWriter:
    """Claude(Sonnet)로 리포트를 쓴다. 키는 SDK가 환경 변수에서 읽는다."""

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def __call__(self, facts: dict, previous: str | None, problems: list[str]) -> str:
        messages: list[dict] = [{'role': 'user', 'content': _facts_text(facts)}]
        if previous is not None:
            # 앞 리포트와 문제를 함께 주고 고치게 한다 (처음부터 새로 쓰는 것보다 덜 흔들린다)
            messages += [
                {'role': 'assistant', 'content': previous},
                {
                    'role': 'user',
                    'content': '앞 리포트에 이런 문제가 있어요. '
                    '고쳐서 리포트 전체를 다시 써 주세요.\n'
                    + '\n'.join(f'- {p}' for p in problems),
                },
            ]
        response = create_message(
            lambda: self.client,
            ReportError,
            _FAILURES,
            max_tokens=4000,
            output_config={'effort': 'medium'},
            system=[
                {'type': 'text', 'text': WRITER_SYSTEM, 'cache_control': {'type': 'ephemeral'}}
            ],
            messages=messages,
        )
        return answer_text(response).strip()


class ClaudeReviewer:
    """Claude(Haiku)로 숫자 말고 다른 문제를 본다.

    답 모양이 틀리면 '문제 없음'으로 넘기지 않고 오류로 본다.
    """

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def __call__(self, facts: dict, report: str) -> list[str]:
        response = create_message(
            lambda: self.client,
            ReportError,
            _FAILURES,
            model=REVIEW_MODEL,
            max_tokens=1500,
            system=REVIEWER_SYSTEM,
            messages=[
                {
                    'role': 'user',
                    'content': f'[계산 결과 JSON]\n{_facts_text(facts)}\n\n[리포트]\n{report}',
                }
            ],
        )
        raw = answer_text(response)
        start, end = raw.find('{'), raw.rfind('}')
        try:
            problems = json.loads(raw[start : end + 1])['problems']
        except (ValueError, KeyError, TypeError) as e:
            logger.warning('리포트 검토 답 모양 오류')
            raise ReportError(_FAILURES.failed) from e
        return [str(p) for p in problems if str(p).strip()]


class ReportAgent:
    """그래프 노드(write_report·verify_report)와 다시 계산(/recalculate)이 같이 쓴다."""

    def __init__(self, writer: Writer, reviewer: Reviewer | None = None) -> None:
        self._write = writer
        self._review = reviewer

    # ── 그래프 노드 한 단계씩 ──

    def write(self, state: dict[str, Any]) -> dict[str, Any]:
        """리포트 초안 쓰기. 앞 검증에서 문제가 나왔으면 그것을 고쳐 다시 쓴다."""
        problems = state.get('report_problems') or []
        retry = bool(problems) and state.get('report_draft') is not None
        attempts = (state.get('report_attempts') or 0) + 1 if retry else 1
        facts, _ = build_facts(state.get('result'), state.get('local_support'), state.get('table'))
        try:
            draft = self._write(facts, state.get('report_draft') if retry else None, problems)
        except ReportError as e:
            return _done(None, ReportCheck(status='unavailable', attempts=attempts, reason=str(e)))
        return {'report_draft': draft, 'report_attempts': attempts, 'report_problems': None}

    def verify(self, state: dict[str, Any]) -> dict[str, Any]:
        """초안 검증. 통과 → report 확정 / 문제 → report_problems (다시 쓰기) / 횟수 끝 → 숨김."""
        draft = state.get('report_draft')
        attempts = state.get('report_attempts') or 1
        if draft is None:  # write에서 이미 실패로 끝냄
            return {}
        facts, allowed = build_facts(
            state.get('result'), state.get('local_support'), state.get('table')
        )
        problems = check_amounts(draft, allowed)
        if not problems and self._review is not None:
            try:
                problems = self._review(facts, draft)
            except ReportError as e:
                # 검토만 못 했다: 금액은 확인됐지만 '검증 통과'라고 하지 않는다
                return _done(
                    None, ReportCheck(status='unavailable', attempts=attempts, reason=str(e))
                )
        if not problems:
            logger.info('리포트 검증 통과 (%d번째)', attempts)
            return _done(draft, ReportCheck(status='verified', attempts=attempts))
        logger.warning('리포트 검증 실패 (%d번째): %s', attempts, ' / '.join(problems))
        if attempts >= MAX_ATTEMPTS:
            return _done(
                None, ReportCheck(status='unverified', attempts=attempts, problems=problems)
            )
        return {'report_problems': problems}

    @staticmethod
    def needs_rewrite(state: dict[str, Any]) -> bool:
        """verify 다음에 다시 쓸지 (그래프의 갈림길)."""
        return bool(state.get('report_problems'))

    # ── 한 번에 (다시 계산 때) ──

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        """쓰기 → 검증 → 다시 쓰기를 끝까지. 그래프와 같은 결과 칸을 돌려준다."""
        values = {**state, 'report_draft': None, 'report_problems': None, 'report_attempts': 0}
        while True:
            values |= self.write(values)
            if values.get('report_check') and values.get('report_draft') is None:
                return _final(values)
            values |= self.verify(values)
            if not self.needs_rewrite(values):
                return _final(values)


def _done(report: str | None, check: ReportCheck) -> dict[str, Any]:
    """리포트를 확정(또는 숨김)하고 쓰는 중에 쓰던 칸을 비운다."""
    return {
        'report': report,
        'report_check': check.model_dump(mode='json'),
        'report_draft': None,
        'report_problems': None,
    }


def _final(values: dict[str, Any]) -> dict[str, Any]:
    keys = ('report', 'report_check', 'report_draft', 'report_problems', 'report_attempts')
    return {k: values.get(k) for k in keys}


def skipped() -> dict[str, Any]:
    """리포트 기능이 꺼져 있을 때 (키 없음·시연 모드·테스트)."""
    return _done(None, ReportCheck(status='skipped'))
