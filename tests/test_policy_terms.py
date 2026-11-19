"""정착 플랜 정책 카드: 정책 원문을 읽어 지원 내용·주의·조건을 정리
(support/terms.py, GET /sessions/{id}/policy-terms).

실제 Claude는 부르지 않는다 (가짜 읽기). 원문 대조·저장·꺼짐을 확인한다.
"""

from conftest import scenario_text
from fastapi.testclient import TestClient

from backend.api import sessions
from backend.main import create_app
from backend.normalize import FixtureExtractor
from backend.plan import PlanItem, PlanSlot, PostingPlan, SettlePlan
from backend.support.terms import PolicyTermsService, TermsError, TermsStore, source_text

POLICY = {
    'plcyNo': '20260001',
    'plcyNm': '청년 월세 지원',
    'plcySprtCn': '월 최대 20만원(최장 12개월) 지원. 예산 소진 시 조기 마감',
    'earnEtcCn': '기준 중위소득 60% 이하',
    'addAplyQlfcCndCn': '무주택 청년 세대주, 주민등록상 해당 시 거주',
    'sprtTrgtMinAge': '19',
    'sprtTrgtMaxAge': '34',
}


class FakeSource:
    def __init__(self, policies):
        self.policies = policies

    def policies_for(self, zip_cd):
        return self.policies


class FakeReader:
    def __init__(self, answer=None, error=False):
        self.calls = []
        self.answer = answer or {
            'summary': '청년 월세 지원',
            'caution': '예산 소진 시 조기 마감',
            'amount': '월 최대 20만원(최장 12개월)',
            'conditions': [
                {'kind': 'no_house', 'text': '무주택', 'quote': '무주택 청년 세대주'},
                {'kind': 'income', 'text': '중위소득 60% 이하', 'quote': '기준 중위소득 60% 이하'},
                {'kind': 'other', 'text': '지어낸 조건', 'quote': '원문에 없는 문장'},
            ],
        }
        self.error = error

    def __call__(self, text):
        self.calls.append(text)
        if self.error:
            raise TermsError('실패')
        return self.answer


def service(tmp_path, reader, policies=(POLICY,)):
    return PolicyTermsService(FakeSource(list(policies)), reader, TermsStore(tmp_path / 't.sqlite'))


def test_quotes_not_in_source_are_dropped(tmp_path):
    reader = FakeReader()
    terms = service(tmp_path, reader).get(['46170'], ['20260001'])['20260001']
    assert [c.kind for c in terms.conditions] == ['no_house', 'income']  # 지어낸 인용은 버림
    assert terms.caution == '예산 소진 시 조기 마감'
    assert terms.amount == '월 최대 20만원(최장 12개월)'


def test_made_up_caution_and_amount_are_dropped(tmp_path):
    answer = {**FakeReader().answer, 'caution': '신규 가입 중단', 'amount': '월 100만원'}
    terms = service(tmp_path, FakeReader(answer)).get(['46170'], ['20260001'])['20260001']
    assert terms.caution is None and terms.amount is None


def test_read_once_and_reused_until_source_changes(tmp_path):
    reader = FakeReader()
    store_service = service(tmp_path, reader)
    store_service.get(['46170'], ['20260001'])
    store_service.get(['46170'], ['20260001'])
    assert len(reader.calls) == 1  # 저장된 결과를 다시 씀 (비용 한 번)
    changed = {**POLICY, 'earnEtcCn': '기준 중위소득 70% 이하'}
    service(tmp_path, reader, [changed]).get(['46170'], ['20260001'])
    assert len(reader.calls) == 2  # 원문이 바뀌면 다시 읽는다


def test_only_public_policy_text_is_sent(tmp_path):
    reader = FakeReader()
    service(tmp_path, reader).get(['46170'], ['20260001'])
    assert reader.calls == [source_text(POLICY)]
    assert '[정책명] 청년 월세 지원' in reader.calls[0]


def test_unknown_ids_and_failures_are_left_out(tmp_path):
    assert service(tmp_path, FakeReader()).get(['46170'], ['없는번호']) == {}
    assert service(tmp_path, FakeReader(error=True)).get(['46170'], ['20260001']) == {}
    bad = FakeReader({'summary': 1})  # 모양이 틀린 답
    assert service(tmp_path, bad).get(['46170'], ['20260001']) == {}


def _plan_with_policy(values):
    """세션 플랜 대신: 정책 하나가 들어 있는 플랜 (정책 고르기 규칙과 상관없이 끝점만 본다)."""
    item = PlanItem(kind='policy', title='청년 월세 지원', policy_id='20260001')
    slot = PlanSlot(kind='anytime', label='언제든', items=[item])
    posting = PostingPlan(
        posting_id='posting-1',
        company=None,
        region=None,
        lawd_cd='46170',
        moving=True,
        slots=[slot],
    )
    return SettlePlan(postings=[posting], start_month='2026-11')


def _session(c) -> str:
    return c.post('/sessions', json={'message': scenario_text('near_pangyo')}).json()['session_id']


def test_endpoint_reads_only_policies_in_the_session_plan(tmp_path, monkeypatch):
    """읽을 정책은 세션 플랜에서 서버가 고른다.
    화면이 보낸 번호를 받지 않아, 아무 정책이나 읽혀 비용을 쓰게 할 수 없다."""
    reader = FakeReader()
    kw = {'extractor': FixtureExtractor(), 'support_source': FakeSource([POLICY])}
    with TestClient(create_app(str(tmp_path / 'a.sqlite'), **kw)) as c:
        sid = _session(c)
        # 플랜이 아직 없음(계산 전) → off
        assert c.get(f'/sessions/{sid}/policy-terms').json() == {'status': 'off', 'terms': {}}
    monkeypatch.setattr(sessions, '_plan', _plan_with_policy)
    with TestClient(create_app(str(tmp_path / 'b.sqlite'), **kw)) as c:
        sid = _session(c)
        assert c.get(f'/sessions/{sid}/policy-terms').json()['status'] == 'off'  # 읽기 꺼짐
    with TestClient(create_app(str(tmp_path / 'c.sqlite'), terms_reader=reader, **kw)) as c:
        sid = _session(c)
        got = c.get(f'/sessions/{sid}/policy-terms').json()
        assert got['status'] == 'ok'
        assert list(got['terms']) == ['20260001']
        assert got['terms']['20260001']['amount'] == '월 최대 20만원(최장 12개월)'
        assert c.get('/sessions/없는세션/policy-terms').status_code == 404
    assert reader.calls == [source_text(POLICY)]  # 공개 정책 글만 보냈다


def test_one_broken_read_does_not_fail_the_others(tmp_path):
    other = {**POLICY, 'plcyNo': '20260002', 'plcyNm': '다른 지원'}

    def reader(text):
        if '다른 지원' in text:
            raise KeyError('버그')
        return FakeReader().answer

    got = service(tmp_path, reader, [POLICY, other]).get(['46170'], ['20260001', '20260002'])
    assert list(got) == ['20260001']
