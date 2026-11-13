"""이미지 속 글자 옮겨 적기 (이미지·이미지 PDF 전용).

[왜 두 단계로 나누나]
이미지에서 바로 비교표를 뽑으면, LLM이 단 인용이 '원문에 글자 그대로 있는지' 확인할 글이 없다.
그래서 ① 이미지 → 글로 옮겨 적고(여기) ② 그 글로 기존 정리(normalize)를 한다.
②의 인용 검사는 ①이 옮겨 적은 글과 대조한다. 즉 이미지에서 나온 '확인'은
"옮겨 적은 글과 맞다"는 뜻이라 글자 PDF보다 약하다 → 화면이 원본과 비교하라고 안내한다.

[주의] 개발 중에는 실제 호출을 하지 않는다 (API 키 한도 100달러).
NORMALIZE_MODE=fixture면 FixtureTranscriber가 미리 받아 둔 글(tests/fixtures/transcribe/)을 쓴다.
이 글은 2026-11-14에 실제 Claude로 시연 이미지 7장을 한 번 읽어 저장한 것이다
(실측 $0.12, 숫자가 사람이 옮긴 글과 모두 같았다).

[비용 추정 — Sonnet 5.5, 입력 $2 / 출력 $10 (100만 토큰당)]
이미지 한 장 최대 약 4,800토큰 + 옮겨 적은 글 약 1,000~1,500토큰 → 한 장에 약 $0.025.
"""

import base64
import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import anthropic

from backend.inputs.errors import FileInputError, InputErrorCode, ReadError
from backend.inputs.image import VisionImage
from backend.normalize.llm import FALLBACK_BETA, MODEL

logger = logging.getLogger(__name__)

# 서버 전체에서 Claude 이미지 호출을 동시에 이만큼만 보낸다. 파일 여러 개 × 쪽 여러 개를 한꺼번에
# 보내면 요청 한도(429)에 걸려 전체가 실패할 수 있다.
_MAX_CONCURRENT_CALLS = threading.BoundedSemaphore(4)

TRANSCRIBE_PROMPT = """\
너는 채용 공고·합격 안내 이미지의 글자를 '그대로 옮겨 적는' 일만 한다. 요약·해석·계산은 하지 않는다.
옮겨 적은 글은 다음 단계 프로그램이 근무 조건을 찾고, 그 인용이 이 글에 있는지 검사하는 데 쓴다.

규칙:
1. 보이는 글자를 원문 그대로 적는다. 숫자·단위·쉼표·괄호·기호(~, →, ·)를 바꾸지 않는다.
   맞춤법을 고치거나 문장을 다듬지 않는다.
2. 읽는 순서는 사람이 읽는 순서(위→아래, 왼쪽→오른쪽)를 따른다.
3. 표는 한 행을 한 줄로 '항목: 내용'처럼 적는다.
   한 칸 안에 줄이 여러 개면 ' / '로 이어 한 줄로 쓴다.
4. 메일·웹 화면 캡처면 본문(메일 내용, 공고 내용)만 옮긴다.
   메일 목록, 메뉴, 검색창, 버튼 글자는 뺀다.
   단, 보낸 사람·받는 사람·날짜·제목·첨부 파일 이름은 적는다.
5. 글자가 아닌 그림(지도, 노선도, 로고)은 [그림: 짧은 설명] 한 줄로만 적는다.
6. 흐리거나 잘려서 확실히 읽을 수 없는 글자는 추측하지 말고 [읽을 수 없음]으로 적는다.
   특히 금액·날짜 숫자는 확실할 때만 적는다.
7. 옮겨 적은 글만 출력한다. 앞뒤 설명을 붙이지 않는다. 글자가 하나도 없으면 빈 답을 한다.
"""


@dataclass(frozen=True)
class ImageSource:
    """옮겨 적을 파일 하나.

    sha256: 원본 파일 바이트의 지문 (가짜 응답 모드가 파일을 알아보는 데 쓴다).
    """

    name: str
    sha256: str
    images: list[VisionImage]


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Transcriber(Protocol):
    """이미지 → 옮겨 적은 글. 실제로는 ClaudeTranscriber, 개발·테스트에서는 가짜."""

    def __call__(self, source: ImageSource) -> str: ...


class ClaudeTranscriber:
    """Claude(Sonnet) 이미지 인식으로 글자를 옮겨 적는다. 키는 SDK가 환경 변수에서 직접 읽는다."""

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            # 한 장에 보통 10~20초. 오래 걸리면 한 번만 다시 시도하고 끝낸다
            # (기본값 10분×3회면 사용자는 화면에서 멈춘 것처럼 기다리게 된다).
            self._client = anthropic.Anthropic(timeout=90, max_retries=1)
        return self._client

    def __call__(self, source: ImageSource) -> str:
        """이미지(쪽·조각)마다 따로 부른다. 여러 장을 한 번에 보내면 옮겨 적은 글이 길어져
        max_tokens에서 잘리고 시간도 오래 걸린다. 장마다 부르면 동시에 보낼 수 있다."""
        if not source.images:
            return ''
        if len(source.images) == 1:
            return self._one(source.images[0])
        with ThreadPoolExecutor(max_workers=min(4, len(source.images))) as pool:
            texts = list(pool.map(self._one, source.images))
        return '\n\n'.join(
            f'--- {i}쪽 ---\n{text}' for i, text in enumerate(texts, start=1) if text
        )

    def _one(self, img: VisionImage) -> str:
        content = [
            {
                'type': 'image',
                'source': {
                    'type': 'base64',
                    'media_type': img.media_type,
                    'data': base64.standard_b64encode(img.data).decode('ascii'),
                },
            },
            {'type': 'text', 'text': '이 이미지의 글자를 규칙대로 옮겨 적어 주세요.'},
        ]
        try:
            with _MAX_CONCURRENT_CALLS:
                response = self.client.beta.messages.create(
                    model=MODEL,
                    max_tokens=8000,
                    betas=[FALLBACK_BETA],
                    fallbacks='default',
                    # 옮겨 적기는 추론이 거의 필요 없다 → 가장 낮은 단계로 비용·시간을 줄인다.
                    output_config={'effort': 'low'},
                    system=TRANSCRIBE_PROMPT,
                    messages=[{'role': 'user', 'content': content}],
                )
        except anthropic.RateLimitError as e:
            logger.warning('Claude 요청 한도 초과 (status=%s)', e.status_code)
            raise ReadError(
                '요청이 많아 이미지를 읽지 못했습니다. 잠시 후 다시 시도해 주세요.'
            ) from e
        except anthropic.APIStatusError as e:
            logger.error('Claude API 오류 (status=%s)', e.status_code)
            raise ReadError('이미지를 읽는 중 문제가 생겼습니다.') from e
        except anthropic.APIConnectionError as e:
            logger.error('Claude API 연결 실패')
            raise ReadError('이미지 읽기 서버에 연결하지 못했습니다.') from e
        except anthropic.AnthropicError as e:
            logger.error('Claude 클라이언트 오류: %s', type(e).__name__)
            raise ReadError('이미지를 읽는 중 문제가 생겼습니다.') from e

        if response.stop_reason == 'refusal':
            raise ReadError(
                '이 이미지는 읽을 수 없습니다. 공고 내용이 보이는 이미지를 넣어 주세요.'
            )
        if response.stop_reason == 'max_tokens':
            raise ReadError(
                '이미지의 글이 너무 많아 다 읽지 못했습니다. 공고 부분만 잘라 넣어 주세요.'
            )
        return ''.join(b.text for b in response.content if b.type == 'text').strip()


# backend/tests/fixtures/transcribe  (이 파일: backend/src/backend/inputs/transcribe.py)
TRANSCRIPTS_DIR = Path(__file__).resolve().parents[3] / 'tests' / 'fixtures' / 'transcribe'


class FixtureTranscriber:
    """시연 파일이면 실제 Claude가 옮겨 적어 저장해 둔 글을 돌려준다 (비용 0원).

    manifest.json: {"원본 파일 이름": {"sha256": "...", "transcript": "옮겨 적은 글 파일"}}
    파일 이름이 아니라 내용(sha256)으로 알아본다 → 이름을 바꿔 올려도 된다.
    """

    def __init__(self, transcripts_dir: Path = TRANSCRIPTS_DIR) -> None:
        manifest = json.loads((transcripts_dir / 'manifest.json').read_text(encoding='utf-8'))
        self.known: dict[str, str] = {
            entry['sha256']: (transcripts_dir / entry['transcript']).read_text(encoding='utf-8')
            for entry in manifest.values()
        }

    def __call__(self, source: ImageSource) -> str:
        text = self.known.get(source.sha256)
        if text is None:
            raise FileInputError(
                InputErrorCode.DEMO_FILE_ONLY,
                '시연 모드에서는 준비된 시연 파일의 이미지만 읽을 수 있어요. '
                'PDF(글자 복사 가능)·엑셀·CSV는 그대로 읽을 수 있어요.',
                source.name,
            )
        return text
