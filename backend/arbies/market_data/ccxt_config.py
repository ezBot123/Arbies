"""Shared CCXT client configuration that honours the host's standard proxy/CA environment.

CCXT's async (aiohttp) clients ignore ``HTTPS_PROXY`` unless ``trust_env`` is set, and they
pin certifi's CA file instead of ``SSL_CERT_FILE``. In a network where outbound HTTPS must go
through an explicit proxy (corporate networks, sandboxed cloud containers) that makes every
exchange connection fail. This module only *reads* the standard environment variables; it never
invents a route, never disables TLS verification, and does nothing when they are unset.
"""

from __future__ import annotations

import os
from typing import Any


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def ccxt_config(**extra: Any) -> dict[str, Any]:
    """Return a CCXT constructor config: proxy from ``HTTPS_PROXY`` and CA from ``SSL_CERT_FILE``."""
    cfg: dict[str, Any] = {"aiohttp_trust_env": True}  # REST: aiohttp reads HTTPS_PROXY / NO_PROXY
    proxy = _env("HTTPS_PROXY", "https_proxy")
    if proxy:
        cfg["wssProxy"] = proxy  # CCXT Pro WebSocket proxy option
    cafile = _env("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
    if cafile and os.path.isfile(cafile):
        cfg["cafile"] = cafile  # verification stays on; only the trust store changes
    cfg.update(extra)
    return cfg
