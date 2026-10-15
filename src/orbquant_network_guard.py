"""Always-on local Alpaca live-host block for OrbQuant Python processes.

This is accident prevention, not an OS sandbox against hostile code.
"""
from __future__ import annotations

import functools
import sys
from urllib.parse import unquote, urlsplit

PAPER_REST = "https://paper-api.alpaca.markets"
PAPER_STREAM = "wss://paper-api.alpaca.markets/stream"
LIVE_HOST = "api.alpaca.markets"
_installed = False


class LiveTradingBlocked(PermissionError):
    """Raised locally before a live Alpaca request reaches its transport."""


def check_host(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="strict")
    if not isinstance(host, str):
        return
    normalized = unquote(host).rstrip(".").lower().encode("idna").decode("ascii")
    if normalized == LIVE_HOST or normalized.endswith("." + LIVE_HOST):
        raise LiveTradingBlocked("OrbQuant locally blocked Alpaca LIVE trading host; use paper-api.alpaca.markets")


def check_url(url):
    # Do not echo URLs: embedded credentials or query strings may be sensitive.
    parsed = urlsplit(str(url))
    check_host(parsed.hostname)


def _audit(event, args):
    if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
        check_host(args[0])
    elif event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple):
            check_host(address[0])


def install():
    """Idempotent; no environment/configuration switch can enable live trading."""
    global _installed
    if _installed:
        return
    import http.client
    import httpx
    import ssl
    from websockets.sync import client as ws_sync
    from websockets.asyncio import client as ws_async

    # Per-hop guards run even with custom transports, pooled connections,
    # explicit proxies, redirects, and asynchronous HTTP clients.
    original_send = httpx.Client._send_single_request
    @functools.wraps(original_send)
    def send(self, request):
        check_url(request.url)
        return original_send(self, request)
    httpx.Client._send_single_request = send
    original_async = httpx.AsyncClient._send_single_request
    @functools.wraps(original_async)
    async def async_send(self, request):
        check_url(request.url)
        return await original_async(self, request)
    httpx.AsyncClient._send_single_request = async_send

    original_connect = ws_sync.connect
    @functools.wraps(original_connect)
    def ws_connect(uri, *args, **kwargs):
        check_url(uri)
        return original_connect(uri, *args, **kwargs)
    ws_sync.connect = ws_connect
    # Preserve the asynchronous connection class and its context-manager API.
    original_init = ws_async.connect.__init__
    @functools.wraps(original_init)
    def ws_init(self, uri, *args, **kwargs):
        check_url(uri)
        original_init(self, uri, *args, **kwargs)
    ws_async.connect.__init__ = ws_init

    original_http = http.client.HTTPConnection.putrequest
    @functools.wraps(original_http)
    def putrequest(self, method, url, *args, **kwargs):
        check_host(self.host)
        check_host(getattr(self, "_tunnel_host", None))
        check_url(url)
        return original_http(self, method, url, *args, **kwargs)
    http.client.HTTPConnection.putrequest = putrequest
    original_tunnel = http.client.HTTPConnection.set_tunnel
    @functools.wraps(original_tunnel)
    def tunnel(self, host, *args, **kwargs):
        check_host(host)
        return original_tunnel(self, host, *args, **kwargs)
    http.client.HTTPConnection.set_tunnel = tunnel

    original_socket = ssl.SSLContext.wrap_socket
    @functools.wraps(original_socket)
    def wrap_socket(self, *args, **kwargs):
        check_host(kwargs.get("server_hostname", args[4] if len(args) > 4 else None))
        return original_socket(self, *args, **kwargs)
    ssl.SSLContext.wrap_socket = wrap_socket
    original_bio = ssl.SSLContext.wrap_bio
    @functools.wraps(original_bio)
    def wrap_bio(self, *args, **kwargs):
        check_host(kwargs.get("server_hostname", args[3] if len(args) > 3 else None))
        return original_bio(self, *args, **kwargs)
    ssl.SSLContext.wrap_bio = wrap_bio
    sys.addaudithook(_audit)
    _installed = True


def assert_installed():
    if not _installed:
        raise RuntimeError("OrbQuant network guard is not installed")
