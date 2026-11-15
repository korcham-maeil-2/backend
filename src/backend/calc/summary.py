"""비교표 전체 → 계산 결과 한 덩어리. calculate 노드가 부른다.

    1) 공고마다 월 잔여액 (remaining.py, 통근 / 이사)
    2) 연봉 비공개 공고마다 손익분기 연봉 (break_even.py)

잘못된 값이 와도 그래프가 멈추지 않게, 문제가 있는 부분만 errors에 적고 나머지는 계산한다.
돌려주는 값은 체크포인터(SQLite)에 저장되도록 평범한 dict다.
"""

from typing import Any

from pydantic import Field, ValidationError

from backend.calc.break_even import (
    BreakEven,
    break_even,
    long_term,
    needs_break_even,
    pick_baseline,
)
from backend.calc.remaining import OutputModel, PostingResult, UserProfile, monthly_remaining
from backend.normalize.schema import ComparisonRow


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


def calculate_all(table: list[dict] | None, profile: dict | None) -> dict[str, Any]:
    """비교표(dict 목록)와 사용자 정보(dict)로 공고별 잔여액과 손익분기 연봉을 만든다."""
    errors: list[str] = []
    user: UserProfile | None = None
    if profile is not None:
        try:
            user = UserProfile.model_validate(profile)
        except ValidationError as e:
            errors.append(f'사용자 정보 형식이 잘못돼 사용하지 않았습니다: {e.error_count()}곳')

    table = table or []
    rows, results = [], []
    for i, raw in enumerate(table, start=1):
        try:
            row = ComparisonRow.model_validate(raw)
        except ValidationError as e:
            errors.append(f'{i}번째 공고 형식이 잘못돼 계산하지 못했습니다: {e.error_count()}곳')
            continue
        rows.append(row)
        results.append(monthly_remaining(row, user))

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
    )
    return result.model_dump(mode='json')


def _unknown_posting_ids(user: UserProfile, rows: list[ComparisonRow]) -> list[str]:
    """사용자 정보가 비교표에 없는 공고 ID를 가리키면 알린다 (조용히 무시하지 않게)."""
    ids = {row.id for row in rows}
    given = set(user.postings) | ({user.baseline_posting_id} - {None})
    unknown = sorted(given - ids)
    if not unknown:
        return []
    return [f'비교표에 없는 공고 ID라 쓰지 않았습니다: {", ".join(unknown)}']


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
            '사용자 정보(지금 집 주거비·교통비 등)가 없어 손익분기 연봉을 계산하지 않았습니다.'
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
            notes.append('기준 공고의 주거지원비는 지원이 끝난 뒤 기준으로 비교했습니다.')

    out = []
    for row in targets:
        try:
            out.append(break_even(row, user, baseline))
        except ValueError as e:  # 계산 중 예상 못 한 값 — 이 공고만 건너뛰고 그래프는 계속
            errors.append(f'{row.id} 손익분기 연봉을 계산하지 못했습니다: {e}')
    return out, notes
