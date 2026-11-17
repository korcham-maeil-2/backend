"""되묻기 (이슈 #13) — 계산에 꼭 필요한 값이 비어 있으면 계산 전에 사용자에게 다시 묻는다.

check_missing 노드(graph/nodes.py)가 이 질문으로 그래프를 멈추고(interrupt),
사용자가 답하면(POST /sessions/{id}/answers) 답을 사용자 정보에 넣고 계산을 이어 간다.

묻는 값 (없으면 매달 남는 돈을 아예 못 구하는 것만):
  current_housing  지금 집 주거비 — '지금 집에서 통근'이 해당되는 공고가 있을 때 (거주지 정보)
  fixed_expenses   고정비 합계     — 아직 한 번도 답하지 않았을 때
                   (null. 빈 목록 []은 '없음'으로 답한 것)
묻지 않는 값:
  - 교통비: 비우면 0원(추정)으로 계산하고 '더 들 수 있어요'라고 알린다 (calc/remaining.py)
  - 이사할 집 주거비: 근무지 월세 시세(국토부 실거래가)로 채운다 (get_rent)
  - 연봉: 비공개면 '최소 연봉'을 계산한다 (calc/break_even.py)

답은 '사용자 입력'(user_input)으로 넣는다. 건너뛴 값은 그대로 비워 두어 '계산 못 함'으로 보인다.
질문 문장은 코드가 정해진 틀로 만든다 (Claude를 부르지 않는다: 시연 중 비용·지연 없음).
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.calc.remaining import UserProfile

ASK_MISSING = 'ask_missing'
FIXED_FROM_ANSWER = '고정비 (되물음에 답한 합계)'

AskKey = Literal['current_housing', 'fixed_expenses']


class Question(BaseModel):
    key: AskKey
    question: str
    why: str = Field(description='이 값이 왜 필요한지 (어느 계산이 막히는지)')
    hint: str = Field(description='입력 안내 (예: 없으면 0)')
    example: int = Field(description='입력칸 예시 금액 (원)')


class Answers(BaseModel):
    """POST /sessions/{id}/answers 요청. 모르는 값은 null로 보내면 비워 둔 채 계산한다."""

    answers: dict[AskKey, int | None] = Field(
        default_factory=dict,
        description="질문 key → 월 금액(원). 모르면 null (예: {'current_housing': 300000})",
    )


def _commute_companies(table: list[dict], profile: UserProfile) -> list[str]:
    """'지금 집에서 통근'이 해당되는 공고의 회사 이름 (통근할 수 없다고 한 공고는 뺀다)."""
    names = []
    for i, row in enumerate(table, start=1):
        if profile.costs_for(row.get('id', '')).commute_possible is False:
            continue
        names.append(((row.get('company') or {}).get('value') or '').strip() or f'공고 {i}')
    return names


def missing_questions(table: list[dict] | None, profile: dict | None) -> list[Question]:
    """비어 있어 계산을 막는 값마다 질문 하나. 사용자 정보가 아직 없으면 묻지 않는다."""
    if not profile or not table:
        return []
    user = UserProfile.model_validate(profile)
    questions = []
    companies = _commute_companies(table, user)
    if user.current_housing is None and companies:
        questions.append(
            Question(
                key='current_housing',
                question='지금 사는 집에 매달 주거비로 얼마를 내고 있나요?',
                why=(
                    f"{', '.join(companies)}의 '지금 집에서 통근' 경우에 매달 남는 돈을 "
                    '계산하려면 필요해요.'
                ),
                hint=(
                    '월세 + 관리비예요. 본가라면 생활비로 드리는 돈, '
                    '내는 돈이 없으면 0을 넣어 주세요.'
                ),
                example=300_000,
            )
        )
    if user.fixed_expenses is None:
        questions.append(
            Question(
                key='fixed_expenses',
                question='통신비·학자금 상환·보험료처럼 매달 꼭 나가는 돈은 모두 합쳐 얼마인가요?',
                why='어느 회사든 매달 남는 돈을 계산하려면 필요해요.',
                hint='없으면 0을 넣어 주세요.',
                example=150_000,
            )
        )
    return questions


def apply_answers(profile: dict, answers: dict[str, Any]) -> dict:
    """답을 사용자 정보에 넣는다 (사용자 입력). null·모르는 key는 그대로 둔다."""
    out = dict(profile)
    housing = answers.get('current_housing')
    if isinstance(housing, int) and housing >= 0:
        out['current_housing'] = {
            'value': housing,
            'status': 'user_input',
            'source': '되물음에 답함',
        }
    fixed = answers.get('fixed_expenses')
    if isinstance(fixed, int) and fixed >= 0:
        # 0이면 '고정비 없음'으로 답한 것 ([]) — 계산은 0원, 출처는 '고정비 없음'
        out['fixed_expenses'] = [{'name': FIXED_FROM_ANSWER, 'monthly_krw': fixed}] if fixed else []
    return UserProfile.model_validate(out).model_dump(mode='json')
