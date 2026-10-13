"""CCXT client config honours the standard proxy/CA environment and never weakens TLS."""

import ccxt.async_support as ccxt_async

from arbies.market_data.ccxt_config import ccxt_config

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


def _clear(monkeypatch):
    for v in PROXY_VARS:
        monkeypatch.delenv(v, raising=False)


def test_no_environment_means_no_proxy(monkeypatch):
    _clear(monkeypatch)
    cfg = ccxt_config(enableRateLimit=True)
    assert cfg == {"aiohttp_trust_env": True, "enableRateLimit": True}


def test_proxy_and_ca_taken_from_environment(monkeypatch, tmp_path):
    _clear(monkeypatch)
    ca = tmp_path / "ca.crt"
    ca.write_text("x")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    cfg = ccxt_config(timeout=15000)
    assert cfg["wssProxy"] == "http://127.0.0.1:3128"
    assert cfg["cafile"] == str(ca)
    assert cfg["timeout"] == 15000
    assert "verify" not in cfg  # TLS verification is never switched off


def test_missing_ca_file_is_ignored(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.crt")
    assert "cafile" not in ccxt_config()


async def test_ccxt_accepts_config(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    ex = ccxt_async.kraken(ccxt_config(enableRateLimit=True))
    try:
        assert ex.aiohttp_trust_env is True
        assert ex.wssProxy == "http://127.0.0.1:3128"
        assert ex.verify is True
    finally:
        await ex.close()
