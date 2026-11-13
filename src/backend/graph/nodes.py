"""그래프 노드. 지금은 흐름만 잇는 빈 노드이고, 실제 처리는 각 기능 이슈에서 채운다.

route_input → normalize → confirm_table(interrupt) → calculate → write_report
"""

from typing import Any

from langgraph.types import interrupt

from backend.graph.state import GraphState

CONFIRM_TABLE = 'confirm_table'


def route_input(state: GraphState) -> dict[str, Any]:
    # TODO(#12): 파일 형식에 따라 read_image / read_pdf / read_sheet 로 나눈다.
    return {'input_kind': 'text'}


def normalize(state: GraphState) -> dict[str, Any]:
    # TODO(#5): LLM으로 공고를 비교표 행으로 정규화한다. 지금은 원문만 담은 빈 행 하나.
    raw = '\n'.join(state.get('messages', []))
    return {'table': [{'raw_text': raw}], 'table_confirmed': False}


def confirm_table(state: GraphState) -> dict[str, Any]:
    """비교표를 사용자에게 보여 주고 확인을 받을 때까지 멈춘다.

    재개 값: {"approved": bool, "table": [...] (수정했을 때만)}
    """
    answer = interrupt({'type': CONFIRM_TABLE, 'table': state.get('table', [])})
    table = answer.get('table') or state.get('table', [])
    return {'table': table, 'table_confirmed': bool(answer.get('approved', True))}


def calculate(state: GraphState) -> dict[str, Any]:
    # TODO(#7, #8): 4대보험·세금·월 잔여액 계산 엔진을 연결한다.
    return {'result': {'rows': len(state.get('table', [])), 'monthly_remaining': None}}


def write_report(state: GraphState) -> dict[str, Any]:
    # TODO(#19): 리포트 작성과 자체 검증 루프.
    return {'report': '계산 엔진이 아직 연결되지 않았습니다.'}
