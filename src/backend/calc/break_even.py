"""손익분기 연봉 — 연봉 비공개 공고가 '최소 얼마면 같은 돈이 남는지' 거꾸로 계산한다.

    손익분기 연봉 = 이 금액 이상이면 언제나 기준만큼 이상 남는 가장 작은 연봉 (만 원 단위)

화면 문장 "최소 ○○만 원 이상이면 같은 금액이 남습니다"가 거짓이 되지 않게 아래를 지킨다.
  - 그 연봉 '이상' 어디서나 기준 이상 남아야 한다 (세금이 뛰는 구간 때문에 다시 확인)
  - 기간이 정해진 지원(주거지원비)이 끝난 뒤에도 남아야 한다 → 지원을 뺀 값이 기본 답,
    지원 기간 중 값은 during_support_annual_salary_krw로 따로 준다
  - 최저임금보다 낮게 나오면 최저임금 연봉으로 올린다 (그보다 낮게 제안할 수는 없다)

[기준 — 무엇만큼 남아야 하나] (pick_baseline)
  1) 목표 금액: 사용자가 "매달 150만 원은 남아야 해"라고 정한 값 (target_monthly_remaining)
  2) 기준 공고: 사용자가 고른 공고·경우 (baseline_posting_id, baseline_case)
  3) 그 밖에는 아래 후보 중 매달 가장 많이 남는 것
     - 연봉이 공개된 공고의 각 경우 (예: 서울 회사 + 이사)
     - 지금 회사 그대로 (current_job, 현상 유지선)
  두 공고가 다 비공개여도 지금 회사나 목표 금액이 있으면 계산할 수 있다.

[어떻게 찾나]
1) 이분 탐색: '이 연봉이면 기준 이상 남는가?'를 범위를 반씩 줄여 가며 경계를 찾는다.
2) 위쪽 다시 확인: 간이세액표는 세율 구간이 바뀌는 곳(월 376만·584만·1,000만 원 등)에서 세금이
   한 번에 뛰어, 연봉이 조금 올라도 실수령액이 줄어드는 곳이 있다. 그래서 답 위로 300만 원을
   만 원씩 다시 확인하고, 모자라는 연봉이 나오면 그 위로 올린 뒤 거기서부터 다시 300만 원을 본다.
   (실제 표에서 줄어든 실수령액이 회복되는 거리는 최대 65만 원 — tests/test_break_even.py가 확인)
연봉 외의 값(주거비·교통비·고정비)은 월 잔여액 계산(remaining.py)과 똑같이 쓴다.

숫자만 계산한다 (LLM 없음, 설계 원칙).
"""

from typing import Literal

from pydantic import Field

from backend.calc import rates
from backend.calc.payroll import monthly_net
from backend.calc.remaining import (
    CASE_LABELS,
    MIN_MONTHLY_BASE,
    Case,
    Item,
    OutputModel,
    PostingResult,
    Scenario,
    UserProfile,
    housing_aid,
    monthly_remaining,
    weakest,
)
from backend.calc.text import won
from backend.normalize.schema import ComparisonRow, FieldStatus

STEP_KRW = 10_000  # 만 원 단위
MAX_ANNUAL_KRW = 1_000_000_000  # 10억 원까지 찾는다
MIN_ANNUAL_KRW = MIN_MONTHLY_BASE * 12  # remaining.py가 계산하는 가장 작은 연봉
RECHECK_STEPS = 300  # 답 위로 다시 확인할 칸 수 (300만 원)
_MIN_STEP = -(-MIN_ANNUAL_KRW // STEP_KRW)  # 만 원 단위로 올림
_MAX_STEP = MAX_ANNUAL_KRW // STEP_KRW
_MIN_WAGE_STEP = -(-rates.MINIMUM_WAGE_ANNUAL // STEP_KRW)

BaselineKind = Literal['target', 'posting', 'current_job']


class Baseline(OutputModel):
    """비교 기준: 이만큼은 남아야 한다."""

    kind: BaselineKind = Field(
        description='target=사용자 목표 금액 / posting=연봉 공개 공고 / current_job=지금 회사'
    )
    label: str = Field(description='화면 표시용 (예: "(주)새늘소재 · 회사 근처로 이사")')
    monthly_remaining: int
    status: FieldStatus
    posting_id: str | None = None
    case: Case | None = None


class BreakEvenCase(OutputModel):
    case: Case
    label: str
    annual_salary_krw: int | None = Field(
        description=(
            '이 금액 이상이면 늘 기준 이상 남는 최소 세전 연봉(원, 만 원 단위). '
            '기간이 정해진 지원이 끝난 뒤 기준. 못 구하면 null'
        )
    )
    monthly_remaining: int | None = Field(
        default=None, description='그 연봉일 때의 월 잔여액 (기준 이상)'
    )
    net_income_krw: int | None = Field(
        default=None, description='그 연봉일 때의 세후 월급 (화면이 거꾸로 계산하지 않게)'
    )
    housing: Item | None = Field(
        default=None, description='이 답에 쓴 주거비 (주거지원비가 끝난 뒤 기준)'
    )
    transport: Item | None = Field(default=None, description='이 답에 쓴 교통비')
    during_support_annual_salary_krw: int | None = Field(
        default=None, description='주거지원비를 받는 동안만 따진 손익분기 연봉 (지원이 있을 때만)'
    )
    support_excluded: bool = Field(
        default=False,
        description='주거지원비를 빼고 계산했는지 (지원이 끝난 뒤 기준). 화면이 주거비 옆에 알린다',
    )
    floor: Literal['minimum_wage'] | None = Field(
        default=None,
        description=(
            'minimum_wage=계산한 연봉이 최저임금보다 낮아 최저임금 연봉으로 올림. '
            '이때 monthly_remaining은 기준보다 많다'
        ),
    )
    status: FieldStatus = Field(description='기준과 연봉 외 값(비용·수당 등) 중 가장 약한 신뢰도')
    reason: str | None = Field(default=None, description='못 구한 이유')
    missing: list[str] = Field(default_factory=list, description='계산에 필요한데 없는 값')
    notes: list[str] = Field(default_factory=list, description='화면에 함께 보여 줄 안내')


class BreakEven(OutputModel):
    posting_id: str
    company: str | None
    baseline: Baseline
    cases: list[BreakEvenCase]


# ── 지원 기간이 끝난 뒤 (장기) ───────────────────────────────────


def long_term(row: ComparisonRow) -> tuple[ComparisonRow, list[str]]:
    """주거지원비를 뺀 사본과 그 안내. 지원이 없으면 원래 행과 빈 안내를 돌려준다.

    주거지원비는 대개 기간이 있다 (공고에 기간이 없으면 물어볼 것 목록에 들어간다).
    기간을 모를 때도 '끝날 수 있다'고 보고 뺀다 — 남는 돈을 부풀리지 않는 쪽.
    주거지원비는 이사 경우에만 쓰이므로 안내도 이사 경우에만 붙인다.
    """
    aid = housing_aid(row)
    if aid is None:
        return row, []
    amount, months = aid
    copy = row.model_copy(deep=True)
    copy.housing_allowance_monthly_krw.value = None
    period = f'{months}개월 뒤 끝나므로' if months else '지원 기간을 몰라서'
    return copy, [f'주거지원비 월 {won(amount)}은 {period} 지원이 끝난 뒤를 기준으로 계산했어요.']


# ── 기준 고르기 ──────────────────────────────────────────────────


def _posting_candidates(results: list[PostingResult]) -> list[Baseline]:
    return [
        Baseline(
            kind='posting',
            label=f'{r.company or r.posting_id} · {s.label}',
            monthly_remaining=s.monthly_remaining,
            status=s.status,
            posting_id=r.posting_id,
            case=s.case,
        )
        for r in results
        for s in r.scenarios
        if s.applicable and s.monthly_remaining is not None
    ]


def current_job_baseline(profile: UserProfile) -> tuple[Baseline | None, list[str]]:
    """지금 회사 그대로 다닐 때 매달 남는 돈. 돌려주는 값: (기준, 없는 값 목록)."""
    job = profile.current_job
    if job is None:
        return None, []
    missing = []
    if job.annual_salary_krw < MIN_ANNUAL_KRW:
        missing.append('지금 연봉 (금액이 너무 적음, 단위 확인)')
    if job.transport is None:
        missing.append('지금 회사까지 교통비')
    if profile.current_housing is None:
        missing.append('지금 집 주거비')
    if profile.fixed_expenses is None:
        missing.append('고정비')
    if missing:
        return None, missing

    gross = job.annual_salary_krw // 12
    meal = min(job.meal_nontaxable_monthly_krw, rates.MEAL_NONTAXABLE_LIMIT, gross)
    net = monthly_net(gross, meal, profile.family, profile.children_8_to_20).net
    fixed = sum(e.monthly_krw for e in profile.fixed_expenses)
    housing = profile.current_housing
    baseline = Baseline(
        kind='current_job',
        label='지금 회사 그대로',
        monthly_remaining=net - housing.value - job.transport.value - fixed,
        status=weakest([job.status, housing.status, job.transport.status, 'user_input']),
    )
    return baseline, []


def pick_baseline(
    results: list[PostingResult], profile: UserProfile
) -> tuple[Baseline | None, list[str]]:
    """기준을 고른다. results는 지원이 끝난 뒤(long_term) 기준 결과.

    돌려주는 값: (기준, 사용자에게 알릴 말 목록)
    """
    if profile.target_monthly_remaining is not None:
        baseline = Baseline(
            kind='target',
            label='목표 금액',
            monthly_remaining=profile.target_monthly_remaining,
            status='user_input',
        )
        return baseline, []

    notes = []
    postings = _posting_candidates(results)
    wanted, wanted_case = profile.baseline_posting_id, profile.baseline_case
    if wanted is not None or wanted_case is not None:
        chosen = [
            b
            for b in postings
            if (wanted is None or b.posting_id == wanted)
            and (wanted_case is None or b.case == wanted_case)
        ]
        if chosen:
            return max(chosen, key=lambda b: b.monthly_remaining), []
        what = ' · '.join(
            x
            for x in (
                f'공고 {wanted}' if wanted else None,
                CASE_LABELS[wanted_case] if wanted_case else None,
            )
            if x
        )
        notes.append(
            f'고른 기준({what})은 남는 돈을 알 수 없어서 가장 많이 남는 경우와 비교했어요.'
        )

    current, current_missing = current_job_baseline(profile)
    if current_missing:
        notes.append(
            '지금 회사 정보 중 ' + ', '.join(current_missing) + ' 값이 없어서 '
            '"지금 회사 그대로" 기준은 쓰지 못했어요.'
        )
    candidates = postings + ([current] if current else [])
    if not candidates:
        if not current_missing:
            notes.append(
                '비교할 기준이 없어요. 지금 연봉이나 매달 남아야 하는 목표 금액을 알려 주세요.'
            )
        return None, notes
    return max(candidates, key=lambda b: b.monthly_remaining), notes


# ── 손익분기 연봉 찾기 ───────────────────────────────────────────


def _with_salary(row: ComparisonRow, annual: int) -> ComparisonRow:
    """연봉 칸만 annual로 바꾼 사본 (원본 행은 건드리지 않는다).

    status를 confirmed로 두는 이유: 세후 월급의 신뢰도가 우리가 넣은 가상의 연봉이 아니라
    나머지 값(수당·식대 등)의 신뢰도만 반영하게 하려고. 결과에 이 연봉이 그대로 나가지는 않는다.
    """
    copy = row.model_copy(deep=True)
    copy.salary_type.value, copy.salary_type.status = 'annual', 'confirmed'
    copy.annual_salary_krw.value, copy.annual_salary_krw.status = annual, 'confirmed'
    copy.monthly_salary_krw.value = None
    return copy


def _scenario_at(
    row: ComparisonRow, profile: UserProfile, step: int, case: Case
) -> tuple[PostingResult, Scenario]:
    result = monthly_remaining(_with_salary(row, step * STEP_KRW), profile)
    return result, next(s for s in result.scenarios if s.case == case)


def _enough(row: ComparisonRow, profile: UserProfile, step: int, case: Case, target: int) -> bool:
    """연봉 step만 원이면 기준(target) 이상 남는가."""
    _, s = _scenario_at(row, profile, step, case)
    return s.monthly_remaining is not None and s.monthly_remaining >= target


def _lowest_safe_step(row: ComparisonRow, profile: UserProfile, case: Case, target: int) -> int:
    """그 이상 어디서나 기준 이상 남는 가장 작은 연봉(만 원 단위). 최댓값에서 충분해야 부른다."""
    # 1) 이분 탐색: lo는 '부족'(또는 계산 범위 밖), hi는 '충분'으로 유지한다.
    lo, hi = _MIN_STEP - 1, _MAX_STEP
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _enough(row, profile, mid, case, target):
            hi = mid
        else:
            lo = mid
    # 2) 위쪽 다시 확인: 모자라는 연봉이 나오면 답을 그 위로 올리고, 거기서부터 다시 본다.
    step = hi + 1
    while step <= min(hi + RECHECK_STEPS, _MAX_STEP):
        if not _enough(row, profile, step, case, target):
            hi = step + 1
        step += 1
    return hi


def _search(
    row: ComparisonRow, profile: UserProfile, baseline: Baseline, case: Case
) -> BreakEvenCase:
    target = baseline.monthly_remaining
    top_result, top = _scenario_at(row, profile, _MAX_STEP, case)
    # 연봉 외 값들의 신뢰도: 비용 + 세후 월급(가상 연봉은 confirmed라 수당·식대 신뢰도만 남음)
    values = [top.housing.status, top.transport.status, top_result.fixed_expenses.status]
    status = weakest([*values, top_result.net_income.status, baseline.status])
    common = {'case': case, 'label': top.label}

    if not top.applicable:
        return BreakEvenCase(
            **common, annual_salary_krw=None, status='user_input', reason='해당 없는 경우'
        )
    if top.monthly_remaining is None:
        return BreakEvenCase(
            **common,
            annual_salary_krw=None,
            status='missing',
            reason='계산에 필요한 값이 없음',
            missing=[m for m in top.missing if m != '세후 월급'],
        )
    if top.monthly_remaining < target:
        return BreakEvenCase(
            **common,
            annual_salary_krw=None,
            status=status,
            reason=f'연봉 {MAX_ANNUAL_KRW // 100_000_000}억 원으로도 기준만큼 남지 않음',
        )

    step = _lowest_safe_step(row, profile, case, target)
    if step > _MAX_STEP:  # 다시 확인하다 범위 끝을 넘음
        return BreakEvenCase(
            **common,
            annual_salary_krw=None,
            status=status,
            reason=f'연봉 {MAX_ANNUAL_KRW // 100_000_000}억 원 근처까지도 기준만큼 남지 않음',
        )
    floor = None
    if step < _MIN_WAGE_STEP:
        # 그 이상 어디서나 충분하므로 최저임금 연봉에서도 충분하다
        step, floor = _MIN_WAGE_STEP, 'minimum_wage'
    found_result, found = _scenario_at(row, profile, step, case)
    return BreakEvenCase(
        **common,
        annual_salary_krw=step * STEP_KRW,
        monthly_remaining=found.monthly_remaining,
        net_income_krw=found_result.net_income.value,
        housing=found.housing,
        transport=found.transport,
        status=status,
        floor=floor,
    )


def break_even(row: ComparisonRow, profile: UserProfile, baseline: Baseline) -> BreakEven:
    """공고 1건의 경우별 손익분기 연봉 (지원이 끝난 뒤 기준 + 지원 기간 중 값)."""
    long_row, support_notes = long_term(row)
    cases = []
    for case in ('commute', 'move'):
        c = _search(long_row, profile, baseline, case)
        if c.reason == '해당 없는 경우':
            cases.append(c)
            continue
        # 수습·연장근로 같은 공고 안내는 공고 결과(PostingResult.notes)에 이미 있다.
        # 여기에는 손익분기 연봉에만 해당하는 안내(주거지원비 기간)만 단다.
        # 최저임금으로 올린 경우는 floor 칸으로 알린다 (화면이 문장을 맞춰 쓴다).
        notes: list[str] = []
        if case == 'move' and long_row is not row:  # 주거지원비는 이사 경우에만 들어간다
            notes += support_notes
            c.support_excluded = True
            if c.annual_salary_krw is not None:
                during = _search(row, profile, baseline, case).annual_salary_krw
                if during != c.annual_salary_krw:
                    c.during_support_annual_salary_krw = during
        c.notes = notes + c.notes
        cases.append(c)
    return BreakEven(posting_id=row.id, company=row.company.value, baseline=baseline, cases=cases)


def needs_break_even(row: ComparisonRow, result: PostingResult) -> bool:
    """연봉 금액이 없어 세후 월급을 못 구한 공고 (비공개·희망 연봉 질문·읽지 못함)."""
    return (
        result.net_income.value is None
        and result.payslip is None
        and not (row.annual_salary_krw.value or row.monthly_salary_krw.value)
    )
