"""그래프 뼈대 + SQLite checkpointer + 세션 API 테스트 (#3).

완료 조건: API로 세션을 시작하면 confirm_table에서 멈추고, 확인을 보내면 끝까지 실행된다.

[실행] backend 폴더에서:  uv run pytest

[pytest 기본]
- test_로 시작하는 함수를 자동으로 찾아 실행한다.
- assert 조건 이 거짓이면 그 테스트가 실패로 표시된다.
- 인자 tmp_path: pytest가 테스트마다 만들어 주는 임시 폴더. 끝나면 정리되므로
  매번 깨끗한 SQLite 파일로 시험할 수 있다.

[TestClient]
실제로 서버를 띄우지 않고 FastAPI 앱에 요청을 보내 보는 도구.
`with TestClient(app) as c:` 블록 안에서만 lifespan(그래프·DB 준비)이 실행된다.
c.post(주소, json=본문), c.get(주소) → 응답 r. r.status_code(숫자), r.json()(본문 dict).
"""

from fastapi.testclient import TestClient

from backend.main import create_app


def client(db_path) -> TestClient:
    """주어진 SQLite 파일을 쓰는 새 앱으로 테스트 클라이언트를 만든다."""
    return TestClient(create_app(str(db_path)))


def test_start_stops_at_confirm_table_then_confirm_completes(tmp_path):
    """기본 흐름: 시작하면 비교표 확인에서 멈추고, 확인하면 끝까지 간다."""
    db = tmp_path / 'cp.sqlite'
    with client(db) as c:
        # 1) 세션 시작 → 멈춰 있어야 한다
        r = c.post('/sessions', json={'message': '판교 백엔드 신입 연봉 3,600만원'})
        assert r.status_code == 201
        s = r.json()
        assert s['status'] == 'awaiting_confirmation'
        assert s['pending']['type'] == 'confirm_table'
        assert s['table'] == [{'raw_text': '판교 백엔드 신입 연봉 3,600만원'}]
        assert s['result'] is None  # 아직 계산 전

        # 2) 비교표 확인 → 이어서 끝까지 실행
        r = c.post(f'/sessions/{s["session_id"]}/confirm', json={'approved': True})
        assert r.status_code == 200
        done = r.json()
        assert done['status'] == 'completed'
        assert done['pending'] is None
        assert done['table_confirmed'] is True
        assert done['result'] == {'rows': 1, 'monthly_remaining': None}
        assert done['report']


def test_state_survives_restart_in_sqlite(tmp_path):
    """서버를 다시 띄워도 같은 SQLite 파일에서 멈춘 세션을 이어 간다.

    (체크포인터가 메모리가 아니라 파일에 저장하는지 확인)
    """
    db = tmp_path / 'cp.sqlite'
    # 첫 번째 서버: 세션을 시작만 하고 끈다 (with 블록이 끝나면 앱이 종료된다)
    with client(db) as c:
        sid = c.post('/sessions', json={'message': '나주 공고'}).json()['session_id']

    # 두 번째 서버: 같은 파일로 새로 켜서, 아까 세션을 이어서 확인한다
    with client(db) as c:
        assert c.get(f'/sessions/{sid}').json()['status'] == 'awaiting_confirmation'
        # 사용자가 비교표를 고쳐서 보낸 경우: 고친 표가 그대로 반영돼야 한다
        edited = [{'raw_text': '나주 공고', 'company': '(가상) 빛가람그리드(주)'}]
        done = c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'table': edited}).json()
        assert done['status'] == 'completed'
        assert done['table'] == edited


def test_sessions_are_isolated(tmp_path):
    """세션 A를 끝내도 세션 B는 영향을 받지 않는다 (thread_id별로 따로 저장되는지)."""
    with client(tmp_path / 'cp.sqlite') as c:
        a = c.post('/sessions', json={'message': 'A'}).json()['session_id']
        b = c.post('/sessions', json={'message': 'B'}).json()['session_id']
        c.post(f'/sessions/{a}/confirm', json={'approved': True})
        assert c.get(f'/sessions/{a}').json()['status'] == 'completed'
        assert c.get(f'/sessions/{b}').json()['status'] == 'awaiting_confirmation'


def test_message_appends_and_rebuilds_table(tmp_path):
    """메시지를 더 보내면 이전 메시지와 합쳐져 비교표를 다시 만든다."""
    with client(tmp_path / 'cp.sqlite') as c:
        sid = c.post('/sessions', json={'message': '첫 공고'}).json()['session_id']
        s = c.post(f'/sessions/{sid}/messages', json={'message': '교통비 월 10만원'}).json()
        assert s['status'] == 'awaiting_confirmation'
        assert s['table'] == [{'raw_text': '첫 공고\n교통비 월 10만원'}]


def test_errors(tmp_path):
    """잘못된 요청에 맞는 오류 코드를 돌려주는지."""
    with client(tmp_path / 'cp.sqlite') as c:
        assert c.get('/sessions/없는-세션').status_code == 404  # 없는 세션
        assert c.post('/sessions', json={'message': ''}).status_code == 422  # 빈 메시지
        sid = c.post('/sessions', json={'message': 'x'}).json()['session_id']
        c.post(f'/sessions/{sid}/confirm', json={})
        # 이미 끝난 세션에 다시 확인을 보내면 409
        assert c.post(f'/sessions/{sid}/confirm', json={}).status_code == 409


def test_cors_allows_frontend_dev_origin(tmp_path):
    """프론트 개발 서버(localhost:3000)에서 오는 요청을 브라우저가 막지 않도록 허락하는지.

    브라우저는 실제 요청 전에 OPTIONS로 '보내도 되나요?'를 먼저 묻는다(사전 요청).
    서버가 허락 헤더(access-control-allow-origin)를 돌려주면 통과다.
    """
    with client(tmp_path / 'cp.sqlite') as c:
        r = c.options(
            '/sessions',
            headers={
                'Origin': 'http://localhost:3000',
                'Access-Control-Request-Method': 'POST',
            },
        )
        assert r.headers.get('access-control-allow-origin') == 'http://localhost:3000'
