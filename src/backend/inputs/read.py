"""올린 파일들 → 비교표 정리(normalize)에 넣을 글. API가 그래프를 돌리기 전에 부른다.

[왜 그래프 밖(API)에서 읽나]
- 파일 원본을 그래프 상태(SQLite 체크포인트)에 넣지 않는다 → 원본이 서버에 남지 않는다.
- 읽다가 실패하면 세션이 아예 만들어지지 않는다 → 같은 파일 글이 두 번 쌓이지 않는다.
- 이미지 글은 원본 지문(sha256)으로 기억해 두어, 같은 이미지를 다시 올려도 다시 돈을 쓰지 않는다.

[형식별 읽는 곳 — 이슈 #12의 read_image / read_pdf / read_sheet]
  image → image.prepare_image → transcribe (Claude 이미지 인식, 비용)
  pdf   → pdf.read_pdf (글자 쪽 0원, 그림 쪽만 이미지 인식)
  sheet → sheet.read_xlsx / read_xls / read_csv (0원)

[글 모양] 파일마다 '--- 파일 N ---' 머리줄을 붙인다. 파일 이름은 글에 넣지 않는다:
  이름이 '연봉 5000만원.png'이면 그 이름이 인용 검사를 통과해 버리기 때문이다.
"""

import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from backend.inputs import detect, pdf, sheet
from backend.inputs.errors import FileInputError, InputErrorCode
from backend.inputs.image import VisionImage, prepare_image
from backend.inputs.limits import (
    DEFAULT_VISION_DAILY_PAGE_LIMIT,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TEXT_CHARS,
    MAX_TOTAL_BYTES,
    MAX_VISION_PAGES,
    MB,
    TOTAL_TOO_LARGE_MESSAGE,
)
from backend.inputs.transcribe import ImageSource, Transcriber, file_sha256

UNREADABLE_MARK = '[읽을 수 없음]'
KST = timezone(timedelta(hours=9))

# PyMuPDF는 여러 스레드에서 동시에 쓰면 안 된다. 그림 풀기·PDF 읽기를 한 번에 하나씩만 한다
# (라즈베리파이 메모리도 아낀다). 엑셀·CSV는 이 잠금 없이 읽는다.
_DECODE_LOCK = threading.Lock()

InputKind = Literal['pdf', 'sheet', 'image']
ReadMethod = Literal['text', 'vision', 'ocr', 'mixed']
SHEET_READERS = {'xlsx': sheet.read_xlsx, 'xls': sheet.read_xls, 'csv': sheet.read_csv}


@dataclass(frozen=True)
class UploadedFile:
    name: str
    data: bytes


@dataclass
class ReadResult:
    """파일 하나를 읽은 결과. 필드 이름은 API의 InputSummary와 같다 (그대로 옮겨 담는다)."""

    index: int  # 1부터
    name: str
    kind: InputKind
    method: ReadMethod
    pages: int | None = None  # PDF 쪽 수
    vision_pages: int = 0  # 이미지 인식으로 읽은 장 수
    unreadable: int = 0  # 옮겨 적다가 [읽을 수 없음]으로 남긴 곳 수
    ignored_images: int = 0  # PDF 글자 쪽 안에서 읽지 않은 큰 그림 수
    plain_text: str = ''  # 코드가 꺼낸 글 (글자 PDF·엑셀·CSV)
    image_text: str = ''  # 이미지에서 옮겨 적은 글 (이미지·그림 쪽·OCR 글자)


@dataclass
class _Pending:
    """읽는 중인 파일: 글자 부분은 이미 꺼냈고, 이미지 부분은 옮겨 적기를 기다린다."""

    result: ReadResult
    sha256: str = ''  # 이미지 부분이 있을 때만 계산한다
    images: list[VisionImage] = field(default_factory=list)


class VisionBudgetError(Exception):
    """오늘 이미지 인식 한도를 다 썼다 (→ 503)."""


class DailyVisionBudget:
    """이미지 인식 장 수를 날짜(한국 시간)별로 센다. 서버를 다시 켜면 0부터 센다."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._day = ''
        self.used = 0
        self._lock = threading.Lock()

    def take(self, pages: int) -> None:
        with self._lock:
            today = datetime.now(KST).date().isoformat()
            if today != self._day:
                self._day, self.used = today, 0
            if self.used + pages > self.limit:
                raise VisionBudgetError(
                    '오늘 이미지 읽기 한도를 다 썼어요. 공고 본문을 붙여 넣거나 '
                    '글자를 복사할 수 있는 PDF·엑셀로 넣어 주세요.'
                )
            self.used += pages

    def give_back(self, pages: int) -> None:
        """읽기에 실패한 장 수를 돌려준다."""
        with self._lock:
            self.used = max(0, self.used - pages)


def daily_budget_from_env() -> DailyVisionBudget:
    raw = os.getenv('VISION_DAILY_PAGE_LIMIT', '').strip()
    try:
        limit = int(raw) if raw else DEFAULT_VISION_DAILY_PAGE_LIMIT
    except ValueError as e:
        raise ValueError(f'VISION_DAILY_PAGE_LIMIT는 정수여야 합니다 (받은 값: {raw!r})') from e
    if limit < 0:
        raise ValueError('VISION_DAILY_PAGE_LIMIT는 0 이상이어야 합니다')
    return DailyVisionBudget(limit)


def check_upload(files: list[UploadedFile]) -> None:
    """파일 수·크기 검사 (읽기 전에)."""
    if not files:
        raise FileInputError(InputErrorCode.FILE_MISSING, '공고 파일을 하나 이상 넣어 주세요.')
    if len(files) > MAX_FILES:
        raise FileInputError(
            InputErrorCode.FILE_TOO_MANY,
            f'파일은 한 번에 {MAX_FILES}개까지 넣을 수 있어요 (넣은 파일 {len(files)}개).',
        )
    for i, f in enumerate(files):
        if not f.data:
            raise FileInputError(InputErrorCode.FILE_EMPTY, '빈 파일이에요.', f.name)
        # 같은 파일을 두 번 넣으면 비교표에 같은 공고가 두 줄 생긴다 → 받지 않는다
        # (이름이 달라도 내용이 같으면 같은 파일)
        twin = next((g for g in files[:i] if g.data == f.data), None)
        if twin is not None:
            raise FileInputError(
                InputErrorCode.FILE_DUPLICATE,
                f'{twin.name}과(와) 같은 파일이에요. 하나만 넣어 주세요.',
                f.name,
            )
        if len(f.data) > MAX_FILE_BYTES:
            raise FileInputError(
                InputErrorCode.FILE_TOO_LARGE,
                f'파일 하나는 {MAX_FILE_BYTES // MB}MB까지 넣을 수 있어요.',
                f.name,
            )
    if sum(len(f.data) for f in files) > MAX_TOTAL_BYTES:
        raise FileInputError(InputErrorCode.FILE_TOO_LARGE, TOTAL_TOO_LARGE_MESSAGE)


class FileReader:
    """파일 읽기 담당. 서버에 하나만 만들어 두고 같이 쓴다 (옮겨 적은 글 기억, 하루 한도)."""

    CACHE_SIZE = 50

    def __init__(self, transcriber: Transcriber, budget: DailyVisionBudget | None = None) -> None:
        self.transcriber = transcriber
        self.budget = budget  # None이면 한도 없음 (가짜 응답 모드·테스트)
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_lock = threading.Lock()

    # ── 1단계: 코드로 읽기 (0원) ─────────────────────────────────

    def _extract(self, index: int, f: UploadedFile, vision_left: int) -> _Pending:
        """vision_left: 이 요청에서 아직 읽을 수 있는 이미지 장 수. 넘으면 그리기 전에 멈춘다."""
        kind = detect.detect_kind(f.name, f.data)
        if kind == 'image':
            with _DECODE_LOCK:
                images = prepare_image(f.data, f.name, max_tiles=vision_left)
            return _Pending(
                ReadResult(index, f.name, 'image', 'vision'), file_sha256(f.data), images
            )
        if kind == 'pdf':
            with _DECODE_LOCK:
                content = pdf.read_pdf(f.data, f.name, max_vision=vision_left)
            methods = {m for _, _, m in content.text_pages} | (
                {'vision'} if content.image_pages else set()
            )
            result = ReadResult(
                index,
                f.name,
                'pdf',
                methods.pop() if len(methods) == 1 else 'mixed',
                pages=content.page_count,
                ignored_images=content.ignored_images,
                plain_text='\n\n'.join(t for _, t, m in content.text_pages if m == 'text'),
                image_text='\n\n'.join(t for _, t, m in content.text_pages if m == 'ocr'),
            )
            images = [img for _, img in content.image_pages]
            return _Pending(result, file_sha256(f.data) if images else '', images)
        text = SHEET_READERS[kind](f.data, f.name)
        if not text.strip():
            raise FileInputError(InputErrorCode.FILE_NO_TEXT, '표에 내용이 없어요.', f.name)
        return _Pending(ReadResult(index, f.name, 'sheet', 'text', plain_text=text))

    # ── 2단계: 이미지 옮겨 적기 (비용, 같은 이미지는 기억해 둔 글) ────────

    def _cached(self, sha: str) -> str | None:
        with self._cache_lock:
            if sha in self._cache:
                self._cache.move_to_end(sha)
                return self._cache[sha]
        return None

    def _remember(self, sha: str, text: str) -> None:
        with self._cache_lock:
            self._cache[sha] = text
            self._cache.move_to_end(sha)
            while len(self._cache) > self.CACHE_SIZE:
                self._cache.popitem(last=False)

    def _run(self, p: _Pending) -> str:
        pages = len(p.images)
        if self.budget is not None:
            self.budget.take(pages)
        try:
            text = self.transcriber(ImageSource(p.result.name, p.sha256, p.images))
        except BaseException:
            # 읽지 못했으면 한도를 돌려준다 (실패가 반복돼도 하루 한도가 바닥나지 않게)
            if self.budget is not None:
                self.budget.give_back(pages)
            raise
        # 성공한 파일은 기억한다 → 다른 파일이 실패해서 다시 올려도 이 파일은 0원
        self._remember(p.sha256, text)
        return text

    def _transcribe_all(self, pending: list[_Pending]) -> None:
        # 같은 파일을 두 번 넣어도 한 번만 읽는다 (sha256이 같으면 같은 파일)
        unique = {p.sha256: p for p in pending if p.images}
        texts = {sha: self._cached(sha) for sha in unique}
        misses = [p for sha, p in unique.items() if texts[sha] is None]
        if misses:
            # 파일마다 Claude 호출이 따로라 동시에 보낸다 (5장을 차례로 하면 1분이 넘을 수 있다)
            with ThreadPoolExecutor(max_workers=min(4, len(misses))) as pool:
                for p, text in zip(misses, pool.map(self._run, misses), strict=True):
                    texts[p.sha256] = text
        for p in pending:
            if p.images:
                text = texts[p.sha256].strip()
                p.result.vision_pages = len(p.images)
                p.result.unreadable = text.count(UNREADABLE_MARK)
                p.result.image_text = '\n\n'.join(t for t in (p.result.image_text, text) if t)

    def read(self, files: list[UploadedFile], message: str = '') -> list[ReadResult]:
        """파일들을 읽는다. 사용자가 고칠 수 있는 문제면 FileInputError(→ 422),
        이미지 읽기 서비스 문제면 ReadError(→ 502), 하루 한도면 VisionBudgetError(→ 503)."""
        check_upload(files)
        pending: list[_Pending] = []
        vision_used = 0  # 이 요청에서 이미지로 읽을 장 수
        for i, f in enumerate(files, start=1):
            p = self._extract(i, f, MAX_VISION_PAGES - vision_used)
            vision_used += len(p.images)
            pending.append(p)
        self._transcribe_all(pending)
        results = [p.result for p in pending]
        for r in results:
            if not (r.plain_text.strip() or r.image_text.strip()):
                raise FileInputError(
                    InputErrorCode.FILE_NO_TEXT, '파일에서 글자를 찾지 못했어요.', r.name
                )
        # 그래프가 정리할 글과 똑같이 이어 붙여 센다 (응답의 input_text로 새 세션을 만들 때
        # 붙여 넣기 상한 MAX_MESSAGE_CHARS를 넘지 않게).
        total = len('\n'.join(graph_messages(message, results)[0]))
        if total > MAX_TEXT_CHARS:
            raise FileInputError(
                InputErrorCode.FILE_TOO_LONG,
                f'읽은 글이 모두 {total:,}자예요. {MAX_TEXT_CHARS:,}자까지 정리할 수 있어요. '
                '공고가 있는 부분만 넣어 주세요.',
            )
        return results


def graph_messages(message: str, results: list[ReadResult]) -> tuple[list[str], list[str]]:
    """그래프에 넣을 (messages, image_texts). image_texts는 messages 중 이미지에서 옮겨 적은 글.

    API·글자 수 검사·테스트가 모두 이 함수를 써서, 세는 글과 정리하는 글이 어긋나지 않게 한다.
    """
    messages = [message] if message else []
    image_texts = []
    for r in results:
        if r.plain_text.strip():
            messages.append(f'--- 파일 {r.index} ---\n{r.plain_text.strip()}')
        if r.image_text.strip():
            part = f'--- 파일 {r.index} (이미지에서 옮겨 적은 글) ---\n{r.image_text.strip()}'
            messages.append(part)
            image_texts.append(part)
    return messages, image_texts


def safe_name(name: str | None, index: int) -> str:
    """화면에 보여 줄 파일 이름. 경로를 떼고 길이를 줄인다 (이름은 정리할 글에 넣지 않는다)."""
    base = (name or '').replace('\\', '/').rsplit('/', 1)[-1].strip()
    base = ''.join(c for c in base if c.isprintable())
    if len(base) > 120:  # 확장자는 남긴다 (형식 판별에 쓴다)
        stem, dot, ext = base.rpartition('.')
        base = stem[: 120 - len(ext) - 1] + dot + ext if dot and len(ext) <= 10 else base[:120]
    return base or f'파일 {index}'
