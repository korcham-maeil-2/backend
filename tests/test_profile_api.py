"""사용자 정보 API 테스트 — 비교표 확인 때 사용자 정보를 보내면 월 잔여액이 나온다.

    POST /sessions/{id}/confirm  {"approved": true, "profile": {...}}  → 계산 결과
    POST /sessions/{id}/recalculate  {profile, table?}                   → 계산만 다시

[실행] backend 폴더에서 uv run pytest tests/test_profile_api.py
"""

from conftest import FakeExtractor, scenario_text
from fastapi.testclient import TestClient
from test_demo_scenarios import profile_a

from backend.main import create_app

PANGYO = scenario_text('near_pangyo')
NAJU = scenario_text('down_naju')


def client(db_path, extractor=None) -> TestClient:
    return TestClient(create_app(str(db_path), extractor=extractor or FakeExtractor()))


def pangyo_profile() -> dict:
    """사용자 A의 판교 비용을 그래프 안 공고 ID(posting-1)로 옮긴 것."""
    p = profile_a()
    p['postings'] = {'posting-1': p['postings']['near_pangyo']}
    return p


def remaining(session: dict) -> dict:
    [posting] = session['result']['postings']
    return {s['case']: s['monthly_remaining'] for s in posting['scenarios']}


def start(c: TestClient, text: str = PANGYO) -> str:
    r = c.post('/sessions', json={'message': text})
    assert r.status_code == 201
    return r.json()['session_id']


def test_confirm_with_profile_gives_remaining(tmp_path):
    with client(tmp_path / 'cp.sqlite') as c:
        sid = start(c)
        r = c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': pangyo_profile()})
        assert r.status_code == 200
        done = r.json()
        assert done['status'] == 'completed'
        assert done['result']['has_user_profile'] is True
        values = remaining(done)
        assert values['commute'] is not None and values['move'] is not None

        # 다시 조회해도 같은 결과 (SQLite에 저장됨)
        assert c.get(f'/sessions/{sid}').json()['result'] == done['result']


def test_confirm_without_profile_still_works(tmp_path):
    """사용자 정보 없이 확인해도 된다 (세후 월급까지만, 잔여액은 null)."""
    with client(tmp_path / 'cp.sqlite') as c:
        sid = start(c)
        done = c.post(f'/sessions/{sid}/confirm', json={'approved': True}).json()
        assert done['result']['has_user_profile'] is False
        assert remaining(done) == {'commute': None, 'move': None}


def test_invalid_profile_is_rejected_with_422(tmp_path):
    """음수 금액·'값 없음' 상태의 금액처럼 잘못된 사용자 정보는 그래프를 돌리기 전에 422."""
    with client(tmp_path / 'cp.sqlite') as c:
        sid = start(c)
        for profile in (
            {'current_housing': {'value': -1}},  # 음수 금액
            {'current_housing': {'value': 0, 'status': 'missing'}},  # '값 없음'인데 0원
            {'current_housng': {'value': 500_000}},  # 칸 이름 오타 → 조용히 버리지 않는다
        ):
            r = c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': profile})
            assert r.status_code == 422, profile
            assert r.json()['code'] == 'VALIDATION_ERROR'
        # 거절됐으니 세션은 여전히 확인을 기다린다
        assert c.get(f'/sessions/{sid}').json()['status'] == 'awaiting_confirmation'


def test_recalculate_recalculates_without_rebuilding_table(tmp_path):
    extractor = FakeExtractor()
    with client(tmp_path / 'cp.sqlite', extractor) as c:
        sid = start(c)
        first = c.post(
            f'/sessions/{sid}/confirm', json={'approved': True, 'profile': pangyo_profile()}
        ).json()
        calls = len(extractor.calls)

        # 지금 집 월세를 10만 원 올리면 통근 잔여액만 10만 원 준다
        changed = pangyo_profile()
        changed['current_housing']['value'] += 100_000
        r = c.post(f'/sessions/{sid}/recalculate', json={'profile': changed})
        assert r.status_code == 200
        after = r.json()
        assert after['status'] == 'completed'
        assert remaining(after)['commute'] == remaining(first)['commute'] - 100_000
        assert remaining(after)['move'] == remaining(first)['move']
        assert after['table'] == first['table']  # 비교표는 그대로
        assert len(extractor.calls) == calls  # 공고 정리(LLM)를 다시 부르지 않음
        assert after['report']
        assert after['profile']['current_housing']['value'] == changed['current_housing']['value']

        # 또 바꿔도 된다
        r = c.post(f'/sessions/{sid}/recalculate', json={'profile': pangyo_profile()})
        assert remaining(r.json()) == remaining(first)


def test_recalculate_before_confirm_is_409(tmp_path):
    with client(tmp_path / 'cp.sqlite') as c:
        sid = start(c)
        r = c.post(f'/sessions/{sid}/recalculate', json={'profile': pangyo_profile()})
        assert r.status_code == 409
        assert r.json()['code'] == 'SESSION_NOT_COMPLETED'
        # 409 뒤에도 정상적으로 확인할 수 있다
        assert c.post(f'/sessions/{sid}/confirm', json={'approved': True}).status_code == 200


def test_recalculate_unknown_session_is_404(tmp_path):
    with client(tmp_path / 'cp.sqlite') as c:
        r = c.post('/sessions/nope/recalculate', json={'profile': pangyo_profile()})
        assert r.status_code == 404


def test_break_even_through_api(tmp_path):
    """연봉 비공개 공고(나주)를 사용자 정보와 함께 확인하면 손익분기 연봉 기준 안내가 온다.

    공고가 나주 하나뿐이고 사용자 A는 지금 연봉이 없어서, 목표 금액을 기준으로 준다.
    """
    p = profile_a()
    p['postings'] = {'posting-1': p['postings']['down_naju']}
    p['target_monthly_remaining'] = 1_500_000
    with client(tmp_path / 'cp.sqlite') as c:
        sid = start(c, NAJU)
        done = c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': p}).json()
        [be] = done['result']['break_even']
        assert be['baseline']['kind'] == 'target'
        move = be['cases'][1]
        assert move['annual_salary_krw'] is not None
        assert move['during_support_annual_salary_krw'] < move['annual_salary_krw']


def test_openapi_has_typed_result_and_profile(tmp_path):
    """프론트의 pnpm gen:api가 결과·사용자 정보 타입을 만들 수 있게 스키마에 실린다."""
    with client(tmp_path / 'cp.sqlite') as c:
        schemas = c.get('/openapi.json').json()['components']['schemas']
    for name in ('CalculationResult', 'PostingResult', 'Scenario', 'BreakEven', 'UserProfile'):
        assert name in schemas, name
    assert 'profile' in schemas['ConfirmRequest']['properties']
    assert 'profile' in schemas['SessionResponse']['properties']
    # 응답에 항상 들어가는 칸은 TS 타입에서도 필수(?: 아님)여야 프론트가 빈 값 검사를 덜 한다
    assert {'break_even', 'break_even_note', 'errors'} <= set(
        schemas['CalculationResult']['required']
    )
    assert {'applicable', 'missing'} <= set(schemas['Scenario']['required'])


def test_old_result_shape_does_not_break_get(tmp_path):
    """계산 결과 모양이 바뀌기 전에 끝난 세션(예전 모양)을 조회해도 500이 나지 않는다."""
    app = create_app(str(tmp_path / 'cp.sqlite'), extractor=FakeExtractor())
    with TestClient(app) as c:
        sid = start(c)
        c.post(f'/sessions/{sid}/confirm', json={'approved': True})
        graph = app.state.graph
        config = {'configurable': {'thread_id': sid}}
        graph.update_state(config, {'result': {'rows': 1, 'monthly_remaining': None}})
        r = c.get(f'/sessions/{sid}')
        assert r.status_code == 200
        assert r.json()['result'] is None


def test_profile_is_returned_for_prefill(tmp_path):
    """새로고침한 화면이 입력칸을 다시 채울 수 있게 저장된 사용자 정보를 돌려준다."""
    with client(tmp_path / 'cp.sqlite') as c:
        sid = start(c)
        assert c.get(f'/sessions/{sid}').json()['profile'] is None
        c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': pangyo_profile()})
        profile = c.get(f'/sessions/{sid}').json()['profile']
        assert profile['postings']['posting-1']['commute_transport']['value'] == 110_000


def test_failed_recalculation_leaves_session_usable(tmp_path, monkeypatch):
    """다시 계산하다 실패해도 세션은 completed 그대로라, 고친 뒤 다시 보낼 수 있다."""
    import backend.api.sessions as sessions

    with client(tmp_path / 'cp.sqlite') as c:
        sid = start(c)
        first = c.post(
            f'/sessions/{sid}/confirm', json={'approved': True, 'profile': pangyo_profile()}
        ).json()

        def boom(*args, **kwargs):
            raise RuntimeError('계산 중 오류')

        monkeypatch.setattr(sessions, 'calculate_all', boom)
        r = c.post(f'/sessions/{sid}/recalculate', json={'profile': pangyo_profile()})
        assert r.status_code == 500
        after = c.get(f'/sessions/{sid}').json()
        assert after['status'] == 'completed'
        assert after['result'] == first['result']  # 아무것도 바뀌지 않음

        monkeypatch.undo()
        r = c.post(f'/sessions/{sid}/recalculate', json={'profile': pangyo_profile()})
        assert r.status_code == 200


def test_concurrent_profile_updates_are_serialized(tmp_path):
    """같은 세션에 사용자 정보 변경이 동시에 와도 둘 다 처리되고, 각자 자기 결과를 받는다."""
    import asyncio

    import httpx

    app = create_app(str(tmp_path / 'cp.sqlite'), extractor=FakeExtractor())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url='http://t') as c:
                sid = (await c.post('/sessions', json={'message': PANGYO})).json()['session_id']
                await c.post(
                    f'/sessions/{sid}/confirm',
                    json={'approved': True, 'profile': pangyo_profile()},
                )
                profiles = []
                for extra in (0, 100_000, 200_000):
                    p = pangyo_profile()
                    p['current_housing']['value'] += extra
                    profiles.append(p)
                responses = await asyncio.gather(
                    *(c.post(f'/sessions/{sid}/recalculate', json={'profile': p}) for p in profiles)
                )
                return profiles, [r.json() for r in responses]

    profiles, bodies = asyncio.run(scenario())
    for sent, body in zip(profiles, bodies, strict=True):
        assert body['status'] == 'completed'
        # 각 응답은 자기가 보낸 값으로 계산한 결과다 (다른 요청의 결과가 섞이지 않는다)
        assert body['profile']['current_housing']['value'] == sent['current_housing']['value']


def test_new_message_clears_old_profile_and_result(tmp_path):
    """공고를 더해 비교표를 새로 만들면, 이전 표 기준의 사용자 정보·결과는 지운다.

    공고 ID(posting-1 …)가 다른 회사를 가리킬 수 있어서다. 확인할 때 다시 받는다.
    """
    with client(tmp_path / 'cp.sqlite') as c:
        sid = start(c)
        c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': pangyo_profile()})
        r = c.post(f'/sessions/{sid}/messages', json={'message': '추가 공고'})
        s = r.json()
        assert s['status'] == 'awaiting_confirmation'
        assert s['profile'] is None
        assert s['result'] is None
        assert s['report'] is None


def test_unknown_posting_id_in_profile_is_reported(tmp_path):
    p = pangyo_profile()
    p['postings']['posting-9'] = p['postings']['posting-1']
    p['baseline_posting_id'] = 'posting-7'
    with client(tmp_path / 'cp.sqlite') as c:
        sid = start(c)
        done = c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': p}).json()
        [error] = done['result']['errors']
        assert 'posting-9' in error and 'posting-7' in error


def test_recalculate_with_edited_table_does_not_call_llm(tmp_path):
    """결과를 본 뒤 비교표를 고쳐도 새 세션 없이 다시 계산한다 (공고 정리 LLM은 안 부른다)."""
    extractor = FakeExtractor()
    with client(tmp_path / 'cp.sqlite', extractor) as c:
        sid = start(c)
        first = c.post(
            f'/sessions/{sid}/confirm', json={'approved': True, 'profile': pangyo_profile()}
        ).json()
        calls = len(extractor.calls)

        table = first['table']
        table[0]['annual_salary_krw'] = {'value': 48_000_000, 'status': 'user_input'}
        r = c.post(
            f'/sessions/{sid}/recalculate', json={'profile': pangyo_profile(), 'table': table}
        )
        assert r.status_code == 200
        after = r.json()
        assert after['table'][0]['annual_salary_krw']['value'] == 48_000_000
        assert remaining(after)['commute'] > remaining(first)['commute']  # 연봉이 올라 더 남는다
        assert len(extractor.calls) == calls

        # 다시 조회해도 고친 표·결과가 저장돼 있다
        assert c.get(f'/sessions/{sid}').json()['result'] == after['result']


def test_recalculate_rejects_empty_or_bad_table(tmp_path):
    with client(tmp_path / 'cp.sqlite') as c:
        sid = start(c)
        c.post(f'/sessions/{sid}/confirm', json={'approved': True, 'profile': pangyo_profile()})
        negative = {'value': -1}  # 음수 연봉
        for body in (
            {'profile': pangyo_profile(), 'table': []},  # 빈 표
            {'profile': pangyo_profile(), 'table': [{'id': 'p', 'annual_salary_krw': negative}]},
            {'table': None},  # 사용자 정보 없음
        ):
            r = c.post(f'/sessions/{sid}/recalculate', json=body)
            assert r.status_code == 422, body
        assert c.get(f'/sessions/{sid}').json()['status'] == 'completed'
