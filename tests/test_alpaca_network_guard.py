import asyncio
import http.client
import os
import socket
import ssl
import subprocess
import sys

import httpx
import pytest
from orbquant_network_guard import LiveTradingBlocked, check_url, assert_installed


@pytest.mark.parametrize('url', [
    'https://api.alpaca.markets/v2/orders',
    'wss://api.alpaca.markets/stream',
    'https://API.ALPACA.MARKETS./v2/account',
    'http://api.alpaca.markets:80/x',
    'https://anything.api.alpaca.markets/x',
    'https://%61pi.alpaca.markets/x',
    'https://paper-api.alpaca.markets@api.alpaca.markets/x',
])
def test_live_variants(url):
    with pytest.raises(LiveTradingBlocked):
        check_url(url)


def test_before_transport_even_with_proxy_and_bad_config(monkeypatch):
    monkeypatch.setenv('APCA_API_BASE_URL', 'https://api.alpaca.markets')
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
        with pytest.raises(LiveTradingBlocked):
            client.get('https://api.alpaca.markets/v2/account')
    assert not calls


def test_redirect_blocked_before_second_transport():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={'location': 'https://api.alpaca.markets/v2/account'})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(LiveTradingBlocked):
            client.get('https://paper-api.alpaca.markets/v2/account')
    assert calls == ['https://paper-api.alpaca.markets/v2/account']


def test_async_before_transport():
    calls = []
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
            with pytest.raises(LiveTradingBlocked):
                await client.get('https://api.alpaca.markets/v2/account')
    asyncio.run(run())
    assert not calls


def test_paper_and_data_allowed():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        for host in ('paper-api.alpaca.markets', 'data.alpaca.markets'):
            assert client.get('https://' + host).status_code == 200
    check_url('wss://paper-api.alpaca.markets/stream')
    check_url('wss://stream.data.alpaca.markets/v2/iex')


def test_socket_and_stdlib_block():
    with pytest.raises(LiveTradingBlocked):
        socket.getaddrinfo('api.alpaca.markets', 443)
    with pytest.raises(LiveTradingBlocked):
        http.client.HTTPSConnection('api.alpaca.markets').request('GET', '/v2/account')
    with pytest.raises(LiveTradingBlocked):
        http.client.HTTPSConnection('localhost').set_tunnel('api.alpaca.markets')


def test_both_websocket_apis_block_locally():
    from websockets.sync.client import connect
    from websockets.asyncio.client import connect as aconnect
    with pytest.raises(LiveTradingBlocked):
        connect('wss://api.alpaca.markets/stream')
    async def run():
        with pytest.raises(LiveTradingBlocked):
            aconnect('wss://api.alpaca.markets/stream')
    asyncio.run(run())


def test_startup_without_importing_orbquant(tmp_path):
    code = "import orbquant_network_guard as g; g.assert_installed(); import socket; socket.getaddrinfo('api.alpaca.markets',443)"
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'LiveTradingBlocked' in result.stderr
    assert_installed()


def test_explicit_proxy_blocked_before_connect(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: calls.append(a))
    with httpx.Client(proxy='http://127.0.0.1:9') as client:
        with pytest.raises(LiveTradingBlocked):
            client.get('https://api.alpaca.markets/v2/account')
    assert not calls


def test_tls_hostname_blocked_before_handshake():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with pytest.raises(LiveTradingBlocked):
        context.wrap_socket(None, server_hostname='api.alpaca.markets')
    with pytest.raises(LiveTradingBlocked):
        context.wrap_bio(ssl.MemoryBIO(), ssl.MemoryBIO(), server_hostname='api.alpaca.markets')


def test_startup_fails_closed_if_guard_cannot_load(tmp_path):
    (tmp_path / 'orbquant_network_guard.py').write_text('raise RuntimeError("simulated broken guard")')
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    result = subprocess.run([sys.executable, '-c', 'print("UNSAFE_STARTUP")'],
                            env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 78
    assert 'UNSAFE_STARTUP' not in result.stdout
    assert 'guard failed' in result.stderr
