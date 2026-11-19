from __future__ import annotations

import logging
import os
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx
from protego import Protego

log = logging.getLogger(__name__)
USER_AGENT = "CyprusBot/0.1 (+https://github.com/Ay00luwwa/Project-Cyprus)"
RETRYABLE = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    pass


def _ca_from_env() -> ssl.SSLContext | bool:
    """Honor the CA environment variables that httpx reads only with trust_env."""
    cafile, capath = os.environ.get("SSL_CERT_FILE"), os.environ.get("SSL_CERT_DIR")
    if not cafile and not capath:
        return True
    return ssl.create_default_context(cafile=cafile or None, capath=capath or None)


def _proxy_mounts(verify: ssl.SSLContext | bool) -> dict[str, httpx.BaseTransport] | None:
    """
    Client mounts for the proxy environment, skipping rules httpx cannot parse.

    httpx 0.28 turns a no_proxy entry such as '::1' into 'all://*[::1]', and
    parsing that raises InvalidURL while the client is being built, so a plain
    httpx.Client() crashes before a single request leaves the process. The
    fetcher reads the same environment itself and installs only usable rules:
    proxy transports for proxied origins, direct transports for bypasses.
    """
    try:
        from httpx._utils import URLPattern, get_environment_proxies
    except ImportError:  # pragma: no cover - depends on httpx internals
        return None
    mounts: dict[str, httpx.BaseTransport] = {}
    for pattern, proxy in get_environment_proxies().items():
        try:
            URLPattern(pattern)
        except httpx.InvalidURL:
            log.warning("Ignoring proxy setting httpx cannot parse: %s", pattern)
            continue
        mounts[pattern] = httpx.HTTPTransport(proxy=proxy, verify=verify) if proxy else httpx.HTTPTransport(verify=verify)
    return mounts


class Fetcher:
    def __init__(
        self,
        delay: float = 1.0,
        retries: int = 3,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if delay < 0 or not 1 <= retries <= 5:
            raise ValueError("Delay must be non-negative and retries must be between 1 and 5")
        self.delay = delay
        self.retries = retries
        options: dict = {
            "headers": {"User-Agent": USER_AGENT, "Accept-Language": "en"},
            "timeout": timeout,
            "follow_redirects": False,
        }
        if transport is not None:
            # An injected transport already decides where requests go, and
            # httpx refuses transport together with mounts.
            options["transport"] = transport
        else:
            verify = _ca_from_env()
            mounts = _proxy_mounts(verify)
            if mounts is None:  # pragma: no cover - httpx internals moved
                options["verify"] = verify  # fall back to httpx's own env handling
            else:
                # trust_env=False stops httpx re-reading the broken proxy rules.
                options.update(trust_env=False, verify=verify, mounts=mounts)
        self._client = httpx.Client(**options)
        self._robots: dict[str, Protego] = {}
        self._last_request: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise FetchError("Only HTTP(S) URLs without embedded credentials are allowed")
        return f"{parts.scheme}://{parts.netloc}"

    def get(self, url: str) -> httpx.Response:
        origin = self._origin(url)
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(f"{origin}/robots.txt")
        parser = self._robots[origin]
        for _ in range(6):
            if not parser.can_fetch(url, USER_AGENT):
                raise FetchError(f"Blocked by robots.txt: {url}")
            response = self._request(url)
            if response.has_redirect_location:
                url = self._redirect(url, response)
                continue
            if not response.is_success:
                raise FetchError(f"HTTP {response.status_code}: {url}")
            return response
        raise FetchError("Too many redirects")

    def get_text(self, url: str) -> str:
        return self.get(url).text

    def get_json(self, url: str):
        return self.get(url).json()

    def _redirect(self, url: str, response: httpx.Response) -> str:
        target = urljoin(url, response.headers["location"])
        if self._origin(target) != self._origin(url):
            raise FetchError(f"Cross-origin redirect refused: {target}")
        return target

    def _request(self, url: str) -> httpx.Response:
        last_error = ""
        for attempt in range(self.retries):
            self._throttle(url)
            try:
                response = self._client.get(url)
            except httpx.TransportError as exc:
                last_error = type(exc).__name__
                wait = max(1.0, self.delay) * 2 ** attempt
            else:
                if response.status_code not in RETRYABLE:
                    return response
                last_error = f"HTTP {response.status_code}"
                wait = max(max(1.0, self.delay) * 2 ** attempt, self._retry_after(response))
            if attempt + 1 < self.retries:
                if wait > 60:
                    raise FetchError(f"Server requested a long retry delay; try later: {url}")
                log.warning("%s: %s; retry in %.1fs", url, last_error, wait)
                time.sleep(wait)
        raise FetchError(f"Giving up on {url}: {last_error}")

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        value = response.headers.get("retry-after", "0")
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                return 0.0

    def _throttle(self, url: str) -> None:
        origin = self._origin(url)
        delay = self.delay
        parser = self._robots.get(origin)
        if parser:
            delay = max(delay, parser.crawl_delay(USER_AGENT) or 0)
            rate = parser.request_rate(USER_AGENT)
            if rate:
                delay = max(delay, rate.seconds / rate.requests)
        last = self._last_request.get(origin)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[origin] = time.monotonic()

    def _load_robots(self, robots_url: str) -> Protego:
        for _ in range(6):
            response = self._request(robots_url)
            if response.has_redirect_location:
                robots_url = self._redirect(robots_url, response)
                continue
            if response.status_code in {404, 410}:
                return Protego.parse("")
            if response.status_code in {401, 403}:
                return Protego.parse("User-agent: *\nDisallow: /")
            if response.status_code != 200:
                raise FetchError(f"Cannot verify robots.txt (HTTP {response.status_code})")
            if "<html" in response.text.lower() or "<!doctype html" in response.text.lower():
                raise FetchError("robots.txt returned HTML instead of a robots policy")
            return Protego.parse(response.text)
        raise FetchError("Too many robots.txt redirects")
