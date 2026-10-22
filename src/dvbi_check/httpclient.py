"""Thread-safe HTTP client with cache, retries and timing."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl

import requests

from . import __version__

DEFAULT_UA = f"dvbi-check/{__version__} (DVB-I service list checker)"


@dataclass
class HttpResult:
    url: str
    status: int | None
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    elapsed: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def describe(self) -> str:
        if self.error:
            return f"Error: {self.error}"
        return f"HTTP {self.status}, {len(self.content)} bytes, {self.elapsed:.2f} s"


def add_query(url: str, **params) -> str:
    """Appends query parameters to a URL, even if it already has some."""
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    q.extend((k, str(v)) for k, v in params.items() if v is not None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q, safe=":/,"), parts.fragment))


class HttpClient:
    def __init__(self, user_agent: str = DEFAULT_UA, timeout: float = 10.0, retries: int = 1,
                 max_bytes: int = 30 * 1024 * 1024, verify: bool = True):
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.max_bytes = max_bytes
        self.verify = verify
        self._local = threading.local()
        self._cache: dict[tuple, HttpResult] = {}
        self._lock = threading.Lock()
        self.request_count = 0

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = self.user_agent
            self._local.session = s
        return s

    def get(self, url: str, headers: dict[str, str] | None = None, use_cache: bool = True) -> HttpResult:
        key = (url, tuple(sorted((headers or {}).items())))
        if use_cache:
            with self._lock:
                if key in self._cache:
                    return self._cache[key]
        result = self._fetch(url, headers or {})
        if use_cache:
            with self._lock:
                self._cache[key] = result
        return result

    def _fetch(self, url: str, headers: dict[str, str]) -> HttpResult:
        last_err = None
        for attempt in range(self.retries + 1):
            t0 = time.perf_counter()
            try:
                with self._lock:
                    self.request_count += 1
                r = self._session().get(url, headers=headers, timeout=self.timeout,
                                        verify=self.verify, stream=True, allow_redirects=True)
                chunks, size = [], 0
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > self.max_bytes:
                        r.close()
                        return HttpResult(url, r.status_code, {k.lower(): v for k, v in r.headers.items()},
                                          b"".join(chunks), time.perf_counter() - t0,
                                          f"Response larger than {self.max_bytes} bytes, aborted")
                    chunks.append(chunk)
                return HttpResult(url, r.status_code, {k.lower(): v for k, v in r.headers.items()},
                                  b"".join(chunks), time.perf_counter() - t0)
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
        return HttpResult(url, None, {}, b"", 0.0, last_err)

    def post(self, url: str, data: bytes, content_type: str, timeout: float | None = None,
             headers: dict[str, str] | None = None) -> HttpResult:
        """Single POST without cache or retries (used for the reference validator)."""
        t0 = time.perf_counter()
        with self._lock:
            self.request_count += 1
        try:
            r = self._session().post(url, data=data, headers={"Content-Type": content_type, **(headers or {})},
                                     timeout=timeout or self.timeout, verify=self.verify)
            return HttpResult(url, r.status_code, {k.lower(): v for k, v in r.headers.items()},
                              r.content, time.perf_counter() - t0)
        except requests.RequestException as e:
            return HttpResult(url, None, {}, b"", time.perf_counter() - t0, f"{type(e).__name__}: {e}")
