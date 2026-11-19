"""정착 플랜 정책 카드: 정책 원문을 읽어 지원 내용·주의·조건을 정리
(support/terms.py, POST /plan/policy-terms).

실제 Claude는 부르지 않는다 (가짜 읽기). 원문 대조·저장·꺼짐을 확인한다.
"""

from fastapi.testclient import TestClient

from backend.main import create_app
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


def test_endpoint_off_without_reader_and_ok_with_reader(tmp_path):
    body = {'lawd_cds': ['46170'], 'ids': ['20260001']}
    app = create_app(str(tmp_path / 'a.sqlite'), support_source=FakeSource([POLICY]))
    with TestClient(app) as c:
        assert c.post('/plan/policy-terms', json=body).json() == {'status': 'off', 'terms': {}}
    app = create_app(
        str(tmp_path / 'b.sqlite'), support_source=FakeSource([POLICY]), terms_reader=FakeReader()
    )
    with TestClient(app) as c:
        got = c.post('/plan/policy-terms', json=body).json()
        assert got['status'] == 'ok'
        assert got['terms']['20260001']['amount'] == '월 최대 20만원(최장 12개월)'
        # 지역 코드·정책 번호 모양이 아니면 422
        assert c.post('/plan/policy-terms', json={'lawd_cds': ['x'], 'ids': []}).status_code == 422
