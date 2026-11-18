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


# ── 리뷰 반영 (frontend#29 리뷰): 답은 지금 폼 값을 통째로 /confirm으로 다시 보낸다 ──


def ask_first(c: TestClient, **overrides) -> str:
    """지금 집 주거비를 비우고 확인 → 되묻기에서 멈춘 세션."""
    sid = start(c)
    asked = c.post(
        f'/sessions/{sid}/confirm',
        json={'approved': True, 'profile': pangyo_profile(current_housing=None, **overrides)},
    ).json()
    assert asked['status'] == 'awaiting_answer'
    return sid


def commute_of(done: dict) -> dict:
    return next(s for s in done['result']['postings'][0]['scenarios'] if s['case'] == 'commute')


def test_answer_with_whole_form_uses_values_edited_while_asking(tmp_path):
    """질문이 떠 있는 동안 다른 칸(통근 교통비)을 고치고 답해도, 고친 값으로 계산한다."""
    with api(tmp_path) as c:
        sid = ask_first(c)
        profile = pangyo_profile()  # 주거비를 채운 지금 폼 값
        profile['postings']['posting-1']['commute_transport'] = {'value': 222_000}
        done = c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': profile})
        done = done.json()
        assert done['status'] == 'completed'
        assert commute_of(done)['transport']['value'] == 222_000
        assert done['profile']['postings']['posting-1']['commute_transport']['value'] == 222_000


def test_answer_with_edited_table_keeps_the_edit(tmp_path):
    """질문 중에 비교표를 고쳐 함께 보내면 고친 표로 계산한다 (표가 되돌아가지 않는다)."""
    with api(tmp_path) as c:
        sid = ask_first(c)
        table = c.get(f'/sessions/{sid}').json()['table']
        table[0]['company']['value'] = '고친 회사명'
        table[0]['company']['status'] = 'user_input'
        done = c.post(
            f'/sessions/{sid}/confirm',
            json={'approved': True, 'table': table, 'profile': pangyo_profile()},
        ).json()
        assert done['table'][0]['company']['value'] == '고친 회사명'
        assert done['result']['postings'][0]['company'] == '고친 회사명'


def test_reconfirm_with_empty_table_keeps_table(tmp_path):
    """빈 표([])를 보내면 원래 표를 쓴다 (confirm_table 노드와 같은 처리)."""
    with api(tmp_path) as c:
        sid = ask_first(c)
        done = c.post(
            f'/sessions/{sid}/confirm',
            json={'approved': True, 'table': [], 'profile': pangyo_profile()},
        ).json()
        assert done['status'] == 'completed' and len(done['table']) == 1


def test_skip_keeps_typed_values_and_does_not_ask_again(tmp_path):
    """'모르겠어요, 이대로 계산': 써 둔 다른 값은 그대로 쓰고, 빈 값은 묻지 않고 '계산 못 함'."""
    with api(tmp_path) as c:
        sid = ask_first(c)
        profile = pangyo_profile(current_housing=None)
        profile['postings']['posting-1']['commute_transport'] = {'value': 111_000}
        done = c.post(
            f'/sessions/{sid}/confirm',
            json={'approved': True, 'profile': profile, 'skip_missing': True},
        ).json()
        assert done['status'] == 'completed'
        assert commute_of(done)['monthly_remaining'] is None
        assert commute_of(done)['transport']['value'] == 111_000
        # 다음 확인에서 다시 빈 값이면 다시 묻는다 (건너뛰기는 그 확인 한 번만)
        sid2 = ask_first(c)
        assert c.get(f'/sessions/{sid2}').json()['status'] == 'awaiting_answer'


def test_answers_are_validated(tmp_path):
    """음수·아주 큰 수·true는 422 (예전: -5는 조용히 건너뛰고, 큰 수는 500, true는 1원)."""
    with api(tmp_path) as c:
        sid = ask_first(c)
        for bad in (-5, 10**16, True, '300000', 1.5):
            r = c.post(f'/sessions/{sid}/answers', json={'answers': {'current_housing': bad}})
            assert r.status_code == 422, bad
        assert c.get(f'/sessions/{sid}').json()['status'] == 'awaiting_answer'
