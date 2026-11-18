"""오류 추적 (GlitchTip, 이슈 #22) — Sentry SDK로 보낸다 (GlitchTip은 Sentry와 같은 형식을 받는다).

SENTRY_DSN이 있을 때만 켠다 (로컬·테스트·CI는 아무것도 보내지 않는다).

무엇이 기록되나:
  - 처리되지 않은 오류 (api/errors.py가 logger.exception으로 남긴다)
  - logger.error / logger.exception (Claude API 오류, 리포트 실패 등). WARNING 이하는 보내지 않고
    오류 직전 흐름(breadcrumb)으로만 붙는다.

개인정보 (CLAUDE.md: 개인정보는 외부로 보내지 않는다, 세션이 끝나면 남기지 않는다):
  - 요청 본문(공고 원문·생활비·파일)은 받지도 보내지도 않는다 (max_request_body_size='never')
  - 오류 난 함수의 지역 변수(공고 글이 들어 있을 수 있다)를 보내지 않는다
  - IP·쿠키·헤더를 보내지 않는다. 오류 글·로그 글은 privacy.mask_text로 한 번 더 가린다
"""

import os
from typing import Any

from backend.privacy import mask_text

DSN_ENV = 'SENTRY_DSN'


def _mask(value: Any) -> Any:
    return mask_text(value) if isinstance(value, str) else value


def scrub(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """보내기 직전 마지막 거름망 (before_send·before_send_transaction).

    SDK 설정으로 이미 빠지는 것도 여기서 한 번 더 지운다 (설정이 바뀌어도 새지 않게).
    """
    request = event.get('request')
    if isinstance(request, dict):
        # 주소·메서드만 남긴다 (어느 API에서 났는지). 쿼리는 지역 코드·직무 코드뿐이지만 같이 뺀다
        event['request'] = {k: request[k] for k in ('url', 'method') if k in request}
    event.pop('user', None)
    for exc in (event.get('exception') or {}).get('values') or []:
        exc['value'] = _mask(exc.get('value'))
        for frame in (exc.get('stacktrace') or {}).get('frames') or []:
            frame.pop('vars', None)
    logentry = event.get('logentry')
    if isinstance(logentry, dict):
        logentry['message'] = _mask(logentry.get('message'))
        logentry['formatted'] = _mask(logentry.get('formatted'))
        logentry.pop('params', None)
    event['message'] = _mask(event.get('message'))
    for crumb in (event.get('breadcrumbs') or {}).get('values') or []:
        crumb['message'] = _mask(crumb.get('message'))
        crumb.pop('data', None)
    event.pop('extra', None)
    return event


def init_monitoring() -> bool:
    """SENTRY_DSN이 있으면 오류 추적을 켠다. 켰으면 True."""
    dsn = os.getenv(DSN_ENV, '').strip()
    if not dsn:
        return False
    # DSN이 없으면 불러오지도 않는다
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv('SENTRY_ENVIRONMENT', 'production'),
        release=os.getenv('SENTRY_RELEASE') or None,
        # 설치된 라이브러리를 보고 저절로 붙는 연동(Anthropic·LangChain 등)은 끈다.
        # AI 호출 기록은 프롬프트(공고 글)를 담을 수 있다. 기본 연동(logging 등)과 FastAPI만 쓴다
        auto_enabling_integrations=False,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        data_collection={
            'user_info': False,  # IP·사용자 정보
            'http_bodies': [],  # 요청 본문 (공고 원문·생활비·파일)
            'stack_frame_variables': False,  # 오류 난 함수의 지역 변수
        },
        # 성능 추적은 쓰지 않는다 (오류만). 라즈베리파이·GlitchTip 부담을 줄인다
        traces_sample_rate=0.0,
        before_send=scrub,
        before_send_transaction=scrub,
    )
    return True
