"""계산 엔진 — 숫자는 LLM이 아니라 이 코드가 계산한다 (설계 원칙).

rates.py       2026년 4대보험 요율·기준 금액 (출처 포함)
insurance.py   4대보험 근로자 부담액
income_tax.py  근로소득세·지방소득세 (국세청 간이세액표, withholding_table_2024.csv)
payroll.py     세후 월급(실수령액) 한 번에 계산
remaining.py   월 잔여액 (통근 / 이사 두 경우, 값마다 출처·신뢰도)
break_even.py  손익분기 연봉 (비공개 공고가 최소 얼마면 같은 돈이 남는지)
summary.py     비교표 전체 → 공고별 잔여액 + 손익분기 연봉 (calculate 노드가 부름)
"""

from backend.calc.payroll import Payslip, monthly_net, monthly_net_from_annual
from backend.calc.remaining import UserProfile, monthly_remaining
from backend.calc.summary import calculate_all

__all__ = [
    'Payslip',
    'UserProfile',
    'calculate_all',
    'monthly_net',
    'monthly_net_from_annual',
    'monthly_remaining',
]
