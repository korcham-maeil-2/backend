"""비교표 스키마 — 공고 1건 = 비교표 한 행(ComparisonRow).

이 파일에는 모양(설계도)이 두 벌 있다.

1) ComparisonRow (API·프론트용)
   - 세션 API 응답의 table 칸, 확인 화면(frontend#3), 계산 노드(#8)가 쓰는 모양.
   - /openapi.json에 실리므로 프론트의 `pnpm gen:api`가 TS 타입을 만들어 준다.
   - 모든 칸은 '셀' 하나: {value, status, source_text}
       value       : 값. 금액은 '원' 단위 정수. 모르면 null
       status      : confirmed(확인) / estimated(추정)
                     / user_input(사용자 입력) / missing(공고에 없음)
       source_text : 근거가 된 원문 조각 (프론트가 '공고 원문' 툴팁 등으로 보여 줄 수 있다)

2) ExtractionResult (LLM 출력용, API에는 안 나감)
   - Claude에게 '이 모양으로만 답하라'고 넘기는 구조화 출력 스키마.
   - 칸마다 {value, quote}만 있다. status는 일부러 없다:
     LLM이 자기 답을 '확인'이라고 우기지 못하게, status는 코드(rows.py)가 원문과 대조해서 정한다.
"""

from typing import Literal

from pydantic import BaseModel, Field

# ── 1) API·프론트용 ─────────────────────────────────────────────

FieldStatus = Literal['confirmed', 'estimated', 'user_input', 'missing']
"""confirmed=공고에서 확인 / estimated=추정 / user_input=사용자가 입력·수정 / missing=공고에 없음"""

SalaryType = Literal['annual', 'monthly', 'undisclosed', 'to_propose']
"""annual=연봉 금액 있음 / monthly=월급 금액 있음 / undisclosed=금액 없음(내규·협의)
/ to_propose=회사가 희망 연봉을 물음.
연봉 범위(3,000~3,500만원)·시급처럼 금액 하나로 정할 수 없으면
null(공고에 없음)로 두고 사용자에게 묻는다."""

EmploymentType = Literal['정규직', '계약직', '인턴', '파견', '프리랜서']
"""LLM이 고를 수 있는 고용형태. '신입·경력직'은 채용 구분(HiringTrack)이라 여기 들어가지 않는다."""

HiringTrack = Literal['신입', '경력', '무관']
"""채용 구분. 무관 = 신입·경력 모두 지원 가능 ('신입 또는 경력 2년 미만' 등)."""

_STATUS_DESC = (
    'confirmed=공고에서 확인, estimated=추정(원문과 대조되지 않음), '
    'user_input=사용자가 확인 화면에서 입력·수정, missing=공고에 없음'
)


class _Cell(BaseModel):
    """모든 셀의 공통 칸. 기본값은 '공고에 없음'."""

    status: FieldStatus = Field(default='missing', description=_STATUS_DESC)
    source_text: str | None = Field(default=None, description='근거가 된 원문 조각')


class TextCell(_Cell):
    value: str | None = None


# 값 범위. 터무니없는 값(20자리 금액, 1000%)은 API에서 422로 막는다.
# (범위가 없으면 아주 큰 숫자가 체크포인터 저장 단계까지 가서 500이 난다)
MAX_KRW = 10**12  # 1조 원
MAX_MONTHS = 1200  # 100년


class MoneyCell(_Cell):
    value: int | None = Field(default=None, ge=0, le=MAX_KRW, description='금액(원)')


class IntCell(_Cell):
    value: int | None = Field(default=None, ge=0, le=MAX_MONTHS, description='개월 수')


class RateCell(_Cell):
    value: float | None = Field(default=None, ge=0, le=1, description='비율. 90%면 0.9')


class BoolCell(_Cell):
    value: bool | None = None


class SalaryTypeCell(_Cell):
    value: SalaryType | None = None


class Location(BaseModel):
    """근무지. sido·sigungu·lawd_cd는 코드가 data/lawd_codes.csv에서 찾아 채운다."""

    raw: str = Field(description='공고에 적힌 근무지 원문')
    sido: str | None = Field(default=None, description='코드표 기준 시도명')
    sigungu: str | None = Field(default=None, description='코드표 기준 시군구명')
    lawd_cd: str | None = Field(default=None, description='법정동 시군구 코드 5자리')


class LocationCell(_Cell):
    value: Location | None = None


class OtherAllowance(BaseModel):
    """기타 월 수당 (통신비 등)."""

    name: str
    monthly_krw: MoneyCell


class ComparisonRow(BaseModel):
    """비교표 한 행 = 공고 1건."""

    id: str = Field(description='행 구분용 ID (posting-1, posting-2 …)')
    company: TextCell = Field(default_factory=TextCell, description='회사명')
    job_title: TextCell = Field(default_factory=TextCell, description='직무')
    employment_type: TextCell = Field(default_factory=TextCell, description='고용형태')
    hiring_track: TextCell = Field(
        default_factory=TextCell, description='채용 구분 (신입 / 경력 / 무관)'
    )
    location: LocationCell = Field(default_factory=LocationCell, description='근무지')
    salary_type: SalaryTypeCell = Field(default_factory=SalaryTypeCell, description='연봉 종류')
    annual_salary_krw: MoneyCell = Field(default_factory=MoneyCell, description='세전 연봉(원)')
    monthly_salary_krw: MoneyCell = Field(
        default_factory=MoneyCell, description='세전 월급(원). salary_type이 monthly일 때'
    )
    meal_allowance_monthly_krw: MoneyCell = Field(
        default_factory=MoneyCell, description='현금으로 주는 식대 월액(원)'
    )
    meal_allowance_in_salary: BoolCell = Field(
        default_factory=BoolCell, description='식대가 연봉 금액 안에 포함되는지'
    )
    meals_provided: BoolCell = Field(
        default_factory=BoolCell, description='회사가 식사를 현물로 제공하는지'
    )
    housing_allowance_monthly_krw: MoneyCell = Field(
        default_factory=MoneyCell, description='주거지원비 월액(원)'
    )
    housing_allowance_months: IntCell = Field(
        default_factory=IntCell, description='주거지원비 지원 개월 수'
    )
    company_housing: BoolCell = Field(default_factory=BoolCell, description='사택·기숙사 제공')
    shuttle_bus: BoolCell = Field(default_factory=BoolCell, description='통근버스 운행')
    transport_allowance_monthly_krw: MoneyCell = Field(
        default_factory=MoneyCell, description='교통비 월액(원)'
    )
    other_allowances: list[OtherAllowance] = Field(
        default_factory=list, description='기타 월 수당 (통신비 등)'
    )
    probation_months: IntCell = Field(default_factory=IntCell, description='수습 개월 수')
    probation_pay_rate: RateCell = Field(
        default_factory=RateCell, description='수습 기간 급여 비율'
    )
    overtime: TextCell = Field(default_factory=TextCell, description='연장·야간근무 관련 메모')
    questions_to_ask: list[str] = Field(
        default_factory=list,
        description='계산에 필요한데 공고에 없어 사용자에게 물어볼 것 (코드가 빈칸을 보고 만든다)',
    )


# ── 2) LLM 출력용 ────────────────────────────────────────────────
# 모든 칸을 '필수 + null 허용'으로 둔다. 모르면 빈칸(null)을 명시적으로 내게 하기 위해서다.


class ExtractedText(BaseModel):
    value: str | None
    quote: str | None


class ExtractedMoney(BaseModel):
    value: int | None = Field(description='원 단위 정수. 20만원이면 200000')
    quote: str | None


class ExtractedInt(BaseModel):
    value: int | None
    quote: str | None


class ExtractedRate(BaseModel):
    value: float | None = Field(description='90%면 0.9')
    quote: str | None


class ExtractedBool(BaseModel):
    value: bool | None
    quote: str | None


class ExtractedSalaryType(BaseModel):
    value: SalaryType | None
    quote: str | None


class ExtractedEmploymentType(BaseModel):
    value: EmploymentType | None
    quote: str | None


class ExtractedHiringTrack(BaseModel):
    value: HiringTrack | None
    quote: str | None


class LocationGuess(BaseModel):
    sido: str | None = Field(description='시도. 원문 표기 그대로 (예: 서울, 전남, 경기도)')
    sigungu: str | None = Field(description='시군구 (예: 강서구, 나주시, 성남시 분당구)')


class ExtractedLocation(BaseModel):
    value: LocationGuess | None
    quote: str | None


class ExtractedAllowance(BaseModel):
    name: str
    monthly_krw: int | None
    quote: str | None


class ExtractedPosting(BaseModel):
    company: ExtractedText
    job_title: ExtractedText
    employment_type: ExtractedEmploymentType
    hiring_track: ExtractedHiringTrack
    location: ExtractedLocation
    salary_type: ExtractedSalaryType
    annual_salary_krw: ExtractedMoney
    monthly_salary_krw: ExtractedMoney
    meal_allowance_monthly_krw: ExtractedMoney
    meal_allowance_in_salary: ExtractedBool
    meals_provided: ExtractedBool
    housing_allowance_monthly_krw: ExtractedMoney
    housing_allowance_months: ExtractedInt
    company_housing: ExtractedBool
    shuttle_bus: ExtractedBool
    transport_allowance_monthly_krw: ExtractedMoney
    other_allowances: list[ExtractedAllowance]
    probation_months: ExtractedInt
    probation_pay_rate: ExtractedRate
    overtime: ExtractedText
    # 물어볼 것(questions_to_ask)은 LLM에게 받지 않는다. LLM은 사용자 사정과 계산에 필요한
    # 칸을 모르기 때문에, 코드(rows.py)가 빈칸을 보고 만든다.


class ExtractionResult(BaseModel):
    """Claude가 돌려주는 전체 답. 한 메시지에 공고가 여러 개면 postings도 여러 개."""

    postings: list[ExtractedPosting]
