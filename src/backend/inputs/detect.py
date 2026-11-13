"""파일 종류 판별. 확장자보다 파일 앞부분의 '매직 바이트'(형식마다 정해진 시작 글자)를 믿는다.

- .png인데 실제로는 JPG인 파일, 크롬이 저장한 .jfif 같은 경우를 그대로 받는다
  (같은 이미지 종류 안에서는 내용을 믿는다).
- 받지 않는 형식(GIF·HEIC·한글·워드 등)은 형식별로 무엇으로 바꾸면 되는지 알려 준다.
"""

from pathlib import PurePath
from typing import Literal

from backend.inputs.errors import FileInputError, InputErrorCode

FileKind = Literal['pdf', 'xlsx', 'xls', 'csv', 'image']

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.jfif', '.webp'}
ACCEPTED = 'PNG·JPG·WEBP 이미지, PDF, 엑셀(XLSX·XLS), CSV'

_OLE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'  # 옛 오피스 형식 (xls, doc, hwp 등)
_HEIF_BRANDS = {b'heic', b'heix', b'hevc', b'hevx', b'mif1', b'msf1', b'avif'}


def _extension(name: str) -> str:
    return PurePath(name).suffix.lower()


def _is_image(data: bytes) -> bool:
    return (
        data.startswith(b'\x89PNG\r\n\x1a\n')
        or data.startswith(b'\xff\xd8\xff')
        or (data[:4] == b'RIFF' and data[8:12] == b'WEBP')
    )


def _looks_like_markup(data: bytes) -> bool:
    """웹 사이트가 '.xls'라고 내려주는 HTML·XML 표."""
    head = data[:512].lstrip(b'\xef\xbb\xbf \t\r\n').lower()
    return head.startswith((b'<', b'\xff\xfe<', b'\xfe\xff\x00<'))


def _unsupported(name: str, why: str) -> FileInputError:
    return FileInputError(InputErrorCode.FILE_TYPE_UNSUPPORTED, why, name)


def detect_kind(name: str, data: bytes) -> FileKind:
    ext = _extension(name)
    if _is_image(data):
        return 'image'
    if b'%PDF-' in data[:1024]:
        return 'pdf'
    if data.startswith(b'PK\x03\x04'):
        # xlsx·docx·hwpx 모두 zip이다. 안에 엑셀 통합 문서가 있어야 xlsx로 본다.
        if b'xl/workbook' in data[:65536] or ext == '.xlsx':
            return 'xlsx'
        raise _unsupported(name, f'이 파일은 읽을 수 없어요. {ACCEPTED} 파일로 넣어 주세요.')
    if data.startswith(_OLE):
        if ext == '.xls':
            return 'xls'
        if ext == '.xlsx':  # 암호를 건 xlsx는 zip이 아니라 옛 형식 상자에 담긴다
            raise FileInputError(
                InputErrorCode.FILE_UNREADABLE,
                '암호가 걸린 엑셀은 읽을 수 없어요. 암호를 풀고 저장해서 넣어 주세요.',
                name,
            )
        if ext == '.hwp':
            raise _unsupported(name, '한글(HWP) 파일은 읽을 수 없어요. PDF로 저장해서 넣어 주세요.')
        raise _unsupported(name, f'이 파일은 읽을 수 없어요. {ACCEPTED} 파일로 넣어 주세요.')
    if data.startswith(b'GIF8'):
        raise _unsupported(name, 'GIF 이미지는 읽을 수 없어요. PNG나 JPG로 저장해서 넣어 주세요.')
    if data[4:8] == b'ftyp' and data[8:12] in _HEIF_BRANDS:
        raise _unsupported(
            name,
            '아이폰 사진(HEIC) 형식은 읽을 수 없어요. 화면 캡처나 JPG로 바꿔서 넣어 주세요.',
        )
    if ext == '.xls' and _looks_like_markup(data):
        raise _unsupported(
            name,
            '웹 사이트에서 받은 엑셀 파일이라 실제로는 웹 페이지 형식이에요. '
            '엑셀에서 열어 .xlsx로 다시 저장해서 넣어 주세요.',
        )
    if ext == '.csv':
        return 'csv'
    if ext in IMAGE_EXTENSIONS or ext in ('.pdf', '.xlsx', '.xls'):
        raise FileInputError(
            InputErrorCode.FILE_UNREADABLE,
            '파일 확장자와 실제 내용이 달라 읽을 수 없어요. '
            '파일이 손상되지 않았는지 확인해 주세요.',
            name,
        )
    raise _unsupported(name, f'이 형식은 읽을 수 없어요. {ACCEPTED} 파일로 넣어 주세요.')
