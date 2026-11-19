"""은행 적금 기본금리 (금융감독원 금융상품 한눈에 API) — 정착 플랜의 '모으기' 참고 (이슈 #18).

명세: docs/api/금감원_금융상품한눈에.md (실제 호출로 확인한 값)
  - GET https://finlife.fss.or.kr/finlifeapi/savingProductsSearch.json
      auth(32자리 키), topFinGrpNo=020000(은행), pageNo
  - 오류여도 HTTP 200 → result.err_cd가 '000'이 아니면 실패
  - baseList(상품) + optionList(기간·적립방식별 금리) → fin_co_no + fin_prdt_cd로 묶는다

고르는 법 (가입을 권하지 않는다 — 화면에 '가입 권유 아님'과 '가입 전 은행에서 확인'을 함께 쓴다):
  - 12개월 옵션이 있고 가입 제한이 없는(join_deny=1) 상품, 이름에 '기업'이 든 법인용은 뺀다
  - 상품마다 12개월 기본금리가 가장 높은 옵션 하나, 기본금리 높은 순으로 SAVINGS_MAX개
  - 우대금리(intr_rate2)는 조건이 자유 서술이라 참고로만 (명세 7절)
  - 실제 은행 적금 60개 가운데 이름에 '청년'이 든 상품은 없었다 (2026-11-17 조회).
    청년 전용 금융 지원(청년도약계좌 등)은 온통청년 정책 목록에 있다.

약관 (명세 7절): 받은 정보를 복제·저장하지 않는다 → DB에 쌓지 않고 메모리에 잠깐(CACHE_SECONDS)만.
화면에 출처 '금융상품통합비교공시시스템(금융감독원) 금융회사 상품정보'를 표시해야 한다.
"""

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import Field

from backend.calc.remaining import OutputModel
from backend.public_data import http

logger = logging.getLogger(__name__)

API_KEY_ENV = 'FINLIFE_API_KEY'
URL = 'https://finlife.fss.or.kr/finlifeapi/savingProductsSearch.json'
BANKS = '020000'
TERM = '12'
SAVINGS_MAX = 3
CACHE_SECONDS = 60 * 60  # 공시는 달마다 바뀐다. 약관상 오래 두지 않는다
# 금감원이 느려도 화면이 오래 기다리지 않게 짧게. 실패하면 이 동안 다시 묻지 않는다
# (youthcenter.py와 같은 방식)
TIMEOUT = httpx.Timeout(8.0, connect=3.0)
FAILURE_COOLDOWN_SECONDS = 120
MAX_PAGES = 5  # 은행 적금은 1쪽(60개). 응답이 이상해도 끝없이 돌지 않게
SOURCE = '금융상품통합비교공시시스템(금융감독원) 금융회사 상품정보'
SOURCE_URL = 'https://finlife.fss.or.kr'


class SavingsProduct(OutputModel):
    bank: str
    name: str
    rate: float = Field(description='12개월 기본금리 (연 %)')
    max_rate: float | None = Field(description='최고 우대금리 (연 %). 조건은 은행에서 확인')
    saving_type: str = Field(description="적립 방식 ('자유적립식' / '정액적립식')")
    limit_krw: int | None = Field(description='공시된 최고 한도 (원). 없으면 null')
    join_way: str | None = Field(description="가입 방법 (예: '인터넷,스마트폰')")


# 화면의 '매달 넣을 금액' 버튼. 남는 돈보다 많은 금액은 화면이 빼고 보여 준다
DEPOSITS = (100_000, 300_000, 500_000, 1_000_000)


class SavingsEstimate(OutputModel):
    """매달 같은 금액을 12개월 넣으면 (금리 1위 상품의 기본금리, 세전)."""

    monthly_krw: int
    principal_krw: int
    interest_krw: int = Field(description='세전 이자 (단리, 1,000원 단위 반올림)')
    total_krw: int


def estimate(monthly_krw: int, rate: float) -> SavingsEstimate:
    """12개월 정기적립 단리: 이자 = 금액 × 연이율 × (12+11+…+1)/12 = 금액 × 연이율 × 6.5."""
    # round()는 .5를 짝수로 보내서(58,500 → 58,000) 직접 반올림한다
    interest = int(monthly_krw * rate * 6.5 / 100 / 1000 + 0.5) * 1000
    principal = monthly_krw * 12
    return SavingsEstimate(
        monthly_krw=monthly_krw,
        principal_krw=principal,
        interest_krw=interest,
        total_krw=principal + interest,
    )


class SavingsList(OutputModel):
    """GET /plan/savings 응답."""

    products: list[SavingsProduct]
    # 숫자는 백엔드가 만든다: 화면은 고르고 보여 주기만
    estimates: list[SavingsEstimate] = Field(
        default_factory=list,
        description='DEPOSITS 금액마다 12개월 뒤 금액 (products[0] 기본금리 기준). 상품 없으면 []',
    )
    term_months: int = 12
    disclosed_month: str | None = Field(description='공시 기준월 (YYYYMM)')
    source: str = SOURCE
    source_url: str = SOURCE_URL
    status: str = Field(description='ok / unavailable(불러오지 못함) / off(키 없음)')
    reason: str | None = None


class FinlifeError(Exception):
    pass


Fetcher = Callable[[], dict[str, Any]]


def http_fetcher(api_key: str) -> Fetcher:
    """은행 권역 적금 전체 (페이지를 돌며 모은다). 실패하면 FinlifeError."""

    def fetch() -> dict[str, Any]:
        base, options, month, page = [], [], None, 1
        with http.client(TIMEOUT) as client:
            while True:
                params = {'auth': api_key, 'topFinGrpNo': BANKS, 'pageNo': page}
                try:
                    result = client.get(URL, params=params).json()['result']
                    if not isinstance(result, dict):
                        raise TypeError('result가 객체가 아님')
                    if result.get('err_cd') != '000':
                        raise FinlifeError(f'err_cd {result.get("err_cd")}')
                    pages = int(result.get('max_page_no') or 1)
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                    raise FinlifeError(type(e).__name__) from e
                # 응답 모양이 다르면 그 부분만 버린다 (500 대신)
                for key, into in (('baseList', base), ('optionList', options)):
                    items = result.get(key)
                    if isinstance(items, list):
                        into += [x for x in items if isinstance(x, dict)]
                if page >= min(pages, MAX_PAGES):
                    break
                page += 1
        if base:
            month = (
                base[0].get('dcls_month') if isinstance(base[0].get('dcls_month'), str) else None
            )
        return {'base': base, 'options': options, 'month': month}

    return fetch


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pick_savings(data: dict[str, Any], n: int = SAVINGS_MAX) -> list[SavingsProduct]:
    """12개월 기본금리 높은 순 n개 (모듈 설명의 고르는 법)."""
    base = {
        (b.get('fin_co_no'), b.get('fin_prdt_cd')): b
        for b in data.get('base', [])
        if b.get('join_deny') == '1' and '기업' not in (b.get('fin_prdt_nm') or '')
    }
    best: dict[tuple, SavingsProduct] = {}
    for o in data.get('options', []):
        key = (o.get('fin_co_no'), o.get('fin_prdt_cd'))
        b = base.get(key)
        rate = _float(o.get('intr_rate'))
        if b is None or str(o.get('save_trm')) != TERM or rate is None:
            continue
        if key in best and best[key].rate >= rate:
            continue
        limit = b.get('max_limit')
        best[key] = SavingsProduct(
            bank=(b.get('kor_co_nm') or '').replace('주식회사', '').strip(),
            name=' '.join((b.get('fin_prdt_nm') or '').split()),  # 이름 속 줄바꿈 정리
            rate=rate,
            max_rate=_float(o.get('intr_rate2')),
            saving_type=o.get('rsrv_type_nm') or '',
            limit_krw=int(limit) if isinstance(limit, int | float) else None,
            join_way=b.get('join_way') or None,
        )
    return sorted(best.values(), key=lambda p: (-p.rate, p.bank, p.name))[:n]


class SavingsSource:
    """적금 목록 공급자. 메모리에만 잠깐 담아 둔다 (약관: 저장 금지)."""

    def __init__(self, fetch: Fetcher | None, clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch, self._clock = fetch, clock
        self._cached: tuple[float, SavingsList] | None = None
        self._failed: tuple[float, SavingsList] | None = None

    def get(self) -> SavingsList:
        if self._fetch is None:
            return SavingsList(
                products=[],
                disclosed_month=None,
                status='off',
                reason='적금 정보를 불러오지 않아요 (서버에 금감원 인증키 없음)',
            )
        now = self._clock()
        if self._cached and now - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        if self._failed and now - self._failed[0] < FAILURE_COOLDOWN_SECONDS:
            return self._failed[1]
        try:
            data = self._fetch()
            products = pick_savings(data)
        except (FinlifeError, TypeError, AttributeError, KeyError, ValueError) as e:
            logger.warning('금감원 적금 조회 실패: %s', e)
            failed = SavingsList(
                products=[],
                disclosed_month=None,
                status='unavailable',
                reason='적금 정보를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.',
            )
            self._failed = (now, failed)
            return failed
        found = SavingsList(
            products=products,
            estimates=[estimate(d, products[0].rate) for d in DEPOSITS] if products else [],
            disclosed_month=data.get('month'),
            status='ok',
        )
        self._cached = (now, found)
        return found


def savings_from_env() -> SavingsSource:
    key = os.getenv(API_KEY_ENV, '').strip()
    return SavingsSource(http_fetcher(key) if key else None)
