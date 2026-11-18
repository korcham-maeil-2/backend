"""1년 정착 플랜 (이슈 #18) — 공고마다 '언제 무엇을 해야 하는지'를 달마다 정리한다.

재료는 이미 계산·조회한 것뿐이다 (새로 API를 부르거나 Claude를 부르지 않는다):
  - 계산 결과(calc/summary.py): 공고마다 '회사 근처로 이사'가 해당되는지
  - 월세 시세(public_data/rent.py): 근무지 원룸 보증금 중앙값
  - 청년 지원(support/policies.py): 근무지 지역 정책 + 전국 정책, 신청 기간

달 나누기 (조회한 날 기준 달력):
  before   '이사 전'      이사하는 경우만. 보증금 마련
  YYYY-MM  그달           이사한 달의 할 일(전입신고), 정책은 마감 달(신청 중) 또는 시작 달(곧 시작)
  anytime  '언제든'       상시·기간 모름 정책
입사일을 모르므로 이사한 달은 '이번 달'로 두지 않고 따로 '이사한 달'로 보여 준다.

규칙 (코드로 정한다, 설계 원칙: 날짜·숫자는 코드):
  - 이사하는 경우(이사 경우가 해당): 이사·주거 정책(fit=moving)과 일반 정책을 넣는다.
  - 통근만 하는 경우: 지역 정책은 그 지역에 사는 사람이 대상인 경우가 많아 빼고 전국 정책만.
  - 특정 대상 정책(fit=targeted: 자립준비청년·창업 등)은 플랜에 넣지 않는다 (목록에는 남아 있다).
  - 신청 기간이 있는 정책: 신청 중이면 마감 달, 곧 시작이면 시작 달. 12개월 밖은 넣지 않는다.
  - 상시·기간 모름 정책은 할 일 시점으로 나눈다 (실제 정책은 대부분 상시라 달로는 나뉘지 않는다):
      보증금·전세 대출(보증금·전세·전월세·버팀목·보증부) → '이사 전'
      월세 지원(월세)                          → '이사한 달'
      그 밖 이사·주거 정책                      → '언제든' (최대 ANYTIME_MAX개)
    일반 정책(fit=general)은 상시면 넣지 않는다 (상담·행사 등이 많다. 지원 목록에는 남아 있다).
  - 자격은 판단하지 않는다 (지원 목록과 같이 '신청 페이지에서 확인'). 금액을 더하지 않는다.

전입신고 기한(이사한 날부터 14일 이내)은 주민등록법 제16조 제1항.
"""

import re
from datetime import date
from typing import Literal

from pydantic import Field

from backend.calc.remaining import OutputModel
from backend.calc.summary import CalculationResult
from backend.normalize.schema import ComparisonRow
from backend.support.policies import LocalSupport, SupportPolicy

MONTHS = 12
ANYTIME_MAX = 5
# '전월세'(예: 청년 전월세 대출보증)는 보증금 대출이라 이사 전 — '월세'보다 먼저 본다
_DEPOSIT_WORDS = re.compile(r'보증금|전세|전월세|버팀목|보증부')
_RENT_WORDS = re.compile(r'월세')

ItemKind = Literal['deposit', 'move_in', 'policy']
SlotKind = Literal['before', 'move_month', 'month', 'anytime']


class PlanItem(OutputModel):
    kind: ItemKind = Field(description='deposit=보증금 마련, move_in=전입신고, policy=지원 신청')
    title: str
    detail: str | None = Field(default=None, description='한 줄 설명 (해요체)')
    when: str | None = Field(
        default=None, description="날짜 표시 (예: '11월 30일 마감', '12월 1일 시작', '상시')"
    )
    amount_krw: int | None = Field(default=None, description='보증금처럼 금액이 있는 할 일 (원)')
    status: Literal['estimated'] | None = Field(
        default=None, description='금액이 추정값이면 estimated (보증금 시세)'
    )
    source: str | None = Field(default=None, description='출처 (데이터명·기간 또는 법령)')
    url: str | None = Field(default=None, description='신청·안내 페이지')
    policy_id: str | None = None
    scope: Literal['local', 'national'] | None = Field(
        default=None, description='정책이면 지역 정책(local)인지 전국 정책(national)인지'
    )


class PlanSlot(OutputModel):
    kind: SlotKind
    label: str = Field(description="'이사 전', '이사한 달', '2026년 12월', '언제든'")
    month: str | None = Field(default=None, description='달력 달이면 YYYY-MM')
    items: list[PlanItem]


class PostingPlan(OutputModel):
    posting_id: str
    company: str | None
    region: str | None = Field(description='근무지 시도 시군구')
    lawd_cd: str | None = Field(
        default=None, description='근무지 시군구 코드 (화면이 재직자 강의를 찾을 때 쓴다)'
    )
    moving: bool = Field(description='이사하는 경우를 넣었는지 (이사 경우가 해당될 때)')
    slots: list[PlanSlot]
    notes: list[str] = Field(default_factory=list)


class SettlePlan(OutputModel):
    """세션 응답의 plan 칸. 계산 결과·청년 지원에서 바로 만든다."""

    postings: list[PostingPlan]
    start_month: str = Field(description='달력의 첫 달 (조회한 날의 달, YYYY-MM)')
    notes: list[str] = Field(default_factory=list)


# ── 날짜 ─────────────────────────────────────────────────────────


def _period(policy: SupportPolicy) -> tuple[date | None, date | None]:
    """'2026-04-01 ~ 2026-11-30' → (시작, 끝). 상시·기간 없음이면 (None, None)."""
    text = policy.apply_period or ''
    try:
        return date.fromisoformat(text[:10]), date.fromisoformat(text[-10:])
    except ValueError:
        return None, None


def _month_key(d: date) -> str:
    return f'{d.year}-{d.month:02d}'


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def _md(d: date) -> str:
    return f'{d.month}월 {d.day}일'


# ── 할 일 만들기 ──────────────────────────────────────────────────


def _summary(text: str | None, limit: int = 80) -> str | None:
    """지원 내용 원문을 한 줄로: 줄바꿈·글머리 기호(□○▪※-, 자음 ㅁ·ㅇ)를 빼고 limit자에서 줄인다."""
    if not text:
        return None
    # 자음 낱자 ㅁ·ㅇ은 온통청년 원문에서 글머리로만 쓰인다 ('ㅁ대상:', 'ㅇ (전세)')
    line = re.sub(r'(?m)[□■○●▪◦※•ㅁㅇ\ufe0f]|^\s*-\s*', ' ', text)
    line = re.sub(r'\s+', ' ', line).strip()
    if not line:
        return None
    return line if len(line) <= limit else line[: limit - 1].rstrip() + '…'


def _policy_item(policy: SupportPolicy, when: str) -> PlanItem:
    detail = _summary(policy.support)
    return PlanItem(
        kind='policy',
        title=policy.name,
        detail=detail,
        when=when,
        source=policy.agency,
        url=policy.url,
        policy_id=policy.id,
        scope=policy.scope,
    )


def _place_policy(
    policy: SupportPolicy, today: date, last: date
) -> tuple[SlotKind, str | None, PlanItem] | None:
    """정책을 넣을 칸: (칸 종류, 달 YYYY-MM, 할 일). 12개월 밖이면 None."""
    start, end = _period(policy)
    if policy.apply_status in ('always', 'unknown') or start is None or end is None:
        if policy.fit != 'moving':
            return None  # 상시 일반 정책은 넣지 않는다 (모듈 설명)
        when = '상시' if policy.apply_status == 'always' else '기간은 신청 페이지에서 확인'
        if _DEPOSIT_WORDS.search(policy.name):
            return 'before', None, _policy_item(policy, when)
        if _RENT_WORDS.search(policy.name):
            return 'move_month', None, _policy_item(policy, when)
        return 'anytime', None, _policy_item(policy, when)
    if policy.apply_status == 'upcoming':
        if start >= last:
            return None
        when = f'{_md(start)} 시작 ~ {_md(end)} 마감'
        return 'month', _month_key(start), _policy_item(policy, when)
    # 신청 중: 마감 달에 넣는다 (그 전에 내야 한다). 마감이 1년 넘게 남았으면 '언제든'으로
    if end >= last:
        return 'anytime', None, _policy_item(policy, f'{end.year}년 {_md(end)}까지')
    return 'month', _month_key(end), _policy_item(policy, f'{_md(end)} 마감')


def _deposit_item(rent: dict | None) -> PlanItem | None:
    if not rent or rent.get('status') != 'estimated' or rent.get('deposit_krw') is None:
        return None
    place = (rent.get('region') or '').split(' ', 1)[-1]
    count = rent.get('sample_size') or 0
    return PlanItem(
        kind='deposit',
        title='보증금 마련',
        detail=f'{place} 원룸 보증금 시세예요. 집마다 차이가 커요.',
        amount_krw=rent['deposit_krw'],
        status='estimated',
        source=f'국토부 실거래가 원룸 월세 {count:,}건의 보증금 중앙값 ({rent.get("period")})',
    )


MOVE_IN = PlanItem(
    kind='move_in',
    title='전입신고',
    detail='이사한 날부터 14일 안에 주민센터나 정부24에서 해요. 지역 지원은 전입한 주소를 봐요.',
    when='이사 후 14일 안',
    source='주민등록법 제16조',
    url='https://www.gov.kr',
)


def _posting_plan(
    posting_id: str,
    company: str | None,
    row: ComparisonRow | None,
    moving: bool,
    rent: dict | None,
    regional: list[SupportPolicy],
    national: list[SupportPolicy],
    today: date,
) -> PostingPlan:
    last = _add_months(today.replace(day=1), MONTHS)
    loc = row.location.value if row else None
    region = ' '.join(x for x in (loc.sido, loc.sigungu) if x) or None if loc else None
    slots: dict[tuple[SlotKind, str | None], list[PlanItem]] = {}
    notes: list[str] = []

    if moving:
        deposit = _deposit_item(rent)
        if deposit:
            slots[('before', None)] = [deposit]
        slots[('move_month', None)] = [MOVE_IN]
        policies = [p for p in regional + national if p.fit != 'targeted']
    else:
        # 통근이면 이사·주거 정책(월세·전세 등)도 해당이 없다
        policies = [p for p in national if p.fit == 'general']
        notes.append('지금 집에서 통근하는 경우라 근무지 지역 정책과 주거 정책은 넣지 않았어요.')

    seen: set[str] = set()
    for policy in policies:
        if policy.id in seen:
            continue
        seen.add(policy.id)
        placed = _place_policy(policy, today, last)
        if placed is None:
            continue
        kind, month, item = placed
        if not moving and kind in ('before', 'move_month'):
            continue
        slots.setdefault((kind, month), []).append(item)
    anytime = slots.get(('anytime', None), [])
    if len(anytime) > ANYTIME_MAX:
        notes.append(
            f'언제든 신청할 수 있는 이사·주거 지원이 {len(anytime) - ANYTIME_MAX}개 더 있어요. '
            '청년 지원 목록에서 볼 수 있어요.'
        )
        slots[('anytime', None)] = anytime[:ANYTIME_MAX]

    order = {'before': 0, 'move_month': 1, 'month': 2, 'anytime': 3}
    built = []
    for (kind, month), items in sorted(
        slots.items(), key=lambda kv: (order[kv[0][0]], kv[0][1] or '')
    ):
        if kind == 'month' and month:
            y, m = month.split('-')
            label = f'{y}년 {int(m)}월'
        else:
            label = {'before': '이사 전', 'move_month': '이사한 달', 'anytime': '언제든'}[kind]
        # 이사하는 경우: 이사·주거 정책을 같은 칸 안에서 먼저
        items.sort(key=lambda i: (i.kind != 'deposit', i.kind != 'move_in'))
        built.append(PlanSlot(kind=kind, label=label, month=month, items=items))
    return PostingPlan(
        posting_id=posting_id,
        company=company,
        region=region,
        lawd_cd=loc.lawd_cd if loc else None,
        moving=moving,
        slots=built,
        notes=notes,
    )


def build_plan(
    result: dict | None,
    local_support: dict | None,
    table: list[dict] | None,
    today: date,
) -> SettlePlan | None:
    """계산 결과가 있으면 공고마다 정착 플랜을 만든다. 없으면 None."""
    if not result:
        return None
    calc = CalculationResult.model_validate(result)
    support = LocalSupport.model_validate(local_support) if local_support else None
    rows = {r['id']: ComparisonRow.model_validate(r) for r in table or []}
    rents = {r.lawd_cd: r.model_dump() for r in calc.rent_estimates}
    if support and support.checked_on:
        today = date.fromisoformat(support.checked_on)

    plans = []
    for posting in calc.postings:
        row = rows.get(posting.posting_id)
        code = row.location.value.lawd_cd if row and row.location.value else None
        move = next((s for s in posting.scenarios if s.case == 'move'), None)
        moving = bool(move and move.applicable)
        regional = []
        if support:
            regional = next(
                (r.policies for r in support.regions if posting.posting_id in r.posting_ids), []
            )
        plans.append(
            _posting_plan(
                posting.posting_id,
                posting.company,
                row,
                moving,
                rents.get(code) if code else None,
                regional,
                support.national if support else [],
                today,
            )
        )
    notes = ['신청 조건과 일정은 바뀔 수 있어요. 신청 전에 안내 페이지에서 확인해 주세요.']
    if support is None:
        notes.append('청년 지원 정보를 불러오지 못해 지원 신청 일정은 빠져 있어요.')
    return SettlePlan(postings=plans, start_month=_month_key(today), notes=notes)
