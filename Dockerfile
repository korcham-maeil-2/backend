# 월급지도 백엔드 이미지 (라즈베리파이 arm64 / x86 모두 동작)
#
# [Docker 기본]
# Dockerfile = "이 프로그램을 돌릴 작은 컴퓨터(컨테이너)를 만드는 순서".
# 한 줄씩 위에서 아래로 실행되고, 바뀌지 않은 단계는 캐시를 재사용해 다시 빌드가 빠르다.
# 그래서 자주 안 바뀌는 의존성 설치를 먼저, 자주 바뀌는 코드 복사를 나중에 둔다.

# uv와 Python 3.13이 이미 들어 있는 공식 이미지
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# 바이트코드 미리 컴파일(시작 빠르게), 컨테이너 안에서는 Python을 따로 내려받지 않음
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# 1) 의존성만 먼저 설치 (uv.lock 그대로, 개발용 패키지 제외)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

# 2) 코드 복사 후 프로젝트 설치
COPY src ./src
RUN uv sync --locked --no-dev

# 3) 코드가 실행 중에 읽는 파일
#    - data/lawd_codes.csv: 공고 근무지 → 지역 코드 (공고 정리에 필요)
#    - tests/fixtures/{normalize,transcribe}: NORMALIZE_MODE=fixture(시연 모드, 비용 0원)의 미리 써 둔 답
#    (테스트 코드 자체는 .dockerignore로 빠진다)
COPY data/lawd_codes.csv ./data/
COPY tests/fixtures ./tests/fixtures

# .venv 안의 실행 파일(uvicorn 등)을 바로 쓸 수 있게
ENV PATH="/app/.venv/bin:$PATH" \
    CHECKPOINT_DB_PATH=/data/checkpoints.sqlite

# 세션 상태(SQLite)는 컨테이너가 아니라 볼륨(/data)에 저장해야 재배포해도 남는다
VOLUME ["/data"]
EXPOSE 8000

# --proxy-headers: 앞단 Nginx가 넘겨준 실제 주소(https 등)를 믿고 쓴다
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
