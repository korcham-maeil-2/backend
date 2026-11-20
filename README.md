# 월급지도 backend

**시연** https://maeil2-frontend.sunghoyaaa.com · **API 문서** https://maeil2-backend.sunghoyaaa.com/docs

저장소: **backend** (여기) · [frontend](https://github.com/korcham-maeil-2/frontend) · [docs](https://github.com/korcham-maeil-2/docs)

---

취업·이주를 고민하는 청년이 공고를 넣으면, 일자리마다 **매달 실제로 남는 돈**을 비교하고 1년 정착 플랜까지 만들어 주는 AI 에이전트의 백엔드입니다. 2026 대한민국 청년 CAREER FAIR AI 해커톤 출품작(첫월급연구소).

FastAPI + LangGraph 1.x, Python 3.13, 패키지 관리는 [uv](https://docs.astral.sh/uv/).

## 원칙

- **숫자는 코드가 계산하고, 해석·질문·문장은 AI가 쓴다.** 세금·4대보험·매달 남는 돈·손익분기 연봉은 순수 Python 계산 엔진(`calc/`)이 만든다.
- **값마다 출처와 신뢰도**(확인 / 추정 / 사용자 입력)를 붙인다. AI가 읽은 값은 원문 인용을 코드가 원문과 대조한다.
- **개인정보는 AI에 보내기 전에 가린다**(`privacy.py`). 원본 파일은 저장하지 않고, 세션은 마지막 사용 후 24시간이 지나면 지운다(`retention.py`).

## 에이전트 흐름

```
route_input → normalize → confirm_table ⏸ → check_missing ⏸ → get_rent ∥ get_local_support → calculate → write_report ⇄ verify_report
```

| 노드 | 하는 일 | AI |
|---|---|---|
| `route_input` | 글·PDF·엑셀·이미지 읽기 | 이미지·글자 없는 PDF만 Claude Sonnet 5.5가 글자로 옮김 |
| `normalize` | 공고 → 회사별 비교표 (값 + 원문 인용) | Claude Sonnet 5.5, 보내기 전 마스킹 |
| `confirm_table` ⏸ | 사용자가 표를 확인·수정 (LangGraph `interrupt`) | — |
| `check_missing` ⏸ | 계산을 막는 빈 값만 되묻기 | — |
| `get_rent` ∥ `get_local_support` | 근무지 월세 실거래가·청년 정책 병렬 조회 | — |
| `calculate` | 세후 월급, 통근·이사별 매달 남는 돈, 손익분기 연봉 | — (코드) |
| `write_report` ⇄ `verify_report` | 결과 설명을 쓰고 검증, 실패하면 다시 쓰기(최대 3번) | Sonnet 5.5 작성, Haiku 5.5 검토. 금액은 자리표시로만 쓰고 코드가 채움 |

정착 플랜(`plan.py`)과 정책 신청 조건 정리(`support/terms.py`)는 계산이 끝난 세션에서 만든다.

## 폴더

```
src/backend/
  api/          HTTP 주소 (세션, 월세, 정착 플랜 참고 자료)
  graph/        LangGraph 상태·노드·그래프
  inputs/       파일 읽기 (PDF·엑셀·CSV·이미지), 입력 상한
  normalize/    공고 → 비교표, 원문 대조 검사
  calc/         세금·4대보험·남는 돈·손익분기 연봉 (AI 아님)
  report/       리포트 작성·검증 에이전트
  support/      온통청년 청년 정책, 정책 조건 정리
  public_data/  국토부 실거래가, 고용24 훈련과정, 금감원 적금
  plan.py       1년 정착 플랜
  privacy.py    개인정보 마스킹
  retention.py  오래된 세션 지우기
  monitoring.py 오류 추적 (GlitchTip, Sentry 형식)
tests/          pytest (계산 엔진 기댓값은 간이세액표·고시 원문으로 직접 만듦)
data/           시군구 코드표, 시연용 가상 공고·사용자 (data/demo/README.md)
```

## 실행

```bash
uv sync
cp .env.example .env   # 필요한 키만 채운다
uv run fastapi dev src/backend/main.py
```

http://localhost:8000/docs 에서 모든 API를 눌러 볼 수 있습니다.

**AI 비용 없이 해 보기:** `.env`에 `NORMALIZE_MODE=fixture`를 넣으면 시연 공고(`data/demo/`)는 미리 받아 둔 답으로 처리합니다. 키가 하나도 없어도 서버는 켜지고, 해당 기능만 빠집니다(월세 시세, 청년 정책 등).

## 환경 변수

자세한 설명은 [`.env.example`](.env.example)에 있습니다. 키는 저장소에 올리지 않습니다.

| 변수 | 용도 | 없으면 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 공고 정리·이미지 읽기·리포트·정책 조건 | 시연 모드만 동작 |
| `NORMALIZE_MODE` | `claude`(기본) / `fixture`(시연, 비용 0원) | `claude` |
| `DATA_GO_KR_SERVICE_KEY` | 국토부 전월세 실거래가 | 월세 시세 없이 사용자 입력만 |
| `YOUTHCENTER_API_KEY` | 온통청년 청년 정책 | 청년 지원을 찾지 않음 |
| `FINLIFE_API_KEY` | 금감원 적금 기본금리 | 적금 참고 숨김 |
| `WORK24_TRAINING_*_KEY` | 고용24 재직자 훈련과정 | 강의 참고 숨김 |
| `VISION_DAILY_PAGE_LIMIT` | 이미지 읽기 하루 상한(장) | 300 |
| `SESSION_TTL_HOURS` | 세션 보관 시간 | 24 |
| `SENTRY_DSN` | 오류 추적 (GlitchTip) | 보내지 않음 |
| `CORS_ORIGINS`, `CHECKPOINT_DB_PATH` | 허용할 화면 주소, 세션 SQLite 위치 | 로컬 기본값 |

## 테스트

```bash
uv run pytest
uv run ruff format --check . && uv run ruff check .
```

PR마다 GitHub Actions가 같은 검사를 돌립니다.

## 배포

라즈베리파이 홈서버에서 Docker Compose로 돌립니다. `main`에 병합되면 CI가 통과한 뒤 셀프호스티드 러너가 자동으로 다시 빌드·배포합니다(`.github/workflows/deploy.yml`).

```bash
docker compose up -d --build
```

세션 SQLite는 Docker 볼륨(`wolgeup-backend-data`)에 있어 다시 배포해도 남습니다(24시간 지난 세션은 지움).

## 데이터 출처

| 데이터 | 제공 | 쓰는 곳 |
|---|---|---|
| 전월세 실거래가 | 국토교통부 (공공데이터포털) | 근무지 월세 시세 |
| 청년정책 | 온통청년 | 근무지 청년 지원 |
| 국민내일배움카드·컨소시엄 훈련과정 | 고용24 | 재직자 강의 |
| 금융상품 한눈에 | 금융감독원 (금융상품통합비교공시시스템) | 적금 기본금리 (가입 권유 아님) |
| 근로소득 간이세액표 | 소득세법 시행령 [별표2] (2026. 2. 27. 개정) | 소득세 |
| 4대보험 요율 | 2026년 고시 | 4대보험 |
| 법정동코드 | 행정표준코드관리시스템 (2026-10-01) | 근무지 → 시군구 코드 |

API 명세와 이용 조건은 [docs/api](https://github.com/korcham-maeil-2/docs/tree/main/api)에 있습니다. 채용 사이트 크롤링은 하지 않습니다.

## Credits / Licenses

주요 오픈소스: FastAPI (MIT), LangGraph·LangChain Core (MIT), Anthropic Python SDK (MIT), PyMuPDF (AGPL-3.0, 이 저장소는 공개), openpyxl (MIT), xlrd (BSD), Pillow (MIT-CMU), defusedxml (PSF), httpx (BSD), Sentry SDK (MIT).
AI 활용 내역과 출처 전체 목록은 [docs/제출](https://github.com/korcham-maeil-2/docs/tree/main/%EC%A0%9C%EC%B6%9C)에 있습니다.
