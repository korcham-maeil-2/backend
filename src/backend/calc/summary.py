"""비교표 전체 → 계산 결과 한 덩어리. calculate 노드가 부른다.

    1) 공고마다 월 잔여액 (remaining.py, 통근 / 이사)
    2) 연봉 비공개 공고마다 손익분기 연봉 (break_even.py)

이사할 집 월세를 사용자가 비워 두면, 그 회사 지역의 실거래가 추정값(get_rent 노드,
public_data/rent.py)을 '추정'으로 채워 계산한다. 사용자가 넣은 값이 있으면 그 값이 먼저다.

잘못된 값이 와도 그래프가 멈추지 않게, 문제가 있는 부분만 errors에 적고 나머지는 계산한다.
돌려주는 값은 체크포인터(SQLite)에 저장되도록 평범한 dict다.
"""

import logging
from typing import Any

from pydantic import Field, ValidationError

from backend.calc.break_even import (
    BreakEven,
    break_even,
    long_term,
    needs_break_even,
    pick_baseline,
)
from backend.calc.remaining import (
    Cost,
    OutputModel,
    PostingCosts,
    PostingResult,
    UserProfile,
    monthly_remaining,
)
from backend.normalize.schema import ComparisonRow
from backend.public_data.rent import RentEstimate

logger = logging.getLogger(__name__)


class CalculationResult(OutputModel):
    """calculate 노드의 결과 = 세션 응답의 result 칸. 프론트 결과 화면이 그대로 쓴다."""

    rows: int = Field(description='비교표 행 수')
    has_user_profile: bool = Field(description='사용자 정보를 받아 계산했는지')
    postings: list[PostingResult] = Field(description='공고별 세후 월급·통근/이사 월 잔여액')
    break_even: list[BreakEven] = Field(
        default_factory=list, description='연봉 비공개 공고별 손익분기 연봉'
    )
    break_even_note: str | None = Field(
        default=None, description='손익분기 연봉 관련 안내 (기준을 못 정한 이유 등)'
    )
    errors: list[str] = Field(default_factory=list, description='계산하지 못한 부분과 이유')
    rent_estimates: list[RentEstimate] = Field(
        default_factory=list,
        description=(
            '회사 지역별 원룸급 월세 추정 (국토부 실거래가). 이사할 집 월세를 비우면 이 값을 씀'
        ),
    )


def calculate_all(
    table: list[dict] | None, profile: dict | None, rents: dict[str, dict] | None = None
) -> dict[str, Any]:
    """비교표(dict 목록)와 사용자 정보(dict)로 공고별 잔여액과 손익분기 연봉을 만든다.

    rents: 시군구 코드 → 월세 추정(RentEstimate의 dict). get_rent 노드가 만든다.
    """
    errors: list[str] = []
    user: UserProfile | None = None
    if profile is not None:
        try:
            user = UserProfile.model_validate(profile)
        except ValidationError as e:
            errors.append(f'사용자 정보 형식이 잘못돼서 쓰지 않았어요: {e.error_count()}곳')

    table = table or []
    rows = []
    for i, raw in enumerate(table, start=1):
        try:
            rows.append(ComparisonRow.model_validate(raw))
        except ValidationError as e:
            errors.append(f'{i}번째 공고 형식이 잘못돼서 계산하지 못했어요: {e.error_count()}곳')
    estimates = _rent_estimates(rents)
    if user is not None:
        user = _fill_move_housing(user, rows, estimates)
    results = [monthly_remaining(row, user) for row in rows]

    if user is not None:
        errors += _unknown_posting_ids(user, rows)
    break_evens, notes = _break_evens(rows, results, user, errors)
    result = CalculationResult(
        rows=len(table),
        has_user_profile=user is not None,
        postings=results,
        break_even=break_evens,
        break_even_note=' '.join(notes) or None,
        errors=errors,
        rent_estimates=[
            estimates[c] for c in sorted({_lawd(r) for r in rows} - {None}) if c in estimates
        ],
    )
    return result.model_dump(mode='json')


def _lawd(row: ComparisonRow) -> str | None:
    location = row.location.value
    return location.lawd_cd if location else None


def _rent_estimates(rents: dict[str, dict] | None) -> dict[str, RentEstimate]:
    out = {}
    for code, raw in (rents or {}).items():
        try:
            out[code] = RentEstimate.model_validate(raw)
        except ValidationError:
            logger.warning('저장된 월세 추정이 지금 모양과 맞지 않아 쓰지 않았습니다: %s', code)
    return out


def _fill_move_housing(
    user: UserProfile, rows: list[ComparisonRow], estimates: dict[str, RentEstimate]
) -> UserProfile:
    """이사할 집 월세가 빈 공고에 그 지역 월세 추정값을 '추정'으로 넣은 사본.

    채우지 않는 경우: 사용자가 값을 넣음 / 이사를 생각하지 않음(move_possible=False) /
    회사가 사택·기숙사를 줌 (시세가 아니라 본인 부담 금액을 물어야 한다) / 추정값이 없음.
    """
    postings = dict(user.postings)
    for row in rows:
        estimate = estimates.get(_lawd(row) or '')
        costs = user.costs_for(row.id)
        if (
            estimate is None
            or estimate.monthly_rent_krw is None
            or costs.move_housing is not None
            or costs.move_possible is False
            or row.company_housing.value is True
        ):
            continue
        rent = Cost(value=estimate.monthly_rent_krw, status='estimated', source=estimate.source)
        postings[row.id] = PostingCosts(**{**costs.model_dump(), 'move_housing': rent})
    return user.model_copy(update={'postings': postings})


def _unknown_posting_ids(user: UserProfile, rows: list[ComparisonRow]) -> list[str]:
    """사용자 정보가 비교표에 없는 공고 ID를 가리키면 알린다 (조용히 무시하지 않게)."""
    ids = {row.id for row in rows}
    given = set(user.postings) | ({user.baseline_posting_id} - {None})
    unknown = sorted(given - ids)
    if not unknown:
        return []
    return [f'비교표에 없는 공고 ID라서 쓰지 않았어요: {", ".join(unknown)}']


def _break_evens(
    rows: list[ComparisonRow],
    results: list[PostingResult],
    user: UserProfile | None,
    errors: list[str],
) -> tuple[list[BreakEven], list[str]]:
    """연봉 비공개 공고마다 손익분기 연봉. 돌려주는 값: (결과 목록, 화면 안내 목록)."""
    targets = [row for row, r in zip(rows, results, strict=True) if needs_break_even(row, r)]
    if not targets:
        return [], []
    if user is None:
        return [], [
            '사용자 정보(지금 집 주거비·교통비 등)가 없어서 손익분기 연봉을 계산하지 않았어요.'
        ]

    # 기준은 '지원이 끝난 뒤' 남는 돈으로 비교한다 (기준 공고의 주거지원비가 기준을 부풀리지 않게)
    long_rows = [long_term(row) for row in rows]
    long_results = [monthly_remaining(r, user) for r, _ in long_rows]
    baseline, notes = pick_baseline(long_results, user)
    if baseline is None:
        return [], notes
    if baseline.kind == 'posting':
        i = next(i for i, r in enumerate(rows) if r.id == baseline.posting_id)
        if long_rows[i][0] is not rows[i]:
            notes.append('기준 공고의 주거지원비는 지원이 끝난 뒤를 기준으로 비교했어요.')

    out = []
    for row in targets:
        try:
            out.append(break_even(row, user, baseline))
        except ValueError:  # 계산 중 예상 못 한 값 — 이 공고만 건너뛰고 그래프는 계속
            logger.exception('손익분기 연봉 계산 실패: %s', row.id)  # 원문은 로그로만
            errors.append(f'{row.company.value or row.id}의 최소 연봉을 계산하지 못했어요.')
    return out, notes
