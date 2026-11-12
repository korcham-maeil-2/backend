"""시연용 가상 채용공고 파일 생성기 (backend#10).

모든 회사·공고·사용자는 가상이다. 실제 개인정보를 쓰지 않는다.

한 공고를 여러 형식으로 만든다.
- {id}_text.pdf      글자를 복사할 수 있는 PDF (Chrome 인쇄)
- {id}_image.pdf     글자를 복사할 수 없는 PDF (페이지를 이미지로 바꿈)
- {id}.png           채용 사이트 캡처처럼 보이는 이미지
- {id}.xlsx          엑셀 (xlsx는 내부가 유니코드라 인코딩 구분 없음)
- {id}_utf8.csv      CSV, UTF-8 (BOM 없음)
- {id}_euckr.csv     CSV, EUC-KR

실행 (backend 폴더에서, macOS + Google Chrome 필요):
    uv run --no-project --with openpyxl --with pymupdf python scripts/build_demo_postings.py
"""

import csv
import html
import json
import subprocess
import tempfile
from pathlib import Path

import pymupdf
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
OUT_DIR = Path(__file__).resolve().parent.parent / 'data' / 'demo' / 'postings'
NOTICE = '본 공고는 2026 AI 해커톤 시연용 가상 데이터이며 실제 회사·채용과 관계없습니다.'

POSTINGS = [
    {
        'id': 'down_naju',
        'scenario': '위에서 아래로: 서울 거주 사용자가 전남 나주(혁신도시) 일자리로 이사',
        'title': '[가상] 전력설비 엔지니어 신입 채용',
        'company': '(가상) 빛가람그리드(주)',
        'rows': [
            ('회사명', '(가상) 빛가람그리드(주)'),
            ('업종', '전력설비 점검·유지보수 (사원 수 62명, 중소기업, 에너지밸리 입주)'),
            ('모집 직무', '전력설비 엔지니어 신입 2명'),
            ('고용 형태', '정규직 (수습 3개월, 수습 기간 급여 90%)'),
            ('근무지', '전남 나주시 빛가람동 (나주혁신도시 에너지밸리)'),
            ('급여', '회사 내규에 따름 (면접 후 결정)'),
            (
                '근무 시간',
                '주 5일(월~금) 09:00~18:00, 휴게 1시간 / 설비 점검 일정에 따라 출장·야간 근무 있음'
                '(수당 별도 지급)',
            ),
            ('주거 지원', '사택 없음. 타 지역 입사자 주거지원비 월 20만원 (입사 후 2년간)'),
            ('식사', '중식 제공 (구내식당)'),
            ('통근', '통근버스 운행 (광주 송정역·상무지구 노선)'),
            ('기타 복리후생', '명절 귀향 교통비 지원(연 2회), 4대보험, 퇴직금'),
            ('지원 자격', '학력 무관 (전기·전자 관련 전공 우대), 전기기사 또는 전기산업기사 우대'),
            ('전형 절차', '서류 → 면접 → 최종 합격'),
            ('접수 기간', '2026-11-16 ~ 2026-12-15'),
            ('문의', '인사 담당자 recruit@example.com'),
        ],
    },
    {
        'id': 'near_pangyo',
        'scenario': '위 근처: 서울 거주 사용자가 경기 성남 판교 일자리로 통근 또는 이사',
        'title': '[가상] 백엔드 개발자 신입 채용',
        'company': '(가상) 이음데이터랩(주)',
        'rows': [
            ('회사명', '(가상) 이음데이터랩(주)'),
            ('업종', 'IT 서비스·데이터 플랫폼 (사원 수 85명)'),
            ('모집 직무', '백엔드 개발자 신입 2명'),
            ('고용 형태', '정규직 (수습 3개월, 수습 기간 급여 100%)'),
            ('근무지', '경기 성남시 분당구 판교역로 (판교테크노밸리)'),
            ('연봉', '3,600만원 (기본급 기준, 성과급 별도)'),
            ('식대', '월 20만원 별도 지급 (비과세)'),
            ('근무 시간', '주 5일, 시차출퇴근제 (08~10시 출근), 1일 8시간'),
            ('주거 지원', '없음'),
            ('교통비', '지원 없음'),
            ('기타 복리후생', '통신비 월 5만원, 교육비 연 100만원, 4대보험, 퇴직연금'),
            ('지원 자격', '신입 또는 경력 2년 미만, Python 또는 Java 개발 경험'),
            ('전형 절차', '서류 → 코딩 테스트 → 면접 2회 → 최종 합격'),
            ('접수 기간', '2026-11-16 ~ 2026-12-10'),
            ('문의', '채용 담당자 jobs@example.com'),
        ],
    },
]

PAGE_CSS = """
@page { size: A4; margin: 14mm; }
* { box-sizing: border-box; }
body { margin: 0; background: #f2f4f7; color: #1d2433;
  font-family: 'Apple SD Gothic Neo', 'Noto Sans KR', sans-serif; }
.site { background: #1f4fd1; color: #fff; padding: 14px 28px; font-weight: 700; font-size: 18px; }
.site span { font-weight: 400; font-size: 13px; opacity: .85; margin-left: 10px; }
.wrap { max-width: 780px; margin: 24px auto; background: #fff; border: 1px solid #dfe3ea;
  border-radius: 10px; padding: 28px 32px; }
.company { color: #5b6475; font-size: 15px; }
h1 { font-size: 24px; margin: 6px 0 18px; }
table { width: 100%; border-collapse: collapse; font-size: 14.5px; }
th { width: 130px; text-align: left; background: #f6f8fb; color: #3a4357; font-weight: 600;
  vertical-align: top; }
th, td { border-top: 1px solid #e6e9ef; padding: 10px 12px; line-height: 1.55; }
.notice { margin-top: 18px; font-size: 12.5px; color: #8a5a00; background: #fff7e6;
  border: 1px solid #ffe2a8; border-radius: 6px; padding: 8px 12px; }
@media print { body { background: #fff; } .wrap { border: 0; margin: 0; padding: 0; } }
"""


def build_html(p: dict) -> str:
    rows = '\n'.join(
        f'<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>' for k, v in p['rows']
    )
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>{html.escape(p['title'])}</title><style>{PAGE_CSS}</style></head><body>
<div class="site">가상 채용정보<span>해커톤 시연용</span></div>
<div class="wrap">
  <div class="company">{html.escape(p['company'])}</div>
  <h1>{html.escape(p['title'])}</h1>
  <table>{rows}</table>
  <div class="notice">{html.escape(NOTICE)}</div>
</div></body></html>"""


def chrome(*args: str) -> None:
    subprocess.run(
        [CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars', *args],
        check=True,
        capture_output=True,
    )


def write_text_pdf(src: Path, out: Path) -> None:
    chrome('--no-pdf-header-footer', f'--print-to-pdf={out}', src.as_uri())


def write_png(src: Path, out: Path) -> None:
    chrome(
        '--window-size=860,960',
        '--force-device-scale-factor=2',
        f'--screenshot={out}',
        src.as_uri(),
    )


def write_image_pdf(text_pdf: Path, out: Path, dpi: int = 160) -> None:
    """텍스트 PDF의 각 페이지를 이미지로 바꿔 글자를 복사할 수 없게 만든다."""
    src = pymupdf.open(text_pdf)
    dst = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=dpi)
        new = dst.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, pixmap=pix)
    dst.save(out, garbage=4, deflate=True)


def write_xlsx(p: dict, out: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = '채용공고'
    ws['A1'] = p['title']
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells('A1:B1')
    ws.append(['항목', '내용'])
    for cell in ws[2]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDE6F7')
    for row in p['rows']:
        ws.append(list(row))
    ws.append([])
    ws.append(['비고', NOTICE])
    ws.column_dimensions['A'].width = 16
    ws.column_dimensions['B'].width = 80
    for row in ws.iter_rows(min_row=3):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical='top')
    wb.save(out)


def write_csv(p: dict, out: Path, encoding: str) -> None:
    with out.open('w', encoding=encoding, newline='') as f:
        w = csv.writer(f)
        w.writerow(['항목', '내용'])
        w.writerows(p['rows'])
        w.writerow(['비고', NOTICE])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for p in POSTINGS:
            # 생성 파일만 덮어쓴다. 같은 폴더의 expected.json(손으로 쓴 정답)은 지우지 않는다.
            d = OUT_DIR / p['id']
            d.mkdir(exist_ok=True)
            src = Path(tmp) / f'{p["id"]}.html'
            src.write_text(build_html(p), encoding='utf-8')
            write_text_pdf(src, d / f'{p["id"]}_text.pdf')
            write_image_pdf(d / f'{p["id"]}_text.pdf', d / f'{p["id"]}_image.pdf')
            write_png(src, d / f'{p["id"]}.png')
            write_xlsx(p, d / f'{p["id"]}.xlsx')
            write_csv(p, d / f'{p["id"]}_utf8.csv', 'utf-8')
            write_csv(p, d / f'{p["id"]}_euckr.csv', 'euc-kr')
            meta = {k: p[k] for k in ('id', 'scenario', 'title', 'company')}
            (d / 'source.json').write_text(
                json.dumps({**meta, 'rows': p['rows']}, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
            print('생성:', d.relative_to(OUT_DIR.parent.parent.parent))


if __name__ == '__main__':
    main()
