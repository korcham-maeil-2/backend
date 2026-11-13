"""Claude 호출 — 공고 글을 ExtractionResult 모양으로 뽑아 달라고 요청한다.

[주의] 개발 중에는 이 호출을 실제로 실행하지 않는다 (API 키 사용 한도 100달러).
테스트는 가짜 클라이언트와 손으로 쓴 정답 응답(tests/fixtures/normalize/)을 쓴다.
실제 호출은 서비스 실행 중 또는 시연 전 최종 점검 때만 한다.

[구조화 출력]
client.beta.messages.parse(output_format=Pydantic모델)을 쓰면 Claude가 그 JSON 모양으로만 답하고,
SDK가 Pydantic 모델로 검사해서 response.parsed_output에 넣어 준다.

[fallbacks='default']
드물게 안전 분류기가 요청을 거절(stop_reason='refusal')하면, 서버가 다른 모델로 같은 요청을
다시 돌려 준다. 공고 정리에서 거절될 일은 거의 없지만 시연 중 멈추지 않게 켜 둔다.
"""

import logging
from typing import Protocol

import anthropic

from backend.normalize.schema import ExtractionResult

logger = logging.getLogger(__name__)

MODEL = 'claude-sonnet-5-5'
FALLBACK_BETA = 'server-side-fallback-2026-07-01'

SYSTEM_PROMPT = """\
너는 채용 공고·합격 안내·채팅에서 근무 조건을 '옮겨 적는' 일을 한다.
뽑은 값으로 이 사람이 매달 남는 돈을 계산하므로, 틀린 값보다 빈칸이 훨씬 낫다.

규칙:
1. 글에 적힌 것만 옮긴다. 글에 없으면 value와 quote를 모두 null로 둔다.
   업계 평균, 상식, '보통 그렇다'로 채우지 않는다. '동종업계 상위 수준' 같은 말은 금액이 아니다.
2. quote에는 그 값이 적힌 구절만 짧게, 원문에서 글자 그대로 복사한다. 고쳐 쓰거나 요약하지 않는다.
   프로그램이 quote가 원문에 그대로 있는지, quote 속 숫자·단위가 value와 맞는지 다시 검사한다.
3. 금액은 원 단위 정수로 쓴다 (20만원 → 200000, 3,600만원 → 36000000).
   단위 바꾸기 말고는 계산하지 않는다 (월급×12, 세금, 합계 등은 하지 않는다).
4. salary_type (급여는 연봉인지 월급인지 단위를 꼭 구분한다):
   - annual: 연봉 금액 하나가 적혀 있음 → annual_salary_krw. monthly_salary_krw는 null.
     연봉과 월 환산액이 같이 적혀 있어도 annual이고, quote는 연봉 금액 부분만 짧게 적는다.
   - monthly: 월급 금액 하나가 적혀 있음 (예: '월급 250만원', '월 250만원') → monthly_salary_krw.
     annual_salary_krw는 null (월급×12로 바꾸지 않는다)
   - undisclosed: 금액 없이 '회사 내규', '면접 후 결정', '협의'만 있음 (금액 칸은 모두 null)
   - to_propose: 회사가 희망 연봉을 먼저 알려 달라고 함 (금액 칸은 모두 null)
   - 범위('3,000~3,500만원'), 시급, '3,000만원 이상'처럼 금액 하나로 정할 수 없으면
     salary_type과 금액 칸을 모두 null로 두고, salary_type의 quote에만 그 원문을 적는다.
5. '없음', '지원 없음'이라고 적혀 있으면 그것도 정보다: 금액은 0, 여부는 false로 쓰고 quote를 단다.
   '주거 지원: 없음'처럼 한 범주 전체가 없다고 하면
   그 범주의 칸(주거지원비 0, 사택 false)을 모두 채운다.
   아예 언급이 없으면 null이다. '언급 없음'을 false나 0으로 바꾸지 않는다.
6. 식대: 현금으로 주는 식대는 meal_allowance_monthly_krw, 연봉에 포함된다고 적혀 있으면
   meal_allowance_in_salary=true. 회사가 밥을 주면(중식 제공, 구내식당) meals_provided=true.
7. 주거: 주거지원비는 housing_allowance_monthly_krw와 지원 기간(housing_allowance_months, 개월).
   사택·기숙사 제공 여부는 company_housing.
8. location은 근무지. sido·sigungu는 원문 표기대로 쓴다 (예: '전남', '나주시').
   글에 시군구가 없으면 지명으로 추측하지 말고 sigungu를 null로 둔다 (예: '판교'만 있으면 null).
9. employment_type은 정규직·계약직·인턴·파견·프리랜서 중 글에 적힌 것만 고른다.
   '신입', '경력직'은 채용 구분이지 고용형태가 아니다. 해당하는 말이 없으면 null.
   채용 구분은 hiring_track에 쓴다: 신입 / 경력 / 무관(신입·경력 모두 가능). 글에 없으면 null.
10. other_allowances에는 '월 N원'처럼 매달 현금으로 주는 금액이 적힌 수당만 넣는다.
    금액이 없거나, 연 단위('교육비 연 100만원')이거나, 1회성인 것은 넣지 않는다.
11. 사용자 본인의 사정(지금 사는 집 월세, 이사 비용 등)은 공고 조건이 아니므로 칸에 넣지 않는다.
12. 글에 공고가 여러 개면 postings에 공고마다 하나씩 넣고, 각 공고의 quote는 그 공고 부분에서만
    가져온다. 공고가 아니면 postings는 빈 목록.
"""


class NormalizeError(Exception):
    """공고 정리(LLM 호출)가 실패했을 때. message는 사용자에게 보여 줘도 되는 한국어."""


class Extractor(Protocol):
    """공고 글 → ExtractionResult. 실제로는 ClaudeExtractor, 테스트에서는 가짜를 넣는다."""

    def __call__(self, text: str) -> ExtractionResult: ...


class ClaudeExtractor:
    """Claude(Sonnet)로 공고를 구조화한다.

    client를 안 주면 처음 호출할 때 anthropic.Anthropic()을 만든다.
    키는 환경 변수 ANTHROPIC_API_KEY에서 SDK가 직접 읽는다 (코드·로그에 키를 남기지 않는다).
    """

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def __call__(self, text: str) -> ExtractionResult:
        try:
            response = self.client.beta.messages.parse(
                model=MODEL,
                max_tokens=16000,
                betas=[FALLBACK_BETA],
                fallbacks='default',
                # 옮겨 적기라 깊은 추론은 필요 없다. 기본값(high)보다 낮춰 비용·시간을 줄인다.
                output_config={'effort': 'medium'},
                system=SYSTEM_PROMPT,
                messages=[{'role': 'user', 'content': text}],
                output_format=ExtractionResult,
            )
        except anthropic.RateLimitError as e:
            logger.warning('Claude 요청 한도 초과 (status=%s)', e.status_code)
            raise NormalizeError(
                '요청이 많아 공고를 정리하지 못했습니다. 잠시 후 다시 시도해 주세요.'
            ) from e
        except anthropic.APIStatusError as e:
            logger.error('Claude API 오류 (status=%s)', e.status_code)
            raise NormalizeError('공고를 정리하는 중 문제가 생겼습니다.') from e
        except anthropic.APIConnectionError as e:
            logger.error('Claude API 연결 실패')
            raise NormalizeError('공고 정리 서버에 연결하지 못했습니다.') from e
        except anthropic.AnthropicError as e:
            # 키가 없을 때 등. 원인 문장에 키 값이 들어가지 않는다.
            logger.error('Claude 클라이언트 오류: %s', type(e).__name__)
            raise NormalizeError('공고를 정리하는 중 문제가 생겼습니다.') from e

        if response.stop_reason == 'refusal':
            raise NormalizeError('이 내용은 정리할 수 없습니다. 공고 내용만 붙여 넣어 주세요.')
        if response.stop_reason == 'max_tokens':
            raise NormalizeError('글이 너무 길어 정리하지 못했습니다. 공고를 나눠서 보내 주세요.')
        if response.parsed_output is None:
            raise NormalizeError('공고를 정리하는 중 문제가 생겼습니다.')
        return response.parsed_output
