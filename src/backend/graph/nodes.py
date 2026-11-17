"""그래프 노드 — 그래프 안에서 실제로 일을 하는 함수들.

실행 순서:
    route_input → normalize → confirm_table(여기서 멈춤) ─┬→ get_rent → calculate ─┬→ write_report
                                                        └→ get_local_support ───┘      ↕
                                                                                 verify_report

[노드 함수 규칙]
- 인자로 현재 상태(state, 즉 GraphState dict)를 받는다.
- '바꾸고 싶은 칸만' dict로 돌려준다. 예: return {'table': [...]}
  → LangGraph가 그 칸만 상태에 반영하고, 나머지 칸은 그대로 둔다.
- 상태를 직접 고치지(state['x'] = ...) 않고 항상 return으로 돌려준다.

지금은 흐름만 이어 둔 '빈 노드'다. 실제 처리는 각 TODO에 적은 이슈에서 채운다.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END
from langgraph.types import interrupt

from backend.calc.summary import calculate_all
from backend.graph.ask import ASK_MISSING, apply_answers, missing_questions
from backend.graph.state import GraphState
from backend.normalize import Extractor, build_rows
from backend.privacy import mask_text
from backend.public_data.rent import RentEstimator
from backend.report import ReportAgent, ReportCheck
from backend.report import skipped as report_skipped
from backend.support import PolicySource, find_local_support

logger = logging.getLogger(__name__)

# 그래프가 멈춘 이유를 API에 알려 줄 때 쓰는 이름표.
CONFIRM_TABLE = 'confirm_table'


def route_input(state: GraphState) -> dict[str, Any]:
    """입력 확인 자리. 지금은 하는 일이 없다.

    파일(이미지·PDF·엑셀)은 그래프에 들어오기 전에 API가 글로 바꿔 messages에 넣는다
    (backend/inputs/read.py: 형식에 따라 이미지 인식 / PDF / 엑셀·CSV 읽기).
    그래프 안에서 읽지 않는 이유:
      - 파일 원본이 그래프 상태(SQLite 체크포인트)에 저장되지 않게
      - 읽다가 실패하면 세션을 만들지 않아, 다시 올렸을 때 같은 글이 두 번 쌓이지 않게
    """
    return {}


def make_normalize(extract: Extractor) -> Callable[[GraphState], dict[str, Any]]:
    """normalize 노드를 만든다. extract(공고 글 → LLM 답)를 밖에서 넣어 받는다.

    밖에서 넣는 이유: 실제 서비스는 Claude를 부르는 ClaudeExtractor를,
    테스트는 손으로 쓴 정답 응답을 돌려주는 가짜를 넣기 위해서다 (테스트 중 API 호출 금지).
    """

    def normalize(state: GraphState) -> dict[str, Any]:
        """제각각인 공고 내용을 같은 칸의 비교표로 정리한다.

        1) LLM이 칸마다 값과 원문 인용을 뽑는다 (extract)
        2) 코드가 인용·숫자를 원문과 대조해 확인/추정/공고에 없음을 정한다 (build_rows)
        """
        # 개인정보(이메일·전화번호·담당자 이름·상세 주소 등)를 먼저 가린다 (이슈 #20).
        # LLM에 보내는 글과 인용을 대조하는 '원문'이 같아야 인용 확인이 맞으므로 여기서 가린다.
        # (create_message도 보내기 직전에 한 번 더 가린다 — 다른 경로로 새는 것을 막는 안전망)
        messages = [mask_text(m) for m in state.get('messages', [])]
        text = '\n'.join(messages)
        image_texts = {mask_text(t) for t in state.get('image_texts', [])}
        # 이미지에서 옮겨 적은 글을 뺀 나머지. 인용이 여기에 없으면 '이미지에서 읽음' 표시.
        plain = '\n'.join(m for m in messages if m not in image_texts) if image_texts else None
        rows = build_rows(extract(text), text, plain)
        # 체크포인터(SQLite)에 저장되도록 평범한 dict로 바꿔 담는다.
        # 표를 새로 만들면 공고 ID(posting-1 …)가 다른 회사를 가리킬 수 있으므로,
        # 이전 표 기준의 사용자 정보·계산 결과·리포트는 지운다 (확인할 때 다시 받는다).
        return {
            'table': [r.model_dump(mode='json') for r in rows],
            'table_confirmed': False,
            'user_profile': None,
            'result': None,
            'local_support': None,
            'report': None,
            'report_check': None,
        }

    return normalize


def confirm_table(state: GraphState) -> dict[str, Any]:
    """비교표를 사용자에게 보여 주고, 확인을 받을 때까지 그래프를 멈춘다.

    [interrupt — 사람 확인을 기다리는 기능]
    interrupt(값)을 부르면 그래프가 '여기서 일시정지'된다.
      1) 처음 실행될 때: interrupt()가 멈춤 신호를 내고 이 함수는 더 진행하지 않는다.
         넘겨준 값({'type': ..., 'table': ...})은 API를 통해 프론트 화면에 전달된다.
      2) 사용자가 확인 버튼을 누르면 API가 Command(resume=답)으로 그래프를 다시 깨운다.
         그러면 이 함수가 처음부터 다시 실행되는데, 이번에는 interrupt()가 멈추지 않고
         사용자의 답(answer)을 돌려준다. 그 답으로 아래 코드가 이어서 실행된다.
    멈춘 상태는 체크포인터(SQLite)에 저장되므로, 서버를 껐다 켜도 이어서 진행할 수 있다.

    사용자의 답 모양: {"approved": True, "table": [...], "profile": {...}}
      table은 고쳤을 때만, profile(사용자 정보)은 입력했을 때만 온다.
    """
    answer = interrupt({'type': CONFIRM_TABLE, 'table': state.get('table', [])})

    # 사용자가 표를 고쳐서 보냈으면 그 표를, 아니면 원래 표를 쓴다.
    table = answer.get('table') or state.get('table', [])
    update = {'table': table, 'table_confirmed': bool(answer.get('approved', True))}
    # 사용자 정보는 보냈을 때만 바꾼다 (안 보내면 이미 있던 값을 그대로 둔다)
    if answer.get('profile') is not None:
        update['user_profile'] = answer['profile']
    return update


def check_missing(state: GraphState) -> dict[str, Any]:
    """되묻기 (이슈 #13): 계산을 막는 값이 비어 있으면 사용자에게 묻고 기다린다 (graph/ask.py).

    confirm_table과 같은 방식으로 멈춘다: 처음 실행에서 interrupt()가 질문을 내보내고,
    POST /sessions/{id}/answers가 Command(resume={'answers': {...}})로 깨우면 이 함수가 처음부터
    다시 돌며 interrupt()가 그 답을 돌려준다. 질문을 고르는 일은 상태만 보므로 다시 돌아도 같다.
    빠진 값이 없으면 아무것도 바꾸지 않고 지나간다.
    """
    questions = missing_questions(state.get('table'), state.get('user_profile'))
    if not questions:
        return {}
    answer = interrupt(
        {'type': ASK_MISSING, 'questions': [q.model_dump(mode='json') for q in questions]}
    )
    answers = (answer or {}).get('answers') or {}
    return {'user_profile': apply_answers(state['user_profile'], answers)}


def table_lawd_codes(table: list[dict] | None) -> set[str]:
    """비교표의 근무지 시군구 코드들 (코드를 정하지 못한 공고는 빠진다)."""
    codes = set()
    for row in table or []:
        location = (row.get('location') or {}).get('value') or {}
        if location.get('lawd_cd'):
            codes.add(location['lawd_cd'])
    return codes


def make_get_rent(estimator: RentEstimator | None) -> RunnableLambda:
    """get_rent 노드를 만든다. estimator가 없으면(인증키 없음·테스트) 조회하지 않는다.

    조회는 비동기(여러 달·유형을 한꺼번에 요청)라 async 함수로 만들고, 그래프를 동기로 돌리는
    테스트(graph.invoke)를 위해 같은 일을 하는 동기 함수도 함께 준다 (RunnableLambda).
    """

    async def get_rent(state: GraphState) -> dict[str, Any]:
        """회사 지역마다 원룸급 월세 시세를 국토부 실거래가로 추정한다 (public_data/rent.py).

        - 확인된 비교표의 근무지(시군구 코드)마다 한 번씩, 결과는 SQLite에 캐시
        - 사용자가 이사할 집 월세를 비워 두면 calculate가 이 값을 '추정'으로 쓴다
        - 조회에 실패해도 그래프는 멈추지 않는다 (그 지역만 status='missing')
        get_local_support(청년 지원 조회)와 동시에 돈다 (builder.py).
        """
        if estimator is None:
            return {'rent_estimates': {}}
        found = await estimator.estimate_many(table_lawd_codes(state.get('table')))
        return {'rent_estimates': {c: e.model_dump(mode='json') for c, e in found.items()}}

    def get_rent_sync(state: GraphState) -> dict[str, Any]:
        return asyncio.run(get_rent(state))

    return RunnableLambda(get_rent_sync, afunc=get_rent, name='get_rent')


def make_get_local_support(
    source: PolicySource | None,
) -> Callable[[GraphState], dict[str, Any]]:
    """get_local_support 노드를 만든다. source가 없으면(인증키 없음·테스트) 조회하지 않는다."""

    def get_local_support(state: GraphState) -> dict[str, Any]:
        """근무지 지역마다 받을 수 있는 청년 지원 정책을 온통청년에서 찾는다 (support/policies.py).

        - 지역(시군구) 하나에 한 번 조회, 결과는 SQLite에 하루 캐시
        - 나이(user_profile.age)·분류(주거, 금융·복지·문화)·신청 기간으로 거른다
        - 조회에 실패해도 그래프는 멈추지 않는다 (그 지역 error에 이유)
        동기 함수라 그래프를 비동기로 돌리면 LangGraph가 스레드에서 실행한다 (서버를 막지 않음).
        """
        if source is None:
            return {'local_support': None}
        try:
            found = find_local_support(state.get('table'), state.get('user_profile'), source)
        except Exception:
            # 부가 정보라 실패해도 계산 결과는 그대로 보여 준다 (노드가 죽으면 세션이 갇힌다)
            logger.exception('청년 지원 정책을 찾지 못했습니다')
            return {'local_support': None}
        return {'local_support': found}

    return get_local_support


def calculate(state: GraphState) -> dict[str, Any]:
    """확인된 비교표로 매달 남는 돈을 계산한다.

    공고마다 '지금 집에서 통근'과 '회사 근처로 이사' 두 경우의 월 잔여액을 구한다
    (backend/calc/remaining.py). 숫자 계산은 LLM이 아니라 이 파이썬 코드가 한다 (설계 원칙).
    사용자 정보(user_profile)가 없으면 세후 월급까지만 계산하고 잔여액은 null로 둔다.
    """
    return {
        'result': calculate_all(
            state.get('table', []), state.get('user_profile'), state.get('rent_estimates')
        )
    }


def make_report_nodes(agent: ReportAgent | None):
    """write_report·verify_report 노드와, verify 뒤에 어디로 갈지 정하는 함수를 만든다.

    agent가 없으면(Claude 키 없음·시연 모드·테스트) 리포트를 쓰지 않는다 (report_check=skipped).

        write_report → verify_report ─(문제 있음, 횟수 남음)→ write_report
                                    └(통과 / 횟수 끝 / 실패)→ 끝
    """

    def write_report(state: GraphState) -> dict[str, Any]:
        """계산 결과를 쉬운 말 리포트로 쓴다 (Claude Sonnet). 앞 검증의 문제가 있으면 고쳐 쓴다."""
        if agent is None:
            return report_skipped()
        try:
            return agent.write(state)
        except Exception:
            # 리포트는 덤이다: 실패해도 계산 결과는 보여 준다 (노드가 죽으면 세션이 갇힌다)
            logger.exception('리포트를 쓰지 못했습니다')
            return report_failed()

    def verify_report(state: GraphState) -> dict[str, Any]:
        """리포트의 금액을 코드로 대조하고, 다른 문제는 Claude Haiku가 본다 (report/agent.py)."""
        if agent is None:
            return {}
        try:
            return agent.verify(state)
        except Exception:
            logger.exception('리포트를 검증하지 못했습니다')
            return report_failed()

    def after_verify(state: GraphState) -> str:
        return 'write_report' if ReportAgent.needs_rewrite(state) else END

    return write_report, verify_report, after_verify


def report_failed() -> dict[str, Any]:
    check = ReportCheck(
        status='unavailable', reason='리포트를 쓰지 못했어요. 계산 결과는 그대로 볼 수 있어요.'
    )
    return {
        'report': None,
        'report_check': check.model_dump(mode='json'),
        'report_draft': None,
        'report_problems': None,
    }
