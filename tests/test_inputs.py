"""파일 입력 (#12) 테스트 — 이미지·PDF·엑셀·CSV를 넣어도 붙여 넣은 글과 같은 비교표가 나오는지.

[이 테스트가 확인하는 것 / 못 하는 것]
  - 확인함: 파일 읽기 코드(형식 판별, PDF 쪽 판별, 엑셀·CSV 인코딩, 이미지 줄이기·자르기),
           상한(422), 업로드 API, 가짜 응답 모드 연결, '이미지에서 읽음' 표시.
  - 이미지 글(tests/fixtures/transcribe/)은 2026-11-14에 실제 Claude로 한 번 읽어 저장한 것이다.
    그래서 시연 이미지 7장은 '실제로 이렇게 읽힌다'까지 확인한 셈이다. 다만 Claude가 매번 똑같이
    읽는다는 보장은 없고, 이미지 글로 정리(normalize)하는 단계는 여전히 손으로 쓴 응답을 쓴다.
"""

import functools
import io
import json
import struct
import zipfile
import zlib
from types import SimpleNamespace

import anthropic
import httpx
import openpyxl
import pymupdf
import pytest
from conftest import DEMO_POSTINGS, ROOT, scenario_expected, scenario_text
from fastapi.testclient import TestClient
from PIL import Image
from test_normalize import KNOWN_DIFFERENCES, actual, expected_pairs

from backend.claude import MODEL
from backend.inputs import detect, sheet
from backend.inputs.errors import FileInputError, InputErrorCode, ReadError
from backend.inputs.image import encode_for_vision, prepare_image, split_tall
from backend.inputs.limits import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_IMAGE_PIXELS,
    MAX_PDF_PAGES,
    MAX_REQUEST_BYTES,
    MAX_TEXT_CHARS,
    MAX_VISION_PAGES,
    VISION_MAX_EDGE,
    VISION_MAX_IMAGE_BYTES,
)
from backend.inputs.pdf import read_pdf
from backend.inputs.read import (
    DailyVisionBudget,
    FileReader,
    UploadedFile,
    VisionBudgetError,
    check_upload,
    graph_messages,
    safe_name,
)
from backend.inputs.transcribe import (
    TRANSCRIPTS_DIR,
    ClaudeTranscriber,
    FixtureTranscriber,
    ImageSource,
    file_sha256,
)
from backend.main import create_app
from backend.normalize import FixtureExtractor, build_rows, checks

# ── 준비물 ───────────────────────────────────────────────────────

# 시연 파일 → 어느 공고인지. up_seoul은 메일 캡처 하나, stay_ulsan은 엑셀·CSV가 없다.
DEMO_FILES = {
    'down_naju': [
        'down_naju.png',
        'down_naju.xlsx',
        'down_naju_euckr.csv',
        'down_naju_image.pdf',
        'down_naju_text.pdf',
        'down_naju_utf8.csv',
    ],
    'near_pangyo': [
        'near_pangyo.png',
        'near_pangyo.xlsx',
        'near_pangyo_euckr.csv',
        'near_pangyo_image.pdf',
        'near_pangyo_text.pdf',
        'near_pangyo_utf8.csv',
    ],
    'stay_ulsan': ['stay_ulsan.png', 'stay_ulsan_image.pdf', 'stay_ulsan_text.pdf'],
    'up_seoul': ['up_seoul_offer_mail.png'],
}
DEMO_CASES = [(pid, name) for pid, names in DEMO_FILES.items() for name in names]
IMAGE_FILES = {n for _, n in DEMO_CASES if n.endswith('.png') or n.endswith('_image.pdf')}


def demo(name: str) -> UploadedFile:
    path = next(DEMO_POSTINGS.glob(f'*/{name}'))
    return UploadedFile(name, path.read_bytes())


class CountingTranscriber:
    """가짜 이미지 읽기: 정해 둔 글을 돌려주고, 몇 번 불렸는지 센다."""

    def __init__(self, text: str = '회사명: (가상) 테스트(주)') -> None:
        self.text = text
        self.calls: list[ImageSource] = []

    def __call__(self, source: ImageSource) -> str:
        self.calls.append(source)
        return self.text


def png_bytes(width: int, height: int, color='white', mode='RGB') -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (width, height), color).save(buf, format='PNG')
    return buf.getvalue()


def pdf_bytes(pages: list[str | None], **save_kwargs) -> bytes:
    """쪽마다 글(str) 또는 글 없는 그림 쪽(None)으로 PDF를 만든다."""
    doc = pymupdf.open()
    picture = png_bytes(40, 56, 'lightgray')
    for content in pages:
        page = doc.new_page(width=200, height=280)  # 작은 쪽: 그림으로 그리는 시간을 줄인다
        if content is None:
            page.insert_image(page.rect, stream=picture)
        else:
            page.insert_text((72, 72), content)
    data = doc.tobytes(**save_kwargs)
    doc.close()
    return data


def xlsx_bytes(rows: list[list[object]], hidden: list[list[object]] | None = None) -> bytes:
    book = openpyxl.Workbook()
    ws = book.active
    for row in rows:
        ws.append(row)
    if hidden is not None:
        secret = book.create_sheet('숨김')
        for row in hidden:
            secret.append(row)
        secret.sheet_state = 'hidden'
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def upload(c: TestClient, files: list[UploadedFile], message: str = ''):
    return c.post(
        '/sessions/uploads',
        files=[('files', (f.name, f.data, 'application/octet-stream')) for f in files],
        data={'message': message} if message else None,
    )


def app_client(tmp_path, transcriber=None) -> TestClient:
    """가짜 응답 모드(손으로 쓴 정리 응답 + 미리 받아 둔 이미지 글)로 앱을 띄운다."""
    return TestClient(
        create_app(
            str(tmp_path / 'cp.sqlite'),
            extractor=FixtureExtractor(),
            transcriber=transcriber or FixtureTranscriber(),
        )
    )


# 가짜 응답 도구는 파일을 읽어 두는 것뿐이라, 테스트마다 새로 만들지 않고 같이 쓴다.
FIXTURE_READER = FileReader(FixtureTranscriber())
FIXTURE_EXTRACTOR = FixtureExtractor()


@functools.cache
def _rows_for(names: tuple[str, ...], message: str):
    results = FIXTURE_READER.read([demo(n) for n in names], message)
    messages, image_parts = graph_messages(message, results)
    text = '\n'.join(messages)
    plain = '\n'.join(m for m in messages if m not in image_parts)
    return build_rows(FIXTURE_EXTRACTOR(text), text, plain), results


def rows_from_files(files: list[UploadedFile], message: str = ''):
    """API 없이: 파일 읽기 → 가짜 정리 응답 → 비교표 (API·normalize 노드와 같은 순서).

    시연 파일은 같은 이름이면 결과를 기억해 둔다 (여러 테스트가 같은 파일을 읽어서).
    그래서 결과는 읽기만 하고 고치지 않는다.
    """
    return _rows_for(tuple(f.name for f in files), message)


# ── 1) 시연 파일 → 정답과 같은 비교표 ──────────────────────────────


@pytest.mark.parametrize(('pid', 'name'), DEMO_CASES)
def test_demo_file_matches_expected(pid, name):
    """시연 파일마다 붙여 넣은 글과 같은 정답(expected.json) 값이 나온다."""
    rows, results = rows_from_files([demo(name)])
    assert len(rows) == 1, name
    row = rows[0]
    for key, want in expected_pairs(scenario_expected(pid)).items():
        if (pid, key) in KNOWN_DIFFERENCES:
            continue
        assert actual(row, key) == want, f'{name}.{key}: {actual(row, key)!r} != {want!r}'
    # 값이 있는 칸은 모두 원문(읽은 글)에서 확인된다
    for cell in [
        getattr(row, k) for k in type(row).model_fields if hasattr(getattr(row, k), 'status')
    ]:
        if cell.value is not None:
            assert cell.status == 'confirmed', name
    # 읽은 방법: 글자 PDF·엑셀·CSV는 0원(text), 이미지·이미지 PDF는 이미지 인식(vision)
    assert results[0].method == ('vision' if name in IMAGE_FILES else 'text'), name


@pytest.mark.parametrize(('pid', 'name'), DEMO_CASES)
def test_image_values_are_marked_from_image(pid, name):
    """이미지에서 읽은 값에만 '이미지에서 읽음'(from_image) 표시가 붙는다."""
    row = rows_from_files([demo(name)])[0][0]
    assert row.company.from_image is (name in IMAGE_FILES)


def test_csv_encodings_give_same_text():
    """EUC-KR CSV와 UTF-8 CSV는 같은 글이 된다 (인코딩 판별)."""
    for pid in ('near_pangyo', 'down_naju'):
        utf8 = sheet.read_csv(demo(f'{pid}_utf8.csv').data, 'a.csv')
        euckr = sheet.read_csv(demo(f'{pid}_euckr.csv').data, 'b.csv')
        assert utf8 == euckr
        assert '회사명: (가상)' in utf8


def test_transcript_manifest_matches_demo_files():
    """미리 받아 둔 이미지 글이 지금의 시연 파일(sha256)과 짝이 맞는다.

    시연 파일을 다시 만들면 여기서 실패해서, 옮겨 적은 글도 고치라고 알려 준다.
    """
    manifest = json.loads((TRANSCRIPTS_DIR / 'manifest.json').read_text(encoding='utf-8'))
    for name, entry in manifest.items():
        data = (ROOT / entry['source']).read_bytes()
        assert file_sha256(data) == entry['sha256'], name
        assert (TRANSCRIPTS_DIR / entry['transcript']).exists()
    assert set(manifest) == IMAGE_FILES


def test_pdf_text_with_pasted_answer_is_not_all_confirmed():
    """가짜 응답 모드는 인용이 다 있어야 답을 쓰므로 늘 '확인'만 나온다.
    그래서 코드의 정직함은 따로 본다: 붙여 넣은 글용 답을 PDF 글에 대면, 콜론이 있는 인용
    ('주거 지원: 없음')은 PDF 글에 없으니 '추정'이 된다."""
    from conftest import scenario_llm

    pdf_text = read_pdf(demo('near_pangyo_text.pdf').data, 'p.pdf').text_pages[0][1]
    row = build_rows(scenario_llm('near_pangyo'), pdf_text)[0]
    assert row.company.status == 'confirmed'
    assert row.housing_allowance_monthly_krw.status == 'estimated'
    assert row.transport_allowance_monthly_krw.status == 'estimated'


# ── 2) 섞어 넣기 · 업로드 API ─────────────────────────────────────


def test_upload_api_mixes_text_and_files_in_order(tmp_path):
    """붙여 넣은 글 + 파일 여러 개 → 넣은 순서대로 비교표 행이 나온다."""
    with app_client(tmp_path) as c:
        r = upload(
            c,
            [demo('down_naju.png'), demo('stay_ulsan_text.pdf')],
            message=scenario_text('near_pangyo'),
        )
    assert r.status_code == 201, r.json()
    body = r.json()
    assert body['status'] == 'awaiting_confirmation'
    companies = [row['company']['value'] for row in body['table']]
    assert companies == ['(가상) 이음데이터랩(주)', '(가상) 빛가람그리드(주)', '(주)여울케미칼']
    assert [row['company']['from_image'] for row in body['table']] == [False, True, False]
    assert [(i['index'], i['name'], i['kind'], i['method']) for i in body['inputs']] == [
        (1, 'down_naju.png', 'image', 'vision'),
        (2, 'stay_ulsan_text.pdf', 'pdf', 'text'),
    ]


def test_same_posting_as_text_and_file_is_one_row_in_fixture_mode(tmp_path):
    """같은 공고를 글과 PDF로 두 번 넣으면 가짜 응답 모드에서는 한 행이다.
    (실제 Claude는 두 행을 낼 수도 있다 — 가짜 모드만의 동작)"""
    rows, _ = rows_from_files([demo('near_pangyo_text.pdf')], message=scenario_text('near_pangyo'))
    assert [r.company.value for r in rows] == ['(가상) 이음데이터랩(주)']


def test_file_names_are_not_in_checked_text(tmp_path):
    """파일 이름은 정리할 글에 넣지 않는다 ('연봉 9900만원.csv'가 인용 검사를 통과하면 안 된다)."""
    f = UploadedFile('연봉 9900만원.csv', demo('near_pangyo_utf8.csv').data)
    with app_client(tmp_path) as c:
        body = upload(c, [f]).json()
    assert '9900' not in body['input_text']
    assert '--- 파일 1 ---' in body['input_text']
    assert body['inputs'][0]['name'] == '연봉 9900만원.csv'


def test_input_text_recreates_same_table(tmp_path):
    """확인을 마친 세션을 다시 고칠 때: 응답의 input_text로 새 세션을 만들면 같은 표가 나온다."""
    with app_client(tmp_path) as c:
        first = upload(c, [demo('down_naju_text.pdf')]).json()
        again = c.post('/sessions', json={'message': first['input_text']}).json()
    assert again['table'] == first['table']


def test_checkpoint_does_not_store_file_bytes(tmp_path):
    """파일 원본은 SQLite에 남지 않는다 (읽은 글만 남는다)."""
    png = demo('stay_ulsan.png')
    with app_client(tmp_path) as c:
        assert upload(c, [png]).status_code == 201
    db = (tmp_path / 'cp.sqlite').read_bytes()
    assert png.data[:64] not in db
    assert b'\x89PNG' not in db


def test_unknown_image_in_demo_mode_is_422(tmp_path):
    with app_client(tmp_path) as c:
        r = upload(c, [UploadedFile('new.png', png_bytes(50, 50))])
    assert r.status_code == 422
    assert r.json()['code'] == 'DEMO_FILE_ONLY'
    assert r.json()['details'] == {'file_name': 'new.png'}


def test_read_failure_is_502_and_makes_no_session(tmp_path):
    def broken(source):
        raise ReadError('이미지를 읽는 중 문제가 생겼습니다.')

    with app_client(tmp_path, transcriber=broken) as c:
        r = upload(c, [UploadedFile('a.png', png_bytes(50, 50))])
    assert r.status_code == 502
    assert r.json()['code'] == 'READ_FAILED'


def test_vision_budget_is_503_and_cache_is_free(tmp_path):
    """하루 한도를 넘으면 503. 같은 이미지는 기억해 둔 글을 써서 한도도 돈도 쓰지 않는다."""
    fake = CountingTranscriber()
    with app_client(tmp_path, fake) as c:
        c.app.state.file_reader.budget = DailyVisionBudget(limit=1)
        a = UploadedFile('a.png', png_bytes(50, 50, 'white'))
        b = UploadedFile('b.png', png_bytes(50, 50, 'black'))
        assert upload(c, [a]).status_code == 201
        assert upload(c, [a]).status_code == 201  # 같은 이미지 → 다시 부르지 않음
        r = upload(c, [b])
    assert len(fake.calls) == 1
    assert r.status_code == 503
    assert r.json()['code'] == 'VISION_DAILY_LIMIT'


def test_daily_budget_from_env_rejects_typo(monkeypatch, tmp_path):
    monkeypatch.setenv('VISION_DAILY_PAGE_LIMIT', 'abc')
    monkeypatch.setenv('NORMALIZE_MODE', 'claude')
    with pytest.raises(ValueError, match='VISION_DAILY_PAGE_LIMIT'):
        create_app(str(tmp_path / 'cp.sqlite'))


def test_budget_counts_pages():
    budget = DailyVisionBudget(limit=3)
    budget.take(3)
    with pytest.raises(VisionBudgetError):
        budget.take(1)


# ── 3) 상한 (정확히 상한은 통과, 하나 넘으면 422) ───────────────────


def test_file_count_limit(tmp_path):
    files = [
        UploadedFile(f'{i}.csv', f'항목,내용\n번호,{i}\n'.encode()) for i in range(MAX_FILES + 1)
    ]
    check_upload(files[:MAX_FILES])
    with app_client(tmp_path) as c:
        r = upload(c, files)
    assert r.status_code == 422
    assert r.json()['code'] == 'FILE_TOO_MANY'


def test_no_files_is_422(tmp_path):
    with app_client(tmp_path) as c:
        r = c.post('/sessions/uploads', data={'message': '공고'})
    assert r.status_code == 422
    assert r.json()['code'] == 'FILE_MISSING'


def test_file_size_limit():
    check_upload([UploadedFile('a.csv', b'x' * MAX_FILE_BYTES)])
    with pytest.raises(FileInputError) as e:
        check_upload([UploadedFile('a.csv', b'x' * (MAX_FILE_BYTES + 1))])
    assert e.value.code == InputErrorCode.FILE_TOO_LARGE


def test_total_size_limit():
    files = [UploadedFile(f'{c}.csv', c.encode() * MAX_FILE_BYTES) for c in 'xyz']
    with pytest.raises(FileInputError) as e:
        check_upload(files)
    assert e.value.code == InputErrorCode.FILE_TOO_LARGE


def test_request_size_checked_before_reading_body(tmp_path):
    """요청 본문이 상한보다 크면 본문을 받기 전에 422로 막는다."""
    with app_client(tmp_path) as c:
        r = c.post(
            '/sessions/uploads',
            files=[('files', ('big.csv', b'x' * (MAX_REQUEST_BYTES + 1), 'text/csv'))],
        )
    assert r.status_code == 422
    assert r.json()['code'] == 'FILE_TOO_LARGE'


def test_request_without_length_is_411(tmp_path):
    def chunks():
        yield b'--x\r\n'

    with app_client(tmp_path) as c:
        r = c.post(
            '/sessions/uploads',
            content=chunks(),
            headers={'content-type': 'multipart/form-data; boundary=x'},
        )
    assert r.status_code == 411
    assert r.json()['code'] == 'LENGTH_REQUIRED'


def test_empty_file_is_422(tmp_path):
    with app_client(tmp_path) as c:
        r = upload(c, [UploadedFile('empty.pdf', b'')])
    assert r.status_code == 422
    assert r.json()['code'] == 'FILE_EMPTY'


def test_text_length_limit_counts_message_and_files():
    reader = FileReader(FixtureTranscriber())
    f = demo('near_pangyo_utf8.csv')
    # 붙여 넣은 글 + 줄바꿈 1자 + 파일 글 = 정리할 글 전체 (응답의 input_text와 같은 길이)
    file_chars = len('\n'.join(graph_messages('', reader.read([f]))[0])) + 1
    reader.read([f], 'x' * (MAX_TEXT_CHARS - file_chars))
    with pytest.raises(FileInputError) as e:
        reader.read([f], 'x' * (MAX_TEXT_CHARS - file_chars + 1))
    assert e.value.code == InputErrorCode.FILE_TOO_LONG


def test_pasted_message_length_limit(tmp_path):
    with app_client(tmp_path) as c:
        ok = c.post('/sessions', json={'message': 'x' * MAX_TEXT_CHARS})
        too_long = c.post('/sessions', json={'message': 'x' * (MAX_TEXT_CHARS + 1)})
    assert ok.status_code == 201
    assert too_long.status_code == 422


def test_pdf_page_limit():
    read_pdf(pdf_bytes(['공고 본문 글자가 충분히 있는 쪽입니다 ' * 2] * MAX_PDF_PAGES), 'ok.pdf')
    with pytest.raises(FileInputError) as e:
        read_pdf(pdf_bytes(['글'] * (MAX_PDF_PAGES + 1)), 'long.pdf')
    assert e.value.code == InputErrorCode.FILE_TOO_LONG


def test_vision_page_limit_is_per_request():
    """이미지 인식 장 수는 요청 전체(모든 파일 합계)로 센다."""
    fake = CountingTranscriber()
    reader = FileReader(fake)
    images = [UploadedFile(f'{i}.png', png_bytes(40 + i, 40)) for i in range(MAX_FILES)]
    scan = UploadedFile('scan.pdf', pdf_bytes([None] * (MAX_VISION_PAGES - MAX_FILES)))
    reader.read(images[:-1] + [scan])  # 4 + 4 = 8장
    with pytest.raises(FileInputError) as e:
        reader.read(images[:-1] + [UploadedFile('s.pdf', pdf_bytes([None] * 5))])  # 9장
    assert e.value.code == InputErrorCode.FILE_TOO_LONG


@pytest.mark.filterwarnings('ignore::PIL.Image.DecompressionBombWarning')  # 일부러 낸 경고
def test_image_pixel_limit_checked_before_decoding():
    """픽셀 수가 상한을 넘으면 그림을 풀기 전에 막는다 (머리글만 있는 PNG로 확인)."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = struct.pack('>I', zlib.crc32(kind + body))
        return struct.pack('>I', len(body)) + kind + body + crc

    def png_header(width, height):
        # 크기만 크고 그림 데이터(IDAT)는 비어 있는 '폭탄' 파일
        ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
        signature = b'\x89PNG\r\n\x1a\n'
        return signature + chunk(b'IHDR', ihdr) + chunk(b'IDAT', b'') + chunk(b'IEND', b'')

    side = int(MAX_IMAGE_PIXELS**0.5) + 1
    with pytest.raises(FileInputError) as e:
        prepare_image(png_header(side, side), 'huge.png')
    assert e.value.code == InputErrorCode.FILE_TOO_LARGE


# ── 4) 형식 판별 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ('name', 'data', 'code'),
    [
        ('a.gif', b'GIF89a' + b'\x00' * 20, InputErrorCode.FILE_TYPE_UNSUPPORTED),
        (
            'a.heic',
            b'\x00\x00\x00\x18ftypheic' + b'\x00' * 20,
            InputErrorCode.FILE_TYPE_UNSUPPORTED,
        ),
        (
            'a.hwp',
            b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 20,
            InputErrorCode.FILE_TYPE_UNSUPPORTED,
        ),
        ('a.docx', b'PK\x03\x04word/document.xml', InputErrorCode.FILE_TYPE_UNSUPPORTED),
        (
            'a.xls',
            b'<html><table><tr><td>1</td></tr></table></html>',
            InputErrorCode.FILE_TYPE_UNSUPPORTED,
        ),
        ('a.txt', b'hello', InputErrorCode.FILE_TYPE_UNSUPPORTED),
        ('a.pdf', b'not a pdf at all', InputErrorCode.FILE_UNREADABLE),
        ('a.png', b'not a png', InputErrorCode.FILE_UNREADABLE),
    ],
)
def test_unsupported_or_broken_files(name, data, code):
    with pytest.raises(FileInputError) as e:
        detect.detect_kind(name, data)
    assert e.value.code == code
    assert name in e.value.message


def test_content_wins_over_extension_for_images():
    """.png라고 저장된 JPG, 크롬의 .jfif도 이미지로 읽는다."""
    buf = io.BytesIO()
    Image.new('RGB', (10, 10)).save(buf, format='JPEG')
    assert detect.detect_kind('photo.png', buf.getvalue()) == 'image'
    assert detect.detect_kind('photo.jfif', buf.getvalue()) == 'image'
    assert detect.detect_kind('scan.PDF', pdf_bytes(['x'])) == 'pdf'


def test_webp_is_accepted():
    buf = io.BytesIO()
    Image.new('RGB', (30, 30), 'white').save(buf, format='WEBP')
    assert detect.detect_kind('a.webp', buf.getvalue()) == 'image'
    assert len(prepare_image(buf.getvalue(), 'a.webp')) == 1


# ── 5) PDF ───────────────────────────────────────────────────────


def test_encrypted_pdf_is_422():
    data = pdf_bytes(['비밀'], encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw='u', owner_pw='o')
    with pytest.raises(FileInputError) as e:
        read_pdf(data, 'secret.pdf')
    assert e.value.code == InputErrorCode.FILE_UNREADABLE


def test_owner_password_only_pdf_is_readable():
    """편집만 막은 PDF(여는 암호 없음)는 읽는다."""
    data = pdf_bytes(
        ['Salary 36000000 KRW per year for this posting'],
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw='o',
        permissions=pymupdf.PDF_PERM_PRINT,
    )
    assert read_pdf(data, 'locked.pdf').text_pages


def test_mixed_pdf_reads_text_pages_free_and_image_pages_by_vision():
    fake = CountingTranscriber('옮겨 적은 글')
    data = pdf_bytes(['Text page with enough characters to count as text', None, None])
    [result] = FileReader(fake).read([UploadedFile('mixed.pdf', data)])
    assert result.method == 'mixed'
    assert result.vision_pages == 2
    assert len(fake.calls) == 1 and len(fake.calls[0].images) == 2  # 파일 하나 = 호출 한 번
    assert 'Text page' in result.plain_text
    assert result.image_text == '옮겨 적은 글'


def test_blank_pdf_is_422():
    with pytest.raises(FileInputError) as e:
        read_pdf(pdf_bytes(['']), 'blank.pdf')
    assert e.value.code == InputErrorCode.FILE_NO_TEXT


def test_demo_text_pdf_with_route_map_is_text():
    """글자 PDF 안의 작은 그림(통근버스 노선도)은 무시하고 글자 쪽으로 읽는다."""
    content = read_pdf(demo('stay_ulsan_text.pdf').data, 'p.pdf')
    assert [m for _, _, m in content.text_pages] == ['text']
    assert content.image_pages == []


# ── 6) 이미지 ─────────────────────────────────────────────────────


def test_tall_capture_is_split_into_overlapping_tiles():
    img = Image.new('RGB', (1000, 6000))
    for y in range(6000):  # 줄마다 다른 색 → 조각이 어디서 잘렸는지 알 수 있다
        img.putpixel((0, y), (y % 256, y // 256, 0))
    tiles = split_tall(img)
    assert all(t.size == (1000, 2000) for t in tiles)
    assert tiles[0].getpixel((0, 0)) == img.getpixel((0, 0))  # 맨 위부터
    assert tiles[-1].getpixel((0, 1999)) == img.getpixel((0, 5999))  # 맨 아래까지
    assert len(tiles) == 4  # 가로:세로 1:2 조각을 8%씩 겹치게
    assert len(split_tall(Image.new('RGB', (1000, 2500)))) == 1


def test_too_long_capture_is_422():
    tall = png_bytes(100, 100 * 2 * (MAX_VISION_PAGES + 1))
    with pytest.raises(FileInputError) as e:
        prepare_image(tall, 'tall.png')
    assert e.value.code == InputErrorCode.FILE_TOO_LONG


def test_big_image_is_shrunk_and_size_capped():
    """긴 변은 2576px 이하, 크기는 상한 이하 (사진처럼 복잡한 그림도)."""
    noisy = Image.effect_noise((3000, 3000), 100).convert('RGB')
    out = encode_for_vision(noisy)
    assert len(out.data) <= VISION_MAX_IMAGE_BYTES
    assert max(Image.open(io.BytesIO(out.data)).size) <= VISION_MAX_EDGE


def test_transparent_image_gets_white_background():
    out = prepare_image(png_bytes(10, 10, (0, 0, 0, 0), mode='RGBA'), 'clear.png')[0]
    assert Image.open(io.BytesIO(out.data)).convert('RGB').getpixel((5, 5)) == (255, 255, 255)


# ── 7) 엑셀·CSV ──────────────────────────────────────────────────


def test_xlsx_cells_keep_their_text():
    import datetime as dt

    data = xlsx_bytes(
        [['연봉', 36000000.0], ['접수', dt.datetime(2026, 11, 16)], ['a', 'b', 'c']],
        hidden=[['숨긴 연봉', '9900만원']],
    )
    text = sheet.read_xlsx(data, 'a.xlsx')
    assert text == '연봉: 36000000\n접수: 2026-11-16\na | b | c'  # 숨긴 시트는 읽지 않는다


def test_xlsx_zip_bomb_is_422():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('xl/workbook.xml', b'0' * (51 * 1024 * 1024))
    with pytest.raises(FileInputError) as e:
        sheet.read_xlsx(buf.getvalue(), 'bomb.xlsx')
    assert e.value.code == InputErrorCode.FILE_TOO_LARGE


def test_xls_old_excel_is_read():
    import xlwt

    book = xlwt.Workbook()
    ws = book.add_sheet('공고')
    for r, (k, v) in enumerate([('회사명', '(가상) 옛엑셀(주)'), ('연봉', 36000000)]):
        ws.write(r, 0, k)
        ws.write(r, 1, v)
    hidden = book.add_sheet('숨김')
    hidden.write(0, 0, '숨긴 값')
    hidden.visibility = 1
    buf = io.BytesIO()
    book.save(buf)
    assert detect.detect_kind('old.xls', buf.getvalue()) == 'xls'
    assert sheet.read_xls(buf.getvalue(), 'old.xls') == '회사명: (가상) 옛엑셀(주)\n연봉: 36000000'


def test_csv_utf16_and_quoted_commas():
    text = '항목,내용\n고용 형태,"정규직 (수습 3개월, 급여 100%)"\n'
    assert sheet.read_csv(text.encode('utf-16'), 'a.csv').endswith(
        '고용 형태: 정규직 (수습 3개월, 급여 100%)'
    )


@pytest.mark.parametrize('data', [b'a,b\x00c', b'\xff\xff\xff\x80\x80'])
def test_broken_csv_is_422(data):
    with pytest.raises(FileInputError) as e:
        sheet.read_csv(data, 'bad.csv')
    assert e.value.code == InputErrorCode.FILE_UNREADABLE


def test_empty_sheet_is_422():
    with pytest.raises(FileInputError) as e:
        FileReader(FixtureTranscriber()).read([UploadedFile('a.csv', b',,\n,,\n')])
    assert e.value.code == InputErrorCode.FILE_NO_TEXT


# ── 8) 이름 정리 · 가짜 응답 · Claude 호출부 ───────────────────────


@pytest.mark.parametrize(
    ('raw', 'want'),
    [
        ('../../etc/passwd', 'passwd'),
        ('C:\\Users\\me\\공고.pdf', '공고.pdf'),
        ('', '파일 3'),
        ('a\x00b.png', 'ab.png'),
    ],
)
def test_safe_name(raw, want):
    assert safe_name(raw, 3) == want


def test_fixture_needs_enough_quotes(tmp_path):
    """인용이 적은 가짜 응답은 아무 글에나 맞아 버리므로 불러올 때 막는다."""
    src = (ROOT / 'tests/fixtures/normalize/near_pangyo.llm.json').read_text(encoding='utf-8')
    data = json.loads(src)
    for key, cell in data['postings'][0].items():
        if isinstance(cell, dict) and key != 'company':
            cell['quote'] = None
            cell['value'] = None
    data['postings'][0]['other_allowances'] = []
    (tmp_path / 'weak.llm.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='인용'):
        FixtureExtractor(tmp_path)


class FakeCreate:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def _client(messages: FakeCreate):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


ONE_IMAGE = prepare_image(png_bytes(20, 20), 'a.png')


def test_claude_transcriber_request_shape():
    """요청 모양: 같은 모델, 장마다 따로 부름(이미지 1장 + 지시 한 줄), 낮은 effort, fallback."""
    reply = SimpleNamespace(
        stop_reason='end_turn', content=[SimpleNamespace(type='text', text=' 옮긴 글 ')]
    )
    messages = FakeCreate(reply)
    one = ClaudeTranscriber(_client(messages))(ImageSource('a.png', 'sha', ONE_IMAGE))
    assert one == '옮긴 글'
    two = ClaudeTranscriber(_client(messages))(ImageSource('a.png', 'sha', ONE_IMAGE * 2))
    assert two == '--- 1쪽 ---\n옮긴 글\n\n--- 2쪽 ---\n옮긴 글'
    assert len(messages.calls) == 3  # 1장 + 2장 → 호출 3번
    for kw in messages.calls:
        assert kw['model'] == MODEL
        assert kw['fallbacks'] == 'default'
        assert kw['output_config'] == {'effort': 'low'}
        assert [b['type'] for b in kw['messages'][0]['content']] == ['image', 'text']
        assert 'a.png' not in json.dumps(kw, ensure_ascii=False)  # 파일 이름은 보내지 않는다


@pytest.mark.parametrize('stop', ['refusal', 'max_tokens'])
def test_claude_transcriber_bad_stop(stop):
    reply = SimpleNamespace(stop_reason=stop, content=[])
    with pytest.raises(ReadError):
        ClaudeTranscriber(_client(FakeCreate(reply)))(ImageSource('a', 's', ONE_IMAGE))


def test_claude_transcriber_connection_error():
    err = anthropic.APIConnectionError(request=httpx.Request('POST', 'https://example.invalid'))
    with pytest.raises(ReadError):
        ClaudeTranscriber(_client(FakeCreate(error=err)))(ImageSource('a', 's', ONE_IMAGE * 2))


def test_claude_transcriber_missing_api_key_is_read_error(sdk_client_without_key, caplog):
    """리뷰(backend#28)에서 찾은 경우: 키 없이 켠 서버. SDK의 TypeError도 ReadError(→502)."""
    with pytest.raises(ReadError) as info:
        ClaudeTranscriber(sdk_client_without_key)(ImageSource('a', 's', ONE_IMAGE))
    assert isinstance(info.value.__cause__, TypeError)
    assert 'ANTHROPIC_API_KEY' in caplog.text


def test_claude_transcriber_other_type_error_is_not_hidden():
    err = TypeError('unexpected keyword')
    with pytest.raises(TypeError, match='unexpected keyword'):
        ClaudeTranscriber(_client(FakeCreate(error=err)))(ImageSource('a', 's', ONE_IMAGE))


# ── 9) Fable 코드 검토에서 나온 경우 ─────────────────────────────────


def test_sparse_sheet_is_rejected_fast():
    """맨 끝 칸(XFD20000)에만 값이 있는 엑셀: 빈칸을 끝없이 훑지 않고 바로 422."""
    import time

    book = openpyxl.Workbook()
    book.active['A1'] = '회사명'
    book.active['XFD20000'] = 'x'
    buf = io.BytesIO()
    book.save(buf)
    start = time.monotonic()
    with pytest.raises(FileInputError) as e:
        sheet.read_xlsx(buf.getvalue(), 'sparse.xlsx')
    assert e.value.code == InputErrorCode.FILE_TOO_LONG
    assert time.monotonic() - start < 3


@pytest.mark.parametrize('part', ['xl/worksheets/sheet1.xml', 'xl/workbook.xml', 'xl/styles.xml'])
def test_corrupt_xlsx_part_is_422_not_500(part):
    good = xlsx_bytes([['회사명', '(가상) 테스트(주)']])
    src = zipfile.ZipFile(io.BytesIO(good))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as out:
        for item in src.infolist():
            data = src.read(item)
            out.writestr(item, data[: len(data) // 2] if item.filename == part else data)
    with pytest.raises(FileInputError) as e:
        sheet.read_xlsx(buf.getvalue(), 'broken.xlsx')
    assert e.value.code == InputErrorCode.FILE_UNREADABLE


def test_corrupt_xls_is_422_not_500():
    import random

    import xlwt

    book = xlwt.Workbook()
    book.add_sheet('a').write(0, 0, '회사명')
    buf = io.BytesIO()
    book.save(buf)
    rng = random.Random(0)
    for _ in range(200):
        data = bytearray(buf.getvalue())
        for _ in range(8):
            data[rng.randrange(512, len(data))] = rng.randrange(256)
        try:
            sheet.read_xls(bytes(data), 'm.xls')
        except FileInputError:
            pass  # 422로 바뀌면 된다. 그 밖의 예외(500)는 테스트 실패


def test_failed_reads_give_budget_back():
    """이미지 읽기가 실패하면 하루 한도를 돌려준다 (실패 반복으로 한도가 바닥나지 않게)."""

    def broken(source):
        raise ReadError('실패')

    budget = DailyVisionBudget(limit=1)
    reader = FileReader(broken, budget)
    for _ in range(3):
        with pytest.raises(ReadError):
            reader.read([UploadedFile('a.png', png_bytes(30, 30))])
    assert budget.used == 0


def test_image_posting_marked_as_a_whole_when_mixed_with_text():
    """붙여 넣은 판교 글 + 나주 이미지: 나주 행의 '수습 3개월'은 판교 글에도 있지만,
    나주 공고 자체가 이미지에서 읽은 것이라 모든 칸이 '이미지에서 읽음'이다."""
    rows, _ = rows_from_files([demo('down_naju.png')], message=scenario_text('near_pangyo'))
    pangyo, naju = rows
    assert naju.probation_months.source_text == '수습 3개월'
    assert naju.probation_months.from_image is True
    assert pangyo.probation_months.from_image is False


def test_phone_mpo_jpeg_is_accepted():
    """휴대폰 카메라 JPG(MPO, 여러 장이 든 JPG)도 첫 장을 읽는다."""
    buf = io.BytesIO()
    Image.new('RGB', (40, 40), 'white').save(
        buf, format='MPO', save_all=True, append_images=[Image.new('RGB', (40, 40))]
    )
    assert Image.open(io.BytesIO(buf.getvalue())).format == 'MPO'
    assert len(prepare_image(buf.getvalue(), 'photo.jpg')) == 1


def test_long_name_keeps_extension():
    name = safe_name('가' * 200 + '.csv', 1)
    assert len(name) <= 120 and name.endswith('.csv')


def test_password_xlsx_says_password():
    with pytest.raises(FileInputError) as e:
        detect.detect_kind('secret.xlsx', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 20)
    assert e.value.code == InputErrorCode.FILE_UNREADABLE
    assert '암호' in e.value.message


def test_too_many_scanned_pages_rejected_before_rendering(monkeypatch):
    """그림 쪽이 상한을 넘으면 그리기(비싼 일) 전에 422."""
    import backend.inputs.pdf as pdf_module

    rendered = []
    monkeypatch.setattr(pdf_module, '_render', lambda page: rendered.append(page) or None)
    with pytest.raises(FileInputError) as e:
        read_pdf(pdf_bytes([None] * (MAX_VISION_PAGES + 1)), 'scan.pdf')
    assert e.value.code == InputErrorCode.FILE_TOO_LONG
    assert rendered == []


def test_pdf_page_with_text_and_picture_reports_ignored_picture():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), '공고 제목과 설명이 충분히 길게 적혀 있는 쪽입니다 ' * 2)
    page.insert_image(pymupdf.Rect(72, 200, 500, 600), stream=png_bytes(300, 300, 'gray'))
    content = read_pdf(doc.tobytes(), 'word.pdf')
    assert [m for _, _, m in content.text_pages] == ['text']
    assert content.ignored_images == 1


def test_quote_check_still_works_on_file_text():
    """파일에서 꺼낸 글에도 인용 검사(띄어쓰기 무시)가 그대로 동작한다."""
    pdf_text = read_pdf(demo('down_naju_text.pdf').data, 'p.pdf').text_pages[0][1]
    # PDF에서 줄이 바뀐 '수당 별\n도 지급'도 원문 인용으로 찾는다
    assert checks.quote_in_text(
        '설비 점검 일정에 따라 출장·야간 근무 있음(수당 별도 지급)', pdf_text
    )


def test_fixture_order_follows_company_position():
    """여러 파일을 넣으면 넣은 순서대로 행이 나온다. '수습 3개월'처럼 여러 공고에 흔한 구절이
    앞 파일에 있어도 뒤 공고가 앞으로 끌려오지 않는다 (브라우저 확인 중 찾은 버그)."""
    names = [
        'down_naju.xlsx',
        'near_pangyo_euckr.csv',
        'stay_ulsan_image.pdf',
        'up_seoul_offer_mail.png',
    ]
    rows, _ = rows_from_files([demo(n) for n in names])
    assert [r.company.value for r in rows] == [
        '(가상) 빛가람그리드(주)',
        '(가상) 이음데이터랩(주)',
        '(주)여울케미칼',
        '(주)새늘소재',
    ]


def test_input_error_codes_exist_in_api_error_codes():
    """파일 오류 코드가 API 오류 코드 목록에도 모두 있어야 한다 (없으면 422 대신 500이 난다)."""
    from backend.api.errors import ErrorCode

    assert {c.value for c in InputErrorCode} <= {c.value for c in ErrorCode}


def test_same_file_twice_is_rejected():
    """같은 파일을 두 번 넣으면 (이름이 달라도 내용이 같으면) 422로 하나만 넣으라고 알려 준다."""
    f = demo('near_pangyo_utf8.csv')
    with pytest.raises(FileInputError) as e:
        check_upload([f, UploadedFile('복사본.csv', f.data)])
    assert e.value.code == InputErrorCode.FILE_DUPLICATE
    assert e.value.file_name == '복사본.csv' and f.name in e.value.message
