"""원문 대조 도구 — LLM이 낸 값을 코드가 원문과 맞춰 본다.

LLM은 값과 '원문 인용(quote)'을 함께 낸다. 여기 함수들은
  - 인용이 정말 원문에 있는지 (quote_in_text)
  - 인용 안의 금액·개월·퍼센트가 LLM이 낸 숫자와 같은지 (amounts_krw 등)
  - 인용이 '월' 금액인지 '연' 금액인지, 범위인지 (단위 혼동 막기)
  - 근무지를 코드표(data/lawd_codes.csv)의 시군구 코드로 바꿀 수 있는지 (find_region)
를 확인한다. 숫자는 LLM이 아니라 이 코드가 원문에서 다시 읽는다 (설계 원칙).
"""

import csv
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path

# backend/data/lawd_codes.csv  (이 파일: backend/src/backend/normalize/checks.py)
LAWD_CODES_PATH = Path(__file__).resolve().parents[3] / 'data' / 'lawd_codes.csv'


def squash(s: str) -> str:
    """비교용으로 글자를 정리한다: 전각·반각 통일(NFKC), 공백·줄바꿈 전부 제거, 따옴표 통일."""
    s = unicodedata.normalize('NFKC', s)
    s = s.replace('“', '"').replace('”', '"').replace('‘', "'").replace('’', "'")
    return re.sub(r'\s+', '', s)


def quote_in_text(quote: str | None, text: str) -> bool:
    """인용이 원문에 글자 그대로 있는지 (띄어쓰기·줄바꿈 차이는 무시)."""
    if not quote or not quote.strip():
        return False
    return squash(quote) in squash(text)


# ── 금액 읽기 ────────────────────────────────────────────────────

_NUM_RE = re.compile(r'(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?')
_SMALL_UNITS = {'천': 1_000, '백': 100, '십': 10}
_BIG_UNITS = {'만': 10_000, '억': 100_000_000}


def _read_amount(s: str, pos: int) -> tuple[int | None, int]:
    """s[pos]부터 한국식 금액 하나를 읽는다. (금액 또는 None, 다음 위치)

    '20만원', '3,600만원', '40,000,000원', '4천만원', '3천6백만원', '1억 5천만원', '1.5억'.
    '원·만·억'이 하나도 없는 맨숫자(12개월, 2026-11-16)는 금액이 아니므로 None.
    """
    total = 0.0  # 만·억 단위로 끝난 부분의 합
    section = 0.0  # 아직 만·억을 만나지 않은 천·백·십 부분
    is_money = False
    while True:
        m = _NUM_RE.match(s, pos)
        x = float(m.group(1).replace(',', '') + (m.group(2) or '')) if m else None
        if m:
            pos = m.end()
        unit = s[pos] if pos < len(s) else ''
        if unit in _SMALL_UNITS and x is not None:
            section += x * _SMALL_UNITS[unit]
            pos += 1
            continue
        if unit in _BIG_UNITS and (x is not None or section):
            total += (section + (x or 0)) * _BIG_UNITS[unit]
            section = 0
            is_money = True
            pos += 1
            continue
        if unit == '원' and (x is not None or section or is_money):
            total += section + (x or 0)
            is_money = True
            pos += 1
        break
    return (round(total) if is_money else None), pos


def amounts_krw(quote: str) -> set[int]:
    """인용 안의 금액을 원 단위로 모두 읽는다.

    예) '3,600만원' → 36000000, '월 20만 원' → 200000, '40,000,000원' → 40000000,
        '1억 2,000만원' → 120000000, '4천만원' → 40000000
    """
    s = squash(quote)
    found: set[int] = set()
    i = 0
    while i < len(s):
        starts_number = s[i].isdigit() and (i == 0 or not (s[i - 1].isdigit() or s[i - 1] in ',.'))
        if not starts_number:
            i += 1
            continue
        amount, end = _read_amount(s, i)
        if amount is not None:
            found.add(amount)
        i = max(end, i + 1)
    return found


def says_none(quote: str) -> bool:
    """'없음', '지원 없음'처럼 '없다'는 뜻의 인용인지."""
    return '없' in squash(quote)


# '이용료 없음', '본인 부담 없음'처럼 비용이 없다는 말 (그 자체는 '제공 안 함'이 아니다)
_NO_COST = re.compile(r'(이용료|이용비|요금|비용|본인부담|자부담|부담)(은|는|이)?없(음|이|다)?')


def denies_existence(quote: str) -> bool:
    """'통근버스는 없대', '사택 없음'처럼 그 자체가 없다는 인용인지.

    '통근버스 운행 (이용료 없음)'처럼 비용이 없다는 말은 빼고 본다.
    """
    return '없' in _NO_COST.sub('', squash(quote))


def is_range(quote: str) -> bool:
    """금액 하나로 정할 수 없는 표기인지.

    '3,000~3,500만원', '3,000만원~3,500만원', '3,000만원 이상', '월 최대 30만원' 등.
    '2026-01 기준' 같은 날짜의 하이픈은 범위로 보지 않는다.
    """
    s = squash(quote)
    if re.search(r'[~∼〜]', s):
        return True
    if re.search(r'이상|이하|부터|최대|최소|내외|이내|미만|초과', s):
        return True
    # 날짜(2026-01, 2026-11-16)만 지운다. '3000-3500만원'의 3000은 연도가 아니라 남는다.
    no_dates = re.sub(r'(19|20)\d{2}-(1[0-2]|0?[1-9])(?!\d)(-\d{1,2})?', '', s)
    return re.search(r'\d-\d', no_dates) is not None


def mentions_monthly_pay(quote: str) -> bool:
    """연봉 없이 월급·시급만 적힌 표기인지 ('월급 250만원', '월 250만원', '시급 1만원').

    '연봉 4,000만원 (식대 월 20만원 포함)'처럼 연봉이 함께 적힌 인용은 해당하지 않는다.
    """
    s = squash(quote)
    if '연봉' in s:
        return False
    return bool(re.search(r'월급|월\d|시급|시간당', s))


def mentions_yearly_amount(quote: str) -> bool:
    """'연 100만원'처럼 1년 단위 금액이 적혀 있는지 (월 금액 칸에 넣으면 안 되는 것)."""
    s = squash(quote)
    # '연봉3,000만원'은 연 금액이지만 '연봉에 포함'(식대 인용)은 아니다
    # → '연봉' 바로 뒤에 숫자가 올 때만 본다
    return bool(re.search(r'연\d|연간\d|1년에\d|연봉\d', s)) and not re.search(r'월\d', s)


def months_in(quote: str) -> set[int]:
    """인용 안의 기간을 개월 수로 읽는다. '3개월' → 3, '2년간' → 24, '1년 6개월' → 18"""
    s = squash(quote)
    found = {int(m.group(1)) for m in re.finditer(r'(\d+)개월', s)}
    found |= {int(m.group(1)) * 12 for m in re.finditer(r'(\d+)년', s)}
    found |= {int(m.group(1)) * 12 + int(m.group(2)) for m in re.finditer(r'(\d+)년(\d+)개월', s)}
    return found


def rates_in(quote: str) -> set[float]:
    """인용 안의 퍼센트를 비율로 읽는다. '90%' → 0.9"""
    s = squash(quote)
    return {round(float(m.group(1)) / 100, 4) for m in re.finditer(r'(\d+(?:\.\d+)?)%', s)}


# ── 근무지 → 시군구 코드 ─────────────────────────────────────────

# 원문에 흔히 쓰는 줄임·옛 표기 → 코드표의 시도명.
# 광주·전남은 2026-07-01부터 전남광주통합특별시 (코드표 기준).
SIDO_ALIASES = {
    '서울': '서울특별시',
    '서울시': '서울특별시',
    '부산': '부산광역시',
    '대구': '대구광역시',
    '인천': '인천광역시',
    '광주': '전남광주통합특별시',
    '광주광역시': '전남광주통합특별시',
    '대전': '대전광역시',
    '울산': '울산광역시',
    '세종': '세종특별자치시',
    '세종시': '세종특별자치시',
    '경기': '경기도',
    '강원': '강원특별자치도',
    '강원도': '강원특별자치도',
    '충북': '충청북도',
    '충남': '충청남도',
    '전북': '전북특별자치도',
    '전라북도': '전북특별자치도',
    '전남': '전남광주통합특별시',
    '전라남도': '전남광주통합특별시',
    '경북': '경상북도',
    '경남': '경상남도',
    '제주': '제주특별자치도',
    '제주도': '제주특별자치도',
}

# 시군구가 따로 없는 시도 (시도 = 시군구 한 줄)
_SINGLE_REGION_SIDO = {'세종특별자치시'}


@dataclass(frozen=True)
class Region:
    lawd_cd: str
    sido: str
    sigungu: str
    selectable: bool  # false면 '성남시'처럼 구를 골라야 하는 상위 코드 (조회에 바로 못 씀)


@cache
def load_regions() -> tuple[Region, ...]:
    with LAWD_CODES_PATH.open(encoding='utf-8') as f:
        return tuple(
            Region(r['lawd_cd'], r['sido'], r['sigungu'], r['selectable'] == 'true')
            for r in csv.DictReader(f)
        )


def normalize_sido(sido: str | None) -> str | None:
    if not sido:
        return None
    sido = sido.strip()
    return SIDO_ALIASES.get(sido, sido)


def find_region(sido: str | None, sigungu: str | None) -> Region | None:
    """시도·시군구로 코드표의 행을 찾는다. 딱 하나로 정해질 때만 돌려준다 (애매하면 None).

    돌려준 행의 selectable이 false면 구를 더 골라야 한다 (호출하는 쪽에서 확인).
    """
    nsido = normalize_sido(sido) or normalize_sido(sigungu)
    if nsido in _SINGLE_REGION_SIDO:
        rows = [r for r in load_regions() if r.sido == nsido]
        return rows[0] if len(rows) == 1 else None
    if not sigungu:
        return None
    sigungu = ' '.join(sigungu.split())
    nsido = normalize_sido(sido)
    regions = [r for r in load_regions() if nsido is None or r.sido == nsido]

    exact = [r for r in regions if r.sigungu == sigungu]
    if not exact:
        # '분당구'만 적힌 경우 → '성남시 분당구'
        exact = [r for r in regions if r.sigungu.endswith(' ' + sigungu)]
    # 같은 이름이 여럿이면 선택 가능한(selectable) 행을 우선한다.
    selectable = [r for r in exact if r.selectable]
    candidates = selectable or exact
    return candidates[0] if len(candidates) == 1 else None
