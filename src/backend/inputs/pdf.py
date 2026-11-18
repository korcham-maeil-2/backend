"""PDF → 글 (PyMuPDF). 쪽마다 따로 판단한다.

| 쪽 모양                                   | 처리                         | 비용 |
|-------------------------------------------|------------------------------|------|
| 글자가 충분히 있는 쪽                      | 글자를 그대로 꺼낸다          | 0원  |
| 그림이 쪽을 덮고 글자가 적음(스캔+쪽 번호) | 그림으로 그려 이미지 인식     | 있음 |
| 그림이 쪽을 덮고 글자도 많음(OCR 된 스캔)  | 숨은 OCR 글자를 쓴다 (ocr)    | 0원  |
| 글자가 깨짐(글꼴 문제)                     | 그림으로 그려 이미지 인식     | 있음 |
| 아무것도 없는 빈 쪽                        | 건너뛴다                      | -    |

OCR 글자는 스캐너가 기계로 읽은 것이라 이미지 인식과 똑같이 '이미지에서 읽음'으로 표시한다.
화면에 안 보이는 흰 글씨도 get_text에는 나온다 (알려진 한계).
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

import pymupdf
from PIL import Image

from backend.inputs.errors import FileInputError, InputErrorCode
from backend.inputs.image import VisionImage, encode_for_vision
from backend.inputs.limits import MAX_PDF_PAGES, MAX_VISION_PAGES, VISION_MAX_EDGE

# 공백을 뺀 글자가 이보다 적으면 '글자 없는 쪽'으로 본다.
MIN_TEXT_CHARS = 20
# 스캔 쪽인데 글자가 이보다 적으면 쪽 번호·워터마크로 보고 이미지 인식으로 읽는다.
SCAN_TEXT_CHARS = 200
# 그림이 쪽 넓이의 이 비율 이상을 덮으면 스캔 쪽으로 본다.
SCAN_COVERAGE = 0.5
# 정상 글자(한글·영문·숫자·문장부호) 비율이 이보다 낮으면 글꼴이 깨진 쪽으로 본다.
MIN_CLEAN_RATIO = 0.8
# 글자 쪽 안의 그림이 쪽의 이 비율 이상이면 '읽지 않은 그림'으로 알려 준다
# (표를 캡처해 붙인 공고 등).
IGNORED_IMAGE_COVERAGE = 0.15
# 그림으로 그릴 때 해상도 상한 (A4 기준 약 2339px — 긴 변 2576px 안쪽)
MAX_RENDER_DPI = 200

PageMethod = Literal['text', 'ocr', 'vision']


@dataclass
class PdfContent:
    page_count: int
    # (쪽 번호, 글, text|ocr) — 쪽 번호는 1부터
    text_pages: list[tuple[int, str, PageMethod]] = field(default_factory=list)
    # (쪽 번호, 이미지) — 이미지 인식으로 읽을 쪽
    image_pages: list[tuple[int, VisionImage]] = field(default_factory=list)
    # 글자 쪽이라 글자만 읽고, 그 안의 큰 그림은 읽지 않은 쪽 수 (화면이 알려 준다)
    ignored_images: int = 0


def _clean_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 1.0
    ok = sum(
        1
        for c in chars
        if c.isascii()
        or '가' <= c <= '힣'  # 한글 음절
        or unicodedata.category(c)[0] in ('P', 'S', 'N')  # 문장부호·기호·숫자
    )
    return ok / len(chars)


def _image_coverage(page: pymupdf.Page) -> float:
    area = abs(page.rect) or 1
    covered = sum(abs(pymupdf.Rect(info['bbox']) & page.rect) for info in page.get_image_info())
    return min(covered / area, 1.0)


def _render(page: pymupdf.Page) -> VisionImage:
    rect = page.rect
    zoom = min(VISION_MAX_EDGE / max(rect.width, rect.height, 1), MAX_RENDER_DPI / 72)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes('RGB', (pix.width, pix.height), pix.samples_mv)  # 복사 없이 읽기
    return encode_for_vision(img)


def _classify(text: str, coverage: float) -> PageMethod | None:
    """쪽 글자와 그림이 덮은 비율로 읽는 방법을 정한다. None이면 빈 쪽."""
    chars = len(''.join(text.split()))
    if chars >= MIN_TEXT_CHARS and _clean_ratio(text) < MIN_CLEAN_RATIO:
        return 'vision'  # 글꼴이 깨져 엉뚱한 글자가 나오는 쪽
    if coverage >= SCAN_COVERAGE:
        return 'ocr' if chars >= SCAN_TEXT_CHARS else 'vision'
    if chars >= MIN_TEXT_CHARS:
        return 'text'
    if coverage > 0:
        return 'vision'
    return 'text' if chars else None


_SPACES = re.compile(r'[ \t\u00a0\u3000]{2,}')
_BLANK_LINES = re.compile(r'\n[ \t]*(?:\n[ \t]*)+')
EDGE_LINES = 2  # 쪽 위·아래 몇 줄을 인쇄 머리줄 후보로 볼지


def tidy_page(text: str) -> str:
    """쪽 글 정리: 이어진 공백을 하나로, 줄 끝 공백과 빈 줄을 뺀다 (이슈 #52).

    get_text(sort=True)는 화면 배치대로 줄을 맞추려고 공백을 넣는다. 잡코리아 공고를 'PDF로
    저장'한 파일은 글의 66%가 공백이었다 (실제 22,300자 → 62,277자로 세어 3만 자 제한에 걸림).
    인용 대조(checks.squash)는 공백을 무시하므로 정확도에는 영향이 없다.
    """
    text = _SPACES.sub(' ', text)
    text = '\n'.join(line.rstrip() for line in text.split('\n'))
    return _BLANK_LINES.sub('\n', text).strip()


def drop_print_margins(pages: list[str]) -> list[str]:
    """브라우저 인쇄가 쪽마다 위·아래에 넣는 줄(인쇄 날짜·페이지 제목·주소)을 뺀다.

    쪽 위·아래 EDGE_LINES줄 가운데 3쪽 이상, 그리고 절반 이상의 쪽에 똑같이 나오는 줄만 뺀다
    (본문 속 '자격요건' 같은 되풀이 제목은 위·아래 끝에 오지 않아 남는다).
    """
    if len(pages) < 3:
        return pages
    seen: dict[str, int] = {}
    for page in pages:
        lines = [line.strip() for line in page.split('\n') if line.strip()]
        for line in set(lines[:EDGE_LINES] + lines[-EDGE_LINES:]):
            seen[line] = seen.get(line, 0) + 1
    margins = {line for line, n in seen.items() if n >= max(3, (len(pages) + 1) // 2)}
    if not margins:
        return pages
    return ['\n'.join(ln for ln in p.split('\n') if ln.strip() not in margins) for p in pages]


def read_pdf(data: bytes, name: str, max_vision: int = MAX_VISION_PAGES) -> PdfContent:
    """max_vision: 이 요청에서 아직 이미지로 읽을 수 있는 쪽 수. 넘으면 그리기 전에 422."""
    try:
        doc = pymupdf.open(stream=data, filetype='pdf')
    except Exception as e:  # PyMuPDF는 손상 정도에 따라 여러 예외를 낸다
        raise FileInputError(
            InputErrorCode.FILE_UNREADABLE,
            'PDF를 열지 못했어요. 파일이 손상되지 않았는지 확인해 주세요.',
            name,
        ) from e
    with doc:
        # 여는 데 암호가 필요한 PDF (편집만 막은 PDF는 빈 암호로 열린다)
        if doc.needs_pass and not doc.authenticate(''):
            raise FileInputError(
                InputErrorCode.FILE_UNREADABLE,
                '암호가 걸린 PDF는 읽을 수 없어요. 암호를 푼 PDF로 다시 저장해 넣어 주세요.',
                name,
            )
        if doc.page_count == 0:
            raise FileInputError(InputErrorCode.FILE_NO_TEXT, 'PDF에 쪽이 없어요.', name)
        if doc.page_count > MAX_PDF_PAGES:
            raise FileInputError(
                InputErrorCode.FILE_TOO_LONG,
                f'PDF가 {doc.page_count}쪽이에요. 공고가 있는 쪽만 {MAX_PDF_PAGES}쪽 이내로 '
                '저장해서 넣어 주세요.',
                name,
            )
        content = PdfContent(page_count=doc.page_count)
        try:
            # 1) 쪽마다 판별만 먼저 한다 (그림으로 그리는 건 비싸서, 장 수 상한을 본 뒤에)
            plan = []
            for page in doc:
                text = tidy_page(page.get_text(sort=True))
                coverage = _image_coverage(page)
                method = _classify(text, coverage)
                plan.append((page.number, text, method))
                if method == 'text' and coverage >= IGNORED_IMAGE_COVERAGE:
                    content.ignored_images += 1  # 글자 쪽 안의 큰 그림은 읽지 않는다
            vision = sum(1 for _, _, m in plan if m == 'vision')
            if vision > max_vision:
                raise FileInputError(
                    InputErrorCode.FILE_TOO_LONG,
                    f'글자를 복사할 수 없는 쪽이 {vision}쪽이에요. '
                    f'이미지는 한 번에 모두 {MAX_VISION_PAGES}장까지 읽을 수 있어요'
                    + (' (앞의 파일 이미지를 합쳐서)' if max_vision < MAX_VISION_PAGES else '')
                    + '. 공고가 있는 쪽만 남겨서 넣어 주세요.',
                    name,
                )
            # 2) 그림 쪽만 그린다
            # 글자로 읽는 쪽들에서 인쇄 머리줄을 뺀다 (판별은 위에서 이미 끝났다)
            kept = drop_print_margins([text for _, text, m in plan if m not in (None, 'vision')])
            texts = iter(kept)
            plan = [(i, next(texts) if m not in (None, 'vision') else t, m) for i, t, m in plan]
            for index, text, method in plan:
                if method == 'vision':
                    content.image_pages.append((index + 1, _render(doc[index])))
                elif method is not None:
                    content.text_pages.append((index + 1, text, method))
        except FileInputError:
            raise
        except Exception as e:
            raise FileInputError(
                InputErrorCode.FILE_UNREADABLE,
                'PDF 내용을 읽다가 문제가 생겼어요. 다른 프로그램에서 PDF로 다시 저장해 주세요.',
                name,
            ) from e
    if not content.text_pages and not content.image_pages:
        raise FileInputError(InputErrorCode.FILE_NO_TEXT, 'PDF에 읽을 내용이 없어요.', name)
    return content
