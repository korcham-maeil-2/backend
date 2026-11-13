"""세션 API — 프론트가 부르는 주소들. 요청을 받아 LangGraph 그래프를 실행·재개·조회한다.

[세션이란]
사용자 한 명이 대화를 시작해서 결과를 받을 때까지의 한 번의 흐름.
세션마다 session_id가 있고, 이 값이 LangGraph의 thread_id가 되어 상태가 따로 저장된다.

[전체 흐름 예시]
  1) POST /sessions                 {"message": "판교 공고 …"}
       → 그래프 실행 → confirm_table에서 멈춤 → status: "awaiting_confirmation", 비교표 반환
  2) (프론트가 비교표를 보여 주고 사용자가 확인)
  3) POST /sessions/{id}/confirm    {"approved": true}
       → 멈춘 곳부터 이어서 끝까지 실행 → status: "completed", 결과·리포트 반환
  언제든 GET /sessions/{id} 로 현재 상태를 다시 볼 수 있다.

[async / await]
async def 함수 안의 await는 "DB나 그래프 작업이 끝날 때까지 기다리되, 그동안 서버가
다른 사람의 요청도 처리하게 하라"는 뜻이다. 결과를 받을 때까지 다음 줄로 넘어가지 않는다.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from backend.api.schemas import (
    ConfirmRequest,
    MessageRequest,
    PendingQuestion,
    SessionResponse,
)

# APIRouter: 관련된 주소들을 묶는 단위. prefix='/sessions'라서 아래 '/{session_id}'는
# 실제로 '/sessions/{session_id}'가 된다. tags는 /docs 화면에서 묶어 보여 줄 이름.
router = APIRouter(prefix='/sessions', tags=['sessions'])


def _graph(request: Request) -> CompiledStateGraph:
    """main.py의 lifespan에서 만들어 둔 그래프를 꺼낸다."""
    return request.app.state.graph


def _config(session_id: str) -> dict:
    """LangGraph에게 '어느 세션의 상태로 실행할지' 알려 주는 설정.
    같은 thread_id로 부르면 저장된 상태를 이어서 쓰고, 새 thread_id면 새로 시작한다."""
    return {'configurable': {'thread_id': session_id}}


async def _snapshot(graph: CompiledStateGraph, session_id: str) -> SessionResponse:
    """저장된 세션 상태를 읽어 API 응답 모양으로 바꾼다.

    모든 엔드포인트가 마지막에 이걸 돌려준다.
    """
    # aget_state: 체크포인터(SQLite)에서 이 세션의 최신 상태를 읽는다.
    #   snap.values     → 상태 값 (messages, table, result ...)
    #   snap.interrupts → interrupt()로 멈춰 있으면 그 내용 (없으면 빈 튜플)
    #   snap.next       → 다음에 실행될 노드 이름들 (끝까지 갔으면 빈 튜플)
    snap = await graph.aget_state(_config(session_id))
    if not snap.values:
        # 한 번도 실행된 적 없는 세션 → 404 Not Found.
        # HTTPException을 raise하면 FastAPI가 그 상태 코드와 메시지로 응답한다.
        raise HTTPException(status.HTTP_404_NOT_FOUND, '세션을 찾을 수 없습니다.')

    # 멈춰 있다면, confirm_table 노드가 interrupt()에 넘긴 값을 꺼낸다.
    pending = None
    if snap.interrupts:
        value = snap.interrupts[0].value
        pending = PendingQuestion(type=value.get('type', 'unknown'), payload=value)

    if pending:
        state = 'awaiting_confirmation'
    elif not snap.next:
        state = 'completed'
    else:
        state = 'running'

    values = snap.values
    return SessionResponse(
        session_id=session_id,
        status=state,
        pending=pending,
        table=values.get('table', []),
        table_confirmed=values.get('table_confirmed', False),
        result=values.get('result'),
        report=values.get('report'),
    )


# @router.post('') → POST /sessions 요청을 이 함수가 처리한다.
# body: MessageRequest → 요청 JSON을 MessageRequest 모양으로 검사해서 넣어 준다.
# request: Request    → 앱 정보(app.state.graph)에 접근하려고 받는다.
# 반환 타입(-> SessionResponse)이 응답 JSON 모양이 된다. status_code=201은 '새로 만들었음'.
@router.post('', status_code=status.HTTP_201_CREATED)
async def start_session(body: MessageRequest, request: Request) -> SessionResponse:
    """세션을 만들고 첫 메시지로 그래프를 실행한다. 비교표 확인 단계에서 멈춘다."""
    graph = _graph(request)
    session_id = str(uuid.uuid4())  # 겹치지 않는 무작위 ID
    # ainvoke(입력, 설정): 그래프를 실행한다. 입력은 상태의 초기값.
    # confirm_table의 interrupt()에서 멈추면 그 시점에 ainvoke가 끝나고 돌아온다.
    await graph.ainvoke({'messages': [body.message]}, _config(session_id))
    return await _snapshot(graph, session_id)


@router.post('/{session_id}/messages')
async def send_message(session_id: str, body: MessageRequest, request: Request) -> SessionResponse:
    """같은 세션에 메시지를 더한다. 이전 메시지와 합쳐 비교표부터 다시 만든다.

    {session_id}처럼 중괄호로 쓴 주소 부분은 같은 이름의 함수 인자로 들어온다.
    """
    graph = _graph(request)
    await _snapshot(graph, session_id)  # 없는 세션이면 여기서 404
    # 새 입력으로 다시 실행한다. messages 칸은 '뒤에 붙이기'라서(state.py 참고)
    # 이전 메시지 + 새 메시지가 합쳐진 채로 처음 노드부터 다시 돈다.
    await graph.ainvoke({'messages': [body.message]}, _config(session_id))
    return await _snapshot(graph, session_id)


@router.post('/{session_id}/confirm')
async def confirm_table(session_id: str, body: ConfirmRequest, request: Request) -> SessionResponse:
    """비교표 확인 = 멈춘 그래프를 다시 깨운다(interrupt 재개).

    확인을 기다리는 세션이 아니면 409.
    """
    graph = _graph(request)
    current = await _snapshot(graph, session_id)
    if current.status != 'awaiting_confirmation':
        # 409 Conflict: 지금 상태와 맞지 않는 요청 (예: 이미 끝난 세션에 또 확인을 보냄)
        raise HTTPException(status.HTTP_409_CONFLICT, '비교표 확인을 기다리는 세션이 아닙니다.')
    # Command(resume=값): "멈춘 곳에서 이 값을 답으로 넣고 이어서 실행해"라는 명령.
    # 이 값이 nodes.confirm_table 안의 interrupt()가 돌려주는 answer가 된다.
    # body.model_dump(): Pydantic 모델 → 평범한 dict ({'approved': True, 'table': None})
    await graph.ainvoke(Command(resume=body.model_dump()), _config(session_id))
    return await _snapshot(graph, session_id)


@router.get('/{session_id}')
async def get_session(session_id: str, request: Request) -> SessionResponse:
    """현재 상태와 결과를 조회한다 (그래프는 실행하지 않고 저장된 값만 읽는다)."""
    return await _snapshot(_graph(request), session_id)
