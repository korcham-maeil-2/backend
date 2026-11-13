"""이미지 → Claude에 보낼 이미지 (Pillow). 크기를 확인하고, 크면 줄이고, 길면 자른다.

- 받는 형식: PNG, JPEG, WEBP (GIF·HEIC 등은 detect.py에서 먼저 막는다)
- 픽셀 수가 상한을 넘으면 풀기 전에 422 (작은 파일이 엄청 큰 그림으로 풀리는 '압축 폭탄' 막기)
- 휴대폰 사진의 회전 정보(EXIF)를 반영해 바로 세운다
- 긴 변 2576px 넘으면 줄인다 (Claude가 어차피 그 크기로 줄여 본다)
- 세로로 아주 긴 캡처(스크롤 캡처)는 통째로 줄이면 글자가 뭉개져서, 겹치게 잘라 여러 장으로 보낸다
- 항상 새로 저장해서 보낸다: 위치 정보 같은 메타데이터가 바깥으로 나가지 않는다
"""

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from backend.inputs.errors import FileInputError, InputErrorCode
from backend.inputs.limits import (
    MAX_IMAGE_PIXELS,
    MAX_VISION_PAGES,
    TALL_IMAGE_RATIO,
    VISION_MAX_EDGE,
    VISION_MAX_IMAGE_BYTES,
)

# Pillow 자체 경고 기준도 우리 상한에 맞춘다 (상한의 2배를 넘으면 Pillow가 직접 예외를 낸다).
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# MPO: 휴대폰 카메라가 저장하는 JPG(여러 장이 든 JPG). 첫 장만 쓴다.
ALLOWED_FORMATS = {'PNG', 'JPEG', 'MPO', 'WEBP'}
TILE_OVERLAP = 0.08  # 자른 조각끼리 겹치는 비율 (경계에 걸린 줄이 잘리지 않게)


@dataclass(frozen=True)
class VisionImage:
    """Claude에 그대로 보낼 이미지 한 장."""

    media_type: str  # 'image/png' 또는 'image/jpeg'
    data: bytes


def _flatten(img: Image.Image) -> Image.Image:
    """투명한 부분은 흰 바탕으로 채운다 (글자 이미지가 검은 바탕이 되지 않게)."""
    if img.mode in ('RGB', 'L'):
        return img
    rgba = img.convert('RGBA')
    flat = Image.new('RGB', rgba.size, 'white')
    flat.paste(rgba, mask=rgba.getchannel('A'))
    return flat


def encode_for_vision(img: Image.Image) -> VisionImage:
    """긴 변을 2576px 이하로 줄여 PNG로 저장한다. 크면 JPEG로, 그래도 크면 더 줄인다.

    img는 이 함수가 바꿔도 되는 그림이어야 한다 (복사하지 않고 그 자리에서 줄인다: 메모리 절약).
    """
    img = _flatten(img)
    if max(img.size) > VISION_MAX_EDGE:
        img.thumbnail((VISION_MAX_EDGE, VISION_MAX_EDGE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    if buf.tell() <= VISION_MAX_IMAGE_BYTES:
        return VisionImage('image/png', buf.getvalue())
    # PNG가 크면 사진처럼 복잡한 그림이다 → 이후로는 JPEG만 해 본다 (PNG를 다시 만들어 봐야
    # 더 작은 크기에서도 JPEG보다 크다). 그래도 크면 조금씩 더 줄인다.
    img = img.convert('RGB')
    while True:
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=85)
        if buf.tell() <= VISION_MAX_IMAGE_BYTES:
            return VisionImage('image/jpeg', buf.getvalue())
        edge = int(max(img.size) * 0.8)
        img.thumbnail((edge, edge), Image.Resampling.LANCZOS)


def split_tall(img: Image.Image) -> list[Image.Image]:
    """세로가 가로의 TALL_IMAGE_RATIO배를 넘으면 가로:세로 1:2 조각으로 겹치게 자른다."""
    width, height = img.size
    if height <= width * TALL_IMAGE_RATIO:
        return [img]
    tile = width * 2
    step = int(tile * (1 - TILE_OVERLAP))
    count = math.ceil((height - tile) / step) + 1
    tops = [min(i * step, height - tile) for i in range(count)]
    return [img.crop((0, top, width, top + tile)) for top in tops]


def prepare_image(data: bytes, name: str, max_tiles: int = MAX_VISION_PAGES) -> list[VisionImage]:
    """이미지 파일 → Claude에 보낼 이미지들 (보통 1장, 긴 캡처는 여러 장).

    max_tiles: 이 요청에서 아직 읽을 수 있는 장 수. 넘으면 저장(비싼 일)하기 전에 422.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            # Image.open은 머리글만 읽는다 → 실제로 풀기 전에 크기부터 본다
            width, height = img.size
            if width * height > MAX_IMAGE_PIXELS:
                raise FileInputError(
                    InputErrorCode.FILE_TOO_LARGE,
                    f'이미지가 너무 커요 ({width}×{height}). '
                    '화면 캡처 크기로 줄여서 다시 넣어 주세요.',
                    name,
                )
            if img.format not in ALLOWED_FORMATS:
                raise FileInputError(
                    InputErrorCode.FILE_TYPE_UNSUPPORTED,
                    'PNG·JPG·WEBP 이미지만 읽을 수 있어요.',
                    name,
                )
            if img.format in ('JPEG', 'MPO'):
                # 큰 사진은 처음부터 작게 푼다 (메모리 절약). 필요한 크기보다 작아지지는 않는다.
                img.draft('RGB', (VISION_MAX_EDGE, VISION_MAX_EDGE))
            img.load()  # 움직이는 WEBP는 첫 장면만 쓴다
            ImageOps.exif_transpose(img, in_place=True)  # 복사하지 않고 그 자리에서 세운다
            tiles = split_tall(img)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as e:
        raise FileInputError(
            InputErrorCode.FILE_UNREADABLE,
            '이미지를 열지 못했어요. 파일이 손상되지 않았는지 확인해 주세요.',
            name,
        ) from e
    if len(tiles) > max_tiles:
        raise FileInputError(
            InputErrorCode.FILE_TOO_LONG,
            '캡처가 너무 길거나 이미지가 너무 많아요. 근무 조건 부분만 나눠서 넣어 주세요.',
            name,
        )
    return [encode_for_vision(t) for t in tiles]
