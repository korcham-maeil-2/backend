"""엑셀(xlsx·xls)·CSV → 글. 비용 0원 (코드만으로 읽는다).

[줄 모양]
행 하나 = 한 줄. 빈칸은 뺀다.
  - 칸이 2개면  '항목: 내용'      (공고 표는 대부분 이 모양이라, 붙여 넣은 글과 같아진다)
  - 그 밖에는   'a | b | c'
시트가 여러 개면 '[시트: 이름]' 줄로 나눈다.

값은 바꾸지 않고 글자 그대로 옮긴다 (인용 검사가 원문 글자와 맞아야 하므로).
숫자 칸은 '3600.0'처럼 바뀌지 않게 정수면 정수로 쓴다.
"""

import csv
import datetime as dt
import io
import zipfile
from collections.abc import Iterable

from backend.inputs.errors import FileInputError, InputErrorCode
from backend.inputs.limits import (
    MAX_SHEET_CELLS,
    MAX_SHEET_SCAN_CELLS,
    MAX_TEXT_CHARS,
    MAX_XLSX_UNZIPPED_BYTES,
)

TOO_BIG = '표가 너무 커요. 공고가 있는 부분만 남겨서 다시 넣어 주세요.'


def _cell_text(value: object) -> str:
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat(' ')
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value).strip()


def rows_to_text(rows: Iterable[Iterable[object]], name: str) -> str:
    """행들을 '항목: 내용' 줄로 바꾼다. 칸 수·글자 수가 상한을 넘으면 422."""
    lines: list[str] = []
    cells = 0
    scanned = 0
    chars = 0
    for row in rows:
        row = list(row)
        scanned += max(len(row), 1)  # 빈칸도 센다 (빈 행도 1칸으로)
        if scanned > MAX_SHEET_SCAN_CELLS:
            raise FileInputError(InputErrorCode.FILE_TOO_LONG, TOO_BIG, name)
        values = [t for t in (_cell_text(v) for v in row) if t]
        cells += len(values)
        if cells > MAX_SHEET_CELLS:
            raise FileInputError(InputErrorCode.FILE_TOO_LONG, TOO_BIG, name)
        if not values:
            continue
        line = f'{values[0]}: {values[1]}' if len(values) == 2 else ' | '.join(values)
        chars += len(line) + 1
        if chars > MAX_TEXT_CHARS:
            raise FileInputError(
                InputErrorCode.FILE_TOO_LONG,
                '표에 글이 너무 많아요. 공고가 있는 부분만 남겨서 다시 넣어 주세요.',
                name,
            )
        lines.append(line)
    return '\n'.join(lines)


def _join_sheets(sheets: list[tuple[str, str]]) -> str:
    sheets = [(title, text) for title, text in sheets if text]
    if len(sheets) == 1:
        return sheets[0][1]
    return '\n\n'.join(f'[시트: {title}]\n{text}' for title, text in sheets)


def _check_unzipped_size(data: bytes, name: str) -> None:
    """xlsx는 zip이다. 10MB 파일이 수 GB로 풀리는 '압축 폭탄'을 열기 전에 막는다."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            total = sum(info.file_size for info in z.infolist())
    except (zipfile.BadZipFile, OSError) as e:
        raise FileInputError(
            InputErrorCode.FILE_UNREADABLE,
            '엑셀 파일을 열지 못했어요. 파일이 손상되지 않았는지 확인해 주세요.',
            name,
        ) from e
    if total > MAX_XLSX_UNZIPPED_BYTES:
        raise FileInputError(
            InputErrorCode.FILE_TOO_LARGE,
            '엑셀 파일 내용이 너무 커요. 공고가 있는 시트만 남겨서 다시 넣어 주세요.',
            name,
        )


def read_xlsx(data: bytes, name: str) -> str:
    # openpyxl은 defusedxml이 설치돼 있으면 그걸로 XML을 읽는다 (XML 폭탄 막기, pyproject에 있음)
    import openpyxl  # 무거운 편이라 필요할 때만 불러온다

    _check_unzipped_size(data, name)
    try:
        # read_only: 메모리를 적게 쓴다. data_only: 수식 대신 저장된 계산 결과를 읽는다.
        # read_only에서는 시트 XML을 행을 읽을 때 푼다 → 깨진 파일 오류가 여기서도 난다.
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            sheets = []
            for ws in book.worksheets:
                if ws.sheet_state != 'visible':  # 숨긴 시트는 화면에 안 보이는 내용이라 읽지 않는다
                    continue
                rows, cols = ws.max_row, ws.max_column  # 파일에 적힌 크기 (없으면 None)
                if rows and cols and rows * cols > MAX_SHEET_SCAN_CELLS:
                    raise FileInputError(InputErrorCode.FILE_TOO_LONG, TOO_BIG, name)
                sheets.append((ws.title, rows_to_text(ws.iter_rows(values_only=True), name)))
        finally:
            book.close()
    except FileInputError:
        raise
    except Exception as e:  # 깨진 엑셀은 zip·XML·날짜 등 곳곳에서 서로 다른 예외를 낸다
        raise FileInputError(
            InputErrorCode.FILE_UNREADABLE,
            '엑셀 파일을 읽지 못했어요. 파일이 손상되지 않았는지 확인해 주세요.',
            name,
        ) from e
    return _join_sheets(sheets)


def read_xls(data: bytes, name: str) -> str:
    import xlrd

    try:
        # on_demand: 시트를 쓸 때 푼다 → 깨진 파일 오류가 시트를 읽을 때도 난다
        book = xlrd.open_workbook(file_contents=data, on_demand=True)
        try:
            sheets = []
            for i in range(book.nsheets):
                ws = book.sheet_by_index(i)
                if ws.visibility != 0:  # 숨긴 시트
                    continue
                if ws.nrows * ws.ncols > MAX_SHEET_SCAN_CELLS:
                    raise FileInputError(InputErrorCode.FILE_TOO_LONG, TOO_BIG, name)
                rows = (
                    [_xls_value(ws.cell(r, c), book.datemode) for c in range(ws.ncols)]
                    for r in range(ws.nrows)
                )
                sheets.append((ws.name, rows_to_text(rows, name)))
        finally:
            book.release_resources()
    except FileInputError:
        raise
    except Exception as e:  # xlrd는 형식 오류마다 다른 예외를 낸다
        raise FileInputError(
            InputErrorCode.FILE_UNREADABLE,
            '엑셀(.xls) 파일을 열지 못했어요. 엑셀에서 .xlsx로 저장해서 다시 넣어 주세요.',
            name,
        ) from e
    return _join_sheets(sheets)


def _xls_value(cell, datemode: int) -> object:
    import xlrd

    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate.xldate_as_datetime(cell.value, datemode)
        except (ValueError, OverflowError):
            return cell.value
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype in (xlrd.XL_CELL_ERROR, xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    return cell.value


def decode_text(data: bytes, name: str) -> str:
    """CSV의 글자 인코딩을 판별한다.

    UTF-16(엑셀 '유니코드 텍스트', BOM으로 구분) → UTF-8(BOM 있어도 됨) → CP949(EUC-KR 포함).
    """
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        try:
            return data.decode('utf-16')
        except UnicodeDecodeError:
            pass
    elif b'\x00' in data:
        raise FileInputError(
            InputErrorCode.FILE_UNREADABLE,
            'CSV 파일이 아니거나 손상된 파일이에요. 엑셀에서 CSV로 다시 저장해 주세요.',
            name,
        )
    for encoding in ('utf-8-sig', 'cp949'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise FileInputError(
        InputErrorCode.FILE_UNREADABLE,
        '파일의 글자를 읽지 못했어요. UTF-8로 저장해서 다시 넣어 주세요.',
        name,
    )


def read_csv(data: bytes, name: str) -> str:
    text = decode_text(data, name)
    try:
        # csv 모듈은 따옴표 안의 쉼표·줄바꿈("정규직 (수습 3개월, ...)")을 한 칸으로 읽는다.
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    try:
        return rows_to_text(csv.reader(io.StringIO(text, newline=''), dialect), name)
    except csv.Error as e:
        raise FileInputError(
            InputErrorCode.FILE_UNREADABLE,
            'CSV 파일 모양이 올바르지 않아요. 엑셀에서 다시 저장해서 넣어 주세요.',
            name,
        ) from e
