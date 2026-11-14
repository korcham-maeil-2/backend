"""개발용 가짜 응답 모드 — 실제 Claude 대신 미리 써 둔 응답을 돌려준다 (비용 0원).

켜는 법: .env에 NORMALIZE_MODE=fixture  (끄면 실제 Claude를 부른다)

화면 개발·시연 연습 때 쓴다. 동작:
  - tests/fixtures/normalize/{id}.input.txt 의 글이 입력 안에 들어 있으면
    {id}.llm.json 에 써 둔 공고를 돌려준다 (띄어쓰기·줄바꿈 차이는 무시).
  - 공고 여러 개가 들어 있으면 입력에 나온 순서대로 모두 돌려준다.
  - 모르는 글이면 '공고 없음'(빈 목록)을 돌려준다.
"""

import logging
from pathlib import Path
from typing import Literal

from backend.normalize.checks import squash
from backend.normalize.llm import ClaudeExtractor, Extractor
from backend.normalize.schema import ExtractionResult

logger = logging.getLogger(__name__)

# backend/tests/fixtures/normalize  (이 파일: backend/src/backend/normalize/fixture.py)
FIXTURES_DIR = Path(__file__).resolve().parents[3] / 'tests' / 'fixtures' / 'normalize'


class FixtureExtractor:
    """입력 글 속에 아는 시연 공고가 있으면 미리 써 둔 응답을 돌려준다."""

    def __init__(self, fixtures_dir: Path = FIXTURES_DIR) -> None:
        self.known: list[tuple[str, ExtractionResult]] = []
        for answer in sorted(fixtures_dir.glob('*.llm.json')):
            source = answer.with_name(answer.name.replace('.llm.json', '.input.txt'))
            self.known.append(
                (
                    squash(source.read_text(encoding='utf-8')),
                    ExtractionResult.model_validate_json(answer.read_text(encoding='utf-8')),
                )
            )
        if not self.known:
            raise FileNotFoundError(f'가짜 응답 파일이 없습니다: {fixtures_dir}')

    def __call__(self, text: str) -> ExtractionResult:
        s = squash(text)
        found = sorted(
            ((s.find(source), result) for source, result in self.known if source in s),
            key=lambda item: item[0],
        )
        return ExtractionResult(postings=[p for _, result in found for p in result.postings])


NormalizeMode = Literal['claude', 'fixture']


def parse_mode(raw: str | None) -> NormalizeMode:
    """NORMALIZE_MODE 값을 읽는다. 비우면 claude.

    정해진 값이 아니면 서버가 켜질 때 바로 멈춘다.
    ('fixtures' 같은 오타가 조용히 실제 Claude 호출(과금)로 넘어가지 않게 하려고)
    """
    mode = (raw or '').strip().lower() or 'claude'
    if mode not in ('claude', 'fixture'):
        raise ValueError(
            f"NORMALIZE_MODE는 비우거나 'claude' 또는 'fixture'여야 합니다 (받은 값: {raw!r})"
        )
    return mode  # type: ignore[return-value]


def make_extractor(mode: NormalizeMode) -> Extractor:
    """fixture면 미리 써 둔 응답, claude면 실제 Claude."""
    if mode == 'fixture':
        logger.warning('NORMALIZE_MODE=fixture: 실제 Claude 대신 미리 써 둔 응답을 씁니다')
        return FixtureExtractor()
    return ClaudeExtractor()
