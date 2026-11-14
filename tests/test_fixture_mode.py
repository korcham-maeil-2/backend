"""개발용 가짜 응답 모드 (NORMALIZE_MODE=fixture) 테스트.

화면을 개발할 때 실제 Claude 대신 미리 써 둔 응답을 돌려주는지 확인한다.
"""

import pytest
from conftest import scenario_text
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.normalize import ClaudeExtractor, FixtureExtractor, make_extractor, parse_mode


def test_known_posting_returns_fixture():
    result = FixtureExtractor()(scenario_text('near_pangyo'))
    assert [p.company.value for p in result.postings] == ['(가상) 이음데이터랩(주)']


def test_spacing_differences_are_ignored():
    text = '  ' + scenario_text('down_naju').replace('\n', '\r\n\n') + '  '
    assert len(FixtureExtractor()(text).postings) == 1


def test_several_postings_in_input_order():
    text = scenario_text('down_naju') + '\n\n---\n\n' + scenario_text('near_pangyo')
    companies = [p.company.value for p in FixtureExtractor()(text).postings]
    assert companies == ['(가상) 빛가람그리드(주)', '(가상) 이음데이터랩(주)']


def test_unknown_text_returns_no_posting():
    assert FixtureExtractor()('처음 보는 공고').postings == []


def test_mode_switch():
    assert parse_mode(None) == parse_mode('') == parse_mode(' Claude ') == 'claude'
    assert parse_mode(' Fixture ') == 'fixture'
    assert isinstance(make_extractor('fixture'), FixtureExtractor)
    assert isinstance(make_extractor('claude'), ClaudeExtractor)


@pytest.mark.parametrize('typo', ['fixtures', 'fake', 'mock'])
def test_typo_in_mode_stops_server(typo, tmp_path, monkeypatch):
    """오타가 조용히 실제 Claude 호출(과금)로 넘어가지 않고, 서버가 켜질 때 멈춘다."""
    with pytest.raises(ValueError, match='NORMALIZE_MODE'):
        parse_mode(typo)
    monkeypatch.setenv('NORMALIZE_MODE', typo)
    with pytest.raises(ValueError):
        create_app(str(tmp_path / 'cp.sqlite'))


def test_app_uses_fixture_mode_from_env(tmp_path, monkeypatch):
    """NORMALIZE_MODE=fixture로 서버를 켜면 API 키 없이 비교표가 나온다."""
    monkeypatch.setenv('NORMALIZE_MODE', 'fixture')
    with TestClient(create_app(str(tmp_path / 'cp.sqlite'))) as c:
        s = c.post('/sessions', json={'message': scenario_text('stay_ulsan')}).json()
    assert s['table'][0]['company']['value'] == '(주)여울케미칼'


def test_health_tells_mode(tmp_path, monkeypatch):
    """프론트가 '시연 모드' 표시를 띄울 수 있게 /health에 모드를 싣는다."""
    monkeypatch.setenv('NORMALIZE_MODE', 'fixture')
    with TestClient(create_app(str(tmp_path / 'cp.sqlite'))) as c:
        assert c.get('/health').json() == {'status': 'ok', 'normalize_mode': 'fixture'}


def test_absurd_values_are_rejected_with_422(tmp_path, monkeypatch):
    """20자리 금액, 1000% 같은 값은 500이 아니라 422(입력 형식 오류)로 막는다."""
    monkeypatch.setenv('NORMALIZE_MODE', 'fixture')
    with TestClient(create_app(str(tmp_path / 'cp.sqlite'))) as c:
        s = c.post('/sessions', json={'message': scenario_text('near_pangyo')}).json()
        for key, value in [
            ('annual_salary_krw', 10**20),
            ('probation_pay_rate', 10.0),
            ('probation_months', -1),
        ]:
            table = [dict(s['table'][0])]
            table[0][key] = {'value': value, 'status': 'user_input', 'source_text': None}
            r = c.post(f'/sessions/{s["session_id"]}/confirm', json={'table': table})
            assert r.status_code == 422, key
            assert r.json()['code'] == 'VALIDATION_ERROR'
