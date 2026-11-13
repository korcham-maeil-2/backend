"""테스트 공통 준비물.

[중요] 테스트는 절대 실제 Claude API를 부르지 않는다 (키 사용 한도 100달러).
  - 모든 테스트에서 ANTHROPIC_API_KEY를 지우고, anthropic.Anthropic()을 만들려 하면 실패시킨다.
  - LLM 응답이 필요한 곳은 tests/fixtures/normalize/의 손으로 쓴 응답(*.llm.json)을 쓴다.

[fixture 파일]
  {id}.input.txt : 사용자가 붙여 넣는 공고 글 (data/demo/postings/{id}/source.json으로 만듦.
                   up_seoul은 합격 메일 캡처의 본문을 옮겨 적음)
  {id}.llm.json  : 그 글을 Claude가 읽었다고 치고 손으로 쓴 구조화 응답 (ExtractionResult 모양)
"""

import json
from pathlib import Path

import anthropic
import pytest

from backend.normalize import ExtractionResult

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures' / 'normalize'
DEMO_POSTINGS = ROOT / 'data' / 'demo' / 'postings'
SCENARIOS = ['stay_ulsan', 'up_seoul', 'across_gwangju', 'near_pangyo', 'down_naju']


@pytest.fixture(autouse=True)
def no_real_anthropic_calls(monkeypatch):
    """모든 테스트에 자동 적용: 실제 Claude 클라이언트를 만들 수 없게 막는다."""
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError('테스트에서 실제 Claude API 클라이언트를 만들면 안 됩니다')

    monkeypatch.setattr(anthropic, 'Anthropic', forbidden)


def scenario_text(pid: str) -> str:
    return (FIXTURES / f'{pid}.input.txt').read_text(encoding='utf-8')


def scenario_llm(pid: str) -> ExtractionResult:
    return ExtractionResult.model_validate_json(
        (FIXTURES / f'{pid}.llm.json').read_text(encoding='utf-8')
    )


def scenario_expected(pid: str) -> dict:
    return json.loads((DEMO_POSTINGS / pid / 'expected.json').read_text(encoding='utf-8'))


class FakeExtractor:
    """가짜 LLM: 입력 글이 시나리오 글과 같으면 그 fixture 응답을, 아니면 '공고 없음'을 돌려준다."""

    def __init__(self) -> None:
        self.answers = {scenario_text(pid): scenario_llm(pid) for pid in SCENARIOS}
        self.calls: list[str] = []

    def __call__(self, text: str) -> ExtractionResult:
        self.calls.append(text)
        return self.answers.get(text, ExtractionResult(postings=[]))


@pytest.fixture
def fake_extractor() -> FakeExtractor:
    return FakeExtractor()
