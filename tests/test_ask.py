"""되묻기 (이슈 #13): 계산을 막는 빈 값을 묻고, 답하면 계산이 이어진다.

완료 조건: 거주지(지금 집 주거비)를 빼고 입력하면 에이전트가 물어보고, 답하면 계산이 이어진다.
"""

from conftest import FakeExtractor, scenario_text
from fastapi.testclient import TestClient
from test_demo_scenarios import profile_a

from backend.graph.ask import FIXED_FROM_ANSWER, apply_answers, missing_questions
from backend.main import create_app


def pangyo_profile(**overrides) -> dict:
    """판교 공고 하나의 사용자 정보 (통근·이사 모두 가능)."""
    p = profile_a()
    p['postings'] = {'posting-1': p['postings']['near_pangyo']}
    return {**p, **overrides}


TABLE = [{'id': 'posting-1', 'company': {'value': '(가상) 이음데이터랩(주)'}}]


def test_asks_current_housing_only_when_commute_is_possible():
    [q] = missing_questions(TABLE, pangyo_profile(current_housing=None))
    assert q.key == 'current_housing'
    assert '이음데이터랩' in q.why and '통근' in q.why
    # 통근할 수 없는 공고뿐이면 지금 집 주거비는 계산에 쓰이지 않는다 → 묻지 않는다
    no_commute = pangyo_profile(current_housing=None)
    no_commute['postings']['posting-1']['commute_possible'] = False
    assert missing_questions(TABLE, no_commute) == []


def test_asks_fixed_expenses_only_when_never_answered():
    keys = [q.key for q in missing_questions(TABLE, pangyo_profile(fixed_expenses=None))]
    assert keys == ['fixed_expenses']
    # 빈 목록은 '고정비 없음'으로 답한 것이다
    assert missing_questions(TABLE, pangyo_profile(fixed_expenses=[])) == []


def test_no_profile_or_full_profile_asks_nothing():
    assert missing_questions(TABLE, None) == []
    assert missing_questions(TABLE, pangyo_profile()) == []


def test_apply_answers_as_user_input():
    profile = pangyo_profile(current_housing=None, fixed_expenses=None)
    out = apply_answers(profile, {'current_housing': 300_000, 'fixed_expenses': 120_000})
    assert out['current_housing'] == {
        'value': 300_000,
        'status': 'user_input',
        'source': '되물음에 답함',
    }
    assert out['fixed_expenses'] == [{'name': FIXED_FROM_ANSWER, 'monthly_krw': 120_000}]
    assert apply_answers(profile, {'fixed_expenses': 0})['fixed_expenses'] == []  # 없음
    skipped = apply_answers(profile, {'current_housing': None})
    assert skipped['current_housing'] is None  # 모르면 비운 채로


def api(tmp_path) -> TestClient:
    return TestClient(create_app(str(tmp_path / 'cp.sqlite'), extractor=FakeExtractor()))


def start(c: TestClient) -> str:
    return c.post('/sessions', json={'message': scenario_text('near_pangyo')}).json()['session_id']


def test_missing_housing_is_asked_and_answer_continues(tmp_path):
    """완료 조건: 거주지를 빼고 확인하면 물어보고, 답하면 계산이 이어진다."""
    with api(tmp_path) as c:
        sid = start(c)
        asked = c.post(
            f'/sessions/{sid}/confirm',
            json={'approved': True, 'profile': pangyo_profile(current_housing=None)},
        ).json()
        assert asked['status'] == 'awaiting_answer'
        assert asked['result'] is None  # 계산 전에 멈춘다
        assert asked['pending']['type'] == 'ask_missing'
        [q] = asked['pending']['payload']['questions']
        assert q['key'] == 'current_housing'
        assert c.get(f'/sessions/{sid}').json()['status'] == 'awaiting_answer'

        done = c.post(f'/sessions/{sid}/answers', json={'answers': {'current_housing': 500_000}})
        done = done.json()
        assert done['status'] == 'completed'
        assert done['profile']['current_housing']['value'] == 500_000
        commute = next(
            s for s in done['result']['postings'][0]['scenarios'] if s['case'] == 'commute'
        )
        assert commute['monthly_remaining'] is not None
        assert commute['housing']['status'] == 'user_input'


def test_skipping_keeps_value_missing(tmp_path):
    with api(tmp_path) as c:
        sid = start(c)
        c.post(
            f'/sessions/{sid}/confirm',
            json={'approved': True, 'profile': pangyo_profile(current_housing=None)},
        )
        done = c.post(f'/sessions/{sid}/answers', json={'answers': {'current_housing': None}})
        done = done.json()
        assert done['status'] == 'completed'
        commute = next(
            s for s in done['result']['postings'][0]['scenarios'] if s['case'] == 'commute'
        )
        assert commute['monthly_remaining'] is None  # '계산 못 함'으로 보인다
        assert '주거비 (지금 집)' in commute['missing']


def test_reconfirm_while_waiting_fills_the_gap(tmp_path):
    """묻는 중에 생활비 화면에서 값을 채워 다시 계산하면, 묻지 않고 계산까지 간다."""
    with api(tmp_path) as c:
        sid = start(c)
        c.post(
            f'/sessions/{sid}/confirm',
            json={'approved': True, 'profile': pangyo_profile(current_housing=None)},
        )
        done = c.post(
            f'/sessions/{sid}/confirm', json={'approved': True, 'profile': pangyo_profile()}
        ).json()
        assert done['status'] == 'completed' and done['result'] is not None


def test_answers_only_while_waiting(tmp_path):
    with api(tmp_path) as c:
        sid = start(c)
        r = c.post(f'/sessions/{sid}/answers', json={'answers': {}})
        assert r.status_code == 409
        assert r.json()['code'] == 'SESSION_NOT_AWAITING_ANSWER'
        # 빈 값이 없으면 묻지 않고 바로 계산한다 (기존 흐름 그대로)
        done = c.post(
            f'/sessions/{sid}/confirm', json={'approved': True, 'profile': pangyo_profile()}
        ).json()
        assert done['status'] == 'completed'
