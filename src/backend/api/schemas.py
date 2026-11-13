"""세션 API가 주고받는 데이터 모양 (이슈 #2의 엔드포인트 목록 기준).

[Pydantic 기본 개념]
Pydantic 모델(BaseModel을 상속한 클래스)은 "JSON이 이런 모양이어야 한다"는 설계도다.
  - 요청: 프론트가 보낸 JSON을 이 모양에 맞는지 FastAPI가 자동 검사한다.
    틀리면(예: message가 빈 문자열) 함수가 실행되기 전에 422 오류를 돌려준다.
  - 응답: 함수가 이 모델을 돌려주면 JSON으로 바뀌어 나간다.
  - 이 모양들은 /openapi.json에도 실려서, 프론트가 openapi-typescript로 TS 타입을 자동 생성한다.

Field(...): 칸마다 기본값, 검사 조건, 설명(description)을 붙이는 도구. 설명은 /docs 화면에 나온다.

비교표 한 행은 ComparisonRow (backend/normalize/schema.py)다.
칸마다 {value, status, source_text} 모양이라
프론트가 확인/추정/사용자 입력/공고에 없음을 표시할 수 있다.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.normalize.schema import ComparisonRow

# 세션 상태 3가지
#   awaiting_confirmation: 비교표 확인을 기다리며 멈춰 있음
#   completed: 끝까지 실행됨
#   running: 그 밖의 경우 (지금 구조에선 거의 나오지 않음)
SessionStatus = Literal['awaiting_confirmation', 'completed', 'running']


class MessageRequest(BaseModel):
    """세션 시작·메시지 보내기 요청. 예: {"message": "판교 백엔드 신입 연봉 3,600만원"}"""

    # min_length=1: 빈 문자열은 받지 않는다 (보내면 422).
    message: str = Field(min_length=1, description='사용자가 보낸 문장 (공고 내용, 조건 등)')


class ConfirmRequest(BaseModel):
    """비교표 확인 요청.

    예: {"approved": true}  또는 고친 표를 함께  {"approved": true, "table": [...]}
    """

    approved: bool = True
    # `X | None = None`: 이 칸은 안 보내도 된다(없으면 None).
    table: list[ComparisonRow] | None = Field(
        default=None,
        description='사용자가 비교표를 고쳤을 때만 보낸다. 고친 칸은 status를 user_input으로',
    )


class PendingQuestion(BaseModel):
    """그래프가 멈춘 이유와, 화면에 보여 줄 내용."""

    type: str = Field(description='멈춘 이유. 지금은 confirm_table 하나')
    payload: dict[str, Any]


class SessionResponse(BaseModel):
    """모든 세션 API가 돌려주는 공통 응답. 프론트는 status를 보고 어떤 화면을 띄울지 정한다."""

    session_id: str  # 다음 요청에 쓸 세션 번호 (= LangGraph의 thread_id)
    status: SessionStatus
    pending: PendingQuestion | None = Field(
        default=None,
        description='사용자 확인을 기다리는 내용 (status가 awaiting_confirmation일 때)',
    )
    # default_factory=list: 기본값을 빈 리스트로. (default=[]로 쓰면 모든 응답이
    # 같은 리스트를 나눠 쓰는 실수가 생길 수 있어 이렇게 쓴다)
    table: list[ComparisonRow] = Field(default_factory=list, description='비교표 (공고 1건 = 1행)')
    table_confirmed: bool = False
    result: dict[str, Any] | None = None
    report: str | None = None
