from pathlib import Path
import pytest
from orbquant.credentials import load_alpaca_credentials
from orbquant.market_data import ReadOnlyAlpaca


def test_template_has_no_credentials():
    root = Path(__file__).resolve().parents[1]
    assert not load_alpaca_credentials(root/'alpaca-credentials.example.toml', environ={}).configured


def test_own_private_file_and_environment_override(tmp_path):
    path = tmp_path/'alpaca-credentials.toml'
    path.write_text('[alpaca]\napi_key="fake-key"\nsecret_key="fake-secret"\n')
    result = load_alpaca_credentials(path,environ={})
    assert result.configured and result.api_key == 'fake-key'
    assert 'fake-key' not in repr(result) and 'fake-secret' not in repr(result)
    overridden = load_alpaca_credentials(path,environ={'APCA_API_KEY_ID':'override'})
    assert overridden.api_key == 'override'


def test_missing_keys_do_not_connect():
    with pytest.raises(ValueError,match='private'):
        ReadOnlyAlpaca()


def test_public_client_has_no_order_api():
    assert not any('order' in name.lower() for name in dir(ReadOnlyAlpaca))
