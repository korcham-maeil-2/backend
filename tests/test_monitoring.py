"""오류 추적(GlitchTip)으로 보내는 내용에서 개인정보가 빠지는지 (이슈 #22)."""

import logging

import sentry_sdk
from fastapi.testclient import TestClient

from backend import monitoring
from backend.main import create_app  # 맨 위에서 (conftest가 anthropic.Anthropic 이름을 막기 전에)
from backend.monitoring import init_monitoring, scrub

PHONE = '010-1234-5678'


def test_no_dsn_does_nothing(monkeypatch):
    monkeypatch.delenv('SENTRY_DSN', raising=False)
    assert init_monitoring() is False


def test_scrub_drops_body_user_vars_and_masks_text():
    event = {
        'request': {
            'url': 'https://x/sessions',
            'method': 'POST',
            'data': {'message': f'담당자 {PHONE}'},
            'headers': {'Cookie': 'a=b'},
            'env': {'REMOTE_ADDR': '1.2.3.4'},
        },
        'user': {'ip_address': '1.2.3.4'},
        'exception': {
            'values': [
                {
                    'value': f'연락처 {PHONE} 읽기 실패',
                    'stacktrace': {'frames': [{'function': 'f', 'vars': {'text': PHONE}}]},
                }
            ]
        },
        'logentry': {'message': f'공고 {PHONE}', 'params': [PHONE]},
        'breadcrumbs': {'values': [{'message': f'로그 {PHONE}', 'data': {'x': PHONE}}]},
        'extra': {'text': PHONE},
    }
    out = scrub(event)
    assert out['request'] == {'url': 'https://x/sessions', 'method': 'POST'}
    assert 'user' not in out and 'extra' not in out
    assert PHONE not in repr(out)
    assert out['exception']['values'][0]['stacktrace']['frames'] == [{'function': 'f'}]


def test_unhandled_error_is_sent_without_request_body(monkeypatch, tmp_path):
    """실제 SDK로: 처리되지 않은 오류가 기록되고, 보낸 내용에 요청 본문·지역 변수가 없다."""
    sent: list[dict] = []

    class Capture(sentry_sdk.transport.Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.type == 'event':
                    sent.append(item.payload.json)

    real_init = sentry_sdk.init
    monkeypatch.setattr(sentry_sdk, 'init', lambda **kw: real_init(**kw, transport=Capture))
    monkeypatch.setenv('SENTRY_DSN', 'https://key@glitchtip.example/1')
    try:
        assert init_monitoring() is True
        app = create_app(db_path=str(tmp_path / 'db.sqlite'))

        @app.post('/boom')
        def boom(body: dict) -> None:
            secret = body['text']  # noqa: F841 — 지역 변수가 보내지지 않는지
            raise RuntimeError('터짐')

        with TestClient(app, raise_server_exceptions=False) as client:
            res = client.post('/boom', json={'text': f'담당자 {PHONE}'})
        assert res.status_code == 500
        sentry_sdk.flush()
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.init()  # 다른 테스트에 남지 않게 끈다

    assert sent, '오류가 기록되지 않았어요'
    assert any('터짐' in repr(e) for e in sent)
    assert all(PHONE not in repr(e) for e in sent)
    assert all(set(e.get('request', {})) <= {'url', 'method'} for e in sent)


def test_module_does_not_import_sdk_without_dsn():
    # 로컬·테스트에서 DSN이 없으면 SDK를 켜지 않는다 (logging 처리기도 붙지 않는다)
    assert monitoring.DSN_ENV == 'SENTRY_DSN'
    assert not any(
        type(h).__module__.startswith('sentry_sdk') for h in logging.getLogger().handlers
    )
