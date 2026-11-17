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
  4) (결과 화면에서 월세 등을 바꾸면)
     POST /sessions/{id}/recalculate   {"profile": {...}, "table": [...] (고쳤을 때만)}
       → 공고를 다시 읽지 않고(Claude 호출 없음) 계산·리포트만 다시 → 새 결과 반환
  언제든 GET /sessions/{id} 로 현재 상태를 다시 볼 수 있다.
  5) (결과 화면을 열 때) GET /sessions/{id}/local-support
       → 근무지별 청년 지원을 오늘 날짜로 다시 고른다 (며칠 뒤 열어도 '신청 중'이 맞게)

[async / await]
async def 함수 안의 await는 "DB나 그래프 작업이 끝날 때까지 기다리되, 그동안 서버가
다른 사람의 요청도 처리하게 하라"는 뜻이다. 결과를 받을 때까지 다음 줄로 넘어가지 않는다.
"""

import asyncio
import logging
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile, status
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import ValidationError

from backend.api.errors import AppError, ErrorCode, error_responses
from backend.api.schemas import (
    CalculationResult,
    ConfirmRequest,
    InputSummary,
    MessageRequest,
    PendingQuestion,
    RecalculateRequest,
    SessionResponse,
)
from backend.calc.remaining import UserProfile
from backend.calc.summary import calculate_all
from backend.graph import nodes
from backend.inputs.errors import FileInputError, ReadError
from backend.inputs.limits import MAX_MESSAGE_CHARS
from backend.inputs.read import (
    FileReader,
    ReadResult,
    UploadedFile,
    VisionBudgetError,
    graph_messages,
    safe_name,
)
from backend.normalize import NormalizeError
from backend.plan import SettlePlan, build_plan
from backend.report import ReportAgent, ReportCheck
from backend.report import skipped as report_skipped
from backend.support import LocalSupport, find_local_support
from backend.support.policies import today_kst

# APIRouter: 관련된 주소들을 묶는 단위. prefix='/sessions'라서 아래 '/{session_id}'는
# 실제로 '/sessions/{session_id}'가 된다. tags는 /docs 화면에서 묶어 보여 줄 이름.
# responses=error_responses(422, 500): 이 라우터의 모든 주소가
# 422(입력 형식 오류)·500(서버 오류) 때 공통 에러 모양을 돌려준다고 /docs와 프론트 타입에 알린다.
# 주소별 에러(404·409)는 각 함수에 적는다.
router = APIRouter(prefix='/sessions', tags=['sessions'], responses=error_responses(422, 500))
logger = logging.getLogger(__name__)

# 세션마다 자물쇠 하나. 같은 세션에 확인·메시지·사용자 정보 변경이 동시에 오면 차례로 처리한다
# (먼저 온 요청이 끝나야 다음 요청이 상태를 읽는다 → 늦게 보낸 입력이 사라지지 않는다).
# 서버가 한 프로세스라 이걸로 충분하다. 쓰는 요청이 없으면 자물쇠는 저절로 정리된다(weakref).
_session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


@asynccontextmanager
async def _locked(session_id: str) -> AsyncIterator[None]:
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = _session_locks[session_id] = asyncio.Lock()
    async with lock:
        yield


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
        # AppError를 raise하면 errors.py의 처리기가 공통 에러 모양으로 응답한다:
        #   {"code": "SESSION_NOT_FOUND", "message": "세션을 찾을 수 없습니다.", "details": null}
        raise AppError(
            status.HTTP_404_NOT_FOUND, ErrorCode.SESSION_NOT_FOUND, '세션을 찾을 수 없습니다.'
        )

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
        inputs=values.get('inputs', []),
        input_text='\n'.join(values.get('messages', [])),
        profile=_profile(values.get('user_profile')),
        result=_result(values.get('result')),
        local_support=_local_support(values.get('local_support')),
        report=values.get('report'),
        report_check=_report_check(values.get('report_check')),
        plan=_plan(values),
    )


def _plan(values: dict) -> SettlePlan | None:
    """1년 정착 플랜 (build_plan). 계산 결과·청년 지원에서 바로 만든다.

    상태에 따로 저장하지 않는다: 다시 계산하거나 지원을 다시 골라도 플랜이 어긋날 일이 없고,
    플랜이 생기기 전에 끝난 세션도 조회하면 플랜이 나온다. 날짜는 지원을 조회한 날 기준.
    """
    try:
        return build_plan(
            values.get('result'), values.get('local_support'), values.get('table'), today_kst()
        )
    except ValidationError:
        logger.warning('저장된 결과가 지금 모양과 맞지 않아 정착 플랜을 만들지 않았습니다')
        return None


def _profile(raw: dict | None) -> UserProfile | None:
    if raw is None:
        return None
    try:
        return UserProfile.model_validate(raw)
    except ValidationError:
        logger.warning('저장된 사용자 정보가 지금 모양과 맞지 않아 응답에서 뺐습니다')
        return None


def _report_check(raw: dict | None) -> ReportCheck | None:
    if raw is None:
        return None
    try:
        return ReportCheck.model_validate(raw)
    except ValidationError:
        return None


def _local_support(raw: dict | None) -> LocalSupport | None:
    if raw is None:
        return None
    try:
        return LocalSupport.model_validate(raw)
    except ValidationError:
        logger.warning('저장된 청년 지원 목록이 지금 모양과 맞지 않아 응답에서 뺐습니다')
        return None


def _result(raw: dict | None) -> CalculationResult | None:
    """저장된 계산 결과를 응답 모양으로. 예전 모양으로 저장된 세션이면 결과 없음으로 본다.

    (계산 결과 모양이 바뀌기 전에 끝난 세션을 조회해도 500이 나지 않게)
    """
    if raw is None:
        return None
    try:
        return CalculationResult.model_validate(raw)
    except ValidationError:
        logger.warning('저장된 계산 결과가 지금 모양과 맞지 않아 응답에서 뺐습니다')
        return None


# @router.post('') → POST /sessions 요청을 이 함수가 처리한다.
# body: MessageRequest → 요청 JSON을 MessageRequest 모양으로 검사해서 넣어 준다.
# request: Request    → 앱 정보(app.state.graph)에 접근하려고 받는다.
# 반환 타입(-> SessionResponse)이 응답 JSON 모양이 된다. status_code=201은 '새로 만들었음'.
# 아래 다른 함수의 responses=error_responses(404) 같은 표시는
# 그 주소에서만 날 수 있는 실패 상태 코드를 /docs·프론트 타입에 알리는 것이다.
@router.post('', status_code=status.HTTP_201_CREATED, responses=error_responses(502))
async def start_session(body: MessageRequest, request: Request) -> SessionResponse:
    """세션을 만들고 첫 메시지로 그래프를 실행한다. 비교표 확인 단계에서 멈춘다."""
    graph = _graph(request)
    session_id = str(uuid.uuid4())  # 겹치지 않는 무작위 ID
    # ainvoke(입력, 설정): 그래프를 실행한다. 입력은 상태의 초기값.
    # confirm_table의 interrupt()에서 멈추면 그 시점에 ainvoke가 끝나고 돌아온다.
    await _run_with_message(graph, body.message, session_id)
    return await _snapshot(graph, session_id)


async def _run_with_message(graph: CompiledStateGraph, message: str, session_id: str) -> None:
    """메시지를 넣어 그래프를 실행한다. 공고 정리(Claude 호출)가 실패하면 502로 알린다."""
    await _run(graph, {'messages': [message]}, session_id)


async def _run(graph: CompiledStateGraph, values: dict | None, session_id: str) -> None:
    """그래프를 실행한다. values=None이면 멈춘(실패한) 곳부터 이어서 실행한다."""
    try:
        await graph.ainvoke(values, _config(session_id))
    except NormalizeError as e:
        # 502 Bad Gateway: 우리 서버가 아니라 바깥 서비스(Claude) 쪽 문제라는 뜻.
        raise AppError(status.HTTP_502_BAD_GATEWAY, ErrorCode.NORMALIZE_FAILED, str(e)) from e


def _file_error(e: FileInputError) -> AppError:
    return AppError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode(e.code.value),
        e.message,
        details={'file_name': e.file_name} if e.file_name else None,
    )


# 파일은 multipart/form-data로 받는다 (JSON에는 파일을 담을 수 없다).
# 파일 크기 상한은 main.py의 미들웨어가 요청 본문을 읽기 전에 먼저 막는다.
@router.post(
    '/uploads',
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(411, 502, 503),
)
async def start_session_with_files(
    request: Request,
    files: Annotated[list[UploadFile] | None, File(description='공고 파일 (최대 5개)')] = None,
    message: Annotated[
        str, Form(max_length=MAX_MESSAGE_CHARS, description='함께 붙여 넣은 공고 글 (선택)')
    ] = '',
) -> SessionResponse:
    """파일(이미지·PDF·엑셀·CSV)과 붙여 넣은 글로 세션을 만든다. 비교표 확인 단계에서 멈춘다.

    파일은 그래프에 넣기 전에 글로 바꾼다 (backend/inputs/read.py). 원본은 저장하지 않는다.
    """
    uploads = [
        UploadedFile(safe_name(f.filename, i), await f.read())
        for i, f in enumerate(files or [], start=1)
    ]
    reader: FileReader = request.app.state.file_reader
    message = message.strip()
    try:
        # 파일 읽기는 시간이 걸리는 일이라 별도 스레드에서 한다 (그동안 /health 등이 멈추지 않게)
        results: list[ReadResult] = await asyncio.to_thread(reader.read, uploads, message)
    except FileInputError as e:
        raise _file_error(e) from e
    except ReadError as e:
        raise AppError(status.HTTP_502_BAD_GATEWAY, ErrorCode.READ_FAILED, str(e)) from e
    except VisionBudgetError as e:
        raise AppError(
            status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.VISION_DAILY_LIMIT, str(e)
        ) from e

    del uploads  # 파일 원본(최대 20MB)을 정리(10~60초) 동안 들고 있지 않는다
    messages, image_texts = graph_messages(message, results)
    inputs = [InputSummary.model_validate(r, from_attributes=True).model_dump() for r in results]
    graph = _graph(request)
    session_id = str(uuid.uuid4())
    await _run(
        graph,
        {'messages': messages, 'image_texts': image_texts, 'inputs': inputs},
        session_id,
    )
    return await _snapshot(graph, session_id)


@router.post('/{session_id}/messages', responses=error_responses(404, 502))
async def send_message(session_id: str, body: MessageRequest, request: Request) -> SessionResponse:
    """같은 세션에 메시지를 더한다. 이전 메시지와 합쳐 비교표부터 다시 만든다.

    {session_id}처럼 중괄호로 쓴 주소 부분은 같은 이름의 함수 인자로 들어온다.
    """
    graph = _graph(request)
    async with _locked(session_id):
        current = await _snapshot(graph, session_id)  # 없는 세션이면 여기서 404
        messages = (await graph.aget_state(_config(session_id))).values.get('messages', [])
        if current.status == 'running' and body.message in messages:
            # 공고를 정리하다 실패해(502) 실행 중에 멈춘 세션에, 이미 들어간 메시지를 다시 보냈다
            # (마지막 것이 아니어도: A 실패 → B 실패 → A 다시).
            # 다시 붙이지 않고 실패한 곳부터 이어서 실행한다.
            # 세션마다 자물쇠가 있어서, 다른 요청이 실행 중인 상태를 여기서 볼 일은 없다
            # (서버가 한 프로세스일 때 — Dockerfile의 uvicorn은 워커 하나).
            await _run(graph, None, session_id)
        else:
            # 새 입력으로 다시 실행한다. messages 칸은 '뒤에 붙이기'라서(state.py 참고)
            # 이전 메시지 + 새 메시지가 합쳐진 채로 처음 노드부터 다시 돈다.
            # (실패해 멈춘 세션이어도 새 메시지로 처음부터 다시 돌면 풀린다)
            await _run_with_message(graph, body.message, session_id)
        return await _snapshot(graph, session_id)


@router.post('/{session_id}/confirm', responses=error_responses(404, 409))
async def confirm_table(session_id: str, body: ConfirmRequest, request: Request) -> SessionResponse:
    """비교표 확인 = 멈춘 그래프를 다시 깨운다(interrupt 재개).

    확인을 기다리는 세션이 아니면 409.
    """
    graph = _graph(request)
    async with _locked(session_id):
        current = await _snapshot(graph, session_id)
        if current.status != 'awaiting_confirmation':
            # 409 Conflict: 지금 상태와 맞지 않는 요청 (예: 이미 끝난 세션에 또 확인을 보냄)
            raise AppError(
                status.HTTP_409_CONFLICT,
                ErrorCode.SESSION_NOT_AWAITING_CONFIRMATION,
                '비교표 확인을 기다리는 세션이 아닙니다.',
            )
        # Command(resume=값): "멈춘 곳에서 이 값을 답으로 넣고 이어서 실행해"라는 명령.
        # 이 값이 nodes.confirm_table 안의 interrupt()가 돌려주는 answer가 된다.
        # body.model_dump(mode='json'): Pydantic 모델 → 평범한 dict
        #   ({'approved': True, 'table': None, 'profile': {...} 또는 None})
        #   mode='json'은 체크포인터(SQLite)에 그대로 저장되는 값(문자열·숫자·리스트)만 남긴다.
        await graph.ainvoke(Command(resume=body.model_dump(mode='json')), _config(session_id))
        return await _snapshot(graph, session_id)


@router.post('/{session_id}/recalculate', responses=error_responses(404, 409))
async def recalculate(
    session_id: str, body: RecalculateRequest, request: Request
) -> SessionResponse:
    """결과 화면에서 생활비나 비교표를 고쳤을 때, 계산·리포트만 다시 한다.

    비교표 확인을 마친(completed) 세션에서만 된다. 확인 전이면 /confirm에 함께 보낸다 → 409.
    사용자 정보는 보낸 값으로 통째로 바꾸고, 비교표는 보냈을 때만 바꾼다.
    공고 정리(Claude 호출)는 다시 하지 않는다 → 비용 0원, 바로 끝난다.
    """
    graph = _graph(request)
    config = _config(session_id)
    async with _locked(session_id):
        current = await _snapshot(graph, session_id)
        if current.status != 'completed':
            raise AppError(
                status.HTTP_409_CONFLICT,
                ErrorCode.SESSION_NOT_COMPLETED,
                '계산이 끝난 세션이 아닙니다. 비교표 확인 때 사용자 정보를 함께 보내 주세요.',
            )
        # 새 결과를 먼저 다 계산한 뒤, 한 번에 저장한다 (중간에 실패하면 아무것도 바뀌지 않는다).
        # as_node='verify_report': "마지막 노드가 돌려준 것처럼" 저장 → 세션은 계속 completed.
        # (그래프를 calculate부터 다시 돌리면, 중간에 실패했을 때 세션이 '실행 중'에 멈춘다)
        # 숫자가 바뀌면 예전 리포트는 틀린 말이 되므로 리포트도 다시 쓰고 검증한다 (Claude 호출).
        values = (await graph.aget_state(config)).values
        data = body.model_dump(mode='json')  # 체크포인터(SQLite)에 저장되는 평범한 값으로
        profile = data['profile']
        table = data['table'] if body.table is not None else values.get('table', [])
        rents = await _rents_for(request, table, values.get('rent_estimates') or {})
        result = calculate_all(table, profile, rents)
        update = {
            'table': table,
            'user_profile': profile,
            'rent_estimates': rents,
            'result': result,
            # 근무지·나이가 바뀌었을 수 있어 다시 고른다 (같은 지역은 캐시라 바로 끝난다)
            'local_support': await _local_support_for(request, table, profile),
        }
        # 넘길 사실이 그대로면(아무것도 안 고침) 예전 리포트를 그대로 쓴다 (Claude 호출 없음)
        report = ReportAgent.reusable(values, {**values, **update}) or await _report_for(
            request, {**values, **update}
        )
        await graph.aupdate_state(config, {**update, **report}, as_node='verify_report')
        return await _snapshot(graph, session_id)


async def _rents_for(request: Request, table: list[dict], saved: dict[str, dict]) -> dict:
    """비교표 근무지들의 월세 추정. 이미 구한 지역은 다시 묻지 않고, 근무지를 고쳐 새로 생긴
    지역만 조회한다 (조회기가 없으면 저장된 것만)."""
    codes = nodes.table_lawd_codes(table)
    # 구한 시세('estimated')와 '거래 없음'('missing')만 다시 쓴다.
    # 불러오지 못했던 지역('unavailable')은 다시 조회한다 (GET /rent와 결과가 어긋나지 않게).
    rents = {
        c: v for c, v in saved.items() if c in codes and v.get('status') in ('estimated', 'missing')
    }
    estimator = getattr(request.app.state, 'rent_estimator', None)
    new_codes = codes - rents.keys()
    if estimator is not None and new_codes:
        found = await estimator.estimate_many(new_codes)
        rents |= {c: e.model_dump(mode='json') for c, e in found.items()}
    return rents


async def _report_for(request: Request, values: dict) -> dict:
    """리포트 쓰기 → 검증 → 다시 쓰기를 끝까지 (Claude 호출이라 스레드에서). 꺼져 있으면 skipped."""
    agent = getattr(request.app.state, 'report_agent', None)
    if agent is None:
        return report_skipped()
    try:
        return await asyncio.to_thread(agent.run, values)
    except Exception:
        logger.exception('리포트를 쓰지 못했습니다')
        return nodes.report_failed()


async def _local_support_for(
    request: Request, table: list[dict], profile: dict | None
) -> dict | None:
    """비교표 근무지들의 청년 지원. 조회는 동기(네트워크·SQLite)라 스레드에서 돌린다."""
    source = getattr(request.app.state, 'support_source', None)
    if source is None:
        return None
    return await asyncio.to_thread(find_local_support, table, profile, source)


@router.get('/{session_id}/local-support', responses=error_responses(404))
async def get_local_support(session_id: str, request: Request) -> LocalSupport | None:
    """근무지별 청년 지원을 오늘 날짜로 다시 고른다. 결과 화면이 열릴 때 부른다.

    확인·다시 계산 때 저장한 목록은 그날 기준이라, 며칠 뒤 다시 열면 '신청 중'이던 정책이
    이미 끝났을 수 있다. 같은 지역은 캐시(하루)라 바로 끝난다. 다시 고른 목록은 저장하지 않는다
    (보기만 한다 — 세션 상태를 바꾸지 않으므로 자물쇠도 필요 없다).
    계산을 마치기 전이거나 온통청년 키가 없으면 null.
    """
    graph = _graph(request)
    current = await _snapshot(graph, session_id)  # 없는 세션이면 404
    if current.status != 'completed':
        return None
    values = (await graph.aget_state(_config(session_id))).values
    found = await _local_support_for(request, values.get('table', []), values.get('user_profile'))
    return _local_support(found)


@router.get('/{session_id}', responses=error_responses(404))
async def get_session(session_id: str, request: Request) -> SessionResponse:
    """현재 상태와 결과를 조회한다 (그래프는 실행하지 않고 저장된 값만 읽는다)."""
    return await _snapshot(_graph(request), session_id)
