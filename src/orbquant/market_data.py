"""Read-only Alpaca adapter. Contains no order submission method."""
import httpx
from orbquant.credentials import load_alpaca_credentials
from orbquant_network_guard import PAPER_REST, assert_installed


class ReadOnlyAlpaca:
    def __init__(self, *, credentials_file=None):
        assert_installed()
        self.credentials = load_alpaca_credentials(credentials_file)
        if not self.credentials.configured:
            raise ValueError('Fill the private alpaca-credentials.toml file first')

    def _get(self, origin, path, params=None):
        if origin not in (PAPER_REST, 'https://data.alpaca.markets'):
            raise ValueError('Origin is not allowed')
        # Follow no redirects; never silently change data feed or environment.
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = client.get(origin+path, params=params, headers={
                'APCA-API-KEY-ID': self.credentials.api_key,
                'APCA-API-SECRET-KEY': self.credentials.secret_key})
            response.raise_for_status()
            return response.json()

    def account_status(self):
        payload = self._get(PAPER_REST, '/v2/account')
        return {key: payload.get(key) for key in ('status', 'currency', 'trading_blocked', 'options_trading_level')}

    def latest_quotes(self, symbols=('SPY', 'QQQ'), feed='iex'):
        if feed not in ('iex', 'sip', 'delayed_sip') or not symbols or any(s not in ('SPY','QQQ') for s in symbols):
            raise ValueError('Explicit supported feed and symbols required')
        return self._get('https://data.alpaca.markets', '/v2/stocks/quotes/latest',
                         {'symbols': ','.join(symbols), 'feed': feed})
