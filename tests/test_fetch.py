from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

import cyprus.fetch as fetch_module
from cyprus.fetch import FetchError, Fetcher


ORIGIN = "https://example.test"
ALLOW = "User-agent: *\nDisallow:\n"


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        assert 0 <= seconds <= 60, "Fetcher must not sleep beyond the retry limit"
        self.sleeps.append(seconds)
        self.elapsed += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(fetch_module, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock.start + timedelta(seconds=clock.elapsed)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(fetch_module, "datetime", FrozenDatetime)
    return clock


@pytest.fixture
def make_fetcher(clock):
    with ExitStack() as stack:
        def make(handler, *, delay=0, retries=3):
            calls = []

            def transport_handler(request):
                calls.append((request.url, clock.elapsed))
                return handler(request)

            client = stack.enter_context(Fetcher(
                delay=delay, retries=retries, transport=httpx.MockTransport(transport_handler),
            ))
            return client, calls

        yield make


def paths(calls):
    return [url.path for url, _ in calls]


def test_robots_is_loaded_once_and_cached_for_origin(make_fetcher):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text=request.url.path)

    fetcher, calls = make_fetcher(handler)
    assert fetcher.get_text(ORIGIN + "/first") == "/first"
    assert fetcher.get_text(ORIGIN + "/second") == "/second"
    with pytest.raises(FetchError, match="robots"):
        fetcher.get(ORIGIN + "/private")
    assert paths(calls) == ["/robots.txt", "/first", "/second"]


@pytest.mark.parametrize("status", [404, 410])
def test_missing_robots_policy_permits_fetch_and_is_cached(make_fetcher, status):
    def handler(request):
        return httpx.Response(status if request.url.path == "/robots.txt" else 200, text="public")

    fetcher, calls = make_fetcher(handler)
    assert fetcher.get_text(ORIGIN + "/page") == "public"
    assert fetcher.get_text(ORIGIN + "/other") == "public"
    assert paths(calls) == ["/robots.txt", "/page", "/other"]


@pytest.mark.parametrize("status", [401, 403])
def test_forbidden_robots_blocks_documents_without_retry(make_fetcher, clock, status):
    fetcher, calls = make_fetcher(lambda request: httpx.Response(status))
    for path in ("/page", "/another"):
        with pytest.raises(FetchError, match="robots"):
            fetcher.get(ORIGIN + path)
    assert paths(calls) == ["/robots.txt"]
    assert clock.sleeps == []


@pytest.mark.parametrize("failure", ["500", "transport"])
def test_robots_server_or_transport_failure_fails_closed(make_fetcher, clock, failure):
    def handler(request):
        if request.url.path != "/robots.txt":
            pytest.fail("Document requested before robots could be verified")
        if failure == "transport":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(500)

    fetcher, calls = make_fetcher(handler, retries=2)
    with pytest.raises(FetchError):
        fetcher.get(ORIGIN + "/page")
    assert paths(calls) == ["/robots.txt", "/robots.txt"]
    assert clock.sleeps == [1.0]


def test_wildcard_disallow_blocks_api_paths_but_allows_public_page(make_fetcher):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /api/*\n")
        return httpx.Response(200, text="public")

    fetcher, calls = make_fetcher(handler)
    for path in ("/api/jobs", "/api/v1/listings?limit=1"):
        with pytest.raises(FetchError, match="robots"):
            fetcher.get(ORIGIN + path)
    assert fetcher.get_text(ORIGIN + "/public") == "public"
    assert paths(calls) == ["/robots.txt", "/public"]


@pytest.mark.parametrize("configured_delay, expected", [(0.25, 3.0), (5.0, 5.0)])
def test_crawl_delay_and_configured_delay_use_the_larger_interval(make_fetcher, clock, configured_delay, expected):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\nCrawl-delay: 3\n")
        return httpx.Response(200, text="ok")

    fetcher, calls = make_fetcher(handler, delay=configured_delay)
    fetcher.get(ORIGIN + "/first")
    fetcher.get(ORIGIN + "/second")
    assert paths(calls) == ["/robots.txt", "/first", "/second"]
    assert [when for _, when in calls] == pytest.approx([0, expected, 2 * expected])
    assert clock.sleeps == pytest.approx([expected, expected])


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_status_is_retried_then_succeeds(make_fetcher, clock, status):
    responses = iter([status, 200])

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW)
        return httpx.Response(next(responses), text="result")

    fetcher, calls = make_fetcher(handler)
    assert fetcher.get_text(ORIGIN + "/page") == "result"
    assert paths(calls) == ["/robots.txt", "/page", "/page"]
    assert clock.sleeps == [1.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 418, 501])
def test_non_transient_status_including_ordinary_404_is_not_retried(make_fetcher, clock, status):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW)
        return httpx.Response(status)

    fetcher, calls = make_fetcher(handler, retries=5)
    with pytest.raises(FetchError, match=f"HTTP {status}"):
        fetcher.get(ORIGIN + "/page")
    assert paths(calls) == ["/robots.txt", "/page"]
    assert clock.sleeps == []


def test_transient_failures_stop_at_retry_budget_without_final_sleep(make_fetcher, clock):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW)
        return httpx.Response(503)

    fetcher, calls = make_fetcher(handler, retries=3)
    with pytest.raises(FetchError, match="503"):
        fetcher.get(ORIGIN + "/page")
    assert paths(calls) == ["/robots.txt", "/page", "/page", "/page"]
    assert clock.sleeps == [1.0, 2.0]


@pytest.mark.parametrize("exception_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_document_transport_failure_is_retried(make_fetcher, clock, exception_type):
    attempts = 0

    def handler(request):
        nonlocal attempts
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW)
        attempts += 1
        if attempts == 1:
            raise exception_type("temporary", request=request)
        return httpx.Response(200, text="recovered")

    fetcher, calls = make_fetcher(handler)
    assert fetcher.get_text(ORIGIN + "/page") == "recovered"
    assert paths(calls) == ["/robots.txt", "/page", "/page"]
    assert clock.sleeps == [1.0]


@pytest.mark.parametrize("header_kind", ["seconds", "http-date"])
@pytest.mark.parametrize("wait", [7, 60, 61])
@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_is_respected_or_aborted_above_sixty_seconds(make_fetcher, clock, header_kind, wait, status):
    header = str(wait) if header_kind == "seconds" else format_datetime(clock.start + timedelta(seconds=wait), usegmt=True)
    responses = iter([httpx.Response(status, headers={"Retry-After": header}), httpx.Response(200, text="ok")])

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW)
        return next(responses)

    fetcher, calls = make_fetcher(handler)
    if wait > 60:
        with pytest.raises(FetchError):
            fetcher.get(ORIGIN + "/page")
        assert paths(calls) == ["/robots.txt", "/page"]
        assert clock.sleeps == []
    else:
        assert fetcher.get_text(ORIGIN + "/page") == "ok"
        assert paths(calls) == ["/robots.txt", "/page", "/page"]
        assert calls[-1][1] - calls[-2][1] == pytest.approx(wait)
        assert clock.sleeps == pytest.approx([wait])


@pytest.mark.parametrize("blocked", [False, True])
def test_same_origin_redirect_destination_is_checked_against_robots(make_fetcher, blocked):
    target = "/private" if blocked else "/public"

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, text="destination")

    fetcher, calls = make_fetcher(handler)
    if blocked:
        with pytest.raises(FetchError, match="robots"):
            fetcher.get(ORIGIN + "/start")
        assert paths(calls) == ["/robots.txt", "/start"]
    else:
        response = fetcher.get(ORIGIN + "/start")
        assert response.text == "destination"
        assert str(response.url) == ORIGIN + target
        assert paths(calls) == ["/robots.txt", "/start", "/public"]


@pytest.mark.parametrize("target", ["https://other.test/page", "http://example.test/page", "https://example.test:8443/page", "//other.test/page"])
@pytest.mark.parametrize("redirect_robots", [False, True])
def test_cross_origin_redirect_is_denied_without_contacting_target(make_fetcher, target, redirect_robots):
    def handler(request):
        if request.url.path == "/robots.txt" and not redirect_robots:
            return httpx.Response(200, text=ALLOW)
        return httpx.Response(302, headers={"Location": target})

    fetcher, calls = make_fetcher(handler)
    with pytest.raises(FetchError, match="[Cc]ross-origin"):
        fetcher.get(ORIGIN + "/page")
    assert paths(calls) == (["/robots.txt"] if redirect_robots else ["/robots.txt", "/page"])
    assert all(str(url).startswith(ORIGIN + "/") for url, _ in calls)


PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
              "http_proxy", "https_proxy", "all_proxy", "no_proxy")


@pytest.fixture
def no_proxy_environment(monkeypatch):
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


def test_broken_no_proxy_entry_does_not_prevent_a_client(no_proxy_environment, monkeypatch):
    # httpx 0.28 renders the bare '::1' as 'all://*[::1]', which it cannot parse,
    # and that failure happens while the client is constructed.
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,127.0.0.0/8,[::1],::1")
    with Fetcher(delay=0) as fetcher:
        patterns = [pattern.pattern for pattern in fetcher._client._mounts]
    assert "all://localhost" in patterns and "all://[::1]" in patterns
    assert not any("*[" in pattern for pattern in patterns)


def test_configured_proxy_is_still_used(no_proxy_environment, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,::1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    with Fetcher(delay=0) as fetcher:
        mounts = {pattern.pattern: transport for pattern, transport in fetcher._client._mounts.items()}
    assert mounts["https://"]._pool._proxy_url.port == 3128
    assert mounts["all://localhost"]._pool._proxy is None


def test_requests_stay_direct_without_proxy_settings(no_proxy_environment):
    with Fetcher(delay=0) as fetcher:
        assert fetcher._client._mounts == {}
        assert fetcher._client.headers["User-Agent"] == fetch_module.USER_AGENT
