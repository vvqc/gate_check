"""有限重试、每线程连接池和协作式请求期限；CLI 另有进程级硬期限。"""

import math
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from urllib3.exceptions import ReadTimeoutError

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; gate-checker)"}
TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class RequestFailure(RuntimeError):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def classify_exception(exc):
    if isinstance(exc, requests.exceptions.SSLError):
        return RequestFailure("tls_error")
    if isinstance(exc, requests.ConnectTimeout):
        return RequestFailure("connect_timeout", True)
    if any(isinstance(item, ReadTimeoutError) for item in exc.args):
        return RequestFailure("read_timeout", True)
    if isinstance(exc, requests.Timeout):
        return RequestFailure("read_timeout", True)
    if isinstance(exc, requests.ConnectionError):
        return RequestFailure("connection_error", True)
    return RequestFailure("request_error")


def retry_delay(value, fallback):
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = fallback
    return max(0, delay) if math.isfinite(delay) else fallback


class HttpClient:
    def __init__(self, config, on_retry=None, clock=time.monotonic, sleep=time.sleep):
        self.config = config
        self.clock = clock
        self.sleep = sleep
        self.deadline = clock() + config.run_timeout
        self.on_retry = on_retry or (lambda code: None)
        self.local = threading.local()
        self.sessions = []
        self.lock = threading.Lock()

    def remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise RequestFailure("deadline_exceeded")
        return remaining

    def session(self):
        if not hasattr(self.local, "session"):
            session = requests.Session()
            self.local.session = session
            with self.lock:
                self.sessions.append(session)
        return self.local.session

    def get(self, url, *, read_timeout=None, limit=1024 * 1024, headers=None):
        for attempt in range(self.config.retries + 1):
            remaining = self.remaining()
            delay = self.config.retry_backoff * (2**attempt)
            try:
                with self.session().get(
                    url,
                    timeout=(
                        min(self.config.connect_timeout, remaining),
                        min(read_timeout or self.config.check_timeout, remaining),
                    ),
                    headers=HEADERS | (headers or {}),
                    stream=True,
                ) as response:
                    if response.status_code >= 400:
                        delay = retry_delay(response.headers.get("Retry-After"), delay)
                        raise RequestFailure(
                            f"http_{response.status_code}",
                            response.status_code in TRANSIENT_STATUS,
                        )
                    content = bytearray()
                    for chunk in response.iter_content(16384):
                        self.remaining()
                        content.extend(chunk)
                        if len(content) > limit:
                            raise RequestFailure("response_too_large")
                    response._content = bytes(content)
                    response._content_consumed = True
                    return response
            except requests.RequestException as exc:
                failure = classify_exception(exc)
            except RequestFailure as exc:
                failure = exc
            if not failure.retryable or attempt == self.config.retries:
                raise failure
            self.on_retry(failure.code)
            if delay >= self.remaining():
                raise RequestFailure("retry_budget_exhausted")
            self.sleep(delay)
        raise AssertionError("unreachable")

    def close(self):
        for session in self.sessions:
            session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
