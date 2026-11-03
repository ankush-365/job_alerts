"""Shared HTTP plumbing (timeouts, retries, per-host rate limiting) and the collector contract."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import httpx

from core.models import CollectionResult, JobRecord, utcnow

logger = logging.getLogger(__name__)

USER_AGENT = "ai-job-radar/0.1 (personal job alert bot; low request rate)"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class HttpFetcher:
    """GET with timeout, exponential backoff on transient failures, and a minimum delay per host."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        min_interval: float = 1.0,
        host_intervals: dict[str, float] | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._min_interval = min_interval
        self._host_intervals = host_intervals or {}
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}
        self.requests_made = 0

    def close(self) -> None:
        self._client.close()

    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self._host_intervals.get(host, self._min_interval) - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def get(self, url: str, *, params: dict | None = None) -> httpx.Response:
        host = urlparse(url).netloc
        for attempt in range(self._max_retries + 1):
            self._throttle(host)
            self.requests_made += 1
            try:
                response = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                error = FetchError(f"GET {url}: {type(exc).__name__}: {exc}")
                retry_after = None
            else:
                if response.status_code == 200:
                    return response
                error = FetchError(f"GET {url}: HTTP {response.status_code}", response.status_code)
                if response.status_code not in RETRYABLE_STATUS:
                    raise error
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))

            if attempt == self._max_retries:
                raise error
            delay = retry_after or self._backoff_base * (2**attempt)
            logger.warning("%s; retry %d/%d in %.1fs", error, attempt + 1, self._max_retries, delay)
            self._sleep(delay)
        raise AssertionError("unreachable")

    def get_json(self, url: str, *, params: dict | None = None):
        response = self.get(url, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"GET {url}: response is not JSON") from exc


def _parse_retry_after(value: str | None) -> float | None:
    try:
        return min(float(value), 120.0) if value else None
    except ValueError:
        return None


class BaseCollector(ABC):
    """A collector fetches one source and returns normalized JobRecords.

    `run()` never raises: any failure is captured in the CollectionResult so one
    broken source cannot stop the pipeline.
    """

    source: str = "base"

    def __init__(self, fetcher: HttpFetcher):
        self.fetcher = fetcher
        self.errors: list[str] = []
        self.complete_boards: list[str] = []

    @abstractmethod
    def collect(self) -> list[JobRecord]:
        """Fetch and normalize jobs. May append non-fatal problems to self.errors."""

    def run(self) -> CollectionResult:
        result = CollectionResult(source=self.source, ok=False)
        before = self.fetcher.requests_made
        self.errors = []
        self.complete_boards = []
        try:
            result.jobs = self.collect()
            result.ok = True
        except Exception as exc:  # noqa: BLE001 - isolate every source failure
            logger.exception("Collector %s failed", self.source)
            self.errors.append(f"{type(exc).__name__}: {exc}")
        result.errors = list(self.errors)
        result.complete_boards = list(self.complete_boards)
        result.requests_made = self.fetcher.requests_made - before
        result.finished_at = utcnow()
        logger.info(
            "Collector %s: ok=%s jobs=%d requests=%d errors=%d",
            self.source, result.ok, len(result.jobs), result.requests_made, len(result.errors),
        )
        return result
