"""Small HTTP helper with retries and a polite User-Agent."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("stockbot.http")

_session = requests.Session()
_session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (compatible; TelegramStocksBot/0.1; personal use)",
        "Accept-Language": "en-MY,en;q=0.9",
    }
)


def get(url: str, *, params=None, headers=None, timeout: int = 25, retries: int = 2, backoff: float = 1.5):
    """GET with retry on network errors, 429 and 5xx. 4xx (other than 429) fail fast."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = _session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last = exc
        else:
            if resp.status_code < 400:
                return resp
            retryable = resp.status_code == 429 or resp.status_code >= 500
            last = requests.HTTPError(f"HTTP {resp.status_code} for {url.split('?')[0]}")
            if not retryable:
                raise last
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    assert last is not None
    log.warning("GET failed: %s", last)
    raise last
