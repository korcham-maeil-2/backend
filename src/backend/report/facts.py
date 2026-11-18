"""리포트에 넘길 사실 정리 + 리포트 속 금액 검사·채우기 (LLM 없음, 숫자는 코드가 책임진다).

[facts]
계산 결과(result)·청년 지원(local_support)·비교표(table)에서 리포트에 필요한 것만 골라
Claude에 넘길 JSON을 만든다. 금액마다 자리표시를 붙인다: '{{A3}} = 196만 3,100원'.

[자리표시] AI는 금액을 숫자로 쓰지 않고 자리표시만 쓴다. 코드가 금액 글자로 바꿔 넣는다.
금액 글자는 결과 화면과 같다: 만 원으로 떨어지면 그대로('59만원'), 아니면 만 원 단위로 반올림해
'약'을 붙인다('약 196만원'). AI가 고르게 두면 '약 224만원'과 '164만 4,570원'이 한 글에 섞인다.
    {{A3}}         약 196만원 (부호 포함: −20만원)
    {{A3|약}}      {{A3}}와 같다 (예전 초안과 맞추려고 받는다)
    {{A3|절대}}    부호 없이 크기만 (매달 모자라는 금액을 말할 때)
    {{A3-A5}}      두 금액의 차이 (항상 양수). '매달 남는 돈'끼리, 연봉끼리만 된다
그래서 AI가 금액을 잘못 옮기거나, 반올림을 틀리거나, 부호를 버릴 수 없다.

[검사] check_draft
  - 숫자로 직접 쓴 금액('500만', '5천만원', '1.5억', '1,970,000')이 있으면 거절
  - 없는 자리표시·종류가 다른 금액의 차이는 거절
  - 흑자를 '적자·모자라요'로, 적자를 '남아요'로 쓰면 거절 (자리표시 바로 뒤 말만 본다)
  어느 금액을 어느 회사·경우의 것으로 설명했는지는 검토 AI(Haiku)가 본다 (agent.py).

[개인정보를 넘기지 않는다] (CLAUDE.md 설계 원칙, #20)
  - 고정비는 합계만 (항목 이름은 사용자가 자유롭게 쓴 글이라 넘기지 않는다)
  - 나이·가족 수·지금 회사 연봉 같은 사용자 정보는 넘기지 않는다
    (지금 회사는 '지금 회사 그대로 매달 남는 돈' 기준값 하나만)
  - 회사 이름·근무지·연봉은 공고에 공개된 정보라 넘긴다
  - 글 칸은 길이를 자른다 (공고·입력 속 긴 글이 AI 지시문처럼 읽히지 않게)
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from backend.calc.text import won

Kind = Literal['remaining', 'salary', 'other']
MAX_TEXT = 120  # 글 칸 하나의 최대 글자 수

# ── 자리표시 ─────────────────────────────────────────────────────


@dataclass
class Amounts:
    """자리표시 → (금액, 종류).

    같은 상태로 만들면 항상 같은 번호가 나온다 (쓰기·검증이 따로 만든다).
    """

    values: dict[str, tuple[int, Kind]] = field(default_factory=dict)
    # 금액 키 → 그 바로 뒤에 붙으면 안 되는 연봉 키. 최소 연봉일 때 세후 월급을 지원 받는 동안
    # 연봉 뒤에 '이때 세후 월급은'으로 붙이는 실수가 첫 초안 9개 중 7개에서 나왔고, 검토(AI)는
    # 이를 안정적으로 잡지 못했다 → 코드가 막는다 (check_draft)
    not_after: dict[str, str] = field(default_factory=dict)
    # 신뢰도가 '추정'인 남는 돈·최소 연봉의 키. 리포트가 하나라도 쓰면 '추정'이라고 밝혀야 한다
    estimated: set[str] = field(default_factory=set)

    def ref(self, value: int | None, kind: Kind = 'other') -> str | None:
        """금액 → '{{A3}} = 196만 3,100원' (없으면 None)."""
        if not isinstance(value, int):
            return None
        key = f'A{len(self.values) + 1}'
        self.values[key] = (value, kind)
        return f'{{{{{key}}}}} = {won(value)}'


def _key(ref: str) -> str:
    """'{{A3}} = 196만원' → 'A3'."""
    return ref[2 : ref.index('}}')]


# 계산 결과 글 속 금액 ('가정값 60만원', '−12만 3,000원', '650,000원')
_TEXT_MONEY = re.compile(
    r'[−-]?(?:\d[\d,]*\s*억\s*)?(?:\d[\d,]*\s*만\s*)?(?:\d[\d,]*\s*천\s*)?(?:\d[\d,]*)?원'
)


def _parse_money(text: str) -> int | None:
    m = re.fullmatch(
        r'([−-])?(?:(\d[\d,]*)\s*억\s*)?(?:(\d[\d,]*)\s*만\s*)?(?:(\d[\d,]*)\s*천\s*)?(\d[\d,]*)?원',
        text.strip(),
    )
    if not m or not any(m.groups()[1:]):
        return None
    sign, eok, man, cheon, rest = m.groups()

    def n(s: str | None) -> int:
        return int(s.replace(',', '')) if s else 0

    value = n(eok) * 100_000_000 + n(man) * 10_000 + n(cheon) * 1_000 + n(rest)
    return -value if sign else value


def _text(value: Any, amounts: Amounts) -> str | None:
    """글 칸: 길이를 자르고, 글 속 금액도 자리표시로 바꾼다 ('가정값 {{A7}} = 60만원')."""
    if value is None:
        return None
    text = ' '.join(str(value).split())
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1] + '…'

    def sub(m: re.Match) -> str:
        parsed = _parse_money(m.group(0))
        return f'({amounts.ref(parsed)})' if parsed is not None else m.group(0)

    return _TEXT_MONEY.sub(sub, text)


def _texts(values: Iterable[Any] | None, amounts: Amounts) -> list[str]:
    return [t for v in values or [] if (t := _text(v, amounts))]


# ── 사실 정리 ────────────────────────────────────────────────────

_STATUS = {
    'confirmed': '공고에서 확인',
    'estimated': '추정',
    'user_input': '사용자 입력',
    'missing': '없음',
}


def _item(item: dict | None, amounts: Amounts) -> dict | None:
    """값 하나 (금액·신뢰도·출처)."""
    if not item:
        return None
    return {
        '금액': amounts.ref(item.get('value')),
        '신뢰도': _STATUS.get(item.get('status'), item.get('status')),
        '출처': _text(item.get('source'), amounts),
    }


def _row_info(table: list[dict] | None, amounts: Amounts) -> dict[str, dict]:
    """공고 ID → 근무지·연봉 표기·주거지원비 (비교표에서)."""
    info = {}
    for row in table or []:
        loc = ((row.get('location') or {}).get('value')) or {}
        place = ' '.join(x for x in (loc.get('sido'), loc.get('sigungu')) if x) or loc.get('raw')
        salary_type = (row.get('salary_type') or {}).get('value')
        annual = (row.get('annual_salary_krw') or {}).get('value')
        monthly = (row.get('monthly_salary_krw') or {}).get('value')
        if annual:
            salary = f'연봉 {amounts.ref(annual, "salary")}'
        elif monthly:
            salary = f'월급 {amounts.ref(monthly)}'
        elif salary_type in ('undisclosed', 'to_propose'):
            salary = '공고에 금액 없음 (비공개)'
        else:
            salary = '공고에서 읽지 못함'
        aid = (row.get('housing_allowance_monthly_krw') or {}).get('value')
        months = (row.get('housing_allowance_months') or {}).get('value')
        info[row.get('id')] = {
            '근무지': _text(place, amounts),
            '급여': salary,
            # 청년 지원(정부·지자체)과 섞지 않게 누가 주는 돈인지 붙인다
            '주거지원비 (회사가 주는 돈, 청년 지원과 다름)': (
                f'월 {amounts.ref(aid)}' + (f', {months}개월' if months else ', 기간 모름')
                if aid
                else None
            ),
        }
    return info


def build_facts(
    result: dict | None, local_support: dict | None, table: list[dict] | None
) -> tuple[dict[str, Any], Amounts]:
    """(Claude에 넘길 사실 JSON, 자리표시 → 금액)."""
    result = result or {}
    amounts = Amounts()
    rows = _row_info(table, amounts)

    postings = []
    # 공고 ID → 리포트에 쓸 이름. ID는 넘기지 않는다 ('posting-1'이 리포트에 그대로 나왔다)
    names = _names(result.get('postings', []), amounts)
    for p in result.get('postings', []):
        info = rows.get(p.get('posting_id'), {})
        cases = []
        for s in p.get('scenarios', []):
            if not s.get('applicable', True):
                # 해당 없는 경우는 이유만 (비용·신뢰도를 붙이면 계산한 것처럼 읽히고,
                # 이유가 없으면 AI가 '거리가 멀어서' 같은 이유를 지어낸다)
                cases.append(
                    {
                        '경우': _text(s.get('label'), amounts),
                        '해당': False,
                        '이유': _NOT_APPLICABLE.get(s.get('case'), '사용자가 해당 없다고 입력함'),
                    }
                )
                continue
            remaining = amounts.ref(s.get('monthly_remaining'), 'remaining')
            if remaining and s.get('status') == 'estimated':
                amounts.estimated.add(_key(remaining))
            cases.append(
                {
                    '경우': _text(s.get('label'), amounts),
                    '해당': s.get('applicable', True),
                    '매달 남는 돈': remaining,
                    '남는 돈 신뢰도': _STATUS.get(s.get('status'), s.get('status')),
                    '주거비': _item(s.get('housing'), amounts),
                    '교통비': _item(s.get('transport'), amounts),
                    '계산에 빠진 값': _texts(s.get('missing'), amounts),
                }
            )
        postings.append(
            {
                '회사': names[p.get('posting_id')],
                '근무지': info.get('근무지'),
                '급여': info.get('급여'),
                **{k: v for k, v in info.items() if k.startswith('주거지원비')},
                '세후 월급': _item(p.get('net_income'), amounts),
                # 연봉과 따로 받는 돈(별도 식대 + 수당). 예전 결과엔 없어서 수당 합계로 대신한다
                '연봉 외로 따로 받는 돈': _item(p.get('pay_on_top'), amounts)
                or amounts.ref((p.get('allowances') or {}).get('value')),
                '고정비 합계': amounts.ref((p.get('fixed_expenses') or {}).get('value')),
                '경우별': cases,
                '안내': _texts(p.get('notes'), amounts),
            }
        )

    break_even = []
    for be in result.get('break_even', []):
        base = be.get('baseline') or {}
        label = _text(base.get('label'), amounts)
        for posting_id, name in names.items():  # 회사명이 없으면 기준 이름에 공고 ID가 들어 있다
            if label and posting_id:  # 'posting-1'이 'posting-10'의 앞부분을 바꾸지 않게
                label = re.sub(rf'{re.escape(posting_id)}(?!\d)', name, label)
        base_amount = amounts.ref(base.get('monthly_remaining'), 'remaining')
        cases = []
        for c in be.get('cases', []):
            if c.get('reason') == '해당 없는 경우':
                continue  # 공고 쪽 '경우별'에 이유와 함께 있다
            # 최소 연봉에 딸린 값(세후 월급 등)은 최소 연봉 바로 뒤에 '최소 연봉일 때'로 붙인다.
            # '그 연봉일 때'로 지원 중 최소 연봉 뒤에 두면 AI가 지원 중 연봉의 월급으로 붙였다.
            case = {
                '경우': _text(c.get('label'), amounts),
                '최소 연봉': amounts.ref(c.get('annual_salary_krw'), 'salary'),
                # 기준·비용 중 가장 약한 신뢰도 (가정값 주거비에 기대면 '추정')
                '신뢰도': (
                    _STATUS.get(c.get('status'), c.get('status'))
                    if c.get('annual_salary_krw') is not None
                    else None
                ),
                '최소 연봉일 때 세후 월급': amounts.ref(c.get('net_income_krw')),
                # 최소 연봉에는 따로 받는 식대·수당이 빠져 있다 (출처 문장에 들어 있다)
                '최소 연봉일 때 세후 월급 계산': _text(c.get('net_income_source'), amounts),
            }
            if case['최소 연봉'] and c.get('status') == 'estimated':
                amounts.estimated.add(_key(case['최소 연봉']))
            # 만원 단위로 올린 연봉이라 기준과 몇백 원 차이 나는 금액을 함께 주면 AI가
            # '기준과 같은 금액'이라며 둘을 섞는다 → 최저임금으로 올린 경우처럼 크게 다를 때만
            left = c.get('monthly_remaining')
            base_left = base.get('monthly_remaining')
            if isinstance(left, int) and (
                not isinstance(base_left, int) or left - base_left >= _SAME_AS_BASE
            ):
                case['최소 연봉일 때 매달 남는 돈 (기준보다 많음)'] = amounts.ref(left, 'remaining')
            during = amounts.ref(c.get('during_support_annual_salary_krw'), 'salary')
            if during and case['최소 연봉일 때 세후 월급']:
                amounts.not_after[_key(case['최소 연봉일 때 세후 월급'])] = _key(during)
            cases.append(
                case
                | {
                    '최저임금으로 올림': c.get('floor') == 'minimum_wage',
                    '지원 받는 동안 최소 연봉 (이때의 세후 월급은 따로 없음)': during,
                    '못 구한 이유': _text(c.get('reason'), amounts),
                    '안내': _texts(c.get('notes'), amounts),
                }
            )
        break_even.append(
            {
                '회사': names.get(be.get('posting_id')) or _text(be.get('company'), amounts),
                '기준': (
                    {
                        '경우': label,
                        '매달 남는 돈': base_amount,
                        '신뢰도': _STATUS.get(base.get('status'), base.get('status')),
                    }
                    if label or base_amount
                    else None
                ),
                '경우별': cases,
            }
        )

    rents = [
        {
            '지역': _text(r.get('region'), amounts),
            '원룸 월세 시세 (관리비 별도)': amounts.ref(r.get('monthly_rent_krw')),
            '보증금': amounts.ref(r.get('deposit_krw')),
            '출처': _text(r.get('source'), amounts),
        }
        for r in result.get('rent_estimates', [])
        if r.get('monthly_rent_krw') is not None
    ]

    facts = {
        '공고별 매달 남는 돈': postings,
        '연봉 비공개 공고의 최소 연봉': break_even,
        '최소 연봉 안내': _text(result.get('break_even_note'), amounts),
        '지역 원룸 월세 시세 (국토부 실거래가 추정)': rents,
        '근무지별 청년 지원 (남는 돈 계산에는 넣지 않음)': _support(local_support, amounts),
        '계산하지 못한 부분': _texts(result.get('errors'), amounts),
    }
    # 청년 지원을 찾지 않았으면 칸을 빼서, '조건을 확인하세요' 같은 안내를 쓰지 않게 한다
    if facts['근무지별 청년 지원 (남는 돈 계산에는 넣지 않음)'] is None:
        del facts['근무지별 청년 지원 (남는 돈 계산에는 넣지 않음)']
    return facts, amounts


# 해당 없는 경우의 이유 (계산은 사용자가 고른 대로만 뺀다: commute_possible·move_possible=False)
_NOT_APPLICABLE = {
    'commute': '사용자가 지금 집에서 통근할 수 없다고 입력함',
    'move': '사용자가 이사를 고려하지 않는다고 입력함',
}
_SAME_AS_BASE = 10_000  # 최소 연봉일 때 남는 돈이 기준보다 이만큼 이상 많을 때만 따로 넘긴다


def _names(postings: list[dict], amounts: Amounts) -> dict[str, str]:
    """공고 ID → 리포트에 쓸 이름. 같은 회사 공고가 둘 이상이면 몇 번째 공고인지 붙여 구분한다."""
    names = {
        p.get('posting_id'): _text(p.get('company'), amounts) or f'공고 {i}'
        for i, p in enumerate(postings, start=1)
    }
    counts = Counter(names.values())
    return {
        posting_id: f'{name} ({i}번째 공고)' if counts[name] > 1 else name
        for i, (posting_id, name) in enumerate(names.items(), start=1)
    }


_ESTIMATE_WORD = re.compile(r'추정|가정')


def missing_estimate_word(draft: str, amounts: Amounts) -> bool:
    """추정인 남는 돈·최소 연봉을 썼는데 '추정'(또는 '가정')이라는 말이 한 번도 없는가."""
    if _ESTIMATE_WORD.search(draft):
        return False
    return any(r.key in amounts.estimated for r in _refs(draft, amounts) if r.key)


def _support(local_support: dict | None, amounts: Amounts) -> dict | None:
    """청년 지원 요약: 지역마다 정책 수와 위쪽 몇 개의 이름·신청 상태. 금액은 넘기지 않는다."""
    if not local_support:
        return None
    status = {
        'open': '접수 중',  # '신청 중'은 사용자가 신청한 것처럼 읽힌다
        'always': '상시',
        'upcoming': '곧 시작',
        'unknown': '기간 확인 필요',
    }

    def top(policies: Iterable[dict], n: int = 3) -> list[str]:
        return [
            f'{_text(p.get("name"), amounts)} ({status.get(p.get("apply_status"), "")})'
            for p in policies
        ][:n]

    return {
        '지역별': [
            {
                '지역': _text(r.get('region'), amounts),
                # '신청할 수 있는'은 받을 수 있다는 단정처럼 읽힌다 (조건은 신청 전에 확인)
                '조건이 맞을 수 있는 지역 정책 수 (신청 전 확인 필요)': r.get('matched', 0),
                '예시': top(r.get('policies') or []),
                '조회 실패': _text(r.get('error'), amounts),
            }
            for r in local_support.get('regions') or []
        ],
        '전국 정책 수': local_support.get('national_matched', 0),
        '출처': _text(local_support.get('source'), amounts),
    }


# ── 초안 검사·채우기 ─────────────────────────────────────────────

_PLACEHOLDER = re.compile(r'\{\{\s*([^{}]*?)\s*\}\}')
_REF = re.compile(r'(A\d+)(?:\s*-\s*(A\d+))?((?:\s*\|\s*(?:약|절대))*)')

# 숫자로 직접 쓴 금액: 숫자 뒤 억·만·천·원 (같은 줄 안, '원룸'은 제외),
# 쉼표 낀 큰 수, 다섯 자리 이상
_LITERAL_MONEY = re.compile(
    r'\d[\d,.]*[ \t]*(?:억|만|천|원(?!룸))'
    r'|\d{1,3}(?:,\d{3})+(?![\d,]*[ \t]*(?:건|명|개|㎡|년|개월|%))'
    r'|(?<![\d,.])\d{5,}'
)
_URL = re.compile(r'https?://\S+')
_ZERO = re.compile(r'0[ \t]*원')
_NUMBER_END = re.compile(r'[\d,.]$')
_DEFICIT = re.compile(r'적자|모자[라랍란]|부족')
_SURPLUS = re.compile(r'남[아습는았을]|여유|흑자')
_AFTER = 15  # 자리표시 뒤 몇 글자까지 '남아요·모자라요'를 볼지


@dataclass
class _Ref:
    text: str  # 자리표시 원문
    start: int
    end: int
    value: int | None = None  # 넣을 금액 (부호 포함, '|절대'면 크기만)
    raw: int | None = None  # 원래 금액 (부호 검사용)
    kind: Kind | None = None
    key: str | None = None  # 금액 하나를 쓴 자리표시의 키 (차이는 None)
    diff: bool = False
    error: str | None = None


def _refs(draft: str, amounts: Amounts) -> list[_Ref]:
    refs = []
    for m in _PLACEHOLDER.finditer(draft):
        ref = _Ref(text=m.group(0), start=m.start(), end=m.end())
        parsed = _REF.fullmatch(m.group(1))
        refs.append(ref)
        if not parsed:
            ref.error = f"'{ref.text}'는 알 수 없는 자리표시예요. JSON에 있는 자리표시만 써 주세요"
            continue
        a, b, mods = parsed.groups()
        absolute = '절대' in mods
        if a not in amounts.values or (b and b not in amounts.values):
            ref.error = f"'{ref.text}'는 JSON에 없는 자리표시예요"
            continue
        va, ka = amounts.values[a]
        if b:
            vb, kb = amounts.values[b]
            if ka != kb or ka == 'other':
                ref.error = f"'{ref.text}': 차이는 '매달 남는 돈'끼리, 연봉끼리만 쓸 수 있어요"
                continue
            ref.value, ref.diff, ref.kind = abs(va - vb), True, ka
        else:
            ref.value, ref.raw, ref.kind, ref.key = (abs(va) if absolute else va), va, ka, a
    return refs


def _format(value: int) -> str:
    """금액 글자: 화면과 같게 만 원으로 떨어지지 않으면 반올림해 '약'을 붙인다."""
    if value % 10_000 == 0:
        return won(value)
    unit = 10_000 if abs(value) >= 10_000 else 1_000 if abs(value) >= 1_000 else 1
    rounded = (abs(value) + unit // 2) // unit * unit
    return '약 ' + won(-rounded if value < 0 else rounded)


def check_draft(draft: str, amounts: Amounts) -> list[str]:
    """초안의 금액 문제 → 다시 쓸 때 알려 줄 문제 목록 (없으면 [])."""
    problems = []
    refs = _refs(draft, amounts)
    problems += [r.error for r in refs if r.error]
    bare = _URL.sub(' ', _PLACEHOLDER.sub(' ', draft))  # 주소 속 숫자는 금액이 아니다
    if '{{' in bare or '}}' in bare:
        problems.append('자리표시 괄호가 덜 닫혔어요. {{A1}}처럼 써 주세요')
    has_zero = any(v == 0 for v, _ in amounts.values.values())
    for m in _LITERAL_MONEY.finditer(bare):
        if has_zero and _ZERO.fullmatch(m.group(0)) and not _NUMBER_END.search(bare[: m.start()]):
            # '교통비는 0원으로 계산했어요': 틀릴 숫자가 없다 (어디에 붙였는지는 검토가 본다)
            continue
        problems.append(
            f"'{m.group(0).strip()}'처럼 금액을 숫자로 직접 썼어요. "
            '금액은 JSON의 자리표시({{A1}} 등)로만 써 주세요'
        )
    for r in refs:
        if r.error or r.diff or r.kind != 'remaining' or r.raw is None:
            continue
        after = draft[r.end : r.end + _AFTER]
        if r.raw >= 0 and _DEFICIT.search(after):
            problems.append(f"'{r.text}'는 남는 돈(흑자)인데 모자란다고 썼어요")
        if r.raw < 0 and r.value is not None and r.value > 0 and r.value == -r.raw:
            if not _DEFICIT.search(after):  # '|절대'로 크기만 썼는데 모자란다는 말이 없다
                problems.append(f"'{r.text}'는 매달 모자라는 금액인데 그렇게 쓰지 않았어요")
        elif r.raw < 0 and _SURPLUS.search(after) and not _DEFICIT.search(after):
            problems.append(f"'{r.text}'는 마이너스(매달 모자람)인데 남는다고 썼어요")
    problems += _wrong_neighbors(draft, refs, amounts)
    return list(dict.fromkeys(problems))


def _wrong_neighbors(draft: str, refs: list[_Ref], amounts: Amounts) -> list[str]:
    """금액이 붙으면 안 되는 연봉 뒤에 왔는지: 바로 앞 연봉 자리표시를 본다 (Amounts.not_after).

    사이에 '최소 연봉'이라고 다시 밝혔으면 맞게 쓴 것이다 ('… 최소 연봉일 때 세후 월급은 {{A27}}').
    """
    problems, last_salary, last_end = [], None, 0
    for r in refs:
        if r.error or r.key is None:
            continue
        banned = amounts.not_after.get(r.key)
        if banned and last_salary == banned and '최소 연봉' not in draft[last_end : r.start]:
            problems.append(
                f"'{r.text}'는 최소 연봉일 때의 세후 월급이에요. 지원 받는 동안 연봉"
                f"('{{{{{banned}}}}}') 뒤에 붙이지 말고 최소 연봉 바로 뒤에 써 주세요"
            )
        if r.kind == 'salary':
            last_salary, last_end = r.key, r.end
    return problems


# 금액은 늘 '원'(받침 있음)으로 끝난다 → 자리표시 바로 뒤 조사를 받침에 맞춘다
# ('11만원를' → '을', '5만원로' → '으로', '5만원로도' → '으로도')
_PARTICLE = re.compile(r'(를|가|는|와)(?![가-힣])|로(?=(?:도|만|는|부터)?(?![가-힣]))')
_FIXED = {'를': '을', '가': '이', '는': '은', '와': '과', '로': '으로'}


# 자리표시 바로 앞의 낱말 '약' ('절약', '계약' 같은 낱말 끝 글자는 아니다)
_ABOUT_BEFORE = re.compile(r'(?:^|[\s(])약\s*$')


def render(draft: str, amounts: Amounts) -> str:
    """검사를 통과한 초안의 자리표시를 금액 글자로 바꾼다."""
    out, pos = [], 0
    for r in _refs(draft, amounts):
        if r.value is None:
            raise ValueError(f'검사하지 않은 자리표시: {r.text}')
        text = _format(r.value)
        # AI가 '약 {{A3}}'처럼 '약'을 따로 썼으면 겹치지 않게 ('약 약 196만원')
        if text.startswith('약 ') and _ABOUT_BEFORE.search(draft[pos : r.start]):
            text = text.removeprefix('약 ')
        out += [draft[pos : r.start], text]
        pos = r.end
        if m := _PARTICLE.match(draft, pos):
            out.append(_FIXED[m.group(0)])
            pos = m.end()
    out.append(draft[pos:])
    return ''.join(out)
