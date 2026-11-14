"""계산 엔진 테스트 (#7) — 4대보험·근로소득세·세후 월급.

[기댓값의 출처] 기댓값은 코드로 계산하지 않고 아래 자료에서 직접 찾은 값만 쓴다.
- 명세서: 팀원이 만든 가상 급여명세서 data/demo/users/user_c/payslip_2026-10.png
  (2026년 10월분, 공제 내역이 2026년 요율대로 계산돼 있음. 값은 users/user_c.json에도 있음)
- 간이세액표: 국세청 홈택스 「소득세법 시행령 [별표2] 근로소득 간이세액표 <개정 2024. 2. 29.>」 PDF
  https://teht.hometax.go.kr/doc/sf/a/a/근로소득_간이세액표(조견표).pdf  (쪽 번호를 함께 적음)
- 손계산: 요율 × 금액을 손으로 계산한 값 (식을 주석에 적음)

[실행] backend 폴더에서 uv run pytest tests/test_calc.py
"""

import pytest

from backend.calc import insurance, monthly_net, monthly_net_from_annual
from backend.calc.income_tax import income_tax, local_income_tax

# ── 1. 실제 급여명세서와 한 원도 틀리지 않는지 ─────────────────────────


def test_matches_demo_payslip_2026_10():
    """명세서: 기본급 2,966,660 + 연장근로수당 363,624 + 식대(비과세) 200,000, 공제대상가족 1명.

    이 회사는 국민연금·건강보험을 기본급(신고 금액)에 매긴다 → insurance_base_monthly로 넘긴다.
    """
    p = monthly_net(
        gross_monthly=3_530_284,
        nontaxable_monthly=200_000,
        family=1,
        insurance_base_monthly=2_966_660,
    )
    assert p.taxable == 3_330_284  # 명세서 '과세 대상 금액'
    assert p.national_pension == 140_880  # 명세서
    assert p.health_insurance == 106_650  # 명세서
    assert p.long_term_care == 14_010  # 명세서
    assert p.employment_insurance == 29_970  # 명세서
    assert p.income_tax == 105_210  # 명세서 (= 간이세액표 17쪽 3,320~3,340천원, 1명)
    assert p.local_income_tax == 10_520  # 명세서
    assert p.deductions == 407_240  # 명세서 '공제 합계'
    assert p.net == 3_123_044  # 명세서 '실지급액'


# ── 2. 간이세액표에서 직접 찾은 소득세 (공제대상가족 1명 = 혼자 사는 사회초년생) ──


@pytest.mark.parametrize(
    ('monthly_taxable', 'expected', 'where'),
    [
        (2_000_000, 19_520, '11쪽 2,000~2,010천원'),  # 연봉 약 2,400만원
        (2_500_000, 35_600, '14쪽 2,500~2,510천원'),  # 연봉 3,000만원
        (3_000_000, 74_350, '16쪽 3,000~3,020천원'),  # 연봉 3,600만원
        (3_330_284, 105_210, '17쪽 3,320~3,340천원'),  # 명세서와 같은 행
        (4_000_000, 195_960, '19쪽 4,000~4,020천원'),  # 연봉 4,800만원
    ],
)
def test_income_tax_family_1_from_table(monthly_taxable, expected, where):
    assert income_tax(monthly_taxable, family=1) == expected, where


def test_income_tax_family_2_from_table():
    # 간이세액표 16쪽 3,000~3,020천원, 공제대상가족 2명
    assert income_tax(3_000_000, family=2) == 56_850


def test_income_tax_row_boundaries():
    """구간은 '이상~미만'이다. 경계 바로 아래와 경계값이 다른 행으로 가는지."""
    assert income_tax(2_999_999, family=1) == 73_060  # 16쪽 2,990~3,000천원
    assert income_tax(3_000_000, family=1) == 74_350  # 16쪽 3,000~3,020천원
    assert income_tax(3_019_999, family=1) == 74_350
    assert income_tax(3_020_000, family=1) == 76_060  # 16쪽 3,020~3,040천원


def test_income_tax_below_table_is_zero():
    assert income_tax(769_999) == 0  # 표가 770천원부터 시작
    assert income_tax(770_000) == 0  # 2쪽 770~775천원 행은 '-'(0원)
    assert income_tax(0) == 0


def test_income_tax_around_10_million():
    assert income_tax(9_999_999, family=1) == 1_503_990  # 33쪽 9,980~10,000천원
    assert income_tax(10_000_000, family=1) == 1_507_400  # 33쪽 '10,000천원' 행
    # 33쪽 공식 (10,000천원 초과 14,000천원 이하):
    #   10,000천원 세액 + 초과분 × 98% × 35% + 25,000원
    #   = 1,507,400 + 2,000,000 × 0.98 × 0.35 + 25,000 = 1,507,400 + 686,000 + 25,000
    assert income_tax(12_000_000, family=1) == 2_218_400


def test_income_tax_more_than_11_family():
    # 표 비고 4번: 11명 세액 − (10명 세액 − 11명 세액) × 11명 초과 인원
    # 17쪽 3,320~3,340천원: 10명 10,390, 11명 7,010 → 12명 = 7,010 − 3,380 × 1
    assert income_tax(3_330_000, family=12) == 3_630


def test_income_tax_children_deduction():
    # 표 비고 3번 (2024.2.29 개정판): 8~20세 자녀 1명 12,500원, 2명 29,160원 공제
    assert income_tax(3_000_000, family=2, children_8_to_20=1) == 56_850 - 12_500
    # 11쪽 2,000~2,010천원 3명 6,600원 − 29,160원 → 음수면 0원
    assert income_tax(2_000_000, family=3, children_8_to_20=2) == 0


def test_income_tax_rejects_invalid_family():
    with pytest.raises(ValueError):
        income_tax(3_000_000, family=0)
    with pytest.raises(ValueError):
        income_tax(3_000_000, family=1, children_8_to_20=1)  # 본인뿐인데 자녀 1명


def test_local_income_tax_is_10_percent_floor_10():
    assert local_income_tax(105_210) == 10_520  # 10,521 → 10,520
    assert local_income_tax(0) == 0


# ── 3. 4대보험 경계 ─────────────────────────────────────────────


def test_national_pension_min_max():
    # 하한 41만원: 300,000원이어도 410,000 × 4.75% = 19,475 → 19,470
    assert insurance.national_pension(300_000) == 19_470
    # 상한 659만원: 10,000,000원이어도 6,590,000 × 4.75% = 313,025 → 313,020
    assert insurance.national_pension(10_000_000) == 313_020
    # 1,000원 미만 버림: 2,966,660 → 2,966,000 × 4.75% = 140,885 → 140,880 (명세서와 같음)
    assert insurance.national_pension(2_966_660) == 140_880


def test_float_error_does_not_shift_floor():
    # 3,133,333 × 0.9% = 28,199.997 → 10원 미만 버림 28,190 (float이면 28,200으로 튈 수 있는 경계)
    assert insurance.employment_insurance(3_133_333) == 28_190


# ── 4. 연봉으로 계산 (시나리오 A: 서울 연봉 4,000만원, 현금 식대 월 20만원 포함) ──


def test_from_annual_meal_included():
    """손계산. 4,000만원 ÷ 12 = 3,333,333원, 그중 식대 200,000원 비과세 → 과세 3,133,333원

    국민연금  3,133,000 × 4.75%  = 148,817.5 → 148,810
    건강보험  3,133,333 × 3.595% = 112,643.3 → 112,640
    장기요양  112,640 × 13.14%   =  14,800.9 →  14,800
    고용보험  3,133,333 × 0.9%   =  28,199.997 → 28,190
    소득세    간이세액표 16쪽 3,120~3,140천원, 1명 = 84,620
    지방소득세 84,620 × 10% = 8,462 → 8,460
    공제 합계 397,520 → 세후 3,333,333 − 397,520 = 2,935,813
    """
    p = monthly_net_from_annual(40_000_000, meal_monthly=200_000, meal_in_salary=True)
    assert (p.gross, p.nontaxable, p.taxable) == (3_333_333, 200_000, 3_133_333)
    assert p.national_pension == 148_810
    assert p.health_insurance == 112_640
    assert p.long_term_care == 14_800
    assert p.employment_insurance == 28_190
    assert p.income_tax == 84_620
    assert p.local_income_tax == 8_460
    assert p.net == 2_935_813


def test_from_annual_meal_separate_and_cap():
    # 연봉과 별도로 식대 월 30만원 → 월급에 더하고, 비과세는 한도 20만원까지만
    p = monthly_net_from_annual(36_000_000, meal_monthly=300_000, meal_in_salary=False)
    assert p.gross == 3_000_000 + 300_000
    assert p.nontaxable == 200_000
    assert p.taxable == 3_100_000


def test_monthly_net_input_validation():
    with pytest.raises(ValueError):
        monthly_net(100_000, nontaxable_monthly=200_000)
    with pytest.raises(ValueError):
        monthly_net(-1)


def test_to_dict_has_net_and_deductions():
    d = monthly_net(3_000_000).to_dict()
    assert d['net'] == d['gross'] - d['deductions']
