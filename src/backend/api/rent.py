"""지역 월세 시세 조회 API — 프론트 '내 생활비' 화면이 이사할 집 월세 칸 옆에 보여 준다.

  GET /rent/{lawd_cd}   → RentEstimate (국토부 실거래가 원룸급 월세 중앙값, 출처·표본 수)

계산에는 get_rent 노드가 같은 값을 쓴다 (이사할 집 월세를 비워 두면 '추정'으로 채움).
화면은 이 값을 미리 보여 주고, 사용자가 직접 넣으면 그 값이 먼저다.
"""

from datetime import date

from fastapi import APIRouter, Request, status

from backend.api.errors import AppError, ErrorCode, error_responses
from backend.normalize.checks import load_regions
from backend.public_data.rent import RentEstimate, period_text, recent_months

router = APIRouter(prefix='/rent', tags=['rent'], responses=error_responses(422, 500))


@router.get('/{lawd_cd}', responses=error_responses(404))
async def get_rent(lawd_cd: str, request: Request) -> RentEstimate:
    """시군구 코드(5자리, 예: 12170 나주시)의 원룸급 월세 추정.

    코드표(data/lawd_codes.csv)에 없거나, 구를 더 골라야 하는 시(예: 성남시 41130)면 404.
    조회 기능이 꺼져 있으면(인증키 없음) status='missing'과 이유를 돌려준다.
    """
    region = next((r for r in load_regions() if r.lawd_cd == lawd_cd), None)
    if region is None or not region.selectable:
        raise AppError(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.REGION_NOT_FOUND,
            '월세를 조회할 수 있는 시군구 코드가 아닙니다.',
        )
    estimator = getattr(request.app.state, 'rent_estimator', None)
    if estimator is None:
        return RentEstimate(
            lawd_cd=lawd_cd,
            region=f'{region.sido} {region.sigungu}',
            monthly_rent_krw=None,
            period=period_text(recent_months(date.today())),
            source='국토교통부 전월세 실거래가',
            status='missing',
            reason='월세 조회가 꺼져 있어요 (서버에 공공데이터 인증키 없음)',
        )
    return await estimator.estimate(lawd_cd)
