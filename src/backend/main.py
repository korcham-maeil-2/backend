"""백엔드 서버의 시작점 — FastAPI 앱을 만들고 그래프·DB·라우터를 붙인다.

[FastAPI 기본 개념]
FastAPI는 "주소(URL)로 요청이 오면 파이썬 함수를 실행하고 결과를 JSON으로 돌려주는" 웹 서버 도구다.
  - @app.get('/health') 처럼 함수 위에 주소를 붙이면, 그 주소로 온 요청을 그 함수가 처리한다.
  - 함수가 dict나 Pydantic 모델을 돌려주면 자동으로 JSON 응답이 된다.
  - 서버를 켜면 http://localhost:8000/docs 에서 모든 API를 눌러 보며 시험할 수 있다 (자동 생성).

실행 방법 (backend 폴더에서):
    uv run fastapi dev src/backend/main.py
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from backend.api.errors import register_error_handlers
from backend.api.sessions import router as sessions_router
from backend.graph import build_graph
from backend.normalize import Extractor

# 그래프 상태를 저장할 SQLite 파일 위치 (Docker에서는 이 경로를 볼륨에 둔다).
# SQLite는 별도 DB 서버 없이 파일 하나로 동작하는 데이터베이스다.
DEFAULT_DB_PATH = 'var/checkpoints.sqlite'

# 프론트엔드(Next.js) 개발 서버 주소.
DEFAULT_CORS_ORIGINS = 'http://localhost:3000'


def create_app(db_path: str | None = None, extractor: Extractor | None = None) -> FastAPI:
    """FastAPI 앱을 만들어 돌려준다.

    함수로 감싼 이유: 테스트에서 매번 새 임시 DB 파일로 앱을 따로 만들 수 있게 하려고.
    db_path를 안 주면 환경 변수 CHECKPOINT_DB_PATH, 그것도 없으면 기본 경로를 쓴다.
    extractor: 공고 정리에 쓸 LLM 호출. 안 주면 실제 Claude(ANTHROPIC_API_KEY 필요).
      테스트는 API를 부르지 않도록 항상 가짜를 넣는다.
    """
    db_path = db_path or os.getenv('CHECKPOINT_DB_PATH', DEFAULT_DB_PATH)

    # [lifespan — 서버가 켜질 때 / 꺼질 때 할 일]
    # yield 앞: 서버가 켜질 때 한 번 실행 (DB 연결 열기, 그래프 만들기)
    # yield 뒤: 서버가 꺼질 때 실행 (async with 블록이 끝나면서 DB 연결이 자동으로 닫힌다)
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)  # var/ 폴더가 없으면 만든다
        # AsyncSqliteSaver: LangGraph 상태를 SQLite 파일에 저장하는 체크포인터.
        # 'Async'는 DB를 기다리는 동안 서버가 다른 요청도 처리할 수 있다는 뜻(비동기).
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            # app.state: 앱 전체에서 같이 쓰는 보관함. 요청 처리 함수들이 여기서 그래프를 꺼내 쓴다.
            app.state.graph = build_graph(saver, extractor)
            yield

    app = FastAPI(title='월급지도 API', version='0.1.0', lifespan=lifespan)

    # 실패 응답을 모두 {code, message, details} 한 가지 모양으로 맞춘다 (api/errors.py 참고).
    # 순서 주의: 반드시 CORS 설정보다 먼저 불러야 500 에러 응답에도 CORS 헤더가 붙는다.
    register_error_handlers(app)

    # [CORS — 다른 주소의 웹페이지가 이 서버를 부를 수 있게 허락하기]
    # 브라우저는 보안 때문에 localhost:3000(프론트)에서 localhost:8000(백엔드)으로 가는 요청을
    # 기본적으로 막는다. 허락할 주소 목록을 여기에 적어 두면 막지 않는다.
    # 배포할 때는 환경 변수 CORS_ORIGINS에 실제 주소를 쉼표로 이어 적는다.
    origins = os.getenv('CORS_ORIGINS', DEFAULT_CORS_ORIGINS).split(',')
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins if o.strip()],
        allow_methods=['*'],
        allow_headers=['*'],
    )

    # 서버가 살아 있는지 확인하는 주소. GET /health → {"status": "ok"}
    @app.get('/health')
    def health() -> dict[str, str]:
        return {'status': 'ok'}

    # 세션 관련 주소들(/sessions ...)은 api/sessions.py에 모아 두고 여기서 붙인다.
    app.include_router(sessions_router)
    return app


# uvicorn·fastapi 명령이 찾아서 실행하는 앱 객체.
app = create_app()
