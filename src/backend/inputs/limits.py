"""파일 입력 상한. 프론트(src/app/postings/new/limits.ts)도 같은 값으로 먼저 막는다.

값을 바꾸면 프론트 limits.ts도 함께 바꾼다.
"""

MB = 1024 * 1024

MAX_FILES = 5  # 한 번에 올리는 파일 수
MAX_FILE_BYTES = 10 * MB  # 파일 하나
MAX_TOTAL_BYTES = 20 * MB  # 한 번에 올리는 파일 합계
TOTAL_TOO_LARGE_MESSAGE = f'파일 크기 합계는 {MAX_TOTAL_BYTES // MB}MB까지예요. 나눠서 넣어 주세요.'
# 요청 전체(파일 + 붙여 넣은 글 + 멀티파트 머리글). 파일 합계에 여유를 조금 더한다.
MAX_REQUEST_BYTES = MAX_TOTAL_BYTES + 1 * MB

MAX_PDF_PAGES = 20  # PDF 쪽 수
# 이미지 인식으로 읽는 장 수 (요청 하나, 모든 파일 합계). 이미지 1장, 이미지 PDF 1쪽,
# 긴 캡처를 자른 조각 1개가 각각 1장. 한 장에 약 $0.025라 8장이면 약 $0.2.
MAX_VISION_PAGES = 8
MAX_IMAGE_PIXELS = (
    60_000_000  # 이미지 픽셀 수 (가로×세로). 압축 폭탄 막기. 휴대폰 50MP 사진은 받는다
)
MAX_XLSX_UNZIPPED_BYTES = 50 * MB  # xlsx(zip) 압축을 푼 크기 합계. 압축 폭탄 막기
MAX_SHEET_CELLS = 20_000  # 표에서 값이 있는 칸 수
# 표에서 훑어보는 칸 수 (빈칸 포함). 맨 끝 칸(XFD1048576)에 서식만 있는 엑셀은 빈칸 수십억 개를
# 훑느라 서버가 멈춘다 → 빈칸도 세어서 이만큼 넘으면 멈춘다.
MAX_SHEET_SCAN_CELLS = 300_000
MAX_TEXT_CHARS = 30_000  # 파일에서 읽은 글 + 붙여 넣은 글 합계
MAX_MESSAGE_CHARS = 30_000  # 붙여 넣은 글 하나

# Claude 이미지 입력: 긴 변 2576px까지 그대로 본다. 더 크면 서버가 줄이므로 미리 줄여 보낸다.
VISION_MAX_EDGE = 2576
# Claude 이미지 하나 크기 한도(5MB)보다 넉넉히 작게 (base64로 바꾸면 4/3배가 된다).
VISION_MAX_IMAGE_BYTES = 3_500_000
# 세로가 가로의 이 배수를 넘는 긴 캡처는 잘라서 읽는다 (통째로 줄이면 글자가 뭉개진다).
TALL_IMAGE_RATIO = 2.5

# 이미지 인식 하루 상한 (장 수). 공개 서버라 누가 반복해서 올려도 API 한도(100달러)가
# 바닥나지 않게 한다. 환경 변수 VISION_DAILY_PAGE_LIMIT로 바꿀 수 있다.
DEFAULT_VISION_DAILY_PAGE_LIMIT = 300
