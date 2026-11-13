import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from backend.api.sessions import router as sessions_router
from backend.graph import build_graph

# SQLite 파일 하나에 세션별 그래프 상태를 저장한다 (Docker에서는 볼륨에 둔다).
DEFAULT_DB_PATH = 'var/checkpoints.sqlite'
DEFAULT_CORS_ORIGINS = 'http://localhost:3000'


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.getenv('CHECKPOINT_DB_PATH', DEFAULT_DB_PATH)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            app.state.graph = build_graph(saver)
            yield

    app = FastAPI(title='월급지도 API', version='0.1.0', lifespan=lifespan)

    origins = os.getenv('CORS_ORIGINS', DEFAULT_CORS_ORIGINS).split(',')
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins if o.strip()],
        allow_methods=['*'],
        allow_headers=['*'],
    )

    @app.get('/health')
    def health() -> dict[str, str]:
        return {'status': 'ok'}

    app.include_router(sessions_router)
    return app


app = create_app()
