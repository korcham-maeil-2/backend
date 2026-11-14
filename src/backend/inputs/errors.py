"""파일 입력 오류. API가 422(사용자가 고칠 문제) 또는 502(바깥 서비스 문제)로 바꿔 보낸다."""

from enum import StrEnum


class InputErrorCode(StrEnum):
    FILE_MISSING = 'FILE_MISSING'  # 파일을 하나도 안 보냄
    FILE_TOO_MANY = 'FILE_TOO_MANY'  # 파일 수 초과
    FILE_TOO_LARGE = 'FILE_TOO_LARGE'  # 파일 하나·합계·요청 크기 초과
    FILE_EMPTY = 'FILE_EMPTY'  # 0바이트 파일
    FILE_DUPLICATE = 'FILE_DUPLICATE'  # 같은 파일을 두 번 넣음
    FILE_TYPE_UNSUPPORTED = 'FILE_TYPE_UNSUPPORTED'  # 받지 않는 형식, 확장자와 내용이 다름
    FILE_UNREADABLE = 'FILE_UNREADABLE'  # 손상·암호·인코딩 판별 실패
    FILE_TOO_LONG = 'FILE_TOO_LONG'  # 쪽 수·칸 수·글자 수 초과
    FILE_NO_TEXT = 'FILE_NO_TEXT'  # 읽었는데 글자가 없음
    DEMO_FILE_ONLY = 'DEMO_FILE_ONLY'  # 시연(가짜 응답) 모드에서 준비 안 된 이미지


class FileInputError(Exception):
    """사용자가 고칠 수 있는 파일 문제 (→ 422). message는 화면에 그대로 보여 줄 한국어."""

    def __init__(self, code: InputErrorCode, message: str, file_name: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = f'{file_name}: {message}' if file_name else message
        self.file_name = file_name


class ReadError(Exception):
    """이미지 글자 읽기(Claude 호출)가 실패했을 때 (→ 502). message는 사용자에게 보여 줄 한국어."""
