"""Claude 호출 공통 부분(backend/claude.py) 테스트 — 실패마다 정한 문구로 바뀌는지.

공고 정리·이미지 읽기·앞으로 리포트(#19)가 모두 이 처리를 쓴다. 실제 API는 부르지 않는다.
"""

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from backend.claude import FALLBACK_BETA, MODEL, FailureMessages, answer_text, create_message

MESSAGES = FailureMessages(
    task='시험',
    busy='바쁨',
    failed='실패',
    unreachable='연결 안 됨',
    refusal='거절',
    too_long='너무 김',
)
REQUEST = httpx.Request('POST', 'https://example.invalid')


class Failed(Exception):
    """부르는 쪽이 정한 예외 (NormalizeError·ReadError 자리)."""


class FakeCreate:
    def __init__(self, response=None, error: BaseException | None = None):
        self.response, self.error = response, error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def client(fake: FakeCreate):
    return SimpleNamespace(beta=SimpleNamespace(messages=fake))


def reply(stop_reason='end_turn'):
    return SimpleNamespace(
        stop_reason=stop_reason, content=[SimpleNamespace(type='text', text='답')]
    )


def status_error(cls, code):
    return cls('오류', response=httpx.Response(code, request=REQUEST), body=None)


def test_request_gets_model_and_fallback():
    fake = FakeCreate(reply())
    response = create_message(lambda: client(fake), Failed, MESSAGES, max_tokens=10, messages=[])
    assert response.stop_reason == 'end_turn'
    [kw] = fake.calls
    assert kw['model'] == MODEL
    assert kw['betas'] == [FALLBACK_BETA]
    assert kw['fallbacks'] == 'default'
    assert kw['max_tokens'] == 10
    assert kw['messages'] == []  # 나머지 요청 값은 그대로 넘어간다


@pytest.mark.parametrize(
    ('error', 'message'),
    [
        (status_error(anthropic.RateLimitError, 429), '바쁨'),
        (status_error(anthropic.InternalServerError, 500), '실패'),
        (anthropic.APIConnectionError(request=REQUEST), '연결 안 됨'),
        (anthropic.AnthropicError('그 밖의 SDK 오류'), '실패'),
        (TypeError('"Could not resolve authentication method. Expected one of api_key"'), '실패'),
    ],
)
def test_sdk_errors_become_given_error(error, message):
    with pytest.raises(Failed, match=f'^{message}$'):
        create_message(lambda: client(FakeCreate(error=error)), Failed, MESSAGES)


@pytest.mark.parametrize(('stop', 'message'), [('refusal', '거절'), ('max_tokens', '너무 김')])
def test_bad_stop_reason_becomes_given_error(stop, message):
    with pytest.raises(Failed, match=f'^{message}$'):
        create_message(lambda: client(FakeCreate(reply(stop))), Failed, MESSAGES)


def test_other_type_error_is_not_hidden():
    """키 문제가 아닌 TypeError(우리 코드 버그)는 바꾸지 않고 그대로 올린다 (→ 500)."""
    with pytest.raises(TypeError, match='unexpected keyword'):
        create_message(
            lambda: client(FakeCreate(error=TypeError('unexpected keyword'))), Failed, MESSAGES
        )


def test_client_creation_error_is_handled_too():
    """클라이언트를 처음 만들다 난 SDK 오류(인증 설정 파일이 깨짐 등)도 같은 처리를 받는다."""

    def broken():
        raise anthropic.AnthropicError('설정 파일을 읽지 못함')

    with pytest.raises(Failed, match='^실패$'):
        create_message(broken, Failed, MESSAGES)


def test_answer_text_skips_non_text_blocks():
    """생각 블록 등은 빼고 글 블록만 이어 붙인다."""
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type='thinking', thinking='생각'),
            SimpleNamespace(type='text', text='앞'),
            SimpleNamespace(type='text', text='뒤'),
        ]
    )
    assert answer_text(response) == '앞뒤'


def test_log_names_the_task(caplog):
    """로그만 봐도 어느 작업(공고 정리·이미지 읽기)이 실패했는지 안다."""
    with pytest.raises(Failed):
        create_message(
            lambda: client(FakeCreate(error=anthropic.APIConnectionError(request=REQUEST))),
            Failed,
            MESSAGES,
        )
    assert '[시험]' in caplog.text
