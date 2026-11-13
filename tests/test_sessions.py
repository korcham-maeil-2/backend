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

from conftest import FakeExtractor, scenario_text
from fastapi.testclient import TestClient

from backend.main import create_app

PANGYO = scenario_text('near_pangyo')
NAJU = scenario_text('down_naju')


def client(db_path, extractor=None) -> TestClient:
    """주어진 SQLite 파일을 쓰는 새 앱으로 테스트 클라이언트를 만든다.

    공고 정리(LLM)는 손으로 쓴 응답을 돌려주는 가짜를 쓴다 (실제 Claude API 호출 금지).
    """
    return TestClient(create_app(str(db_path), extractor=extractor or FakeExtractor()))


def test_start_stops_at_confirm_table_then_confirm_completes(tmp_path):
    """기본 흐름: 시작하면 비교표 확인에서 멈추고, 확인하면 끝까지 간다."""
    db = tmp_path / 'cp.sqlite'
    with client(db) as c:
        # 1) 세션 시작 → 멈춰 있어야 한다
        r = c.post('/sessions', json={'message': PANGYO})
        assert r.status_code == 201
        s = r.json()
        assert s['status'] == 'awaiting_confirmation'
        assert s['pending']['type'] == 'confirm_table'
        assert len(s['table']) == 1
        assert s['table'][0]['company']['value'] == '(가상) 이음데이터랩(주)'
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
        sid = c.post('/sessions', json={'message': NAJU}).json()['session_id']

    # 두 번째 서버: 같은 파일로 새로 켜서, 아까 세션을 이어서 확인한다
    with client(db) as c:
        assert c.get(f'/sessions/{sid}').json()['status'] == 'awaiting_confirmation'
        # 사용자가 비교표를 고쳐서 보낸 경우: 고친 표가 그대로 반영돼야 한다
        # 연봉 칸을 사용자가 직접 입력했다고 치고 status를 user_input으로 바꿔 보낸다.
        edited = c.get(f'/sessions/{sid}').json()['table']
        edited[0]['annual_salary_krw'] = {
            'value': 33000000,
            'status': 'user_input',
            'source_text': None,
            'from_image': False,
        }
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
    fake = FakeExtractor()
    with client(tmp_path / 'cp.sqlite', fake) as c:
        sid = c.post('/sessions', json={'message': '첫 공고'}).json()['session_id']
        s = c.post(f'/sessions/{sid}/messages', json={'message': '교통비 월 10만원'}).json()
        assert s['status'] == 'awaiting_confirmation'
        # 두 번째 실행에서 LLM은 두 메시지를 합친 글을 받는다
        assert fake.calls == ['첫 공고', '첫 공고\n교통비 월 10만원']
        # 공고가 아닌 글이면 비교표는 비어 있다 (지어내지 않는다)
        assert s['table'] == []


def test_errors_use_common_shape(tmp_path):
    """모든 실패 응답이 {code, message, details} 한 가지 모양인지 (api/errors.py의 약속)."""
    with client(tmp_path / 'cp.sqlite') as c:
        # 없는 세션 → 404
        r = c.get('/sessions/없는-세션')
        assert r.status_code == 404
        assert r.json() == {
            'code': 'SESSION_NOT_FOUND',
            'message': '세션을 찾을 수 없습니다.',
            'details': None,
        }

        # 빈 메시지 → 422, details에 어느 칸이 틀렸는지
        r = c.post('/sessions', json={'message': ''})
        assert r.status_code == 422
        body = r.json()
        assert body['code'] == 'VALIDATION_ERROR'
        assert body['message'] == '요청 형식이 올바르지 않습니다.'
        assert body['details'][0]['field'] == 'body.message'

        # 이미 끝난 세션에 다시 확인을 보내면 → 409
        sid = c.post('/sessions', json={'message': 'x'}).json()['session_id']
        c.post(f'/sessions/{sid}/confirm', json={})
        r = c.post(f'/sessions/{sid}/confirm', json={})
        assert r.status_code == 409
        assert r.json()['code'] == 'SESSION_NOT_AWAITING_CONFIRMATION'

        # 없는 주소 → 404, 틀린 메서드 → 405 (FastAPI 기본 에러도 같은 모양으로 바뀌는지)
        assert c.get('/없는-주소').json()['code'] == 'NOT_FOUND'
        r = c.delete('/sessions')
        assert r.status_code == 405
        assert r.json()['code'] == 'METHOD_NOT_ALLOWED'


def test_unexpected_error_hides_internals(tmp_path):
    """예상 못 한 버그(500)도 같은 모양이고, 내부 오류 내용은 응답에 드러내지 않는다."""
    app = create_app(str(tmp_path / 'cp.sqlite'))

    @app.get('/boom')
    def boom():
        raise RuntimeError('비밀 내부 정보')

    # raise_server_exceptions=False: 테스트에서도 실제 서버처럼 500 응답을 받아 보기 위한 설정
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get('/boom', headers={'Origin': 'http://localhost:3000'})
        assert r.status_code == 500
        assert r.json()['code'] == 'INTERNAL_ERROR'
        assert '비밀' not in r.text
        # 500에도 CORS 허락 헤더가 있어야 브라우저(프론트)가 이 에러 본문을 읽을 수 있다
        assert r.headers.get('access-control-allow-origin') == 'http://localhost:3000'


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
