"""세후 월급 계산 — 4대보험과 소득세를 한 번에 뺀다.

    세후 월급(실수령액) = 세전 월급(비과세 포함) − 4대보험 − 근로소득세 − 지방소득세

사용 예:
    >>> monthly_net(gross_monthly=3_000_000, nontaxable_monthly=200_000).net
    연봉으로 계산할 때는 monthly_net_from_annual(36_000_000, meal_monthly=200_000)

이 함수는 숫자만 계산한다 (LLM 없음). 잔여액 계산(#8)과 손익분기 연봉(#14)이 이 결과를 쓴다.
"""

from dataclasses import asdict, dataclass

from backend.calc import insurance, rates
from backend.calc.income_tax import income_tax, local_income_tax


@dataclass(frozen=True)
class Payslip:
    """한 달 급여 계산 결과 (모두 원). 화면에 그대로 보여 줄 수 있게 항목을 나눠 둔다."""

    gross: int  # 세전 월급 (비과세 포함)
    nontaxable: int  # 비과세 금액 (식대 등)
    taxable: int  # 과세 급여 = gross − nontaxable
    national_pension: int
    health_insurance: int
    long_term_care: int
    employment_insurance: int
    income_tax: int
    local_income_tax: int

    @property
    def deductions(self) -> int:
        """공제 합계"""
        return (
            self.national_pension
            + self.health_insurance
            + self.long_term_care
            + self.employment_insurance
            + self.income_tax
            + self.local_income_tax
        )

    @property
    def net(self) -> int:
        """세후 월급 (실수령액)"""
        return self.gross - self.deductions

    def to_dict(self) -> dict[str, int]:
        return {**asdict(self), 'deductions': self.deductions, 'net': self.net}


def monthly_net(
    gross_monthly: int,
    nontaxable_monthly: int = 0,
    family: int = 1,
    children_8_to_20: int = 0,
    insurance_base_monthly: int | None = None,
) -> Payslip:
    """한 달 세후 월급을 계산한다.

    gross_monthly: 세전 월급, 비과세 포함 (원)
    nontaxable_monthly: 그중 비과세 금액 (예: 현금 식대 월 20만원)
    family, children_8_to_20: 소득세 계산용 공제대상가족 수, 8~20세 자녀 수
    insurance_base_monthly: 국민연금·건강보험을 매길 신고 금액(기준소득월액·보수월액).
        모르면 비워 둔다 → 이번 달 과세 급여로 계산
    """
    if gross_monthly < 0 or nontaxable_monthly < 0:
        raise ValueError('금액은 0 이상이어야 합니다.')
    if nontaxable_monthly > gross_monthly:
        raise ValueError('비과세 금액이 월급보다 클 수 없습니다.')

    taxable = gross_monthly - nontaxable_monthly
    base = taxable if insurance_base_monthly is None else insurance_base_monthly
    health = insurance.health_insurance(base)
    tax = income_tax(taxable, family, children_8_to_20)
    return Payslip(
        gross=gross_monthly,
        nontaxable=nontaxable_monthly,
        taxable=taxable,
        national_pension=insurance.national_pension(base),
        health_insurance=health,
        long_term_care=insurance.long_term_care(health),
        employment_insurance=insurance.employment_insurance(taxable),
        income_tax=tax,
        local_income_tax=local_income_tax(tax),
    )


def monthly_net_from_annual(
    annual_salary: int,
    meal_monthly: int = 0,
    meal_in_salary: bool = True,
    family: int = 1,
    children_8_to_20: int = 0,
) -> Payslip:
    """연봉으로 한 달 세후 월급을 계산한다 (연봉을 12개월로 똑같이 나눈다고 본다).

    annual_salary: 세전 연봉 (원)
    meal_monthly: 현금으로 주는 식대 (월). 월 20만원까지만 비과세로 본다.
    meal_in_salary: 식대가 연봉에 포함돼 있으면 True (예: "연봉 4,000만원, 식대 월 20만원 포함"),
        연봉과 별도로 더 주면 False (예: "연봉 3,600만원 + 식대 월 20만원 별도")
    """
    base_monthly = annual_salary // 12
    gross = base_monthly if meal_in_salary else base_monthly + meal_monthly
    nontaxable = min(meal_monthly, rates.MEAL_NONTAXABLE_LIMIT, gross)
    return monthly_net(gross, nontaxable, family, children_8_to_20)
