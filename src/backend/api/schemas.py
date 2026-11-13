"""세션 API 요청·응답 형식 (#2의 엔드포인트 목록 기준).

비교표 행의 상세 필드는 normalize(#5)에서 확정하므로 지금은 dict로 둔다.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

SessionStatus = Literal['awaiting_confirmation', 'completed', 'running']


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, description='사용자가 보낸 문장 (공고 내용, 조건 등)')


class ConfirmRequest(BaseModel):
    approved: bool = True
    table: list[dict[str, Any]] | None = Field(
        default=None, description='사용자가 비교표를 고쳤을 때만 보낸다'
    )


class PendingQuestion(BaseModel):
    type: str = Field(description='멈춘 이유. 지금은 confirm_table 하나')
    payload: dict[str, Any]


class SessionResponse(BaseModel):
    session_id: str
    status: SessionStatus
    pending: PendingQuestion | None = Field(
        default=None,
        description='사용자 확인을 기다리는 내용 (status가 awaiting_confirmation일 때)',
    )
    table: list[dict[str, Any]] = Field(default_factory=list, description='비교표')
    table_confirmed: bool = False
    result: dict[str, Any] | None = None
    report: str | None = None
