"""리포트에 넘길 사실 정리 + 리포트 속 금액 검사 (LLM 없음, 숫자는 코드가 책임진다).

[facts]
계산 결과(result)·청년 지원(local_support)·비교표(table)에서 리포트에 필요한 것만 골라
Claude에 넘길 JSON을 만든다. 금액은 화면과 같은 한글 단위 글자('196만 3,100원')로 준다.

[개인정보를 넘기지 않는다] (CLAUDE.md 설계 원칙, #20)
  - 고정비는 합계만 (항목 이름은 사용자가 자유롭게 쓴 글이라 넘기지 않는다)
  - 나이·가족 수·지금 회사 연봉 같은 사용자 정보는 넘기지 않는다
    (지금 회사는 '지금 회사 그대로 매달 남는 돈' 기준값 하나만)
  - 회사 이름·근무지·연봉은 공고에 공개된 정보라 넘긴다

[금액 검사] check_amounts
리포트 글에서 '…원' 금액을 모두 찾아, 넘긴 금액(또는 두 금액의 차이) 중 하나와 맞는지 본다.
'약 196만원'처럼 반올림해 쓴 것은 쓴 자리까지만 비교한다
(만 원 단위로 쓰면 1만 원 차이까지 같다고 본다).
"""

import re
from collections.abc import Iterable
from itertools import combinations
from typing import Any

from backend.calc.text import won

# ── 사실 정리 ────────────────────────────────────────────────────


def _amount(value: int | None) -> str | None:
    return won(value) if value is not None else None


def _item(item: dict | None) -> dict | None:
    """값 하나 (금액·신뢰도·출처)."""
    if not item:
        return None
    return {
        '금액': _amount(item.get('value')),
        '신뢰도': _STATUS.get(item.get('status'), item.get('status')),
        '출처': item.get('source'),
    }


_STATUS = {
    'confirmed': '공고에서 확인',
    'estimated': '추정',
    'user_input': '사용자 입력',
    'missing': '없음',
}


def _row_info(table: list[dict] | None) -> dict[str, dict]:
    """공고 ID → 근무지·연봉 표기·주거지원비 (비교표에서)."""
    info = {}
    for row in table or []:
        loc = ((row.get('location') or {}).get('value')) or {}
        place = ' '.join(x for x in (loc.get('sido'), loc.get('sigungu')) if x) or loc.get('raw')
        salary_type = (row.get('salary_type') or {}).get('value')
        annual = (row.get('annual_salary_krw') or {}).get('value')
        monthly = (row.get('monthly_salary_krw') or {}).get('value')
        if annual:
            salary = f'연봉 {won(annual)}'
        elif monthly:
            salary = f'월급 {won(monthly)}'
        elif salary_type in ('undisclosed', 'to_propose'):
            salary = '공고에 금액 없음 (비공개)'
        else:
            salary = '공고에서 읽지 못함'
        aid = (row.get('housing_allowance_monthly_krw') or {}).get('value')
        months = (row.get('housing_allowance_months') or {}).get('value')
        info[row.get('id')] = {
            '근무지': place,
            '급여': salary,
            '주거지원비': (
                f'월 {won(aid)}' + (f', {months}개월' if months else ', 기간 모름') if aid else None
            ),
            '_amounts': [x for x in (annual, monthly, aid) if x],
            '_annual': annual,
        }
    return info


def build_facts(
    result: dict | None, local_support: dict | None, table: list[dict] | None
) -> tuple[dict[str, Any], set[int]]:
    """(Claude에 넘길 사실 JSON, 리포트에 나와도 되는 금액들(원))."""
    result = result or {}
    rows = _row_info(table)
    amounts: set[int] = set()
    remaining: list[int] = []  # 매달 남는 돈들 (차이를 말해도 된다)
    salaries: list[int] = []  # 연봉들 (차이를 말해도 된다)

    def keep(*values: int | None) -> None:
        amounts.update(v for v in values if isinstance(v, int))

    postings = []
    for p in result.get('postings', []):
        info = rows.get(p.get('posting_id'), {})
        keep(*info.get('_amounts', []))
        if info.get('_annual'):
            salaries.append(info['_annual'])
        cases = []
        for s in p.get('scenarios', []):
            keep(s.get('monthly_remaining'))
            keep(*((s.get(k) or {}).get('value') for k in ('housing', 'transport')))
            if s.get('monthly_remaining') is not None:
                remaining.append(s['monthly_remaining'])
            cases.append(
                {
                    '경우': s.get('label'),
                    '해당': s.get('applicable', True),
                    '매달 남는 돈': _amount(s.get('monthly_remaining')),
                    '남는 돈 신뢰도': _STATUS.get(s.get('status'), s.get('status')),
                    '주거비': _item(s.get('housing')),
                    '교통비': _item(s.get('transport')),
                    '계산에 빠진 값': s.get('missing') or [],
                }
            )
        net = p.get('net_income') or {}
        keep(net.get('value'), (p.get('allowances') or {}).get('value'))
        keep((p.get('fixed_expenses') or {}).get('value'))
        postings.append(
            {
                '공고': p.get('posting_id'),
                '회사': p.get('company'),
                '근무지': info.get('근무지'),
                '급여': info.get('급여'),
                '주거지원비': info.get('주거지원비'),
                '세후 월급': _item(net),
                '고정비 합계': _amount((p.get('fixed_expenses') or {}).get('value')),
                '경우별': cases,
                '안내': p.get('notes') or [],
            }
        )

    break_even = []
    for be in result.get('break_even', []):
        base = be.get('baseline') or {}
        keep(base.get('monthly_remaining'))
        if base.get('monthly_remaining') is not None:
            remaining.append(base['monthly_remaining'])
        cases = []
        for c in be.get('cases', []):
            keep(
                c.get('annual_salary_krw'),
                c.get('monthly_remaining'),
                c.get('net_income_krw'),
                c.get('during_support_annual_salary_krw'),
                *((c.get(k) or {}).get('value') for k in ('housing', 'transport')),
            )
            salaries += [
                v
                for v in (c.get('annual_salary_krw'), c.get('during_support_annual_salary_krw'))
                if v
            ]
            cases.append(
                {
                    '경우': c.get('label'),
                    '최소 연봉': _amount(c.get('annual_salary_krw')),
                    '지원 받는 동안 최소 연봉': _amount(c.get('during_support_annual_salary_krw')),
                    '최저임금으로 올림': c.get('floor') == 'minimum_wage',
                    '못 구한 이유': c.get('reason'),
                    '안내': c.get('notes') or [],
                }
            )
        break_even.append(
            {
                '회사': be.get('company'),
                '기준': f'{base.get("label")} 매달 {_amount(base.get("monthly_remaining"))}',
                '경우별': cases,
            }
        )

    rents = []
    for r in result.get('rent_estimates', []):
        keep(r.get('monthly_rent_krw'), r.get('deposit_krw'))
        if r.get('monthly_rent_krw') is not None:
            rents.append(
                {
                    '지역': r.get('region'),
                    '원룸 월세 시세': _amount(r.get('monthly_rent_krw')),
                    '보증금': _amount(r.get('deposit_krw')),
                    '출처': r.get('source'),
                }
            )

    # 매달 남는 돈끼리·연봉끼리의 차이는 리포트가 말해도 된다 ('판교가 매달 21만원 더 남아요')
    amounts.update(abs(a - b) for a, b in combinations(set(remaining), 2))
    amounts.update(abs(a - b) for a, b in combinations(set(salaries), 2))

    facts = {
        '공고별 매달 남는 돈': postings,
        '연봉 비공개 공고의 최소 연봉': break_even,
        '최소 연봉 안내': result.get('break_even_note'),
        '지역 원룸 월세 시세 (국토부 실거래가 추정)': rents,
        '근무지별 청년 지원 (남는 돈 계산에는 넣지 않음)': _support(local_support),
        '계산하지 못한 부분': result.get('errors') or [],
    }
    return facts, amounts


def _support(local_support: dict | None) -> dict | None:
    """청년 지원 요약: 지역마다 정책 수와 위쪽 몇 개의 이름·신청 상태. 금액은 넘기지 않는다."""
    if not local_support:
        return None
    status = {
        'open': '신청 중',
        'always': '상시',
        'upcoming': '곧 시작',
        'unknown': '기간 확인 필요',
    }

    def top(policies: Iterable[dict], n: int = 3) -> list[str]:
        return [f'{p.get("name")} ({status.get(p.get("apply_status"), "")})' for p in policies][:n]

    return {
        '지역별': [
            {
                '지역': r.get('region'),
                '신청할 수 있는 지역 정책 수': r.get('matched', 0),
                '예시': top(r.get('policies') or []),
                '조회 실패': r.get('error'),
            }
            for r in local_support.get('regions') or []
        ],
        '전국 정책 수': local_support.get('national_matched', 0),
        '출처': local_support.get('source'),
    }


# ── 금액 검사 ────────────────────────────────────────────────────

# '196만 3,100원', '3,712만원', '1억 2,000만원', '650,000원', '20만원', '1만 2천원', '5천원'
_MONEY = re.compile(
    r'(?<![\d,.])(?:(\d[\d,]*)\s*억\s*)?(?:(\d[\d,]*)\s*만\s*)?'
    r'(?:(\d[\d,]*)\s*천\s*)?(?:(\d[\d,]*)\s*)?원'
)


def _to_int(text: str | None) -> int:
    return int(text.replace(',', '')) if text else 0


def amounts_in(text: str) -> list[tuple[str, int]]:
    """글 속 '…원' 금액들 → (쓴 글자, 원). '지원'·'원룸'처럼 숫자 없는 '원'은 건너뛴다."""
    found = []
    for m in _MONEY.finditer(text):
        eok, man, cheon, rest = m.groups()
        if eok is None and man is None and cheon is None and rest is None:
            continue
        value = (
            _to_int(eok) * 100_000_000
            + _to_int(man) * 10_000
            + _to_int(cheon) * 1_000
            + _to_int(rest)
        )
        found.append((m.group(0).strip(), value))
    return found


def _precision(value: int) -> int:
    """쓴 자리: 끝의 0 개수로 본다 (1,960,000 → 1만, 435,000 → 1천, 1,963,100 → 100)."""
    if value == 0:
        return 1
    zeros = len(str(value)) - len(str(value).rstrip('0'))
    return min(10**zeros, 10_000)


def check_amounts(report: str, allowed: set[int]) -> list[str]:
    """리포트 금액 중 계산 결과에 없는 것 → 다시 쓸 때 알려 줄 문제 목록."""
    problems = []
    for text, value in amounts_in(report):
        step = _precision(value)
        if not any(abs(a - value) < step for a in allowed):
            problems.append(f"'{text}'는 계산 결과에 없는 금액이에요")
    return list(dict.fromkeys(problems))
