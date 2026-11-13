"""LLM 답(ExtractionResult) → 비교표 행(ComparisonRow).

칸마다 status를 코드가 정한다:
  - 값이 없음(null)                                    → missing (공고에 없음). 값을 채우지 않는다.
  - 값은 있는데 인용이 원문에 없음                       → estimated (추정)
  - 인용 속 숫자와 값이 다름                             → estimated
  - 단위가 안 맞음 (월급을 연봉 칸에, 연 금액을 월 칸에, 범위) → estimated
  - '있음'인데 인용이 '없다'고 함                        → estimated
  - 그 밖 (인용이 원문에 있고 숫자·단위도 맞음)            → confirmed (확인)
source_text에는 원문에서 확인된 인용만 담는다 (원문에 없는 인용은 버린다).

사용자에게 물어볼 것(questions_to_ask)도 여기서 코드가 빈칸을 보고 만든다.
"""

from collections.abc import Callable
from typing import Literal

from backend.normalize import checks
from backend.normalize.schema import (
    MAX_KRW,
    MAX_MONTHS,
    BoolCell,
    ComparisonRow,
    ExtractedBool,
    ExtractedInt,
    ExtractedLocation,
    ExtractedMoney,
    ExtractedPosting,
    ExtractedRate,
    ExtractedSalaryType,
    ExtractionResult,
    FieldStatus,
    IntCell,
    Location,
    LocationCell,
    MoneyCell,
    OtherAllowance,
    RateCell,
    SalaryTypeCell,
    TextCell,
    _Cell,
)

REGION_QUESTION = '근무지 시·군·구 확인 (공고 표기로 지역 코드를 정하지 못함)'
LOCATION_MISSING_QUESTION = '근무지 시·군·구 (공고에 없음)'
OVERTIME_QUESTION = '한 달 예상 연장근로 시간 (모르면 0시간으로 계산)'


def _status(value, quote: str | None, text: str, matches: Callable[[str], bool]) -> FieldStatus:
    if value is None:
        return 'missing'
    if not checks.quote_in_text(quote, text):
        return 'estimated'
    return 'confirmed' if matches(quote) else 'estimated'


def _source(quote: str | None, text: str) -> str | None:
    return quote.strip() if checks.quote_in_text(quote, text) else None


def _any(_: str) -> bool:
    return True


def text_cell(e, text: str) -> TextCell:
    """글자 칸 (회사명·직무·고용형태·연장근무 메모). e는 value·quote를 가진 LLM 칸."""
    return TextCell(
        value=e.value,
        status=_status(e.value, e.quote, text, _any),
        source_text=_source(e.quote, text),
    )


def bool_cell(e: ExtractedBool, text: str) -> BoolCell:
    # '있음(true)'인데 인용이 '통근버스는 없대'처럼 '없다'고 하면 모순 → 추정
    # ('통근버스 운행 (이용료 없음)'처럼 비용이 없다는 말은 모순이 아니다)
    def matches(q: str) -> bool:
        return not (e.value is True and checks.denies_existence(q))

    return BoolCell(
        value=e.value,
        status=_status(e.value, e.quote, text, matches),
        source_text=_source(e.quote, text),
    )


def salary_type_cell(e: ExtractedSalaryType, text: str) -> SalaryTypeCell:
    return SalaryTypeCell(
        value=e.value,
        status=_status(e.value, e.quote, text, _any),
        source_text=_source(e.quote, text),
    )


def _within(value, low, high):
    """LLM이 범위를 벗어난 값(음수, 1조 원 초과, 100% 초과)을 내면 버린다 (공고에 없음 취급)."""
    return value if value is None or low <= value <= high else None


def money_cell(e: ExtractedMoney, text: str, per: Literal['year', 'month']) -> MoneyCell:
    """금액 칸. per='year'는 연봉, per='month'는 월급·식대·교통비 같은 월 금액."""
    e = ExtractedMoney(value=_within(e.value, 0, MAX_KRW), quote=e.quote)

    def matches(q: str) -> bool:
        if checks.is_range(q):
            return False  # '3,000~3,500만원'은 금액 하나로 정할 수 없다
        if per == 'year' and checks.mentions_monthly_pay(q):
            return False  # 월급·시급을 연봉 칸에 넣음
        if per == 'month' and checks.mentions_yearly_amount(q):
            return False  # '연 100만원'을 월 금액 칸에 넣음
        # 0원은 '없음', '지원 없음' 같은 인용으로도 확인된다.
        return e.value in checks.amounts_krw(q) or (e.value == 0 and checks.says_none(q))

    return MoneyCell(
        value=e.value,
        status=_status(e.value, e.quote, text, matches),
        source_text=_source(e.quote, text),
    )


def months_cell(e: ExtractedInt, text: str) -> IntCell:
    e = ExtractedInt(value=_within(e.value, 0, MAX_MONTHS), quote=e.quote)
    return IntCell(
        value=e.value,
        status=_status(e.value, e.quote, text, lambda q: e.value in checks.months_in(q)),
        source_text=_source(e.quote, text),
    )


def rate_cell(e: ExtractedRate, text: str) -> RateCell:
    e = ExtractedRate(value=_within(e.value, 0, 1), quote=e.quote)
    return RateCell(
        value=e.value,
        status=_status(e.value, e.quote, text, lambda q: round(e.value, 4) in checks.rates_in(q)),
        source_text=_source(e.quote, text),
    )


def location_cell(e: ExtractedLocation, text: str) -> tuple[LocationCell, bool]:
    """근무지 셀과 '조회에 쓸 지역 코드를 정했는지'를 돌려준다.

    시도·시군구·코드는 LLM 답이 아니라 코드표에서 찾은 값으로 채운다.
    아래 경우는 원문만 믿을 수 없으니 status를 estimated로 두고 사용자에게 묻는다:
      - 코드표에서 못 찾음
      - '경기 성남시'처럼 구를 더 골라야 하는 상위 코드 (lawd_cd는 비워 둔다)
      - LLM이 말한 시군구 이름이 원문에 없음 ('판교'에서 분당구를 추론한 경우 등)
    """
    if e.value is None or not e.quote:
        return LocationCell(), True
    region = checks.find_region(e.value.sido, e.value.sigungu)
    usable = region is not None and region.selectable
    loc = Location(
        raw=e.quote.strip(),
        sido=region.sido if region else None,
        sigungu=region.sigungu if region else None,
        lawd_cd=region.lawd_cd if usable else None,
    )
    sigungu = (e.value.sigungu or '').split()
    named_in_text = not sigungu or checks.quote_in_text(sigungu[-1], text)
    found_in_text = checks.quote_in_text(e.quote, text)
    status: FieldStatus = (
        'confirmed' if (found_in_text and usable and named_in_text) else 'estimated'
    )
    cell = LocationCell(value=loc, status=status, source_text=_source(e.quote, text))
    return cell, usable


def _label(field: str) -> str:
    return ComparisonRow.model_fields[field].description or field


def make_questions(row: ComparisonRow, region_ok: bool) -> list[str]:
    """계산에 필요한데 비어 있는 칸을 보고 사용자에게 물을 것을 만든다.

    주거·통근처럼 사용자 사정(본가 통근인지 등)에 따라 달라지는 질문은 여기서 하지 않는다.
    그건 사용자 정보를 아는 뒤 단계가 맡는다.
    """
    q: list[str] = []
    st = row.salary_type
    if st.value is None:
        q.append(
            f'제시받은 연봉 (공고 표기: {st.source_text})'
            if st.source_text
            else '제시받은 연봉 (공고에 없음)'
        )
    elif st.value == 'undisclosed':
        q.append('제시받은 연봉 (공고에 금액 없음)')
    elif st.value == 'annual' and row.annual_salary_krw.value is None:
        q.append('연봉 금액 (공고에서 금액을 읽지 못함)')
    elif st.value == 'monthly' and row.monthly_salary_krw.value is None:
        q.append('월급 금액 (공고에서 금액을 읽지 못함)')

    if row.probation_months.value and row.probation_pay_rate.value is None:
        q.append('수습 기간 급여 비율 (공고에 없음)')
    if row.meal_allowance_monthly_krw.value and row.meal_allowance_in_salary.value is None:
        q.append('식대가 연봉에 포함되는지 (공고에 없음)')
    if row.housing_allowance_monthly_krw.value and row.housing_allowance_months.value is None:
        q.append('주거지원비 지원 기간 (공고에 없음)')
    if row.overtime.value is not None:
        q.append(OVERTIME_QUESTION)
    if row.location.status == 'missing':
        q.append(LOCATION_MISSING_QUESTION)  # 월세 추정에 근무지가 필요하다
    elif not region_ok:
        q.append(REGION_QUESTION)

    estimated = [
        _label(k)
        for k in ComparisonRow.model_fields
        if getattr(getattr(row, k), 'status', None) == 'estimated' and k != 'location'
    ]
    estimated += [
        f'기타 수당({a.name})' for a in row.other_allowances if a.monthly_krw.status == 'estimated'
    ]
    if estimated:
        q.append('원문과 맞는지 확인이 필요한 값: ' + ', '.join(estimated))
    return q


def _build_row(p: ExtractedPosting, text: str, row_id: str) -> tuple[ComparisonRow, bool]:
    """행 하나를 만든다 (물어볼 것은 아직 비어 있음). (행, 지역 코드를 정했는지)"""
    location, region_ok = location_cell(p.location, text)
    row = ComparisonRow(
        id=row_id,
        company=text_cell(p.company, text),
        job_title=text_cell(p.job_title, text),
        employment_type=text_cell(p.employment_type, text),
        hiring_track=text_cell(p.hiring_track, text),
        location=location,
        salary_type=salary_type_cell(p.salary_type, text),
        annual_salary_krw=money_cell(p.annual_salary_krw, text, 'year'),
        monthly_salary_krw=money_cell(p.monthly_salary_krw, text, 'month'),
        meal_allowance_monthly_krw=money_cell(p.meal_allowance_monthly_krw, text, 'month'),
        meal_allowance_in_salary=bool_cell(p.meal_allowance_in_salary, text),
        meals_provided=bool_cell(p.meals_provided, text),
        housing_allowance_monthly_krw=money_cell(p.housing_allowance_monthly_krw, text, 'month'),
        housing_allowance_months=months_cell(p.housing_allowance_months, text),
        company_housing=bool_cell(p.company_housing, text),
        shuttle_bus=bool_cell(p.shuttle_bus, text),
        transport_allowance_monthly_krw=money_cell(
            p.transport_allowance_monthly_krw, text, 'month'
        ),
        # 계산에 쓰는 건 금액이 적힌 월 수당뿐이다. 금액 없는 항목은 화면만 어지럽히므로 버린다.
        other_allowances=[
            OtherAllowance(
                name=a.name,
                monthly_krw=money_cell(
                    ExtractedMoney(value=a.monthly_krw, quote=a.quote), text, 'month'
                ),
            )
            for a in p.other_allowances
            if a.monthly_krw is not None
        ],
        probation_months=months_cell(p.probation_months, text),
        probation_pay_rate=rate_cell(p.probation_pay_rate, text),
        overtime=text_cell(p.overtime, text),
    )
    return row, region_ok


def _cells(row: ComparisonRow) -> list[_Cell]:
    """행의 모든 칸 (기타 수당 칸 포함)."""
    cells = [getattr(row, k) for k in ComparisonRow.model_fields]
    return [c for c in cells if isinstance(c, _Cell)] + [
        a.monthly_krw for a in row.other_allowances
    ]


def _money_cells(row: ComparisonRow) -> list[MoneyCell]:
    return [c for c in _cells(row) if isinstance(c, MoneyCell)]


def _flag_shared_quotes(rows: list[ComparisonRow], text: str) -> None:
    """공고를 여러 개 붙여 넣었을 때, 한 공고의 금액을 다른 공고 행이 가져다 쓴 경우를 막는다.

    인용은 원문 '전체'와 대조하므로, A 공고의 '연봉 3,600만원'을 B 행이 써도 확인으로 통과한다.
    같은 금액 인용을 쓰는 행 수가 그 인용이 원문에 나오는 횟수보다 많으면,
    그 셀들을 추정으로 내린다.
    (A의 인용을 B만 쓰는 경우는 못 잡는다. 완전히 막으려면 공고별 구간을 잘라 대조해야 한다.)
    """
    if len(rows) < 2:
        return
    squashed_text = checks.squash(text)
    # 인용 → [(행 번호, 셀)].
    # 한 행 안에서 같은 인용을 두 칸이 쓰는 건('식대·교통비 각 월 10만원') 괜찮다.
    users: dict[str, list[tuple[int, MoneyCell]]] = {}
    for i, row in enumerate(rows):
        for cell in _money_cells(row):
            if cell.status == 'confirmed' and cell.source_text and cell.value:
                users.setdefault(checks.squash(cell.source_text), []).append((i, cell))
    for quote, used in users.items():
        if len({i for i, _ in used}) > squashed_text.count(quote):
            for _, cell in used:
                cell.status = 'estimated'


def _mark_from_image(rows: list[ComparisonRow], plain_text: str) -> None:
    """근거 인용이 '이미지에서 옮겨 적은 글'에만 있는 칸에 from_image 표시를 한다.

    이미지 글은 AI가 옮겨 적은 것이라, 인용이 그 글에 있어도 원본 이미지와 같다는 보장은 없다.
    그래서 status(확인/추정)는 그대로 두되, 화면이 '이미지에서 읽음'을 따로 보여 주게 한다.
    """
    plain = checks.squash(plain_text)  # 칸마다 다시 정리하지 않게 한 번만
    for row in rows:
        quoted = [c for c in _cells(row) if c.source_text]
        image_only = [c for c in quoted if checks.squash(c.source_text) not in plain]
        # 공고(행) 단위로 정한다: 붙여 넣은 다른 공고에 '수습 3개월'처럼 흔한 구절이 있으면
        # 그 칸만 우연히 '이미지 아님'이 된다. 회사명이 이미지에만 있거나 인용 절반 이상이
        # 이미지에만 있으면 이 공고는 이미지에서 읽은 것으로 보고, 인용이 있는 칸을 모두 표시한다.
        company_from_image = any(c is row.company for c in image_only)
        if company_from_image or len(image_only) * 2 >= len(quoted) > 0:
            image_only = quoted
        for cell in image_only:
            cell.from_image = True


def build_rows(
    result: ExtractionResult, text: str, plain_text: str | None = None
) -> list[ComparisonRow]:
    """LLM 답 전체 → 비교표. 행 ID는 posting-1, posting-2 … 순서.

    plain_text: text 가운데 이미지에서 옮겨 적은 글을 뺀 부분 (붙여 넣은 글, PDF·엑셀에서 꺼낸 글).
      주면 인용이 여기에 없는 칸을 from_image로 표시한다. 안 주면 이미지 글이 없다고 본다.
    """
    built = [_build_row(p, text, f'posting-{i}') for i, p in enumerate(result.postings, start=1)]
    rows = [row for row, _ in built]
    _flag_shared_quotes(rows, text)
    if plain_text is not None:
        _mark_from_image(rows, plain_text)
    for row, region_ok in built:
        row.questions_to_ask = make_questions(row, region_ok)
    return rows
