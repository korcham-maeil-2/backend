"""그래프 뼈대 + SQLite checkpointer + 세션 API (#3).

완료 조건: API로 세션을 시작하면 confirm_table에서 멈추고, 확인을 보내면 끝까지 실행된다.
"""

from fastapi.testclient import TestClient

from backend.main import create_app


def client(db_path) -> TestClient:
    return TestClient(create_app(str(db_path)))


def test_start_stops_at_confirm_table_then_confirm_completes(tmp_path):
    db = tmp_path / 'cp.sqlite'
    with client(db) as c:
        r = c.post('/sessions', json={'message': '판교 백엔드 신입 연봉 3,600만원'})
        assert r.status_code == 201
        s = r.json()
        assert s['status'] == 'awaiting_confirmation'
        assert s['pending']['type'] == 'confirm_table'
        assert s['table'] == [{'raw_text': '판교 백엔드 신입 연봉 3,600만원'}]
        assert s['result'] is None

        r = c.post(f'/sessions/{s["session_id"]}/confirm', json={'approved': True})
        assert r.status_code == 200
        done = r.json()
        assert done['status'] == 'completed'
        assert done['pending'] is None
        assert done['table_confirmed'] is True
        assert done['result'] == {'rows': 1, 'monthly_remaining': None}
        assert done['report']


def test_state_survives_restart_in_sqlite(tmp_path):
    """서버를 다시 띄워도 같은 SQLite 파일에서 멈춘 세션을 이어 간다."""
    db = tmp_path / 'cp.sqlite'
    with client(db) as c:
        sid = c.post('/sessions', json={'message': '나주 공고'}).json()['session_id']

    with client(db) as c:
        assert c.get(f'/sessions/{sid}').json()['status'] == 'awaiting_confirmation'
        edited = [{'raw_text': '나주 공고', 'company': '(가상) 빛가람그리드(주)'}]
        done = c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'table': edited}).json()
        assert done['status'] == 'completed'
        assert done['table'] == edited


def test_sessions_are_isolated(tmp_path):
    with client(tmp_path / 'cp.sqlite') as c:
        a = c.post('/sessions', json={'message': 'A'}).json()['session_id']
        b = c.post('/sessions', json={'message': 'B'}).json()['session_id']
        c.post(f'/sessions/{a}/confirm', json={'approved': True})
        assert c.get(f'/sessions/{a}').json()['status'] == 'completed'
        assert c.get(f'/sessions/{b}').json()['status'] == 'awaiting_confirmation'


def test_message_appends_and_rebuilds_table(tmp_path):
    with client(tmp_path / 'cp.sqlite') as c:
        sid = c.post('/sessions', json={'message': '첫 공고'}).json()['session_id']
        s = c.post(f'/sessions/{sid}/messages', json={'message': '교통비 월 10만원'}).json()
        assert s['status'] == 'awaiting_confirmation'
        assert s['table'] == [{'raw_text': '첫 공고\n교통비 월 10만원'}]


def test_errors(tmp_path):
    with client(tmp_path / 'cp.sqlite') as c:
        assert c.get('/sessions/없는-세션').status_code == 404
        assert c.post('/sessions', json={'message': ''}).status_code == 422
        sid = c.post('/sessions', json={'message': 'x'}).json()['session_id']
        c.post(f'/sessions/{sid}/confirm', json={})
        # 이미 끝난 세션에 다시 확인을 보내면 409
        assert c.post(f'/sessions/{sid}/confirm', json={}).status_code == 409


def test_cors_allows_frontend_dev_origin(tmp_path):
    with client(tmp_path / 'cp.sqlite') as c:
        r = c.options(
            '/sessions',
            headers={
                'Origin': 'http://localhost:3000',
                'Access-Control-Request-Method': 'POST',
            },
        )
        assert r.headers.get('access-control-allow-origin') == 'http://localhost:3000'
