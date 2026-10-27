"""
http.py - the one way pipelines talk to source APIs.

A thin layer over httpx that every connector shares:

  - the proxy comes from the environment (HTTPS_PROXY, NO_PROXY), which is how
    the compose stack and the VPC route egress through the allowlist
  - pagination follows the Link header's rel="next" (GitHub), or a cursor
    in the body (Slack, Google)
  - rate limits are honoured: 429, Retry-After, GitHub's 403 or 429 with
    x-ratelimit-remaining: 0, and Google's 403 with a rate-limit reason all
    wait and retry, up to a cap

Bodies are never logged. Only method, path and status reach the log.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

log = logging.getLogger("swarm.http")

MAX_TRIES = 6
MAX_WAIT = 900.0          # never sleep longer than 15 minutes on one response
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
# Google reports throttling as a 403 whose body names one of these reasons.
GOOGLE_RATE_LIMITS = {"userRateLimitExceeded", "rateLimitExceeded"}


class SourceError(RuntimeError):
    """A source API failed in a way retrying will not fix. The run aborts
    without a manifest and is retried next schedule. `reason` is Google's
    error reason (exportSizeLimitExceeded, ...) when the body names one."""
    reason: str | None = None

    def __init__(self, status: int, method: str, path: str, reason: str | None = None):
        super().__init__(f"{method} {path} -> {status}" + (f" ({reason})" if reason else ""))
        self.status = status
        self.reason = reason


def _reason(response: httpx.Response) -> str | None:
    """Google's error reason from a JSON error body, or None. Only the
    reason code is kept; the rest of the body is never logged."""
    try:
        errors = response.json()["error"]["errors"]
        return str(errors[0]["reason"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def _wait_for(response: httpx.Response, attempt: int, now: Callable[[], float]) -> float | None:
    """Seconds to wait before retrying, or None when the response is final."""
    headers = response.headers
    status = response.status_code
    if "retry-after" in headers and status in (403, 429, 503):
        try:
            return min(float(headers["retry-after"]), MAX_WAIT)
        except ValueError:
            pass
    if (status in (403, 429) and headers.get("x-ratelimit-remaining") == "0"
            and "x-ratelimit-reset" in headers):
        return min(max(float(headers["x-ratelimit-reset"]) - now(), 1.0), MAX_WAIT)
    if status in (429, 502, 503, 504) or (status == 403 and _reason(response) in GOOGLE_RATE_LIMITS):
        return min(2.0 ** attempt, MAX_WAIT)
    return None


class Client:
    def __init__(self, base_url: str, *, headers: dict[str, str] | None = None,
                 auth: Callable[[], dict[str, str]] | None = None,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time,
                 timeout: float = 30.0):
        self._auth = auth
        self._sleep = sleep
        self._now = now
        self._http = httpx.Client(base_url=base_url, headers=headers or {}, transport=transport,
                                  timeout=timeout, trust_env=True, follow_redirects=True)

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                json: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
        extra = dict(headers or {})
        for attempt in range(MAX_TRIES):
            if self._auth:
                extra.update(self._auth())
            response = self._http.request(method, path, params=params, json=json, headers=extra)
            log.debug("%s %s -> %s", method, response.request.url.path, response.status_code)
            if response.status_code < 400:
                return response
            wait = _wait_for(response, attempt, self._now)
            if wait is None or attempt == MAX_TRIES - 1:
                raise SourceError(response.status_code, method, response.request.url.path, _reason(response))
            log.info("%s %s -> %s; retrying in %.0fs", method, response.request.url.path,
                     response.status_code, wait)
            self._sleep(wait)
        raise AssertionError("unreachable")

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params).json()

    def get_optional(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        """GET that treats 404 and 410 (Gone: deleted, or a feature turned
        off) as absent rather than an error."""
        try:
            return self.get_json(path, params)
        except SourceError as e:
            if e.status in (404, 410):
                return None
            raise

    def paginate(self, path: str, params: dict[str, Any] | None = None, *,
                 key: str | None = None) -> Iterator[Any]:
        """Every element across every page. `key` names the list inside an
        object response, e.g. "repositories" for /installation/repositories."""
        response = self.request("GET", path, params=params)
        while True:
            body = response.json()
            yield from (body[key] if key else body)
            match = _NEXT.search(response.headers.get("link", ""))
            if not match:
                return
            response = self.request("GET", match.group(1))

    def paginate_cursor(self, path: str, params: dict[str, Any] | None = None, *, key: str,
                        cursor_param: str, next_cursor: Callable[[Any], str | None],
                        check: Callable[[Any], None] | None = None) -> Iterator[Any]:
        """Every element across every page, for APIs that put the next
        page's cursor in the body: Slack's response_metadata.next_cursor,
        Google's nextPageToken. `check` sees each body first and may raise,
        for APIs that report errors inside a 200."""
        params = dict(params or {})
        while True:
            body = self.request("GET", path, params=params).json()
            if check is not None:
                check(body)
            yield from body.get(key) or []
            cursor = next_cursor(body)
            if not cursor:
                return
            params[cursor_param] = cursor

    def close(self) -> None:
        self._http.close()
