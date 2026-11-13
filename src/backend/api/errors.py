"""공통 에러 응답 — 프론트와 맞춘 약속.

[약속 (A 방식)]
- 성공: HTTP 200·201 + 데이터를 감싸지 않고 그대로 보낸다.
      {"session_id": "...", "status": "completed", ...}
- 실패: HTTP 4xx·5xx + 항상 아래 한 가지 모양으로 보낸다.
      {"code": "SESSION_NOT_FOUND", "message": "세션을 찾을 수 없습니다.", "details": null}
  · code    : 프론트가 분기할 때 쓰는 고정 영문 코드 (아래 ErrorCode 목록)
  · message : 사용자에게 그대로 보여 줘도 되는 한국어 문장
  · details : 추가 정보. 입력 검사 실패(422)일 때는 어느 칸이 왜 틀렸는지 목록, 나머지는 보통 null

  성공/실패 구분은 HTTP 상태 코드로 한다.
  프론트에서는 `if (!res.ok)` 이면 응답 본문을 이 에러 모양으로 읽으면 된다.

[FastAPI 예외 처리기(exception handler)란]
코드 어디서든 raise한 예외를 FastAPI가 붙잡아, 여기 등록한 함수로 응답을 만든다.
그래서 엔드포인트에서는 `raise AppError(...)` 한 줄만 쓰면 위 모양의 에러 응답이 나간다.
FastAPI가 원래 내보내는 {"detail": ...} 형식(404·422 등)도 여기서 같은 모양으로 바꿔 준다.

[새 에러를 추가할 때]
1) ErrorCode에 코드 이름을 추가한다.
2) 엔드포인트에서 raise AppError(상태코드, ErrorCode.새코드, '한국어 메시지')
3) 엔드포인트의 responses=에 상태 코드를 적어 /docs와 프론트 타입에 나오게 한다.
"""

import logging
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    """프론트가 분기에 쓰는 에러 코드 목록. 문자열 값이 그대로 JSON의 code가 된다."""

    # 우리 서비스가 직접 내는 에러
    SESSION_NOT_FOUND = 'SESSION_NOT_FOUND'  # 404: 없는 세션 ID
    SESSION_NOT_AWAITING_CONFIRMATION = 'SESSION_NOT_AWAITING_CONFIRMATION'  # 409
    # 공통 에러 (FastAPI가 자동으로 내는 것을 바꿔 담는다)
    VALIDATION_ERROR = 'VALIDATION_ERROR'  # 422: 요청 JSON 모양이 틀림
    NOT_FOUND = 'NOT_FOUND'  # 404: 없는 주소
    METHOD_NOT_ALLOWED = 'METHOD_NOT_ALLOWED'  # 405: 주소는 있는데 GET/POST가 틀림
    HTTP_ERROR = 'HTTP_ERROR'  # 그 밖의 4xx
    INTERNAL_ERROR = 'INTERNAL_ERROR'  # 500: 서버 내부 오류 (예상 못 한 버그)


class FieldError(BaseModel):
    """입력 검사 실패(422) 때 칸 하나의 문제."""

    field: str = Field(description='문제가 있는 칸 위치. 예: body.message')
    message: str = Field(description='무엇이 틀렸는지 (Pydantic 원문 메시지)')


class ErrorResponse(BaseModel):
    """모든 실패 응답의 모양. /openapi.json에 실려 프론트 타입으로도 생성된다."""

    code: ErrorCode
    message: str = Field(description='사용자에게 보여 줘도 되는 한국어 메시지')
    details: list[FieldError] | dict[str, Any] | None = None


class AppError(Exception):
    """엔드포인트에서 의도적으로 내는 에러. 예: raise AppError(404, ErrorCode.X, '메시지')"""

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _json(status_code: int, body: ErrorResponse) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=body.model_dump(mode='json'))


# 엔드포인트의 responses= 에 붙여 /docs와 OpenAPI에 에러 모양을 보여 주는 도우미.
# 예) @router.get(..., responses=error_responses(404, 422))
def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    return {code: {'model': ErrorResponse} for code in status_codes}


def register_error_handlers(app: FastAPI) -> None:
    """main.py에서 앱을 만들 때 한 번 불러 예외 처리기들을 등록한다."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return _json(
            exc.status_code, ErrorResponse(code=exc.code, message=exc.message, details=exc.details)
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic 검사 결과(exc.errors())를 [{field, message}] 목록으로 정리한다.
        # loc 예: ('body', 'message') → 'body.message'
        details = [
            FieldError(field='.'.join(str(p) for p in err['loc']), message=err['msg'])
            for err in exc.errors()
        ]
        return _json(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorResponse(
                code=ErrorCode.VALIDATION_ERROR,
                message='요청 형식이 올바르지 않습니다.',
                details=details,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 없는 주소(404), 틀린 메서드(405) 등 FastAPI/Starlette가 자동으로 내는 에러.
        code = {
            status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
            status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
        }.get(exc.status_code, ErrorCode.HTTP_ERROR)
        message = {
            ErrorCode.NOT_FOUND: '요청한 주소를 찾을 수 없습니다.',
            ErrorCode.METHOD_NOT_ALLOWED: '허용되지 않는 요청 방식입니다.',
        }.get(code, str(exc.detail))
        return _json(exc.status_code, ErrorResponse(code=code, message=message))

    # [예상 못 한 버그(500)는 처리기 대신 미들웨어로 잡는다]
    # 미들웨어: 모든 요청이 엔드포인트에 가기 전·후에 거치는 함수.
    # FastAPI의 exception_handler(Exception)로 잡으면 응답이 CORS 미들웨어 바깥에서 만들어져
    # CORS 허락 헤더가 빠진다. 그러면 브라우저는 우리 에러 본문 대신 'CORS 오류'만 보여 준다.
    # 그래서 여기서 잡는다. main.py에서 이 함수를 CORS보다 먼저 불러야
    # 이 미들웨어가 CORS 안쪽에 들어가 헤더가 붙는다 (나중에 추가한 미들웨어가 바깥쪽이 된다).
    @app.middleware('http')
    async def catch_unexpected(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception:
            # 원인은 서버 로그에만 남기고, 사용자에게는 내부 정보를 보여 주지 않는다.
            logger.exception('처리되지 않은 오류: %s %s', request.method, request.url.path)
            return _json(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                ErrorResponse(
                    code=ErrorCode.INTERNAL_ERROR,
                    message='서버에서 문제가 생겼습니다. 잠시 후 다시 시도해 주세요.',
                ),
            )
