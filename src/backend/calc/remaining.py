"""월 잔여액 계산 — 매달 손에 남는 돈.

    월 잔여액 = 실수령액 − 주거비 − 교통비 − 고정비

공고 1건마다 두 경우를 계산한다.
  - commute (지금 집에서 통근): 주거비 = 지금 집 주거비, 교통비 = 지금 집 → 회사 통근비
  - move    (회사 근처로 이사): 주거비 = 새 집 주거비 − 주거지원비, 교통비 = 이사 후 교통비

[출처·신뢰도]
결과의 값마다 status(신뢰도)와 source(어디서 온 값인지)를 함께 담는다.
  confirmed=공고에서 확인 / estimated=추정 / user_input=사용자 입력 / missing=값이 없음
합계(월 잔여액)의 status는 들어간 값 가운데 가장 약한 것을 따른다 (missing > estimated > ...).
값 하나라도 없으면(missing) 월 잔여액은 계산하지 않고 null로 두고, 무엇이 없는지 missing에 적는다.
빈 값을 0원으로 몰래 채우지 않는다 — 남는 돈이 실제보다 많아 보이는 걸 막기 위해서다.

[계산 규칙]
  - 회사 수당(교통비·통신비 등)은 과세 급여로 보고 세전 월급에 더한 뒤 세금을 뺀다.
  - 회사가 식사를 현물로 주면 현금 식대는 비과세가 아니다.
  - 연봉 칸에 금액이 있으면 연봉 종류가 '비공개'여도 그 금액을 쓴다 (확인 화면에서 넣은 값).
  - 통근이 불가능한 공고처럼 해당 없는 경우는 applicable=False, 잔여액은 null.

[단순화한 것] (화면에 notes로 함께 보여 준다)
  - 주거지원비는 세금을 빼지 않고 이사 경우 주거비에서 그대로 뺀다.
  - 연봉은 12개월로 똑같이 나눈다. 성과급·연장근로수당은 넣지 않는다.
  - 수습 기간에 급여가 줄어드는 것은 계산에 넣지 않고 notes에만 적는다.

이 파일은 숫자만 계산한다 (LLM 없음, 설계 원칙).
"""

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.calc import rates
from backend.calc.payroll import Payslip, monthly_net
from backend.calc.text import won
from backend.normalize.schema import MAX_KRW, ComparisonRow, FieldStatus, MoneyCell, _Cell

logger = logging.getLogger(__name__)

Case = Literal['commute', 'move']
CASE_LABELS: dict[Case, str] = {'commute': '지금 집에서 통근', 'move': '회사 근처로 이사'}

# status 약한 순서. 숫자가 클수록 믿기 어렵다.
_WEAKNESS: dict[FieldStatus, int] = {'confirmed': 0, 'user_input': 1, 'estimated': 2, 'missing': 3}


def weakest(statuses: list[FieldStatus]) -> FieldStatus:
    """여러 값의 신뢰도 중 가장 약한 것. 비어 있으면 confirmed."""
    return max(statuses, key=_WEAKNESS.__getitem__, default='confirmed')


class InputModel(BaseModel):
    """사용자가 보내는 값. 모르는 칸(오타 등)은 조용히 버리지 않고 422로 알린다."""

    model_config = ConfigDict(extra='forbid')


class OutputModel(BaseModel):
    """계산 결과. 기본값이 있어도 응답에는 항상 들어가므로, OpenAPI(→ 프론트 TS 타입)에서
    '있을 수도 없을 수도 있는 칸(?:)'이 아니라 '항상 있는 칸'으로 보이게 한다."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


# ── 입력: 사용자 정보 ────────────────────────────────────────────


class Cost(InputModel):
    """사용자 쪽 금액 하나 (월, 원). 기본 신뢰도는 '사용자 입력'.

    월세 실거래가 추정값처럼 코드가 추정한 값이면 status='estimated'와 출처를 넣는다.
    """

    value: int = Field(ge=0, le=MAX_KRW, description='월 금액(원)')
    # 'missing'은 받지 않는다. 값이 없으면 Cost 자체를 비운다(None) — 0원으로 계산되는 걸 막기 위해.
    status: Literal['confirmed', 'estimated', 'user_input'] = 'user_input'
    source: str | None = Field(default=None, description='값의 출처 (예: 국토부 실거래가 중앙값)')


class CurrentJob(InputModel):
    """지금 다니는 회사 (손익분기 연봉의 '현상 유지' 기준). 지금 집에서 다닌다고 본다."""

    annual_salary_krw: int = Field(ge=0, le=MAX_KRW, description='지금 세전 고정연봉(원)')
    meal_nontaxable_monthly_krw: int = Field(
        default=0, ge=0, le=MAX_KRW, description='연봉에 포함된 비과세 식대 월액(원)'
    )
    transport: Cost | None = Field(default=None, description='지금 회사까지 월 교통비')
    status: Literal['confirmed', 'estimated', 'user_input'] = 'user_input'
    source: str | None = Field(default=None, description='예: 2026년 10월 급여명세서')


class FixedExpense(InputModel):
    """고정비 한 줄 (학자금 상환, 통신비 등)."""

    name: str = Field(min_length=1)
    monthly_krw: int = Field(ge=0, le=MAX_KRW)


class PostingCosts(InputModel):
    """공고(회사 위치)마다 달라지는 비용. 모르면 비워 둔다(None)."""

    commute_transport: Cost | None = Field(
        default=None, description='지금 집에서 이 회사까지 월 통근 교통비'
    )
    move_housing: Cost | None = Field(
        default=None, description='회사 근처로 이사했을 때 월 주거비 (월세 + 관리비)'
    )
    move_transport: Cost | None = Field(
        default=None, description='이사한 뒤 월 교통비 (출퇴근 + 본가 왕복 등)'
    )
    commute_possible: bool | None = Field(
        default=None, description='지금 집에서 통근할 수 있는지. False면 통근 경우는 해당 없음'
    )
    move_possible: bool | None = Field(
        default=None, description='이사를 고려하는지. False면 이사 경우는 해당 없음'
    )


class UserProfile(InputModel):
    """계산에 필요한 사용자 정보. 공고에 없는 값은 모두 여기서 온다."""

    family: int = Field(default=1, ge=1, le=20, description='소득세용 공제대상가족 수 (본인 포함)')
    children_8_to_20: int = Field(default=0, ge=0, le=20)
    current_housing: Cost | None = Field(
        default=None, description='지금 집 월 주거비 (월세 + 관리비, 본가면 생활비로 드리는 돈)'
    )
    fixed_expenses: list[FixedExpense] | None = Field(
        default=None,
        description='이사와 관계없이 매달 나가는 돈. 아직 모르면 null, 없다고 답했으면 []',
    )
    default_costs: PostingCosts = Field(
        default_factory=PostingCosts, description='공고별 값이 없을 때 쓰는 값'
    )
    postings: dict[str, PostingCosts] = Field(
        default_factory=dict, description='공고 ID(posting-1 …)별 비용'
    )
    baseline_posting_id: str | None = Field(
        default=None,
        description='손익분기 연봉의 기준 공고 ID. 비우면 월 잔여액이 가장 큰 공고',
    )
    baseline_case: Case | None = Field(
        default=None,
        description='기준 공고에서 쓸 경우 (commute/move). 비우면 그 공고에서 더 많이 남는 쪽',
    )
    current_job: CurrentJob | None = Field(
        default=None, description='지금 회사. 있으면 손익분기 연봉의 기준 후보가 된다'
    )
    target_monthly_remaining: int | None = Field(
        default=None,
        ge=0,
        le=MAX_KRW,
        description='매달 남아야 하는 목표 금액. 있으면 손익분기 기준으로 가장 먼저 쓴다',
    )
    age: int | None = Field(
        default=None,
        ge=15,
        le=99,
        description='만 나이. 청년 지원 정책을 나이 조건으로 거르는 데만 쓴다 (계산에는 안 씀)',
    )

    @model_validator(mode='after')
    def _children_fewer_than_family(self):
        # 소득세 간이세액표 규칙. 여기서 막아야 공고마다 같은 오류가 반복되지 않는다.
        if self.children_8_to_20 >= self.family:
            raise ValueError('8~20세 자녀 수는 공제대상가족 수(본인 포함)보다 작아야 합니다.')
        return self

    def costs_for(self, posting_id: str) -> PostingCosts:
        """이 공고의 비용. 공고별 값이 비어 있는 칸은 default_costs로 채운다."""
        own = self.postings.get(posting_id)
        if own is None:
            return self.default_costs
        merged = {
            k: getattr(own, k) if getattr(own, k) is not None else getattr(self.default_costs, k)
            for k in PostingCosts.model_fields
        }
        return PostingCosts(**merged)


# ── 출력 ────────────────────────────────────────────────────────


class Item(OutputModel):
    """결과의 값 하나와 그 출처·신뢰도."""

    label: str
    value: int | None
    status: FieldStatus
    source: str | None = None


class Scenario(OutputModel):
    case: Case
    label: str
    applicable: bool = Field(default=True, description='False면 해당 없는 경우 (예: 통근 불가)')
    monthly_remaining: int | None = Field(description='월 잔여액(원). 값이 모자라면 null')
    status: FieldStatus = Field(description='월 잔여액의 신뢰도 (들어간 값 중 가장 약한 것)')
    housing: Item
    transport: Item
    missing: list[str] = Field(default_factory=list, description='계산에 필요한데 없는 값')
    assumed_zero: list[str] = Field(
        default_factory=list,
        description='비어 있어 0원으로 놓고 계산한 값 (예: 교통비). 실제로는 더 들 수 있다',
    )


class PostingResult(OutputModel):
    posting_id: str
    company: str | None
    net_income: Item = Field(description='세후 월급 (실수령액)')
    payslip: dict[str, int] | None = Field(default=None, description='공제 내역 (계산했을 때만)')
    allowances: Item = Field(
        description='회사 수당 합계 (교통비·통신비 등). 세후 월급에 이미 들어 있음'
    )
    # 기본값이 있어야 이 칸이 생기기 전에 저장된 세션도 검증을 통과한다 (api/sessions.py)
    pay_on_top: Item | None = Field(
        default=None,
        description=(
            '연봉·월급과 따로 매달 받는 돈 (별도 지급 식대 + 수당). 세후 월급에 이미 들어 있음. '
            '연봉만 보고 비교하면 헷갈리므로 화면이 따로 알린다'
        ),
    )
    fixed_expenses: Item
    scenarios: list[Scenario]
    notes: list[str] = Field(default_factory=list)


# ── 계산 ────────────────────────────────────────────────────────


def _status_of(cell: _Cell) -> FieldStatus:
    """값이 있는데 status가 기본값(missing)으로 남은 칸은 '추정'으로 본다.

    예: 확인 화면에서 금액을 넣었는데 프론트가 status를 user_input으로 바꾸지 않은 경우.
    값이 있는 숫자가 'missing'으로 표시되는 일을 막는다.
    """
    value = getattr(cell, 'value', None)
    return 'estimated' if value is not None and cell.status == 'missing' else cell.status


# 세전 월 기본급이 이보다 적으면 단위를 잘못 읽은 값(예: 만원 단위)으로 보고 계산하지 않는다.
MIN_MONTHLY_BASE = 100_000


def _allowances(row: ComparisonRow) -> Item:
    """회사가 매달 주는 수당 합계 (교통비 + 기타 수당). 주거지원비는 이사 경우에만 따로 쓴다."""
    cells = [row.transport_allowance_monthly_krw] + [a.monthly_krw for a in row.other_allowances]
    # '교통비'만 쓰면 표의 교통비(내가 쓰는 돈)와 헷갈린다 → 회사가 주는 돈은 '교통비 수당'
    names = ['교통비 수당'] + [a.name for a in row.other_allowances]
    present = [(n, c) for n, c in zip(names, cells, strict=True) if c.value]
    if not present:
        return Item(label='회사 수당', value=0, status='confirmed', source='공고에 월 수당 없음')
    return Item(
        label='회사 수당',
        value=sum(c.value for _, c in present),
        status=weakest([_status_of(c) for _, c in present]),
        source=' + '.join(f'{n} {won(c.value)}' for n, c in present),
    )


def _meal_on_top(row: ComparisonRow) -> int:
    """연봉과 따로 주는 식대 (원).

    연봉에 포함인지 모르면 '포함'으로 본다 (실수령액이 적게 나오는 쪽 = 보수적).
    """
    meal = row.meal_allowance_monthly_krw.value or 0
    return meal if row.meal_allowance_in_salary.value is False else 0


def _pay_on_top(row: ComparisonRow, allowances: Item) -> Item:
    """연봉·월급과 따로 매달 받는 돈 = 별도 식대 + 수당.

    세전 월급(gross)에 더해지는 바로 그 금액이다.
    """
    meal = _meal_on_top(row)
    parts = ([f'식대 {won(meal)}'] if meal else []) + (
        [allowances.source] if allowances.value else []
    )
    statuses = [allowances.status]
    if meal:  # 식대 금액과 '연봉과 별도'라는 판단 둘 다 이 금액의 신뢰도다
        statuses += [
            _status_of(row.meal_allowance_monthly_krw),
            _status_of(row.meal_allowance_in_salary),
        ]
    # 화면이 '연봉 외 …' / '월급 외 …'를 다시 판단하지 않게, 계산에 쓴 급여 칸으로 정한다
    picked = _salary_cell(row)
    kind = '월급' if picked and picked[0] == 'monthly' else '연봉'
    return Item(
        label=f'{kind} 외',
        value=meal + allowances.value,
        status=weakest(statuses),
        source=' + '.join(parts) if parts else f'{kind} 외로 따로 받는 돈 없음',
    )


def _salary_cell(row: ComparisonRow) -> tuple[str, MoneyCell] | None:
    """계산에 쓸 급여 칸. 연봉 종류와 맞는 칸을 먼저, 없으면 금액이 있는 칸을 쓴다.

    연봉 종류가 '비공개'여도 사용자가 연봉 칸에 금액을 넣었으면 그 금액을 쓴다.
    0원은 없는 값으로 본다.
    """
    annual = ('annual', row.annual_salary_krw)
    monthly = ('monthly', row.monthly_salary_krw)
    order = [monthly, annual] if row.salary_type.value == 'monthly' else [annual, monthly]
    return next((c for c in order if c[1].value), None)


def _monthly_base(kind: str, cell: MoneyCell) -> int:
    return cell.value // 12 if kind == 'annual' else cell.value


def _net_income(
    row: ComparisonRow, profile: UserProfile, allowances: Item, on_top: Item
) -> tuple[Item, Payslip | None, list[str]]:
    """공고의 급여 칸으로 세후 월급을 계산한다. 금액이 없으면 missing.

    돌려주는 값: (세후 월급, 공제 내역, 화면에 보여 줄 notes)
    """
    label = '세후 월급'
    picked = _salary_cell(row)
    if picked is None:
        reason = {
            'undisclosed': '공고에 연봉 금액이 없음 (내규·협의)',
            'to_propose': '회사가 희망 연봉을 물음 (금액 미정)',
        }.get(row.salary_type.value or '', '공고에서 급여 금액을 읽지 못함')
        return Item(label=label, value=None, status='missing', source=reason), None, []

    kind, cell = picked
    base = _monthly_base(kind, cell)
    if base < MIN_MONTHLY_BASE:
        reason = (
            f'급여 금액({won(cell.value)})이 너무 적어서 계산하지 않았어요 (단위를 확인해 주세요)'
        )
        return Item(label=label, value=None, status='missing', source=reason), None, []
    notes: list[str] = []
    meal, in_salary = row.meal_allowance_monthly_krw, row.meal_allowance_in_salary
    meal_krw = meal.value or 0
    used = [_status_of(cell), allowances.status]
    if meal_krw:
        # 식대가 연봉에 포함인지 모르면 '포함'으로 본다 (실수령액이 적게 나오는 쪽 = 보수적)
        used += [
            _status_of(meal),
            _status_of(in_salary) if in_salary.value is not None else 'estimated',
        ]
        if row.meals_provided.value is True:  # 비과세 여부를 바꾸므로 신뢰도에 넣는다
            used.append(_status_of(row.meals_provided))
        if in_salary.value is None:  # 화면에 '포함으로 봤다'는 걸 숨기지 않는다
            notes.append(
                '식대가 연봉에 포함인지 몰라 포함된 것으로 계산했어요. 따로 받으면 더 남아요.'
            )
    gross = (
        base + on_top.value
    )  # on_top = 별도 식대 + 수당 (_pay_on_top, 화면에도 같은 값을 보낸다)
    if row.meals_provided.value is True:
        nontaxable = 0  # 식사를 현물로 주면 현금 식대는 과세
        if meal_krw:
            notes.append('회사가 식사를 제공해서 현금 식대는 비과세가 아닌 것으로 계산했어요.')
    else:
        nontaxable = min(meal_krw, rates.MEAL_NONTAXABLE_LIMIT, gross)
    if allowances.value:
        notes.append(
            f'회사 수당({allowances.source})은 세금을 내는 급여로 보고 세후 월급에 넣었어요.'
        )

    slip = monthly_net(gross, nontaxable, profile.family, profile.children_8_to_20)
    what = f'연봉 {won(cell.value)} ÷ 12' if kind == 'annual' else f'월급 {won(cell.value)}'
    # 세전 월급에 더한 것(별도 식대·수당)도 적는다.
    # 빼면 '연봉은 적은데 세후가 더 많다'가 설명되지 않는다.
    if on_top.value:
        what = f'{what} + {on_top.source}'
    source = f'{what} → 4대보험·소득세를 뺀 값'
    return Item(label=label, value=slip.net, status=weakest(used), source=source), slip, notes


def _fixed_expenses(profile: UserProfile | None) -> Item:
    label = '고정비'
    if profile is None:
        return Item(label=label, value=None, status='missing', source='사용자 정보 없음')
    if profile.fixed_expenses is None:
        return Item(label=label, value=None, status='missing', source='고정비를 아직 몰라요')
    if not profile.fixed_expenses:
        return Item(label=label, value=0, status='user_input', source='고정비 없음 (사용자 답)')
    return Item(
        label=label,
        value=sum(e.monthly_krw for e in profile.fixed_expenses),
        status='user_input',
        source=' + '.join(f'{e.name} {won(e.monthly_krw)}' for e in profile.fixed_expenses),
    )


def _from_cost(label: str, cost: Cost | None, missing_source: str) -> Item:
    if cost is None:
        return Item(label=label, value=None, status='missing', source=missing_source)
    return Item(label=label, value=cost.value, status=cost.status, source=cost.source)


BLANK_TRANSPORT = '비어 있어 0원으로 계산했어요. 교통비가 더 들 수 있어요'


def _transport(label: str, cost: Cost | None) -> Item:
    """교통비. 비어 있으면 0원으로 놓고 계산한다 (추정) — 교통비 하나 때문에 남는 돈을 못 보는 일을
    막는다. 실제로는 더 들 수 있다고 Scenario.assumed_zero로 알린다."""
    if cost is None:
        return Item(
            label=label,
            value=0,
            status='estimated',
            source=BLANK_TRANSPORT,
        )
    return _from_cost(label, cost, '')


def housing_aid(row: ComparisonRow) -> tuple[int, int | None] | None:
    """주거지원비 (월 금액, 지원 개월 수 — 모르면 None). 지원이 없으면 None.

    '0개월'은 지원 없음, 기간이 비어 있으면(None) '모름'이다.
    남는 돈(여기)·손익분기 연봉(break_even.long_term)이 같은 규칙을 쓰도록 한 곳에 둔다.
    """
    aid, months = row.housing_allowance_monthly_krw.value, row.housing_allowance_months.value
    return (aid, months) if aid and months != 0 else None


def _housing_aid_note(months: int | None) -> str:
    """이사 경우 남는 돈에 주거지원비를 뺐을 때 붙이는 안내 (지원은 언젠가 끝날 수 있다)."""
    if months:
        return f'주거지원비는 {months}개월 뒤 끝나, 그 뒤로는 이사한 경우에 남는 돈이 줄어요.'
    return (
        '주거지원비는 지원 기간을 몰라 계속 받는 것으로 계산했어요. '
        '지원이 끝나면 이사한 경우에 남는 돈이 줄어요.'
    )


def _move_housing(row: ComparisonRow, cost: Cost | None) -> Item:
    """이사 경우 주거비 = 새 집 주거비 − 주거지원비 (0원 아래로는 안 내려감)."""
    label = '주거비 (이사)'
    if row.company_housing.value is True and cost is None:
        return Item(
            label=label,
            value=None,
            status='missing',
            source='사택·기숙사 제공 (본인 부담 금액이 공고에 없음)',
        )
    item = _from_cost(label, cost, '이사할 집의 월 주거비 없음')
    aid = housing_aid(row)
    if item.value is None or aid is None:
        return item
    amount, months = aid
    period = f' ({months}개월간)' if months else ''
    base = item.source or '새 집 주거비'
    return Item(
        label=label,
        value=max(item.value - amount, 0),
        status=weakest([item.status, _status_of(row.housing_allowance_monthly_krw)]),
        source=f'{base} {won(item.value)} − 주거지원비 {won(amount)}{period}',
    )


def _scenario(
    case: Case, base: list[Item], housing: Item, transport: Item, possible: bool | None
) -> Scenario:
    """base(세후 월급·고정비)와 주거비·교통비로 월 잔여액을 계산한다.

    possible=False(통근 불가 등)면 해당 없는 경우라 잔여액을 계산하지 않는다.
    """
    net, fixed = base
    items = [*base, housing, transport]
    if possible is False:
        return Scenario(
            case=case,
            label=CASE_LABELS[case],
            applicable=False,
            monthly_remaining=None,
            status='user_input',
            housing=housing,
            transport=transport,
        )
    missing = [i.label for i in items if i.value is None]
    remaining = None
    if not missing:
        remaining = net.value - housing.value - transport.value - fixed.value
    return Scenario(
        case=case,
        label=CASE_LABELS[case],
        monthly_remaining=remaining,
        status=weakest([i.status for i in items]),
        housing=housing,
        transport=transport,
        missing=missing,
        assumed_zero=['교통비'] if transport.source == BLANK_TRANSPORT else [],
    )


def _notes(row: ComparisonRow) -> list[str]:
    """공고 조건에 관한 안내 (수습·연장근로). 주거지원비 안내는 이사 주거비에서 실제로 뺐을 때만
    monthly_remaining이 단다."""
    notes = []
    rate, months = row.probation_pay_rate.value, row.probation_months.value
    if months and rate is not None and rate < 1:
        notes.append(f'수습 {months}개월 동안은 급여의 {rate:.0%}만 받아서 그동안은 덜 남아요.')
    if row.overtime.value:
        notes.append('연장근로수당은 금액을 알 수 없어서 계산에 넣지 않았어요.')
    return notes


def monthly_remaining(row: ComparisonRow, profile: UserProfile | None) -> PostingResult:
    """공고 1건의 월 잔여액을 '통근'·'이사' 두 경우로 계산한다.

    profile이 None이면(사용자 정보를 아직 못 받음) 세후 월급만 계산하고
    월 잔여액은 null, missing에 필요한 값을 적는다.
    """
    allowances = _allowances(row)
    on_top = _pay_on_top(row, allowances)
    notes: list[str] = []
    try:
        net, slip, net_notes = _net_income(row, profile or UserProfile(), allowances, on_top)
        notes = _notes(row) + net_notes
    except ValueError:  # 간이세액표 범위 밖 등 — 입력 검사로 막혀 있지만, 와도 계산은 계속한다
        # 예외 글은 개발자용(합쇼체·원 단위 숫자)이라 화면에는 정해 둔 문장만 보낸다.
        # 원문은 로그로 남긴다.
        logger.exception('세후 월급 계산 실패: %s', row.id)
        reason = '세후 월급을 계산하지 못했어요. 급여 칸을 확인해 주세요.'
        net, slip = Item(label='세후 월급', value=None, status='missing', source=reason), None
        notes = [*_notes(row), reason]
    if profile is None and net.value is not None:
        # 가족 수를 몰라 1인 가구로 계산했으므로 '확인'이라고 할 수 없다
        net = net.model_copy(update={'status': weakest([net.status, 'estimated'])})

    base = [net, _fixed_expenses(profile)]
    costs = profile.costs_for(row.id) if profile else PostingCosts()
    no_profile = '사용자 정보 없음'
    commute = _scenario(
        'commute',
        base,
        _from_cost(
            '주거비 (지금 집)',
            profile.current_housing if profile else None,
            '지금 집 월 주거비 없음' if profile else no_profile,
        ),
        _transport('교통비 (통근)', costs.commute_transport),
        costs.commute_possible,
    )
    move = _scenario(
        'move',
        base,
        _move_housing(row, costs.move_housing),
        _transport('교통비 (이사)', costs.move_transport),
        costs.move_possible,
    )
    # 주거지원비 안내는 이사 주거비에서 실제로 뺐을 때만 단다 (이사할 집 주거비가 없거나
    # 이사를 안 하면 뺀 적이 없다). 세후 월급이 없으면 손익분기 연봉 쪽 안내가 대신한다.
    aid = housing_aid(row)
    if aid and net.value is not None and move.applicable and move.housing.value is not None:
        notes.append(_housing_aid_note(aid[1]))
    if row.shuttle_bus.value:
        notes.append('통근버스가 있어서 통근 교통비가 줄어들 수 있어요.')
    return PostingResult(
        posting_id=row.id,
        company=row.company.value,
        net_income=net,
        payslip=slip.to_dict() if slip else None,
        allowances=allowances,
        pay_on_top=on_top,
        fixed_expenses=base[1],
        scenarios=[commute, move],
        notes=notes,
    )
