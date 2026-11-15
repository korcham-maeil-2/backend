"""공고를 더할 때(/messages) 공고 정리가 실패해도 세션이 멈추지 않는다.

예전에는 실패하면 세션이 '실행 중'에 멈춰 확인·다시 계산이 모두 409가 되고,
같은 메시지로 다시 보내면 메시지가 두 번 쌓였다.
지금은 같은 메시지로 다시 보내면 실패한 곳부터 이어서 실행한다 (메시지를 다시 쌓지 않는다).

[실행] backend 폴더에서 uv run pytest tests/test_message_retry.py
"""

from conftest import FakeExtractor, scenario_text
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.normalize import NormalizeError

PANGYO = scenario_text('near_pangyo')


class FlakyExtractor(FakeExtractor):
    """fail_next=True면 다음 한 번은 Claude 호출 실패처럼 NormalizeError를 낸다."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    def __call__(self, text: str):
        if self.fail_next:
            self.fail_next = False
            raise NormalizeError('공고를 정리하는 중 문제가 생겼습니다.')
        return super().__call__(text)


def test_message_retry_after_normalize_failure(tmp_path):
    extractor = FlakyExtractor()
    with TestClient(create_app(str(tmp_path / 'cp.sqlite'), extractor=extractor)) as c:
        sid = c.post('/sessions', json={'message': PANGYO}).json()['session_id']
        c.post(f'/sessions/{sid}/confirm', json={'approved': True})

        extractor.fail_next = True
        r = c.post(f'/sessions/{sid}/messages', json={'message': '추가 공고'})
        assert r.status_code == 502

        # 같은 메시지로 다시 보내면 실패한 곳부터 이어서 실행 → 확인을 기다리는 상태가 된다
        r = c.post(f'/sessions/{sid}/messages', json={'message': '추가 공고'})
        assert r.status_code == 200
        s = r.json()
        assert s['status'] == 'awaiting_confirmation'
        assert s['input_text'].count('추가 공고') == 1  # 메시지가 두 번 쌓이지 않는다

        # 이후 흐름도 정상: 확인할 수 있다
        assert c.post(f'/sessions/{sid}/confirm', json={'approved': True}).status_code == 200


def test_failed_message_does_not_block_a_different_message(tmp_path):
    """실패한 뒤 다른 메시지를 보내도 멈춘 세션이 풀리고, 두 메시지 모두 반영된다."""
    extractor = FlakyExtractor()
    with TestClient(create_app(str(tmp_path / 'cp.sqlite'), extractor=extractor)) as c:
        sid = c.post('/sessions', json={'message': PANGYO}).json()['session_id']
        extractor.fail_next = True
        assert c.post(f'/sessions/{sid}/messages', json={'message': '공고 A'}).status_code == 502
        r = c.post(f'/sessions/{sid}/messages', json={'message': '공고 B'})
        assert r.status_code == 200
        s = r.json()
        assert s['status'] == 'awaiting_confirmation'
        assert s['input_text'].count('공고 A') == 1
        assert s['input_text'].count('공고 B') == 1


def test_retrying_an_older_failed_message_does_not_duplicate_it(tmp_path):
    """A 실패 → B 실패 → A를 다시 보내도 A가 두 번 쌓이지 않는다."""
    extractor = FlakyExtractor()
    with TestClient(create_app(str(tmp_path / 'cp.sqlite'), extractor=extractor)) as c:
        sid = c.post('/sessions', json={'message': PANGYO}).json()['session_id']
        for message in ('공고 A', '공고 B'):
            extractor.fail_next = True
            assert c.post(f'/sessions/{sid}/messages', json={'message': message}).status_code == 502
        r = c.post(f'/sessions/{sid}/messages', json={'message': '공고 A'})
        assert r.status_code == 200
        text = r.json()['input_text']
        assert text.count('공고 A') == 1
        assert text.count('공고 B') == 1
