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

router = APIRouter(prefix='/sessions', tags=['sessions'])


def _graph(request: Request) -> CompiledStateGraph:
    return request.app.state.graph


def _config(session_id: str) -> dict:
    return {'configurable': {'thread_id': session_id}}


async def _snapshot(graph: CompiledStateGraph, session_id: str) -> SessionResponse:
    snap = await graph.aget_state(_config(session_id))
    if not snap.values:
        raise HTTPException(status.HTTP_404_NOT_FOUND, '세션을 찾을 수 없습니다.')

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


@router.post('', status_code=status.HTTP_201_CREATED)
async def start_session(body: MessageRequest, request: Request) -> SessionResponse:
    """세션을 만들고 첫 메시지로 그래프를 실행한다. 비교표 확인 단계에서 멈춘다."""
    graph = _graph(request)
    session_id = str(uuid.uuid4())
    await graph.ainvoke({'messages': [body.message]}, _config(session_id))
    return await _snapshot(graph, session_id)


@router.post('/{session_id}/messages')
async def send_message(session_id: str, body: MessageRequest, request: Request) -> SessionResponse:
    """같은 세션에 메시지를 더한다. 이전 메시지와 합쳐 비교표부터 다시 만든다."""
    graph = _graph(request)
    await _snapshot(graph, session_id)  # 없는 세션이면 404
    await graph.ainvoke({'messages': [body.message]}, _config(session_id))
    return await _snapshot(graph, session_id)


@router.post('/{session_id}/confirm')
async def confirm_table(session_id: str, body: ConfirmRequest, request: Request) -> SessionResponse:
    """비교표 확인(interrupt 재개). 확인을 기다리는 세션이 아니면 409."""
    graph = _graph(request)
    current = await _snapshot(graph, session_id)
    if current.status != 'awaiting_confirmation':
        raise HTTPException(status.HTTP_409_CONFLICT, '비교표 확인을 기다리는 세션이 아닙니다.')
    await graph.ainvoke(Command(resume=body.model_dump()), _config(session_id))
    return await _snapshot(graph, session_id)


@router.get('/{session_id}')
async def get_session(session_id: str, request: Request) -> SessionResponse:
    """현재 상태와 결과를 조회한다."""
    return await _snapshot(_graph(request), session_id)
