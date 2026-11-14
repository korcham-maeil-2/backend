"""Claude 호출 공통 부분 — 모델·fallback 설정, 실패를 사용자 문구로 바꾸기.

공고 정리(normalize/llm.py)와 이미지 읽기(inputs/transcribe.py)가 함께 쓴다.
리포트(#19)처럼 Claude를 새로 부르는 곳도 create_message를 쓰면
같은 모델·같은 실패 처리(429·연결 실패·키 없음·거절·잘림)를 그대로 얻는다.

[주의] 개발 중에는 실제 호출을 하지 않는다 (API 키 사용 한도 100달러).
테스트는 client 자리에 가짜를 넣는다.

[fallbacks='default']
드물게 안전 분류기가 요청을 거절(stop_reason='refusal')하면, 서버가 다른 모델로 같은 요청을
다시 돌려 준다. 시연 중 멈추지 않게 켜 둔다.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

MODEL = 'claude-sonnet-5-5'
FALLBACK_BETA = 'server-side-fallback-2026-07-01'

# 서버에 API 키(인증 수단)가 하나도 없을 때 SDK가 요청 직전에 내는 TypeError의 문장.
# AnthropicError가 아니라서 따로 잡아야 한다 (안 잡으면 500 INTERNAL_ERROR가 된다).
# SDK를 올리다 문장이 바뀌면 키 없는 실제 SDK로 재현하는 테스트가 알려 준다.
_MISSING_AUTH_MESSAGE = 'Could not resolve authentication method'


def is_missing_api_key(error: BaseException) -> bool:
    """키가 없어서 난 SDK 오류인지.

    다른 TypeError(우리 코드 버그)는 False → 숨기지 않고 그대로 500으로 드러낸다.
    """
    return isinstance(error, TypeError) and _MISSING_AUTH_MESSAGE in str(error)


@dataclass(frozen=True)
class FailureMessages:
    """실패할 때 사용자에게 보여 줄 한국어 문구. 부르는 곳마다 상황에 맞게 정한다.

    task: 로그에 붙일 작업 이름 (예: '공고 정리'). 어느 호출이 실패했는지 로그로 구분한다.
    """

    task: str
    busy: str  # 요청 한도 초과(429)
    failed: str  # API 오류·키 없음·그 밖의 SDK 오류
    unreachable: str  # 연결 실패
    refusal: str  # 안전 분류기가 거절
    too_long: str  # 답이 max_tokens에서 잘림


def create_message(
    get_client: Callable[[], Any],
    error: type[Exception],
    failure: FailureMessages,
    /,
    **request: Any,
):
    """Claude에 요청 하나를 보내고, 실패하면 error(문구)로 바꿔 올린다.

    get_client: 클라이언트를 돌려주는 함수 (예: lambda: self.client). 클라이언트를 처음 만들 때
      나는 오류(인증 설정 파일이 깨짐 등)도 같은 처리를 받도록 try 안에서 부른다.
    model·betas·fallbacks는 여기서 채우므로 넘기지 않는다 (넘기면 TypeError → 500).
    나머지(max_tokens, system, messages, output_config 등)는 그대로 넘긴다.
    스트리밍은 지원하지 않는다.
    error·failure: 실패하면 올릴 예외(NormalizeError·ReadError 등, API가 502로 바꿈)와 그 문구.
    로그에는 상태 코드·예외 이름만 남긴다 (키 값이나 공고 내용은 남기지 않는다).
    """
    try:
        response = get_client().beta.messages.create(
            model=MODEL,
            betas=[FALLBACK_BETA],
            fallbacks='default',
            **request,
        )
    except anthropic.RateLimitError as e:
        logger.warning('[%s] Claude 요청 한도 초과 (status=%s)', failure.task, e.status_code)
        raise error(failure.busy) from e
    except anthropic.APIStatusError as e:
        logger.error('[%s] Claude API 오류 (status=%s)', failure.task, e.status_code)
        raise error(failure.failed) from e
    except anthropic.APIConnectionError as e:
        logger.error('[%s] Claude API 연결 실패', failure.task)
        raise error(failure.unreachable) from e
    except anthropic.AnthropicError as e:
        logger.error('[%s] Claude 클라이언트 오류: %s', failure.task, type(e).__name__)
        raise error(failure.failed) from e
    except TypeError as e:
        if not is_missing_api_key(e):
            raise
        logger.error(
            '[%s] Claude API 키가 없습니다: 서버 환경 변수 ANTHROPIC_API_KEY를 확인하세요',
            failure.task,
        )
        raise error(failure.failed) from e

    if response.stop_reason == 'refusal':
        raise error(failure.refusal)
    if response.stop_reason == 'max_tokens':
        raise error(failure.too_long)
    return response


def answer_text(response) -> str:
    """답에서 글 부분만 이어 붙인다 (생각 블록 등은 뺀다)."""
    return ''.join(b.text for b in response.content if getattr(b, 'type', None) == 'text')
