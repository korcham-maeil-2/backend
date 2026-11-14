"""계산 엔진 — 숫자는 LLM이 아니라 이 코드가 계산한다 (설계 원칙).

rates.py       2026년 4대보험 요율·기준 금액 (출처 포함)
insurance.py   4대보험 근로자 부담액
income_tax.py  근로소득세·지방소득세 (국세청 간이세액표, withholding_table_2024.csv)
payroll.py     세후 월급(실수령액) 한 번에 계산
"""

from backend.calc.payroll import Payslip, monthly_net, monthly_net_from_annual

__all__ = ['Payslip', 'monthly_net', 'monthly_net_from_annual']
