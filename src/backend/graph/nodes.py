"""그래프 노드 — 그래프 안에서 실제로 일을 하는 함수들.

실행 순서:
    route_input → normalize → confirm_table(여기서 멈춤) → calculate → write_report

[노드 함수 규칙]
- 인자로 현재 상태(state, 즉 GraphState dict)를 받는다.
- '바꾸고 싶은 칸만' dict로 돌려준다. 예: return {'table': [...]}
  → LangGraph가 그 칸만 상태에 반영하고, 나머지 칸은 그대로 둔다.
- 상태를 직접 고치지(state['x'] = ...) 않고 항상 return으로 돌려준다.

지금은 흐름만 이어 둔 '빈 노드'다. 실제 처리는 각 TODO에 적은 이슈에서 채운다.
"""

from collections.abc import Callable
from typing import Any

from langgraph.types import interrupt

from backend.graph.state import GraphState
from backend.normalize import Extractor, build_rows

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
        messages = state.get('messages', [])
        text = '\n'.join(messages)
        image_texts = set(state.get('image_texts', []))
        # 이미지에서 옮겨 적은 글을 뺀 나머지. 인용이 여기에 없으면 '이미지에서 읽음' 표시.
        plain = '\n'.join(m for m in messages if m not in image_texts) if image_texts else None
        rows = build_rows(extract(text), text, plain)
        # 체크포인터(SQLite)에 저장되도록 평범한 dict로 바꿔 담는다.
        return {'table': [r.model_dump(mode='json') for r in rows], 'table_confirmed': False}

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

    사용자의 답 모양: {"approved": True, "table": [...]}  (table은 고쳤을 때만 온다)
    """
    answer = interrupt({'type': CONFIRM_TABLE, 'table': state.get('table', [])})

    # 사용자가 표를 고쳐서 보냈으면 그 표를, 아니면 원래 표를 쓴다.
    table = answer.get('table') or state.get('table', [])
    return {'table': table, 'table_confirmed': bool(answer.get('approved', True))}


def calculate(state: GraphState) -> dict[str, Any]:
    """확인된 비교표로 매달 남는 돈을 계산한다."""
    # TODO(#7, #8): 4대보험·세금·월 잔여액 계산 엔진을 연결한다.
    #               숫자 계산은 LLM이 아니라 이 단계의 파이썬 코드가 한다 (설계 원칙).
    return {'result': {'rows': len(state.get('table', [])), 'monthly_remaining': None}}


def write_report(state: GraphState) -> dict[str, Any]:
    """계산 결과를 사람이 읽을 리포트 문장으로 쓴다."""
    # TODO(#19): LLM으로 리포트를 쓰고, 숫자가 계산 결과와 맞는지 스스로 검증한다.
    return {'report': '계산 엔진이 아직 연결되지 않았습니다.'}
