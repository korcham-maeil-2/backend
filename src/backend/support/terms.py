"""정착 플랜의 정책 카드 — 정책 원문을 Claude가 읽어 '무엇을 주는지·주의할 점·신청 조건'을 정리한다.

[개인정보를 보내지 않는다] (대회 규정 제7조)
Claude에 가는 것은 온통청년의 공개 정책 글뿐이다. 사용자 나이·소득·되묻기 답은 보내지 않고,
그것으로 맞는지 보는 일은 코드(화면)가 한다. 정책마다 한 번 읽고 저장해 모든 사용자가 같이 쓴다
(같은 정책을 여러 사람이 봐도 비용은 한 번).

[정확도 실험에서 정한 안전장치] (정책 30개를 원문과 대조: 지어낸 인용 0개, 큰 실수 3개)
  - 인용은 코드가 원문과 대조한다. 원문에 없는 인용의 조건·주의·금액은 버린다.
  - '신규 가입 중단'·'예산 소진 시 마감' 같은 주의는 따로 뽑는다 (요약에 빠졌던 실수).
  - 금액은 숫자로 바꾸지 않고 원문 구절 그대로 준다
    ('사망 시 최대 7천만원'을 받는 돈으로 읽은 실수).
  - 조건으로 정책을 빼지 않는다. 조건 종류(kind)는 되묻기 질문을 고르는 데만 쓴다
    (고령자의 단독세대주 조건을 세대주 조건으로 읽은 실수). 빼는 것은 나이·지역처럼
    API가 정해진 칸으로 주는 값으로만 한다 (support/policies.py).
  - 원문(온통청년 요약)에 없는 조건도 있다 → 화면은 늘 '신청 전 공고문 확인'과 링크를 함께 둔다.
"""

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

import anthropic
from pydantic import Field, ValidationError

from backend.calc.remaining import OutputModel
from backend.claude import FailureMessages, answer_text, create_message
from backend.normalize.checks import squash
from backend.privacy import mask_text
from backend.support.youthcenter import PolicySource, SupportError

logger = logging.getLogger(__name__)

Kind = Literal[
    'age',
    'income',
    'no_house',
    'householder',
    'moved_in',
    'residence',
    'employed',
    'unmarried',
    'student',
    'other',
]
MAX_IDS = 20  # 한 번에 읽을 정책 수 (플랜에 보이는 정책만 부른다)
READ_THREADS = 4  # 처음 보는 정책을 동시에 읽는 수


class Condition(OutputModel):
    kind: Kind = Field(description='조건 종류 (되묻기 질문을 고를 때만 쓴다. 정책을 빼지 않는다)')
    text: str = Field(description='조건을 짧게 (해요체 아님, 40자 안)')
    quote: str = Field(description='원문 그대로의 구절 (코드가 원문과 대조함)')


class PolicyTerms(OutputModel):
    """정책 하나를 읽은 결과. 인용은 모두 원문에 있는 것만 남는다."""

    summary: str = Field(description='무엇을 지원하는지 한 줄')
    caution: str | None = Field(description='접수 중단·예산 소진 마감 같은 주의 (원문 구절)')
    amount: str | None = Field(description='받는 금액 (숫자로 바꾸지 않은 원문 구절)')
    conditions: list[Condition]


class TermsResult(OutputModel):
    status: Literal['ok', 'off'] = Field(description='off: 이 서버는 정책을 읽지 않는다 (키 없음)')
    terms: dict[str, PolicyTerms] = Field(description='정책 번호 → 읽은 결과 (못 읽은 정책은 없다)')


# ── Claude가 읽기 ─────────────────────────────────────────────────

SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['summary', 'caution', 'amount', 'conditions'],
    'properties': {
        'summary': {'type': 'string'},
        'caution': {'type': ['string', 'null']},
        'amount': {'type': ['string', 'null']},
        'conditions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['kind', 'text', 'quote'],
                'properties': {
                    'kind': {'type': 'string', 'enum': list(Kind.__args__)},
                    'text': {'type': 'string'},
                    'quote': {'type': 'string'},
                },
            },
        },
    },
}
SYSTEM = """\
You read one Korean youth-policy notice and fill the JSON schema. Rules:
- Use ONLY what the text says. Never infer, guess, or add common knowledge.
- summary: what the policy gives, one short Korean phrase (<= 40 chars).
- caution: copy the exact sentence part saying the policy stopped/suspended new applications, ended,
  or closes early when the budget runs out. null if there is none.
- amount: copy the exact phrase with the main money a person receives (e.g. '월 최고 25만 원(최장
  36개월)'). Do not convert to numbers. If the money is only a loan limit, an interest rate, an
  insurance payout for accidents, or unclear, null.
- conditions: every eligibility condition stated, one item each. kind:
  age(나이), income(소득·중위소득), no_house(applicant must own no house),
  householder(applicant must be 세대주), moved_in(전입 시기), residence(거주 기간·거주지),
  employed(재직·취업·구직 상태),
  unmarried(혼인), student(재학), other. Use no_house/householder ONLY when it applies to a young
  applicant like the reader; otherwise other.
  text: the condition in short plain Korean (<= 40 chars).
  quote: copy the exact characters from the text (no paraphrase, no ellipsis)."""
FAILURE = FailureMessages(
    task='정책 조건 읽기',
    busy='잠시 후 다시 시도해 주세요.',
    failed='정책 조건을 읽지 못했어요.',
    unreachable='정책 조건을 읽지 못했어요.',
    refusal='정책 조건을 읽지 못했어요.',
    too_long='정책 조건을 읽지 못했어요.',
)


class TermsError(Exception):
    pass


TermsReader = Callable[[str], dict[str, Any]]


class ClaudeTermsReader:
    """정책 글 → 답(dict). 키는 SDK가 환경 변수에서 읽는다."""

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def __call__(self, text: str) -> dict[str, Any]:
        response = create_message(
            lambda: self.client,
            TermsError,
            FAILURE,
            max_tokens=4000,
            output_config={'effort': 'medium', 'format': {'type': 'json_schema', 'schema': SCHEMA}},
            system=SYSTEM,
            messages=[{'role': 'user', 'content': text}],
        )
        try:
            return json.loads(answer_text(response))
        except json.JSONDecodeError as e:
            raise TermsError(FAILURE.failed) from e


# ── 원문·검사 ─────────────────────────────────────────────────────


def source_text(policy: dict[str, Any]) -> str:
    """Claude에 보낼 정책 글 (공개 자료만). 칸 이름을 붙여 한 덩어리로."""
    low, high = policy.get('sprtTrgtMinAge'), policy.get('sprtTrgtMaxAge')
    parts = [
        ('정책명', policy.get('plcyNm')),
        ('지원내용', policy.get('plcySprtCn')),
        ('소득조건', policy.get('earnEtcCn')),
        ('추가자격', policy.get('addAplyQlfcCndCn')),
        ('나이', f'{low}~{high}세' if low and high and str(high) != '0' else None),
    ]
    return '\n'.join(f'[{k}] {str(v).strip()}' for k, v in parts if v and str(v).strip())


def checked(answer: dict[str, Any], text: str) -> PolicyTerms | None:
    """답을 원문과 대조한다. 원문에 없는 인용의 조건·주의·금액은 버린다. 모양이 틀리면 None."""
    whole = squash(text)
    found = lambda quote: bool(quote) and squash(quote) in whole  # noqa: E731
    try:
        terms = PolicyTerms.model_validate(answer)
    except ValidationError:
        logger.warning('정책 조건 답의 모양이 틀림')
        return None
    dropped = [c for c in terms.conditions if not found(c.quote)]
    if dropped:
        logger.warning('원문에 없는 인용 %d개를 버림', len(dropped))
    return terms.model_copy(
        update={
            'conditions': [c for c in terms.conditions if found(c.quote)],
            'caution': terms.caution if found(terms.caution) else None,
            'amount': terms.amount if found(terms.amount) else None,
        }
    )


# ── 저장 (같은 SQLite 파일, 정책 번호마다 한 줄) ──────────────────────


class TermsStore:
    """정책 번호 → (원문 지문, 읽은 결과). 원문이 바뀌면 지문이 달라져 다시 읽는다."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        with closing(self._connect()) as db, db:
            db.execute(
                'CREATE TABLE IF NOT EXISTS policy_terms '
                '(plcy_no TEXT PRIMARY KEY, source_sha TEXT, body TEXT, saved_at REAL)'
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5)

    def get(self, plcy_no: str, sha: str) -> PolicyTerms | None:
        try:
            with self._lock, closing(self._connect()) as db, db:
                row = db.execute(
                    'SELECT body FROM policy_terms WHERE plcy_no = ? AND source_sha = ?',
                    (plcy_no, sha),
                ).fetchone()
            return PolicyTerms.model_validate_json(row[0]) if row else None
        except (sqlite3.Error, ValidationError):
            logger.warning('저장해 둔 정책 조건을 읽지 못함 (%s)', plcy_no, exc_info=True)
            return None

    def put(self, plcy_no: str, sha: str, terms: PolicyTerms) -> None:
        try:
            with self._lock, closing(self._connect()) as db, db:
                db.execute(
                    'INSERT OR REPLACE INTO policy_terms VALUES (?, ?, ?, ?)',
                    (plcy_no, sha, terms.model_dump_json(), time.time()),
                )
        except sqlite3.Error:
            logger.warning('정책 조건을 저장하지 못함 (%s)', plcy_no, exc_info=True)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class PolicyTermsService:
    """플랜에 보이는 정책들의 조건을 돌려준다. 저장된 것은 바로, 처음 보는 것만 Claude로 읽는다."""

    def __init__(self, source: PolicySource, reader: TermsReader, store: TermsStore) -> None:
        self._source = source
        self._read = reader
        self._store = store

    def _originals(self, lawd_cds: Iterable[str]) -> dict[str, dict[str, Any]]:
        """근무지 지역들의 정책 원문 (온통청년 조회는 하루 캐시라 보통 바로 끝난다)."""
        found: dict[str, dict[str, Any]] = {}
        for code in dict.fromkeys(lawd_cds):
            try:
                for policy in self._source.policies_for(code):
                    found.setdefault(str(policy.get('plcyNo')), policy)
            except SupportError:
                logger.warning('정책 원문을 불러오지 못함 (%s)', code)
        return found

    def _one(self, plcy_no: str, policy: dict[str, Any]) -> PolicyTerms | None:
        text = source_text(policy)
        sha = _sha(text)
        saved = self._store.get(plcy_no, sha)
        if saved is not None:
            return saved
        try:
            # Claude는 가린 글을 받는다 (create_message가 보내기 직전에 가림) → 가린 글과 대조
            terms = checked(self._read(text), mask_text(text))
        except TermsError:
            return None
        except Exception:  # 정책 하나가 잘못돼도 나머지는 보여 준다 (요청 전체가 500이 되지 않게)
            logger.exception('정책 조건을 읽다가 예상 못 한 오류 (%s)', plcy_no)
            return None
        if terms is not None:
            self._store.put(plcy_no, sha, terms)
        return terms

    def get(self, lawd_cds: list[str], ids: list[str]) -> dict[str, PolicyTerms]:
        originals = self._originals(lawd_cds)
        wanted = [i for i in dict.fromkeys(ids) if i in originals][:MAX_IDS]
        with ThreadPoolExecutor(READ_THREADS) as pool:
            read = pool.map(lambda i: (i, self._one(i, originals[i])), wanted)
            return {i: terms for i, terms in read if terms is not None}
