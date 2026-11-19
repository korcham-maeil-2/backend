"""정착 플랜에 함께 보여 줄 참고 정보 — 세션과 상관없이 지역·전국 단위로 조회한다 (이슈 #18).

  GET /plan/savings              → SavingsList   은행 적금 12개월 기본금리 높은 순
                                                 (금감원, 가입 권유 아님)
  GET /plan/training/{lawd_cd}   → TrainingList  근무지 근처 재직자 오프라인 강의 (고용24)
                                                 ?ncs=20 이면 그 직무(NCS 대분류) 과정 먼저
  POST /plan/policy-terms        → TermsResult   플랜 정책 카드의 지원 내용·주의·신청 조건
                                                 (정책 원문을 Claude가 읽음, 개인정보는 보내지 않음)

키가 없으면 status='off', 조회가 실패하면 status='unavailable'로 이유와 함께 돌려준다
(화면은 그 칸만 숨긴다).
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, Field

from backend.api.errors import AppError, ErrorCode, error_responses
from backend.normalize.checks import region_by_code
from backend.public_data.finlife import SavingsList, SavingsSource
from backend.public_data.training import TrainingList, TrainingSource
from backend.support.policies import today_kst
from backend.support.terms import MAX_IDS, PolicyTermsService, TermsResult

router = APIRouter(prefix='/plan', tags=['plan'], responses=error_responses(422, 500))


@router.get('/savings')
async def get_savings(request: Request) -> SavingsList:
    """은행 적금 12개월 기본금리 높은 순 몇 개 (공시 기준월·출처 포함). 가입을 권하지 않는다."""
    source: SavingsSource = request.app.state.savings_source
    return await asyncio.to_thread(source.get)


@router.get('/training/{lawd_cd}', responses=error_responses(404))
async def get_training(
    lawd_cd: str,
    request: Request,
    ncs: Annotated[str | None, Query(pattern=r'^\d{2}$', description='직무 NCS 대분류')] = None,
) -> TrainingList:
    """근무지(시군구 코드)의 재직자 오프라인 강의. 없으면 시도로 넓힌다 (scope='sido')."""
    if len(lawd_cd) != 5 or region_by_code(lawd_cd) is None:
        raise AppError(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.REGION_NOT_FOUND,
            '강의를 찾을 수 있는 시군구 코드가 아닙니다.',
        )
    source: TrainingSource = request.app.state.training_source
    return await asyncio.to_thread(source.get, lawd_cd, today_kst(), ncs)


class TermsRequest(BaseModel):
    """플랜에 보이는 정책 번호와 그 근무지 지역 (공개 정보만, 사용자 정보는 받지 않는다)."""

    lawd_cds: list[Annotated[str, Field(pattern=r'^\d{5}$')]] = Field(max_length=5)
    ids: list[Annotated[str, Field(pattern=r'^[0-9A-Za-z]{1,32}$')]] = Field(max_length=MAX_IDS)


@router.post('/policy-terms')
async def get_policy_terms(body: TermsRequest, request: Request) -> TermsResult:
    """정책마다 무엇을 주는지·주의할 점·신청 조건 (원문 인용 포함). 처음 보는 정책만 Claude가
    읽고 저장해 둔다. 정책을 읽지 않는 서버(키 없음 등)면 status='off'
    (화면은 원문 요약을 보여 준다)."""
    service: PolicyTermsService | None = getattr(request.app.state, 'policy_terms', None)
    if service is None:
        return TermsResult(status='off', terms={})
    terms = await asyncio.to_thread(service.get, body.lawd_cds, body.ids)
    return TermsResult(status='ok', terms=terms)
