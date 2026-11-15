"""월 잔여액 계산 — 매달 손에 남는 돈.

    월 잔여액 = 실수령액 − 주거비 − 교통비 − 기타 고정비

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

from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from backend.calc import rates
from backend.calc.payroll import Payslip, monthly_net
from backend.normalize.schema import MAX_KRW, ComparisonRow, FieldStatus, MoneyCell, _Cell

Case = Literal['commute', 'move']
CASE_LABELS: dict[Case, str] = {'commute': '지금 집에서 통근', 'move': '회사 근처로 이사'}

# status 약한 순서. 숫자가 클수록 믿기 어렵다.
_WEAKNESS: dict[FieldStatus, int] = {'confirmed': 0, 'user_input': 1, 'estimated': 2, 'missing': 3}


def weakest(statuses: list[FieldStatus]) -> FieldStatus:
    """여러 값의 신뢰도 중 가장 약한 것. 비어 있으면 confirmed."""
    return max(statuses, key=_WEAKNESS.__getitem__, default='confirmed')


# ── 입력: 사용자 정보 ────────────────────────────────────────────


class Cost(BaseModel):
    """사용자 쪽 금액 하나 (월, 원). 기본 신뢰도는 '사용자 입력'.

    월세 실거래가 추정값처럼 코드가 추정한 값이면 status='estimated'와 출처를 넣는다.
    """

    value: int = Field(ge=0, le=MAX_KRW, description='월 금액(원)')
    # 'missing'은 받지 않는다. 값이 없으면 Cost 자체를 비운다(None) — 0원으로 계산되는 걸 막기 위해.
    status: Literal['confirmed', 'estimated', 'user_input'] = 'user_input'
    source: str | None = Field(default=None, description='값의 출처 (예: 국토부 실거래가 중앙값)')


class FixedExpense(BaseModel):
    """기타 고정비 한 줄 (학자금 상환, 통신비 등)."""

    name: str = Field(min_length=1)
    monthly_krw: int = Field(ge=0, le=MAX_KRW)


class PostingCosts(BaseModel):
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


class UserProfile(BaseModel):
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


class Item(BaseModel):
    """결과의 값 하나와 그 출처·신뢰도."""

    label: str
    value: int | None
    status: FieldStatus
    source: str | None = None


class Scenario(BaseModel):
    case: Case
    label: str
    applicable: bool = Field(default=True, description='False면 해당 없는 경우 (예: 통근 불가)')
    monthly_remaining: int | None = Field(description='월 잔여액(원). 값이 모자라면 null')
    status: FieldStatus = Field(description='월 잔여액의 신뢰도 (들어간 값 중 가장 약한 것)')
    housing: Item
    transport: Item
    missing: list[str] = Field(default_factory=list, description='계산에 필요한데 없는 값')


class PostingResult(BaseModel):
    posting_id: str
    company: str | None
    net_income: Item = Field(description='세후 월급 (실수령액)')
    payslip: dict[str, int] | None = Field(default=None, description='공제 내역 (계산했을 때만)')
    allowances: Item = Field(
        description='회사 수당 합계 (교통비·통신비 등). 세후 월급에 이미 들어 있음'
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
    names = ['교통비'] + [a.name for a in row.other_allowances]
    present = [(n, c) for n, c in zip(names, cells, strict=True) if c.value]
    if not present:
        return Item(label='회사 수당', value=0, status='confirmed', source='공고에 월 수당 없음')
    return Item(
        label='회사 수당',
        value=sum(c.value for _, c in present),
        status=weakest([_status_of(c) for _, c in present]),
        source=' + '.join(f'{n} {c.value:,}원' for n, c in present),
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
    row: ComparisonRow, profile: UserProfile, allowances: Item
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
        reason = f'급여 금액({cell.value:,}원)이 너무 적어 계산하지 않음 (단위 확인 필요)'
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
    meal_in_salary = in_salary.value is not False

    gross = base + (0 if meal_in_salary else meal_krw) + allowances.value
    if row.meals_provided.value is True:
        nontaxable = 0  # 식사를 현물로 주면 현금 식대는 과세
        if meal_krw:
            notes.append('회사가 식사를 제공해 현금 식대는 비과세가 아닌 것으로 계산했습니다.')
    else:
        nontaxable = min(meal_krw, rates.MEAL_NONTAXABLE_LIMIT, gross)
    if allowances.value:
        notes.append(f'회사 수당({allowances.source})은 과세 급여로 보고 세후 월급에 넣었습니다.')

    slip = monthly_net(gross, nontaxable, profile.family, profile.children_8_to_20)
    what = f'연봉 {cell.value:,}원 ÷ 12' if kind == 'annual' else f'월급 {cell.value:,}원'
    source = f'{what} → 4대보험·소득세를 뺀 값'
    return Item(label=label, value=slip.net, status=weakest(used), source=source), slip, notes


def _fixed_expenses(profile: UserProfile | None) -> Item:
    label = '기타 고정비'
    if profile is None:
        return Item(label=label, value=None, status='missing', source='사용자 정보 없음')
    if profile.fixed_expenses is None:
        return Item(label=label, value=None, status='missing', source='기타 고정비를 아직 모름')
    if not profile.fixed_expenses:
        return Item(label=label, value=0, status='user_input', source='고정비 없음 (사용자 답)')
    return Item(
        label=label,
        value=sum(e.monthly_krw for e in profile.fixed_expenses),
        status='user_input',
        source=' + '.join(f'{e.name} {e.monthly_krw:,}원' for e in profile.fixed_expenses),
    )


def _from_cost(label: str, cost: Cost | None, missing_source: str) -> Item:
    if cost is None:
        return Item(label=label, value=None, status='missing', source=missing_source)
    return Item(label=label, value=cost.value, status=cost.status, source=cost.source)


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
    aid = row.housing_allowance_monthly_krw
    months = row.housing_allowance_months.value
    if item.value is None or not aid.value or months == 0:  # 0개월 = 지원 없음
        return item
    period = f' ({months}개월간)' if months else ''
    base = item.source or '새 집 주거비'
    return Item(
        label=label,
        value=max(item.value - aid.value, 0),
        status=weakest([item.status, _status_of(aid)]),
        source=f'{base} {item.value:,}원 − 주거지원비 {aid.value:,}원{period}',
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
    )


def _notes(row: ComparisonRow) -> list[str]:
    notes = []
    rate, months = row.probation_pay_rate.value, row.probation_months.value
    if months and rate is not None and rate < 1:
        notes.append(f'수습 {months}개월 동안은 급여의 {rate:.0%}만 받아 실제로는 더 적습니다.')
    if row.housing_allowance_monthly_krw.value and row.housing_allowance_months.value:
        notes.append(
            f'주거지원비는 {row.housing_allowance_months.value}개월 뒤 끝나 '
            '그때부터 이사 경우 잔여액이 줄어듭니다.'
        )
    if row.overtime.value:
        notes.append('연장근로수당은 금액을 알 수 없어 계산에 넣지 않았습니다.')
    return notes


def monthly_remaining(row: ComparisonRow, profile: UserProfile | None) -> PostingResult:
    """공고 1건의 월 잔여액을 '통근'·'이사' 두 경우로 계산한다.

    profile이 None이면(사용자 정보를 아직 못 받음) 세후 월급만 계산하고
    월 잔여액은 null, missing에 필요한 값을 적는다.
    """
    notes = _notes(row)
    allowances = _allowances(row)
    try:
        net, slip, net_notes = _net_income(row, profile or UserProfile(), allowances)
        notes += net_notes
    except ValueError as e:  # 간이세액표 범위 밖 등
        net, slip = Item(label='세후 월급', value=None, status='missing', source=str(e)), None
        notes.append(f'세후 월급을 계산하지 못했습니다: {e}')
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
        _from_cost('교통비 (통근)', costs.commute_transport, '지금 집에서 회사까지 교통비 없음'),
        costs.commute_possible,
    )
    move = _scenario(
        'move',
        base,
        _move_housing(row, costs.move_housing),
        _from_cost('교통비 (이사)', costs.move_transport, '이사한 뒤 교통비 없음'),
        costs.move_possible,
    )
    if row.shuttle_bus.value:
        notes.append('통근버스가 있어 통근 교통비가 줄어들 수 있습니다.')
    return PostingResult(
        posting_id=row.id,
        company=row.company.value,
        net_income=net,
        payslip=slip.to_dict() if slip else None,
        allowances=allowances,
        fixed_expenses=base[1],
        scenarios=[commute, move],
        notes=notes,
    )


def calculate_all(table: list[dict] | None, profile: dict | None) -> dict:
    """비교표 전체(dict 목록)와 사용자 정보(dict)로 공고별 결과를 만든다. calculate 노드가 부른다.

    잘못된 값이 와도 그래프가 멈추지 않게, 문제가 있는 부분만 errors에 적고 나머지는 계산한다.
    돌려주는 값은 체크포인터에 저장되도록 평범한 dict다.
    """
    errors: list[str] = []
    user: UserProfile | None = None
    if profile is not None:
        try:
            user = UserProfile.model_validate(profile)
        except ValidationError as e:
            errors.append(f'사용자 정보 형식이 잘못돼 사용하지 않았습니다: {e.error_count()}곳')

    table = table or []
    postings = []
    for i, raw in enumerate(table, start=1):
        try:
            row = ComparisonRow.model_validate(raw)
        except ValidationError as e:
            errors.append(f'{i}번째 공고 형식이 잘못돼 계산하지 못했습니다: {e.error_count()}곳')
            continue
        postings.append(monthly_remaining(row, user).model_dump(mode='json'))

    return {
        'rows': len(table),
        'has_user_profile': user is not None,
        'postings': postings,
        'errors': errors,
    }
