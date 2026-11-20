"""백엔드 서버의 시작점 — FastAPI 앱을 만들고 그래프·DB·라우터를 붙인다.

[FastAPI 기본 개념]
FastAPI는 "주소(URL)로 요청이 오면 파이썬 함수를 실행하고 결과를 JSON으로 돌려주는" 웹 서버 도구다.
  - @app.get('/health') 처럼 함수 위에 주소를 붙이면, 그 주소로 온 요청을 그 함수가 처리한다.
  - 함수가 dict나 Pydantic 모델을 돌려주면 자동으로 JSON 응답이 된다.
  - 서버를 켜면 http://localhost:8000/docs 에서 모든 API를 눌러 보며 시험할 수 있다 (자동 생성).

실행 방법 (backend 폴더에서):
    uv run fastapi dev src/backend/main.py
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from backend.api.errors import register_error_handlers, register_upload_size_limit
from backend.api.plan_extras import router as plan_router
from backend.api.rent import router as rent_router
from backend.api.sessions import router as sessions_router
from backend.graph import build_graph
from backend.inputs.read import FileReader, daily_budget_from_env
from backend.inputs.transcribe import ClaudeTranscriber, FixtureTranscriber, Transcriber
from backend.monitoring import init_monitoring
from backend.normalize import Extractor, make_extractor, parse_mode
from backend.normalize.fixture import example_postings
from backend.public_data.finlife import SavingsSource, savings_from_env
from backend.public_data.rent import HttpRentFetcher, RentCache, RentEstimator, RentFetcher
from backend.public_data.training import TrainingSource, training_from_env
from backend.report import ClaudeReviewer, ClaudeWriter, ReportAgent
from backend.retention import purge_forever, ttl_from_env
from backend.support import PolicySource, is_relevant, source_from_env
from backend.support.terms import ClaudeTermsReader, PolicyTermsService, TermsReader, TermsStore
from backend.support.youthcenter import API_KEY_ENV as YOUTHCENTER_KEY_ENV

# 그래프 상태를 저장할 SQLite 파일 위치 (Docker에서는 이 경로를 볼륨에 둔다).
# SQLite는 별도 DB 서버 없이 파일 하나로 동작하는 데이터베이스다.
DEFAULT_DB_PATH = 'var/checkpoints.sqlite'

# 프론트엔드(Next.js) 개발 서버 주소.
DEFAULT_CORS_ORIGINS = 'http://localhost:3000'

logger = logging.getLogger(__name__)


def _has_api_key_env() -> bool:
    """SDK가 환경 변수에서 읽는 인증 값(키 또는 토큰)이 있는지. 값 자체는 보지도 남기지도 않는다."""
    return any(
        os.getenv(name, '').strip() for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')
    )


def create_app(
    db_path: str | None = None,
    extractor: Extractor | None = None,
    transcriber: Transcriber | None = None,
    rent_fetcher: RentFetcher | None = None,
    support_source: PolicySource | None = None,
    report_agent: ReportAgent | None = None,
    savings_source: SavingsSource | None = None,
    training_source: TrainingSource | None = None,
    terms_reader: TermsReader | None = None,
) -> FastAPI:
    """FastAPI 앱을 만들어 돌려준다.

    함수로 감싼 이유: 테스트에서 매번 새 임시 DB 파일로 앱을 따로 만들 수 있게 하려고.
    db_path를 안 주면 환경 변수 CHECKPOINT_DB_PATH, 그것도 없으면 기본 경로를 쓴다.
    extractor: 공고 정리에 쓸 LLM 호출. 안 주면 환경 변수 NORMALIZE_MODE로 고른다.
      fixture면 미리 써 둔 응답(비용 0원, 화면 개발용),
      그 밖에는 실제 Claude(ANTHROPIC_API_KEY 필요).
      테스트는 API를 부르지 않도록 항상 가짜를 넣는다.
    transcriber: 이미지 글자 옮겨 적기. 안 주면 claude 모드는 실제 Claude(하루 한도 있음),
      fixture·테스트는 미리 받아 둔 시연 파일 글(비용 0원).
    rent_fetcher: 국토부 실거래가 조회. 안 주면 환경 변수 DATA_GO_KR_SERVICE_KEY가 있을 때만
      실제 공공데이터포털을 부른다 (무료, 결과는 같은 SQLite 파일에 캐시).
      키도 없으면 월세를 조회하지 않는다.
    support_source: 온통청년 청년정책 조회. 안 주면 환경 변수 YOUTHCENTER_API_KEY가 있을 때만
      실제 API를 부른다 (무료, 결과는 같은 SQLite 파일에 하루 캐시). 키도 없으면 조회하지 않는다.
    report_agent: 리포트 쓰기·검증 (report/agent.py). 안 주면 공고 정리가 실제 Claude 모드이고
      키가 있을 때만 실제 Claude로 쓴다 (시연 모드·테스트는 리포트를 쓰지 않는다: 비용 0원).
    savings_source / training_source: 정착 플랜의 적금(금감원)·재직자 강의(고용24) 조회.
      안 주면 환경 변수 FINLIFE_API_KEY / WORK24_TRAINING_*_KEY가 있을 때만 실제 API를 부른다.
    """
    db_path = db_path or os.getenv('CHECKPOINT_DB_PATH', DEFAULT_DB_PATH)
    # 공고 정리 방법: 'claude'(실제) / 'fixture'(미리 써 둔 응답) / 'custom'(테스트가 직접 넣음).
    # 오타는 parse_mode가 서버 시작 때 막는다.
    if extractor is None:
        normalize_mode = parse_mode(os.getenv('NORMALIZE_MODE'))
        extractor = make_extractor(normalize_mode)
        if normalize_mode == 'claude' and not _has_api_key_env():
            # 서버는 켠다 (테스트·CI가 키 없이 이 파일을 불러온다). 대신 시작 로그로 바로 알린다.
            # 이대로면 공고 정리·이미지 읽기 요청이 502로 실패한다.
            logger.warning(
                'NORMALIZE_MODE=claude인데 ANTHROPIC_API_KEY·ANTHROPIC_AUTH_TOKEN이 없습니다. '
                '키를 넣거나, 시연이면 NORMALIZE_MODE=fixture로 켜세요.'
            )
    else:
        normalize_mode = 'custom'
    if report_agent is None and normalize_mode == 'claude' and _has_api_key_env():
        report_agent = ReportAgent(ClaudeWriter(), ClaudeReviewer())
    # 정착 플랜 정책 카드: 정책 원문을 Claude가 읽는다 (리포트와 같은 조건: 실제 모드 + 키)
    if terms_reader is None and normalize_mode == 'claude' and _has_api_key_env():
        terms_reader = ClaudeTermsReader()
    if transcriber is not None:
        file_reader = FileReader(transcriber)
    elif normalize_mode == 'claude':
        file_reader = FileReader(ClaudeTranscriber(), daily_budget_from_env())
    else:
        file_reader = FileReader(FixtureTranscriber())

    if rent_fetcher is None and os.getenv('DATA_GO_KR_SERVICE_KEY', '').strip():
        rent_fetcher = HttpRentFetcher(os.environ['DATA_GO_KR_SERVICE_KEY'].strip())
    if rent_fetcher is None:
        logger.info('DATA_GO_KR_SERVICE_KEY가 없어 월세 실거래가를 조회하지 않습니다.')

    # 세션을 서버에 두는 시간 (SESSION_TTL_HOURS, 기본 24시간).
    # 잘못된 값이면 서버가 켜질 때 바로 멈춘다
    session_ttl = ttl_from_env()

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
            rent_estimator = (
                RentEstimator(rent_fetcher, RentCache(db_path)) if rent_fetcher else None
            )
            app.state.rent_estimator = rent_estimator
            # 캐시 표를 만들 때 db_path 폴더가 있어야 해서 여기서 만든다
            source = support_source or source_from_env(db_path, keep=is_relevant)
            if source is None:
                logger.info('YOUTHCENTER_API_KEY가 없어 청년 지원 정책을 조회하지 않습니다.')
            app.state.support_source = source
            app.state.policy_terms = (
                PolicyTermsService(source, terms_reader, TermsStore(db_path))
                if source is not None and terms_reader is not None
                else None
            )
            app.state.report_agent = report_agent
            app.state.graph = build_graph(saver, extractor, rent_estimator, source, report_agent)
            app.state.file_reader = file_reader
            app.state.savings_source = savings_source or savings_from_env()
            app.state.training_source = training_source or training_from_env()
            # 오래된 세션(사용자가 넣은 공고 글·생활비)을 주기적으로 지운다 (retention.py)
            purge = asyncio.create_task(purge_forever(saver, db_path, session_ttl))
            try:
                yield
            finally:
                purge.cancel()
                with suppress(asyncio.CancelledError):
                    await purge

    app = FastAPI(title='월급지도 API', version='0.1.0', lifespan=lifespan)

    # 실패 응답을 모두 {code, message, details} 한 가지 모양으로 맞춘다 (api/errors.py 참고).
    # 순서 주의: 반드시 CORS 설정보다 먼저 불러야 500 에러 응답에도 CORS 헤더가 붙는다.
    register_error_handlers(app)
    # 파일 올리기 크기 상한: 요청 본문을 받기 전에 Content-Length로 먼저 막는다 (CORS보다 먼저).
    register_upload_size_limit(app)

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
    # 청년 지원을 찾을 수 있는지 (공급자를 넣었거나 온통청년 키가 있음).
    # 화면이 나이 칸을 보일지 정한다
    local_support = support_source is not None or bool(os.getenv(YOUTHCENTER_KEY_ENV, '').strip())

    @app.get('/health')
    def health() -> dict[str, str | bool]:
        # normalize_mode: 프론트가 'fixture'면 '시연 모드' 표시를 띄운다.
        # local_support: false면 나이 칸을 숨긴다 (나이는 청년 지원을 고르는 데만 쓴다)
        return {'status': 'ok', 'normalize_mode': normalize_mode, 'local_support': local_support}

    examples = example_postings()  # 서버를 켤 때 한 번 읽는다 (파일이 없으면 바로 알 수 있게)

    @app.get('/examples')
    def get_examples() -> list[str]:
        """'예시로 사용법 보기'에 넣을 공고 글. 이 글만 넣으면 공고 정리에 Claude를 안 부른다."""
        return examples

    # 세션 관련 주소들(/sessions ...)은 api/sessions.py에 모아 두고 여기서 붙인다.
    app.include_router(sessions_router)
    app.include_router(rent_router)
    app.include_router(plan_router)
    return app


# 오류 추적 (GlitchTip): SENTRY_DSN이 있을 때만. 앱을 만들기 전에 켜야 FastAPI에 붙는다
init_monitoring()

# uvicorn·fastapi 명령이 찾아서 실행하는 앱 객체.
app = create_app()
