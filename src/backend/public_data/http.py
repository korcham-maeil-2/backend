"""공공데이터 API 공통 HTTP 클라이언트: 연결에 실패하면 두 번 더 시도한다.

정부 사이트는 가끔 연결이 늦다(연결 시간 초과). 그때 한 번에 포기하면 화면에서 그 칸이 빠진다.
연결 단계의 실패(연결 오류·연결 시간 초과)만 다시 시도한다 — 응답이 온 뒤의 오류(4xx·5xx·읽기 시간
초과)는 다시 보내지 않아 API 한도를 더 쓰지 않는다 (httpx HTTPTransport retries의 동작).
"""

import httpx

RETRIES = 2


def client(timeout: httpx.Timeout) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(retries=RETRIES))


def async_client(timeout: httpx.Timeout, limits: httpx.Limits) -> httpx.AsyncClient:
    # transport를 넘기면 클라이언트의 limits는 쓰이지 않는다 → transport에 넘긴다
    return httpx.AsyncClient(
        timeout=timeout, transport=httpx.AsyncHTTPTransport(retries=RETRIES, limits=limits)
    )
