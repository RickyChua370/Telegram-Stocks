"""Daily price history for Bursa stocks via Yahoo Finance (`<code>.KL`). Unofficial but free."""
from __future__ import annotations

import logging

import pandas as pd
import yfinance as yf

log = logging.getLogger("stockbot.prices")

MIN_BARS = 60


def yahoo_symbol(code: str) -> str:
    return f"{code}.KL"


def _clean(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    df = df.dropna(subset=["Close"]).copy()
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in df.columns:
            return None
    df = df[(df["Close"] > 0)]
    return df if len(df) >= MIN_BARS else None


def fetch_history(codes: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    """Return {code: OHLCV daily DataFrame}. Codes with too little/no data are omitted."""
    codes = list(dict.fromkeys(codes))
    if not codes:
        return {}
    symbols = [yahoo_symbol(c) for c in codes]
    out: dict[str, pd.DataFrame] = {}
    try:
        raw = yf.download(symbols, period=period, interval="1d", group_by="ticker", auto_adjust=False,
                          threads=True, progress=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("yfinance batch download failed: %s", exc)
        raw = pd.DataFrame()
    for code, sym in zip(codes, symbols):
        df = None
        if isinstance(raw.columns, pd.MultiIndex):
            if sym in raw.columns.get_level_values(0):
                df = raw[sym]
        elif len(symbols) == 1 and not raw.empty:
            df = raw
        df = _clean(df) if df is not None else None
        if df is not None:
            out[code] = df
    missing = [c for c in codes if c not in out]
    if missing:
        log.info("No usable Yahoo data for: %s", ", ".join(missing[:15]) + (" ..." if len(missing) > 15 else ""))
    return out


def klci_snapshot() -> dict | None:
    """Latest FBM KLCI level and daily change, for the digest header."""
    try:
        df = yf.download("^KLSE", period="10d", interval="1d", progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"].dropna()
        if len(close) < 2:
            return None
        return {"level": float(close.iloc[-1]), "change_pct": float((close.iloc[-1] / close.iloc[-2] - 1) * 100),
                "date": close.index[-1].date()}
    except Exception as exc:  # noqa: BLE001
        log.info("KLCI snapshot unavailable: %s", exc)
        return None
