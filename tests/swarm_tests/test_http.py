"""The shared HTTP client: pagination, rate limits, and quiet logs."""

import logging

import httpx
import pytest

from swarm.http import Client, SourceError


def client(handler, sleeps=None, now=lambda: 1000.0):
    return Client("https://api.example.test", transport=httpx.MockTransport(handler),
                  sleep=(sleeps.append if sleeps is not None else lambda s: None), now=now)


def test_paginate_follows_link_headers():
    def handler(request):
        page = int(request.url.params.get("page", "1"))
        headers = {}
        if page < 3:
            headers["link"] = f'<https://api.example.test/items?page={page + 1}>; rel="next"'
        return httpx.Response(200, json=[page * 10, page * 10 + 1], headers=headers)

    assert list(client(handler).paginate("/items")) == [10, 11, 20, 21, 30, 31]


def test_paginate_reads_a_key_inside_an_object():
    def handler(request):
        return httpx.Response(200, json={"total_count": 1, "repositories": [{"id": 1}]})

    assert list(client(handler).paginate("/installation/repositories", key="repositories")) == [{"id": 1}]


@pytest.mark.parametrize(("first", "expected_wait"), [
    (httpx.Response(429, headers={"retry-after": "7"}), 7.0),
    (httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"}), 30.0),
    # GitHub also reports an exhausted quota as a 429: wait for the reset, not 2**attempt.
    (httpx.Response(429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1600"}), 600.0),
    # Google reports throttling as a 403 with a reason in the body.
    (httpx.Response(403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}), 1.0),
    (httpx.Response(403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}}), 1.0),
    (httpx.Response(503), 1.0),
])
def test_rate_limits_wait_then_retry(first, expected_wait):
    responses = [first, httpx.Response(200, json={"ok": True})]
    sleeps: list[float] = []
    assert client(lambda r: responses.pop(0), sleeps).get_json("/x") == {"ok": True}
    assert sleeps == [expected_wait]


def test_plain_errors_are_final_and_404_can_be_optional():
    c = client(lambda r: httpx.Response(404, json={"message": "Not Found"}))
    assert c.get_optional("/missing") is None
    assert client(lambda r: httpx.Response(410, json={"message": "Gone"})).get_optional("/gone") is None
    with pytest.raises(SourceError) as err:
        client(lambda r: httpx.Response(401)).get_json("/x")
    assert err.value.status == 401


def test_retries_are_capped():
    sleeps: list[float] = []
    with pytest.raises(SourceError):
        client(lambda r: httpx.Response(429, headers={"retry-after": "1"}), sleeps).get_json("/x")
    assert len(sleeps) == 5


def test_auth_headers_are_sent_on_every_try():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(429 if len(seen) == 1 else 200, json={}, headers={"retry-after": "0"})

    c = Client("https://api.example.test", transport=httpx.MockTransport(handler), sleep=lambda s: None,
               auth=lambda: {"Authorization": "Bearer t"})
    c.get_json("/x")
    assert seen == ["Bearer t", "Bearer t"]


def test_bodies_never_reach_the_log(caplog):
    secret = "body-that-must-not-be-logged"
    caplog.set_level(logging.DEBUG, logger="swarm.http")
    responses = [httpx.Response(429, text=secret, headers={"retry-after": "0"}), httpx.Response(200, text=f'"{secret}"')]
    client(lambda r: responses.pop(0)).get_json("/x")
    assert caplog.records and secret not in caplog.text


def test_paginate_cursor_follows_a_cursor_in_the_body():
    seen = []

    def handler(request):
        cursor = request.url.params.get("cursor")
        seen.append(cursor)
        nxt = {None: "b", "b": "c", "c": ""}[cursor]
        return httpx.Response(200, json={"ok": True, "items": [cursor or "a"], "meta": {"next": nxt}})

    out = list(client(handler).paginate_cursor("/items", {"limit": 2}, key="items", cursor_param="cursor",
                                               next_cursor=lambda b: b["meta"]["next"]))
    assert out == ["a", "b", "c"] and seen == [None, "b", "c"]


def test_paginate_cursor_lets_the_caller_reject_a_200():
    def check(body):
        if not body["ok"]:
            raise ValueError(body["error"])

    c = client(lambda r: httpx.Response(200, json={"ok": False, "error": "not_in_channel"}))
    with pytest.raises(ValueError, match="not_in_channel"):
        list(c.paginate_cursor("/x", key="items", cursor_param="cursor", next_cursor=lambda b: None, check=check))


def test_a_google_403_that_is_not_throttling_is_final_and_keeps_its_reason():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"error": {"errors": [{"reason": "exportSizeLimitExceeded"}]}})

    with pytest.raises(SourceError) as err:
        client(handler).get_json("/export")
    assert (err.value.status, err.value.reason, len(calls)) == (403, "exportSizeLimitExceeded", 1)


def test_throttling_that_outlasts_the_retries_says_so():
    with pytest.raises(SourceError) as err:
        client(lambda r: httpx.Response(403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
               ).get_json("/x")
    assert err.value.reason == "rateLimitExceeded"
