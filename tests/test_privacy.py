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
from backend.privacy import mask_messages, mask_text

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
    ],
)
def test_keeps_values_needed_for_comparison(text):
    """계산·비교에 필요한 값(회사 이름·시군구·금액·날짜)과 보통 문장은 건드리지 않는다."""
    assert mask_text(text) == text


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
