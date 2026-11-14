"""근로소득세·지방소득세 — 국세청 근로소득 간이세액표 기준.

[간이세액표란]
회사가 매달 월급에서 미리 떼는 소득세(원천징수) 금액을 정해 둔 표다 (소득세법 시행령 [별표2]).
"월급(비과세 제외) 구간 × 공제대상가족 수"마다 세액이 정해져 있다.
연말정산으로 최종 세금은 조금 달라질 수 있지만, 매달 통장에 들어오는 돈은 이 표대로 계산된다.
그래서 "매달 남는 돈"을 계산할 때는 이 표를 쓴다.

[표 데이터]
withholding_table_2024.csv — 국세청 홈택스가 제공하는 「소득세법 시행령 [별표2] 근로소득 간이세액표
<개정 2024. 2. 29.>」 PDF(33쪽)의 6번 표를 그대로 옮긴 것이다.
  https://teht.hometax.go.kr/doc/sf/a/a/근로소득_간이세액표(조견표).pdf
  - 열: min_k, max_k = 월급여 구간(천원, 이상~미만)
        family_1 ~ family_11 = 공제대상가족 수별 세액(원)
  - 770천원~10,000천원 미만, 646행
  - 2026. 2. 27. 개정판은 8~20세 자녀 공제(아래 CHILD_*)를 바꾼 것으로 알려져 있다.
    팀원의 2026년 10월분 급여명세서(과세 3,330,284원, 1인) 소득세 105,210원이 이 표의 값과 같아
    1인 기준 본표는 같다고 확인했다.
    2026 개정 원문(법제처 별표2)과의 전체 대조는 남은 일이다.

[공제대상가족 수]
본인 1명 + 배우자·부양가족. 혼자 사는 사회초년생은 1명이다.
"""

import csv
from decimal import Decimal
from functools import cache
from pathlib import Path

from backend.calc import rates
from backend.calc.insurance import times_rate_floor10

TABLE_PATH = Path(__file__).with_name('withholding_table_2024.csv')

# 월급여 10,000천원일 때의 세액 (가족 1~11명). PDF 마지막 쪽 "10,000천원" 행.
# 10,000천원을 넘는 월급은 이 값에 아래 공식으로 더한다.
TAX_AT_10M = (
    1_507_400,
    1_431_570,
    1_200_840,
    1_170_840,
    1_140_840,
    1_110_840,
    1_080_840,
    1_050_840,
    1_020_840,
    990_840,
    960_840,
)

# 10,000천원 초과 구간 (PDF 마지막 쪽). (구간 시작 천원, 더하는 금액, 초과분에 곱하는 비율)
# 예: 14,000천원 초과 28,000천원 이하
#     → 10,000천원 세액 + 1,397,000원 + (14,000천원 초과분 × 98% × 38%)
# 비율은 정확한 계산을 위해 Decimal로 둔다 (insurance.times_rate_floor10 설명 참고).
_OVER_10M = (
    (87_000, 31_034_600, Decimal('0.45')),
    (45_000, 13_394_600, Decimal('0.42')),
    (30_000, 7_394_600, Decimal('0.40')),
    (28_000, 6_610_600, Decimal('0.98') * Decimal('0.40')),
    (14_000, 1_397_000, Decimal('0.98') * Decimal('0.38')),
    (10_000, 25_000, Decimal('0.98') * Decimal('0.35')),
)

# 공제대상가족 중 8세 이상 20세 이하 자녀가 있을 때 빼는 금액
# (표 비고 3번, 2024. 2. 29. 개정판 기준)
# TODO: 2026. 2. 27. 개정으로 이 금액이 바뀌었다. 법제처 원문 확인 후 갱신한다.
CHILD_DEDUCTION_1 = 12_500
CHILD_DEDUCTION_2 = 29_160
CHILD_DEDUCTION_EXTRA = 25_000  # 3명째부터 1명당


@cache
def _load_table() -> tuple[tuple[int, ...], ...]:
    """CSV를 한 번만 읽어 (min_k, max_k, 가족1, …, 가족11) 행 목록으로 둔다."""
    with TABLE_PATH.open(encoding='utf-8') as f:
        reader = csv.reader(f)
        next(reader)  # 머리글
        return tuple(tuple(int(x) for x in row) for row in reader)


def _table_tax(monthly_taxable: int, family: int) -> int:
    """가족 수 1~11명일 때 표에서 세액을 찾는다 (10,000천원 초과는 공식)."""
    col = family - 1  # family_1 → 0번 열
    if monthly_taxable < 770_000:
        return 0  # 표가 770천원부터 시작한다. 그보다 적으면 떼는 세금이 없다.
    if monthly_taxable >= 10_000_000:
        base = TAX_AT_10M[col]
        for start_k, add, ratio in _OVER_10M:
            over = monthly_taxable - start_k * 1000  # 구간 시작을 넘는 금액(원)
            if over > 0:
                # 표에 끝수 처리 규정이 없어, 다른 칸과 같게 10원 미만을 버린다.
                return int((base + add + Decimal(over) * ratio) // 10) * 10
        return base  # 정확히 10,000천원
    for row in _load_table():
        min_k, max_k = row[0], row[1]
        if min_k * 1000 <= monthly_taxable < max_k * 1000:
            return row[2 + col]
    raise ValueError(
        f'간이세액표에서 구간을 찾지 못했습니다: {monthly_taxable}'
    )  # 표가 빠짐없으면 오지 않음


def income_tax(monthly_taxable: int, family: int = 1, children_8_to_20: int = 0) -> int:
    """매달 떼는 근로소득세.

    monthly_taxable: 비과세(식대 등)를 뺀 월 급여(원)
    family: 공제대상가족 수 (본인 포함, 1 이상)
    children_8_to_20: 그중 8세 이상 20세 이하 자녀 수
    """
    if family < 1:
        raise ValueError('공제대상가족 수는 본인을 포함해 1명 이상입니다.')
    if not 0 <= children_8_to_20 < family:
        raise ValueError('자녀 수는 0 이상, 공제대상가족 수보다 작아야 합니다.')

    if family <= 11:
        tax = _table_tax(monthly_taxable, family)
    else:
        # 표 비고 4번: 11명 세액 − (10명 세액 − 11명 세액) × 11명을 넘는 가족 수
        t11, t10 = _table_tax(monthly_taxable, 11), _table_tax(monthly_taxable, 10)
        tax = t11 - (t10 - t11) * (family - 11)

    if children_8_to_20 == 1:
        tax -= CHILD_DEDUCTION_1
    elif children_8_to_20 >= 2:
        tax -= CHILD_DEDUCTION_2 + CHILD_DEDUCTION_EXTRA * (children_8_to_20 - 2)
    return max(tax, 0)  # 빼서 음수가 되면 0원 (표 비고 3번)


def local_income_tax(income_tax_won: int) -> int:
    """지방소득세 = 근로소득세 × 10%, 10원 미만 버림"""
    return times_rate_floor10(income_tax_won, rates.LOCAL_INCOME_TAX_RATIO)
