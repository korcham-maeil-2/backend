"""개발용 가짜 응답 모드 — 실제 Claude 대신 미리 써 둔 응답을 돌려준다 (비용 0원).

켜는 법: .env에 NORMALIZE_MODE=fixture  (끄면 실제 Claude를 부른다)

화면 개발·시연 연습 때 쓴다. 동작:
  - tests/fixtures/normalize/{id}.llm.json 에 손으로 쓴 답의 인용(quote)이
    **모두** 입력 글에 있으면 그 답을 쓴다 (띄어쓰기·줄바꿈 차이는 무시).
    붙여 넣은 글뿐 아니라 PDF·엑셀에서 꺼낸 글, 이미지를 옮겨 적은 글도 알아보기 위해서다
    (이런 글은 줄 모양이 달라 원문 전체로는 맞춰 볼 수 없다).
  - 같은 공고의 변형 답 {id}.{변형}.llm.json 은 인용만 다르다 (예: 글자 PDF는 '주거 지원: 없음'이
    '주거 지원
없음'으로 나와 콜론이 없다). 같은 공고는 한 번만 돌려준다.
  - 공고 여러 개가 들어 있으면 입력에 나온 순서대로 모두 돌려준다.
  - 모르는 글이면 '공고 없음'(빈 목록)을 돌려준다.

[한계] 인용이 모두 있어야 맞추므로, 이 모드에서는 '추정'이 나오는 경우를 볼 수 없다.
  같은 공고를 글과 파일로 두 번 넣으면 여기서는 한 행이 되지만, 실제 Claude는 두 행을 낼 수도 있다.
"""

import logging
from pathlib import Path
from typing import Literal

from backend.normalize.checks import squash
from backend.normalize.llm import ClaudeExtractor, Extractor
from backend.normalize.schema import ExtractedPosting, ExtractionResult

logger = logging.getLogger(__name__)

# backend/tests/fixtures/normalize  (이 파일: backend/src/backend/normalize/fixture.py)
FIXTURES_DIR = Path(__file__).resolve().parents[3] / 'tests' / 'fixtures' / 'normalize'

# 인용이 너무 적은 답은 아무 글에나 맞아 버린다 → 불러올 때 막는다.
MIN_QUOTES = 3


def _quotes(posting: ExtractedPosting) -> set[str]:
    """답 하나에 들어 있는 인용 전부 (비교용으로 squash)."""
    found: set[str] = set()
    for name in ExtractedPosting.model_fields:
        value = getattr(posting, name)
        items = value if isinstance(value, list) else [value]
        for item in items:
            quote = getattr(item, 'quote', None)
            if quote and quote.strip():
                found.add(squash(quote))
    return found


class FixtureExtractor:
    """입력 글 속에 아는 시연 공고가 있으면 미리 써 둔 응답을 돌려준다."""

    def __init__(self, fixtures_dir: Path = FIXTURES_DIR) -> None:
        # (공고 id, 인용 목록, 공고 답). 기본 답이 변형 답보다 앞에 온다.
        self.known: list[tuple[str, set[str], ExtractedPosting]] = []
        files = sorted(fixtures_dir.glob('*.llm.json'), key=lambda f: (f.name.count('.'), f.name))
        for answer in files:
            pid = answer.name.split('.')[0]
            result = ExtractionResult.model_validate_json(answer.read_text(encoding='utf-8'))
            for posting in result.postings:
                quotes = _quotes(posting)
                company = posting.company.quote
                if len(quotes) < MIN_QUOTES or not company or squash(company) not in quotes:
                    raise ValueError(
                        f'가짜 응답 {answer.name}: 회사명 인용을 포함해 인용이 '
                        f'{MIN_QUOTES}개 이상 있어야 합니다'
                    )
                self.known.append((pid, quotes, posting))
        if not self.known:
            raise FileNotFoundError(f'가짜 응답 파일이 없습니다: {fixtures_dir}')

    def __call__(self, text: str) -> ExtractionResult:
        s = squash(text)
        found: dict[str, tuple[int, ExtractedPosting]] = {}
        for pid, quotes, posting in self.known:
            if pid in found or not all(q in s for q in quotes):
                continue
            # 순서는 회사명 인용이 처음 나온 자리로 정한다. 모든 인용의 가장 앞 자리로 정하면
            # '수습 3개월'처럼 여러 공고에 흔한 구절 때문에 뒤 공고가 앞으로 끌려온다.
            company = posting.company.quote or ''
            found[pid] = (s.find(squash(company)), posting)
        ordered = sorted(found.values(), key=lambda item: item[0])
        return ExtractionResult(postings=[posting for _, posting in ordered])


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
