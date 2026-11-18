"""개인정보 마스킹 (이슈 #20): 외부 AI로 가는 글에 개인정보가 남지 않는지.

완료 조건: 개인정보가 들어간 공고를 넣어도 LLM 요청에는 마스킹된 값만 있다.
"""

from types import SimpleNamespace

import pytest
from conftest import scenario_text
from langgraph.checkpoint.memory import InMemorySaver

from backend.claude import FailureMessages, create_message
from backend.graph.builder import build_graph
from backend.normalize.fixture import FixtureExtractor
from backend.privacy import mask_messages, mask_quotes, mask_text

# 가상 개인정보 (실제 사람·번호 아님)
NAME, MOBILE, PHONE, EMAIL, RRN = (
    '홍길동',
    '010-1234-5678',
    '02-123-4567',
    'hong@corp.example',
    '900101-1234567',
)
PII = [NAME, MOBILE, PHONE, EMAIL, RRN, '1234-5678', '235', '1203호']
CONTACT = (
    f'\n문의: 채용 담당자 {NAME} 과장 ({MOBILE}, 대표 {PHONE}) {EMAIL}\n'
    f'제출 서류 예시: 주민등록번호 {RRN}\n'
    '본사: 경기 성남시 분당구 판교역로 235, 101동 1203호\n'
)


@pytest.mark.parametrize(
    ('text', 'masked'),
    [
        ('메일 jobs@example.com 로', '메일 [이메일] 로'),
        ('연락처 010-1234-5678', '연락처 [전화번호]'),
        ('연락처 01012345678', '연락처 [전화번호]'),
        ('+82 10-1234-5678', '[전화번호]'),
        ('대표 02-123-4567', '대표 [전화번호]'),
        ('지점 031)789-1234', '지점 [전화번호]'),
        ('고객센터 1588-1234', '고객센터 [전화번호]'),
        ('주민번호 900101-1234567', '주민번호 [주민번호]'),
        ('채용 담당자 홍길동 과장', '채용 담당자 [이름] 과장'),
        ('성명: 김철수', '성명: [이름]'),
        ('인사담당자 이영희님', '인사담당자 [이름]님'),
        ('경기 성남시 분당구 판교역로 235, 7층', '경기 성남시 분당구 판교역로 [번지], 7층'),
        ('전남 나주시 빛가람로 661 (빛가람동)', '전남 나주시 빛가람로 [번지] (빛가람동)'),
        ('서울 강남구 테헤란로 123길 4', '서울 강남구 테헤란로 123길 [번지]'),
        ('서울 강서구 마곡동 123-45번지 101동 1203호', '서울 강서구 마곡동 [번지] [상세주소]'),
        # ── 여러 공고 예시로 찾은 형식 (PR #45 점검) ──
        ('TEL: (02) 555-1234', 'TEL: [전화번호]'),
        ('인사팀 이서연 (02-555-1234)', '인사팀 [이름] ([전화번호])'),
        ('안심번호 0507-1234-5678', '안심번호 [전화번호]'),
        ('FAX 0505-123-4567', 'FAX [전화번호]'),
        ('82-10-1234-5678', '[전화번호]'),
        ('전화 1588-1234로 문의', '전화 [전화번호]로 문의'),
        ('메일 hong @ corp.com', '메일 [이메일]'),
        ('메일 hong(at)corp.com', '메일 [이메일]'),
        ('주민등록번호: 9001011234567', '주민등록번호: [주민번호]'),
        ('생년월일 1990.01.01', '생년월일 [생년월일]'),
        ('계좌번호 110-123-456789 (신한)', '계좌번호 [식별번호] (신한)'),
        ('급여 계좌: 우리은행 1002-123-456789', '급여 계좌: 우리은행 [식별번호]'),
        ('여권번호 M12345678', '여권번호 [식별번호]'),
        ('카톡 ID : cafe_boss99', '카톡 ID : [아이디]'),
        ('대표이사 최영수', '대표이사 [이름]'),
        ('대표자명 김한빛 | 사원수 120명', '대표자명 [이름] | 사원수 120명'),
        ('담당: 박지영 주임', '담당: [이름] 주임'),
        ('담당자 : 홍 길 동', '담당자 : [이름]'),
        ('성    명 : 홍길동', '성    명 : [이름]'),
        ('담당자 정보람 (010-2222-3333)', '담당자 [이름] ([전화번호])'),
        ('문의: 홍길동 대리 (hong@corp.co.kr)', '문의: [이름] 대리 ([이메일])'),
        ('문의: 백엔드 팀장 김개발 010-1111-2222', '문의: 백엔드 팀장 [이름] [전화번호]'),
        ('김지은 대리\n010-1234-5678', '[이름] 대리\n[전화번호]'),  # 연락처가 다음 줄
        # 합격 메일의 받는 사람(사용자 본인) 이름
        ('받는사람: 서지후 <jihu.seo@mail.example>', '받는사람: [이름] <[이메일]>'),
        ('서지후 님, 안녕하세요.', '[이름] 님, 안녕하세요.'),
        ('서지후님의 입사를 기다리겠습니다.', '[이름]님의 입사를 기다리겠습니다.'),
        ('서울 강남구 역삼동 737', '서울 강남구 역삼동 [번지]'),
        ('전남 나주시 전력로 55 (빛가람동 120)', '전남 나주시 전력로 [번지] (빛가람동 [번지])'),
        ('울산 남구 삼산로 274번길 12', '울산 남구 삼산로 274번길 [번지]'),
    ],
)
def test_masks_personal_info(text, masked):
    assert mask_text(text) == masked


@pytest.mark.parametrize(
    'text',
    [
        '연봉: 3,600만원 (기본급 기준, 성과급 별도)',
        '접수 기간: 2026-11-16 ~ 2026-12-15',
        '2026.11.16 마감',
        '회사명: (가상) 이음데이터랩(주)',
        '근무지: 경기 성남시 분당구 판교역로 (판교테크노밸리)',
        '연봉으로 3,600만원이면',
        '앞으로 3 년간',
        '담당 업무: 백엔드 개발',
        '담당자: 미정',
        '담당자 이메일로 보내 주세요',
        '3호선 근처, 모집 인원 2명, 식대 월 20만원',
        '세후 월급 286만 8,100원, 매달 남는 돈 {{A3}}',
        # ── 여러 공고 예시로 찾은 '잘못 가리는 경우' (PR #45 점검) ──
        '연봉 1800-2000만원',  # 급여 범위를 대표번호(1588-0000 꼴)로 보면 안 된다
        '근무 1700-1800시',
        '월 1500-1800 시간',
        '회사명: 성명기업(주)',
        '이름: 백엔드 개발자 채용',
        '문의: 마케팅 팀장',
        '담당: 신입 매니저 교육',
        '문의: 인사팀',
        '인사 담당자 문의는 메일로',
        '인사팀 문자를 붙여 넣고',
        '수신: 채용담당자 귀하',
        '지원자님께 개별 연락',
        '성명 / 연락처 / 이메일을 적어 주세요',
        '생년월일(6자리) 입력',
        '기관 보수규정 제12조 제2호에 따름',
        '4호 태풍, 9호선 급행',
        '사업자등록번호 123-45-67890',  # 회사의 공개 정보
        '나주시 빛가람동 원룸 월세 30만원',
        '분당구 정자동 20~40㎡',
        '카카오톡 채널 운영 경험',
        '지원: https://corp.com/apply?id=01012345678',  # 주소는 통째로 둔다 (링크 보존)
    ],
)
def test_keeps_values_needed_for_comparison(text):
    """계산·비교에 필요한 값(회사 이름·시군구·금액·날짜)과 보통 문장은 건드리지 않는다."""
    assert mask_text(text) == text


def test_fixture_quotes_are_masked_like_the_text():
    """시연용 가짜 응답의 인용도 같은 규칙으로 가린다 (가린 글과 맞아야 '확인'이 된다)."""
    answer = {'postings': [{'location': {'value': 'x', 'quote': '울산광역시 남구 부곡동 000'}}]}
    assert mask_quotes(answer) == {
        'postings': [{'location': {'value': 'x', 'quote': '울산광역시 남구 부곡동 [번지]'}}]
    }


def test_masking_twice_changes_nothing():
    once = mask_text(CONTACT)
    assert mask_text(once) == once


def test_mask_messages_masks_text_only_and_keeps_original():
    image = {
        'type': 'image',
        'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'},
    }
    messages = [
        {'role': 'user', 'content': f'공고 {EMAIL}'},
        {'role': 'user', 'content': [image, {'type': 'text', 'text': f'전화 {MOBILE}'}]},
    ]
    masked = mask_messages(messages)
    assert masked[0]['content'] == '공고 [이메일]'
    assert masked[1]['content'] == [image, {'type': 'text', 'text': '전화 [전화번호]'}]
    assert messages[0]['content'] == f'공고 {EMAIL}'  # 원본은 그대로


class CapturingClient:
    """Claude 대신 요청을 받아 적어 두는 가짜 (실제 호출 없음)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        text = SimpleNamespace(type='text', text='{}')
        return SimpleNamespace(stop_reason='end_turn', content=[text])


def test_every_claude_request_is_masked():
    """create_message를 거치는 모든 요청(공고 정리·이미지 읽기·리포트)은 보내기 전에 가린다."""
    client = CapturingClient()
    failure = FailureMessages('테스트', '', '', '', '', '')
    create_message(
        lambda: client,
        RuntimeError,
        failure,
        max_tokens=10,
        messages=[{'role': 'user', 'content': f'공고 본문{CONTACT}'}],
    )
    sent = str(client.requests[0]['messages'])
    for value in PII:
        assert value not in sent
    assert '[이메일]' in sent and '[전화번호]' in sent and '[이름]' in sent


class RecordingExtractor:
    """시연용 가짜 응답을 쓰되, LLM 자리로 들어온 글을 적어 둔다."""

    def __init__(self) -> None:
        self.inner = FixtureExtractor()
        self.texts: list[str] = []

    def __call__(self, text: str):
        self.texts.append(text)
        return self.inner(text)


def test_posting_with_personal_info_reaches_llm_masked_and_table_still_confirmed():
    """개인정보가 든 공고를 넣어도 LLM에는 가린 글만 가고, 표의 '확인' 표시는 그대로다."""
    extractor = RecordingExtractor()
    graph = build_graph(InMemorySaver(), extractor)
    state = graph.invoke(
        {'messages': [scenario_text('near_pangyo') + CONTACT]},
        {'configurable': {'thread_id': 'privacy'}},
    )
    sent = extractor.texts[0]
    for value in PII:
        assert value not in sent
    row = state['table'][0]
    assert row['company']['status'] == 'confirmed'
    assert row['location']['status'] == 'confirmed'
    assert row['annual_salary_krw']['status'] == 'confirmed'


def test_offer_mail_recipient_name_is_not_sent():
    """합격 메일에는 받는 사람(사용자 본인) 이름과 메일 주소가 있다 → LLM에는 가려서 보낸다."""
    extractor = RecordingExtractor()
    graph = build_graph(InMemorySaver(), extractor)
    state = graph.invoke(
        {'messages': [scenario_text('up_seoul')]},
        {'configurable': {'thread_id': 'offer-mail'}},
    )
    sent = extractor.texts[0]
    assert '서지후' not in sent
    assert '@' not in sent
    row = state['table'][0]
    assert row['company']['status'] == 'confirmed'
    assert row['annual_salary_krw']['status'] == 'confirmed'


def test_long_text_without_spaces_is_fast():
    """띄어쓰기 없는 긴 글도 빨리 가린다 (이슈 #52: 이메일 규칙이 제곱 시간이라 멈췄다)."""
    import time

    for text in ('x' * 150_000, 'a.b' * 50_000, '010' * 50_000):
        started = time.perf_counter()
        mask_text(text)
        assert time.perf_counter() - started < 1.0
    assert mask_text('x' * 10 + ' hong@corp.example') == 'x' * 10 + ' [이메일]'
