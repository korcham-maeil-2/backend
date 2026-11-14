"""4대보험 근로자 부담액 계산 (국민연금·건강보험·장기요양보험·고용보험).

[금액 단위와 끝수 처리]
모든 금액은 원 단위 정수다. 실제 고지 방식대로 10원 미만은 버린다 (예: 140,885원 → 140,880원).
국민연금은 보험료를 매기는 기준 금액(기준소득월액)의 1,000원 미만도 먼저 버린다.

[보험료를 매기는 기준 금액]
- 국민연금·건강보험: 원래는 회사가 1년 단위로 신고한 금액(기준소득월액·보수월액)에 매긴다.
  월급지도는 신고 금액을 모르므로, 따로 주지 않으면 이번 달 과세 급여를 그대로 쓴다.
- 고용보험: 이번 달 실제 과세 급여(연장근로수당 포함)에 매긴다.
"""

from decimal import Decimal

from backend.calc import rates


def times_rate_floor10(won: int, rate: float) -> int:
    """금액 × 요율을 계산하고 10원 미만을 버린다. 2,966,000 × 4.75% = 140,885 → 140,880

    [Decimal을 쓰는 이유]
    파이썬의 소수(float)는 0.0475 같은 값을 정확히 저장하지 못해, 곱한 결과가 29,999.9999…처럼
    아주 조금 작게 나올 수 있다. 그러면 10원 단위 버림이 한 칸 틀어진다. 돈 계산이라 Decimal(정확한
    십진수)로 계산한다. str(rate)로 바꿔 넣어야 0.0475가 정확히 0.0475가 된다.
    """
    return int(Decimal(won) * Decimal(str(rate)) // 10) * 10


def national_pension(base_monthly: int) -> int:
    """국민연금 근로자 부담액.

    base_monthly: 기준소득월액으로 쓸 월 금액 (보통 비과세를 뺀 월급).
    1,000원 미만을 버린 뒤 하한(41만원)·상한(659만원) 안으로 맞추고 4.75%를 곱한다.
    """
    base = (base_monthly // 1000) * 1000
    base = min(max(base, rates.NATIONAL_PENSION_BASE_MIN), rates.NATIONAL_PENSION_BASE_MAX)
    return times_rate_floor10(base, rates.NATIONAL_PENSION_RATE)


def health_insurance(base_monthly: int) -> int:
    """건강보험 근로자 부담액 = 보수월액 × 3.595%"""
    return times_rate_floor10(base_monthly, rates.HEALTH_INSURANCE_RATE)


def long_term_care(health_premium: int) -> int:
    """장기요양보험 근로자 부담액 = 건강보험료 × 13.14% (건강보험료에서 계산한다)"""
    return times_rate_floor10(health_premium, rates.LONG_TERM_CARE_RATIO)


def employment_insurance(taxable_monthly: int) -> int:
    """고용보험 근로자 부담액 = 이번 달 과세 급여 × 0.9%"""
    return times_rate_floor10(taxable_monthly, rates.EMPLOYMENT_INSURANCE_RATE)
