"""1년 정착 플랜 (이슈 #18) — 공고마다 '언제 무엇을 해야 하는지'를 달마다 정리한다.

재료는 이미 계산·조회한 것뿐이다 (새로 API를 부르거나 Claude를 부르지 않는다):
  - 계산 결과(calc/summary.py): 공고마다 '회사 근처로 이사'가 해당되는지
  - 월세 시세(public_data/rent.py): 근무지 원룸 보증금 중앙값
  - 청년 지원(support/policies.py): 근무지 지역 정책 + 전국 정책, 신청 기간

달 나누기 (조회한 날 기준 달력):
  before   '이사 전'      이사하는 경우만. 보증금 마련
  YYYY-MM  그달           이사한 달의 할 일(전입신고), 정책은 마감 달(신청 중) 또는 시작 달(곧 시작)
  anytime  '언제든'       상시·기간 모름 정책
  job      '입사 N개월 뒤' 공고에서 읽은 날: 수습이 끝나는 달(수습 중 급여가 100%가 아닐 때),
                          회사 주거지원비가 끝나는 달(이사하는 경우, 기간을 알 때)
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

from backend.calc.remaining import OutputModel, housing_aid
from backend.calc.summary import CalculationResult
from backend.calc.text import won
from backend.normalize.schema import ComparisonRow
from backend.public_data.training import ncs_for_job
from backend.support.policies import FIRST_JOB_MAX, LocalSupport, SupportPolicy, is_first_job

MONTHS = 12
ANYTIME_MAX = 5
# '전월세'(예: 청년 전월세 대출보증)는 보증금 대출이라 이사 전 — '월세'보다 먼저 본다
_DEPOSIT_WORDS = re.compile(r'보증금|전세|전월세|버팀목|보증부')
_RENT_WORDS = re.compile(r'월세')

ItemKind = Literal['deposit', 'move_in', 'policy', 'milestone']
SlotKind = Literal['before', 'move_month', 'month', 'start', 'job', 'anytime']


class PlanItem(OutputModel):
    kind: ItemKind = Field(
        description='deposit=보증금 마련, move_in=전입신고, policy=지원 신청, '
        'milestone=공고에서 읽은 날 (수습 끝·주거지원비 끝)'
    )
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
    label: str = Field(description="'이사 전', '이사한 달', '2026년 12월', '입사 3개월 뒤' 등")
    month: str | None = Field(default=None, description='달력 달이면 YYYY-MM')
    items: list[PlanItem]


class PostingPlan(OutputModel):
    posting_id: str
    company: str | None
    region: str | None = Field(description='근무지 시도 시군구')
    lawd_cd: str | None = Field(
        default=None, description='근무지 시군구 코드 (화면이 재직자 강의를 찾을 때 쓴다)'
    )
    ncs: str | None = Field(
        default=None, description='직무 분류 (NCS 대분류 2자리, 재직자 강의를 직무에 맞게 고를 때)'
    )
    moving: bool = Field(description='이사하는 경우를 넣었는지 (이사 경우가 해당될 때)')
    slots: list[PlanSlot]
    notes: list[str] = Field(default_factory=list)


class SettlePlan(OutputModel):
    """세션 응답의 plan 칸. 계산 결과·청년 지원에서 바로 만든다."""

    postings: list[PostingPlan]
    # 이사 플랜이 있는 공고의 통근 플랜: 화면이 통근·이사를 바꿔 본다 (예전 화면은 안 읽음)
    alternates: list[PostingPlan] = Field(default_factory=list)
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


def _placed_on(policy: SupportPolicy) -> date:
    """달력에 놓이는 날: 곧 열리면 시작일, 아니면 마감일 (기간 없으면 맨 뒤)."""
    start, end = _period(policy)
    return (start if policy.apply_status == 'upcoming' else end) or date.max


def _place_policy(
    policy: SupportPolicy, today: date, last: date
) -> tuple[SlotKind, str | None, PlanItem] | None:
    """정책을 넣을 칸: (칸 종류, 달 YYYY-MM, 할 일). 12개월 밖이면 None."""
    start, end = _period(policy)
    if policy.apply_status in ('always', 'unknown') or start is None or end is None:
        when = '상시' if policy.apply_status == 'always' else '기간은 신청 페이지에서 확인'
        if policy.fit == 'moving' and _DEPOSIT_WORDS.search(policy.name):
            return 'before', None, _policy_item(policy, when)
        if policy.fit == 'moving' and _RENT_WORDS.search(policy.name):
            return 'move_month', None, _policy_item(policy, when)
        if is_first_job(policy):  # 입사하면 챙겨 볼 것 (support/policies.py)
            return 'start', None, _policy_item(policy, when)
        if policy.fit != 'moving':
            return None  # 그 밖의 상시 일반 정책은 넣지 않는다 (모듈 설명)
        return 'anytime', None, _policy_item(policy, when)
    if policy.apply_status == 'upcoming':
        if start >= last:
            return None
        when = f'{_md(start)} 시작 ~ {_md(end)} 마감'
        return 'month', _month_key(start), _policy_item(policy, when)
    # 신청 중: 마감 달에 넣는다 (그 전에 내야 한다). 마감이 1년 넘게 남았으면 '언제든'으로
    if end >= last:
        until = _policy_item(policy, f'{end.year}년 {_md(end)}까지')
        return ('start' if is_first_job(policy) else 'anytime'), None, until
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


# '14일'은 설명 문장에 한 번만 (단계 이름·칩에 되풀이하지 않는다). 의무·과태료를 단정하지 않고
# 규칙과 출처, 하면 좋은 이유(근무지 청년 지원은 주민등록 주소를 본다)를 쓴다
MOVE_IN = PlanItem(
    kind='move_in',
    title='전입신고',
    detail='이사한 날부터 14일 안에 주민센터나 정부24에서 해요. '
    '근무지 청년 지원은 주민등록 주소를 봐요.',
    source='주민등록법 제16조',
    url='https://www.gov.kr',
)
# 사택·기숙사로 들어가도 주소를 옮기는 일은 같다. 기숙사 전입 방법은 회사마다 다르다
MOVE_IN_DORM = MOVE_IN.model_copy(
    update={
        'detail': '사는 곳을 사택·기숙사로 옮기면 14일 안에 전입신고를 해요. '
        '기숙사는 방법이 회사마다 달라 회사에 먼저 물어보세요.'
    }
)


def _milestones(row: ComparisonRow | None, moving: bool) -> dict[int, list[PlanItem]]:
    """공고에서 읽은 날 (입사 몇 개월 뒤 → 할 일). 돈이 바뀌는 때만 넣는다."""
    found: dict[int, list[PlanItem]] = {}
    if row is None:
        return found
    months, rate = row.probation_months.value, row.probation_pay_rate.value
    if months and rate is not None and rate < 1:
        found.setdefault(months, []).append(
            PlanItem(
                kind='milestone',
                title='수습이 끝나요',
                detail=f'월급이 {round(rate * 100)}%에서 100%로 올라요.',
                source='공고',
            )
        )
    aid = housing_aid(row)
    if moving and aid and aid[1]:  # 주거지원비는 이사하는 경우에만 남는 돈에 들어간다 (calc)
        amount, aid_months = aid
        found.setdefault(aid_months, []).append(
            PlanItem(
                kind='milestone',
                title='회사 주거지원비가 끝나요',
                detail=f'매달 {won(amount)}이 끊겨 남는 돈이 그만큼 줄어요.',
                amount_krw=amount,
                source='공고',
            )
        )
    return found


def _posting_plan(
    posting_id: str,
    company: str | None,
    row: ComparisonRow | None,
    moving: bool,
    rent: dict | None,
    regional: list[SupportPolicy],
    national: list[SupportPolicy],
    today: date,
    dorm: bool = False,
) -> PostingPlan:
    """공고 하나의 플랜. dorm: 이사하면서 회사 사택·기숙사에 들어간다 (build_plan에서 정한다)."""
    last = _add_months(today.replace(day=1), MONTHS)
    loc = row.location.value if row else None
    region = ' '.join(x for x in (loc.sido, loc.sigungu) if x) or None if loc else None
    slots: dict[tuple[SlotKind, str | None], list[PlanItem]] = {}
    notes: list[str] = []

    if moving:
        # 사택·기숙사에 들어가면 원룸 보증금도, 집 구하기·월세 지원(이사·주거 정책)도 필요 없다.
        # 전입신고는 그대로 (사택 주소로 옮긴다). 첫 취업 지원(청약통장 등)은 주거로 분류돼도 넣는다
        deposit = None if dorm else _deposit_item(rent)
        if deposit:
            slots[('before', None)] = [deposit]
        slots[('move_month', None)] = [MOVE_IN_DORM if dorm else MOVE_IN]
        policies = [
            p
            for p in regional + national
            if p.fit != 'targeted' and not (dorm and p.fit == 'moving' and not is_first_job(p))
        ]
    else:
        # 통근이면 이사·주거 할 일은 없고, 사는 곳을 몰라 지역 정책도 넣지 않는다 (전국 정책만)
        policies = [p for p in national if p.fit != 'targeted']
        notes.append(
            '지금 사는 지역의 지원은 사는 곳을 몰라 넣지 않았어요. '
            '근무지 지역 지원은 청년 지원 목록에서 볼 수 있어요.'
        )

    seen: set[str] = set()
    # 같은 달 칸 안에서도 날짜순 (곧 열리는 것은 시작일, 신청 중은 마감일. 상시는 뒤로)
    for policy in sorted(policies, key=_placed_on):
        if policy.id in seen:
            continue
        seen.add(policy.id)
        placed = _place_policy(policy, today, last)
        if placed is None:
            continue
        kind, month, item = placed
        # 통근: 이사·주거 할 일(이사 전·이사한 달·언제든, 날짜 있는 주거 정책)은 넣지 않는다.
        # 첫 취업 지원(청약통장 등)은 주거로 분류돼도 넣는다
        if not moving and (
            kind in ('before', 'move_month', 'anytime')
            or policy.fit == 'moving'
            and kind == 'month'
            and not is_first_job(policy)
        ):
            continue
        slots.setdefault((kind, month), []).append(item)
    for after, items in _milestones(row, moving).items():
        slots[('job', f'{after:03d}')] = items  # 입사 몇 개월 뒤 (세 자리로 둬서 순서대로)
    anytime = slots.get(('anytime', None), [])
    if len(anytime) > ANYTIME_MAX:
        notes.append(
            f'언제든 신청할 수 있는 지원이 {len(anytime) - ANYTIME_MAX}개 더 있어요. '
            '청년 지원 목록에서 볼 수 있어요.'
        )
        slots[('anytime', None)] = anytime[:ANYTIME_MAX]
    if len(start := slots.get(('start', None), [])) > FIRST_JOB_MAX:
        slots[('start', None)] = start[:FIRST_JOB_MAX]

    order = {'before': 0, 'move_month': 1, 'month': 2, 'start': 3, 'job': 4, 'anytime': 5}
    built = []
    for (kind, month), items in sorted(
        slots.items(), key=lambda kv: (order[kv[0][0]], kv[0][1] or '')
    ):
        if kind == 'month' and month:
            y, m = month.split('-')
            label = f'{y}년 {int(m)}월'
        elif kind == 'job' and month:
            label, month = f'입사 {int(month)}개월 뒤', None  # 입사일을 몰라 달력 달이 아니다
        else:
            label = {
                'before': '이사 전',
                'move_month': '이사한 달',
                'start': '입사하면 챙겨 볼 것',
                'anytime': '언제든',
            }[kind]
        # 이사하는 경우: 이사·주거 정책을 같은 칸 안에서 먼저
        items.sort(key=lambda i: (i.kind != 'deposit', i.kind != 'move_in'))
        built.append(PlanSlot(kind=kind, label=label, month=month, items=items))
    return PostingPlan(
        posting_id=posting_id,
        company=company,
        region=region,
        lawd_cd=loc.lawd_cd if loc else None,
        ncs=ncs_for_job(row.job_title.value) if row else None,
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

    plans, alternates = [], []
    for posting in calc.postings:
        row = rows.get(posting.posting_id)
        code = row.location.value.lawd_cd if row and row.location.value else None
        move = next((s for s in posting.scenarios if s.case == 'move'), None)
        moving = bool(move and move.applicable)
        # 사택·기숙사에 들어가는지: 공고에 사택·기숙사가 있고, 이사할 집 주거비를 0원으로 넣었다.
        # 주거비를 넣었으면 따로 집을 구하는 것으로 본다. 주거지원비가 있으면 이사 주거비가
        # '넣은 금액 − 지원비'(0원 아래로 안 내려감)라 0원이어도 넣은 금액을 알 수 없다
        # → 사택으로 보지 않는다 (틀려도 보증금이 보이는 쪽이 안전하다).
        # 화면 profile.ts livesInCompanyHousing과 같은 규칙
        dorm = bool(
            moving
            and move
            and row
            and row.company_housing.value is True
            and housing_aid(row) is None
            and move.housing.value == 0
        )
        regional = []
        if support:
            regional = next(
                (r.policies for r in support.regions if posting.posting_id in r.posting_ids), []
            )

        head = (posting.posting_id, posting.company, row)
        tail = (
            rents.get(code) if code else None,
            regional,
            support.national if support else [],
            today,
        )
        plans.append(_posting_plan(*head, moving, *tail, dorm=dorm))
        # 이사 플랜이면 통근 플랜도 함께 (통근이 해당될 때): 화면이 둘을 바꿔 보여 준다
        commute = next((s for s in posting.scenarios if s.case == 'commute'), None)
        if moving and commute and commute.applicable:
            alternates.append(_posting_plan(*head, False, *tail))
    notes = ['신청 조건과 일정은 바뀔 수 있어요. 신청 전에 안내 페이지에서 확인해 주세요.']
    if support is None:
        notes.append('청년 지원 정보를 불러오지 못해 지원 신청 일정은 빠져 있어요.')
    return SettlePlan(
        postings=plans, alternates=alternates, start_month=_month_key(today), notes=notes
    )
