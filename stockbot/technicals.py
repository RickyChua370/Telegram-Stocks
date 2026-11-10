"""Technical indicators on DAILY candles: EMA, WMA, RSI, Stochastic, Bollinger Bands, volume.

Pure pandas/numpy (no TA-Lib). Defaults are in config/settings.yaml. Signals are deliberately simple and
explainable: each alert states which rule fired and with what values.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------------- indicators
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n).apply(lambda x: float(np.dot(x, w) / w.sum()), raw=True)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    d = close.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    ru = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + ru / rd)


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series, k: int = 14, smooth: int = 3, d: int = 3):
    """Slow stochastic: %K smoothed over `smooth` bars, %D = SMA(%K, d)."""
    ll, hh = low.rolling(k).min(), high.rolling(k).max()
    raw = 100 * (close - ll) / (hh - ll).replace(0, np.nan)
    pk = raw.rolling(smooth).mean()
    return pk, pk.rolling(d).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
    width = (upper - lower) / mid
    return mid, upper, lower, pct_b, width


def _crossed(a: pd.Series, b: pd.Series, lookback: int) -> str | None:
    """'up' / 'down' if series a crossed b within the last `lookback` bars."""
    diff = (a - b).dropna()
    if len(diff) < lookback + 1:
        return None
    recent = diff.iloc[-(lookback + 1):]
    for i in range(1, len(recent)):
        if recent.iloc[i - 1] <= 0 < recent.iloc[i]:
            return "up"
        if recent.iloc[i - 1] >= 0 > recent.iloc[i]:
            return "down"
    return None


# ----------------------------------------------------------------------------- snapshot
@dataclass
class TechSnapshot:
    code: str
    close: float
    change_pct: float
    volume_ratio: float | None
    rsi: float | None
    stoch_k: float | None
    stoch_d: float | None
    ema_fast: float | None
    ema_slow: float | None
    ema_long: float | None
    wma: float | None
    bb_lower: float | None
    bb_upper: float | None
    pct_b: float | None
    trend: str
    signals: list[str] = field(default_factory=list)       # human-readable observations
    alerts: list[str] = field(default_factory=list)        # what is worth your attention
    bias: str = "neutral"                                   # bullish | bearish | neutral (rule-based)
    oversold_votes: int = 0
    overbought_votes: int = 0

    @property
    def is_alert(self) -> bool:
        return bool(self.alerts)

    def compact(self) -> dict:
        r = lambda v, n=2: None if v is None or pd.isna(v) else round(float(v), n)  # noqa: E731
        return {"code": self.code, "close": r(self.close, 3), "change_pct": r(self.change_pct),
                "rsi": r(self.rsi, 1), "stoch_k": r(self.stoch_k, 1), "stoch_d": r(self.stoch_d, 1),
                "ema20": r(self.ema_fast, 3), "ema50": r(self.ema_slow, 3), "ema200": r(self.ema_long, 3),
                "wma20": r(self.wma, 3), "bb_lower": r(self.bb_lower, 3), "bb_upper": r(self.bb_upper, 3),
                "pct_b": r(self.pct_b), "volume_ratio": r(self.volume_ratio, 1), "trend": self.trend,
                "alerts": self.alerts, "rule_based_bias": self.bias}


def _last(s: pd.Series) -> float | None:
    v = s.iloc[-1] if len(s) else np.nan
    return None if pd.isna(v) else float(v)


def analyze(code: str, df: pd.DataFrame, cfg: dict) -> TechSnapshot | None:
    """Compute indicators and rule-based signals from a daily OHLCV frame."""
    if df is None or len(df) < 60:
        return None
    c, h, l, v = df["Close"].astype(float), df["High"].astype(float), df["Low"].astype(float), df["Volume"].astype(float)
    e_fast, e_slow = ema(c, cfg["ema_fast"]), ema(c, cfg["ema_slow"])
    e_long = ema(c, cfg["ema_long"]) if len(c) >= cfg["ema_long"] else pd.Series(dtype=float)
    w = wma(c, cfg["wma"])
    r = rsi(c, cfg["rsi_period"])
    k, d = stochastic(h, l, c, cfg["stoch_k"], cfg["stoch_smooth"], cfg["stoch_d"])
    mid, up, lo, pb, width = bollinger(c, cfg["bb_period"], cfg["bb_std"])

    close = float(c.iloc[-1])
    prev = float(c.iloc[-2])
    chg = (close / prev - 1) * 100 if prev else 0.0
    vol_avg = v.iloc[-21:-1].mean() if len(v) > 21 else np.nan
    vol_ratio = float(v.iloc[-1] / vol_avg) if vol_avg and not np.isnan(vol_avg) and vol_avg > 0 else None

    snap = TechSnapshot(
        code=code, close=close, change_pct=chg, volume_ratio=vol_ratio,
        rsi=_last(r), stoch_k=_last(k), stoch_d=_last(d),
        ema_fast=_last(e_fast), ema_slow=_last(e_slow), ema_long=_last(e_long) if len(e_long) else None,
        wma=_last(w), bb_lower=_last(lo), bb_upper=_last(up), pct_b=_last(pb), trend="mixed")

    # ---- trend -----------------------------------------------------------------
    if snap.ema_fast and snap.ema_slow:
        if close > snap.ema_fast > snap.ema_slow:
            snap.trend = "uptrend"
        elif close < snap.ema_fast < snap.ema_slow:
            snap.trend = "downtrend"
        else:
            snap.trend = "mixed"

    # ---- oscillator votes ----------------------------------------------------------
    if snap.rsi is not None:
        if snap.rsi < cfg["rsi_oversold"]:
            snap.oversold_votes += 1
            snap.signals.append(f"RSI {snap.rsi:.0f} (oversold below {cfg['rsi_oversold']})")
        elif snap.rsi > cfg["rsi_overbought"]:
            snap.overbought_votes += 1
            snap.signals.append(f"RSI {snap.rsi:.0f} (overbought above {cfg['rsi_overbought']})")
    if snap.stoch_k is not None:
        if snap.stoch_k < cfg["stoch_oversold"]:
            snap.oversold_votes += 1
            snap.signals.append(f"Stochastic %K {snap.stoch_k:.0f} (oversold)")
        elif snap.stoch_k > cfg["stoch_overbought"]:
            snap.overbought_votes += 1
            snap.signals.append(f"Stochastic %K {snap.stoch_k:.0f} (overbought)")
    if snap.pct_b is not None and snap.bb_lower is not None:
        if close <= snap.bb_lower:
            snap.oversold_votes += 1
            snap.signals.append("Closed at/below the lower Bollinger Band")
        elif close >= snap.bb_upper:
            snap.overbought_votes += 1
            snap.signals.append("Closed at/above the upper Bollinger Band")
    if len(width.dropna()) > 120 and width.iloc[-1] <= width.iloc[-120:].quantile(0.1):
        snap.signals.append("Bollinger Bands unusually tight (a bigger move may be building)")

    # ---- moving-average structure ------------------------------------------------
    look = cfg["cross_lookback_bars"]
    cross = _crossed(e_fast, e_slow, look)
    if cross == "up":
        snap.alerts.append(f"EMA{cfg['ema_fast']} crossed ABOVE EMA{cfg['ema_slow']} in the last {look} days (bullish)")
    elif cross == "down":
        snap.alerts.append(f"EMA{cfg['ema_fast']} crossed BELOW EMA{cfg['ema_slow']} in the last {look} days (bearish)")
    if snap.wma and snap.ema_fast:
        rel = "above" if close > snap.wma else "below"
        snap.signals.append(f"Price {rel} WMA{cfg['wma']} ({snap.wma:.3f}); EMA{cfg['ema_fast']} {snap.ema_fast:.3f}")
    if snap.ema_long:
        snap.signals.append(f"Price {'above' if close > snap.ema_long else 'below'} the long-term EMA{cfg['ema_long']}")

    # ---- alerts ------------------------------------------------------------------
    names = "RSI, Stochastic and Bollinger"
    if snap.oversold_votes >= 2:
        snap.alerts.append(f"OVERSOLD: {snap.oversold_votes} of 3 indicators agree ({names})")
    if snap.overbought_votes >= 2:
        snap.alerts.append(f"OVERBOUGHT: {snap.overbought_votes} of 3 indicators agree ({names})")
    if vol_ratio and vol_ratio >= cfg["volume_spike_ratio"]:
        snap.signals.append(f"Volume {vol_ratio:.1f}x the 20-day average")
        if abs(chg) >= 2.5:
            snap.alerts.append(f"Volume spike ({vol_ratio:.1f}x) with a {chg:+.1f}% move")

    # ---- rule-based bias -----------------------------------------------------------
    score = 0
    score += 1 if snap.trend == "uptrend" else -1 if snap.trend == "downtrend" else 0
    score += 1 if cross == "up" else -1 if cross == "down" else 0
    score += 1 if snap.oversold_votes >= 2 else -1 if snap.overbought_votes >= 2 else 0  # mean-reversion view
    snap.bias = "bullish" if score >= 2 else "bearish" if score <= -2 else "neutral"
    return snap
