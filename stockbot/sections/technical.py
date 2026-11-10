"""Section 6: technical alerts on DAILY candles (EMA, WMA, RSI, Stochastic, Bollinger Bands).

Scope: (a) stocks mentioned in today's news / your watchlist / reporting soon, and
(b) the market's most heavily traded stocks (top N by turnover).
"""
from __future__ import annotations

import json
import logging

from .. import technicals
from ..context import Context
from ..fmt import clean, esc, truncate
from ..models import SectionResult
from ..sources import prices
from .common import ai_unavailable_note, dir_emoji, emit_block

log = logging.getLogger("stockbot.s6")

SYSTEM = """Task: explain technical-indicator readings in plain words for a Malaysian retail investor who trades over days to a few
weeks using DAILY candles. Each stock has pre-computed indicators: RSI, slow Stochastic (%K/%D), Bollinger %B, EMA20/EMA50/EMA200,
WMA20, volume ratio vs the 20-day average, trend, and fired alerts. Some stocks also list the news reasons they are in focus.
Guidance: oversold readings inside a downtrend can keep falling (a falling knife), while oversold inside an uptrend is more
often a pullback; overbought inside a strong uptrend can stay overbought; an EMA cross is a slow, lagging signal; a volume spike
makes the move more credible. Mention the news reason only if it is provided. Be balanced; never tell the user to buy or sell.
Use ONLY the numbers given. 'bias' is your read of the next few weeks from the chart alone.
JSON schema:
{"market_note": "1-2 sentences on what the screen says about overall market mood (e.g. many oversold = weak tape)",
 "stocks": [{"code": "code from input", "plain_english": "1-2 sentences, plain words, what the chart is saying",
   "bias": "bullish|bearish|neutral", "caution": "one short caution or what would invalidate the read"}]}"""


def _line(ctx: Context, snap) -> str:
    name = ctx.stock_name(snap.code)
    arrow = "🔺" if snap.change_pct > 0 else "🔻" if snap.change_pct < 0 else "▫️"
    bits = [f"{arrow} <b>{esc(name)}</b> ({esc(snap.code)}) RM{snap.close:.3f} ({snap.change_pct:+.1f}%)",
            f"trend {esc(snap.trend)}"]
    if snap.rsi is not None:
        bits.append(f"RSI {snap.rsi:.0f}")
    if snap.stoch_k is not None:
        bits.append(f"Stoch {snap.stoch_k:.0f}")
    if snap.pct_b is not None:
        bits.append(f"BB%B {snap.pct_b:.2f}")
    if snap.volume_ratio:
        bits.append(f"vol {snap.volume_ratio:.1f}x")
    return " · ".join(bits)


def run(ctx: Context) -> SectionResult:
    title = "📉 <b>6. Technical alerts (daily candles)</b>"
    cfg = ctx.cfg("technicals", default={}) or {}
    hv = ctx.liquid_codes(cfg.get("high_volume_top_n", 30))
    in_focus = sorted(ctx.focus, key=lambda c: -ctx.focus[c].importance)
    codes = list(dict.fromkeys(ctx.watchlist + in_focus + hv))[:90]
    if not codes:
        return SectionResult("technical", title, [title + "\n\nNo stocks to screen."])

    hist = prices.fetch_history(codes, cfg.get("history_period", "1y"))
    snaps = {}
    for code, df in hist.items():
        if (ctx.now.date() - df.index[-1].date()).days > 6:
            continue                                    # suspended / stale data
        s = technicals.analyze(code, df, cfg)
        if s:
            snaps[code] = s
    ctx.tech = snaps
    if not snaps:
        return SectionResult("technical", title, [title + "\n\nPrice data was unavailable (Yahoo Finance)."], ok=False)

    # Penny stocks (below min_price) stay in Section 3 as news; charts on RM0.05 stocks are mostly noise.
    min_price = cfg.get("min_price", 0.30)
    focus_codes = [c for c in dict.fromkeys(ctx.watchlist + in_focus)
                   if c in snaps and (c in ctx.watchlist or snaps[c].close >= min_price)]
    hv_alerts = [c for c in hv if c in snaps and snaps[c].is_alert and c not in focus_codes]
    focus_show = focus_codes[:15]
    hv_show = sorted(hv_alerts, key=lambda c: -(snaps[c].oversold_votes + snaps[c].overbought_votes + len(snaps[c].alerts)))[:10]

    payload_rows = []
    for c in focus_show + hv_show:
        d = snaps[c].compact()
        if c in ctx.focus:
            d["news_reasons"] = [truncate(r, 80) for r in ctx.focus[c].reasons[:2]]
        payload_rows.append(d)
    data = ctx.llm_json(SYSTEM, "Stocks:\n" + json.dumps(payload_rows, ensure_ascii=False), tier="fast",
                        max_tokens=4500) if payload_rows else None
    ai = {str(r.get("code")): r for r in (data or {}).get("stocks", []) if isinstance(r, dict)}

    parts = [title]
    if data and data.get("market_note"):
        n_os = sum(1 for s in snaps.values() if s.oversold_votes >= 2)
        n_ob = sum(1 for s in snaps.values() if s.overbought_votes >= 2)
        parts.append(f"💡 <b>Tape check:</b> {clean(data['market_note'])} "
                     f"<i>({len(snaps)} stocks screened: {n_os} oversold, {n_ob} overbought)</i>")
    elif not data:
        parts.append(ai_unavailable_note(ctx))

    def block(heading: str, codes_: list[str]) -> None:
        if not codes_:
            return
        rows: list[str] = []
        for c in codes_:
            s, rec = snaps[c], ai.get(c, {})
            lines_ = [_line(ctx, s)]
            if c in ctx.focus:
                lines_.append(f"<i>In focus:</i> {esc(truncate('; '.join(ctx.focus[c].reasons[:2]), 150))}")
            for a in s.alerts:
                lines_.append(f"⚠️ {esc(a)}")
            if not s.alerts and s.signals:
                lines_.append(f"<i>Readings:</i> {esc(truncate('; '.join(s.signals[:3]), 170))}")
            if rec.get("plain_english"):
                lines_.append(f"{dir_emoji(rec.get('bias'))} {clean(rec['plain_english'])}"
                              + (f" <i>Caution: {clean(rec['caution'])}</i>" if rec.get("caution") else ""))
            rows.append("\n".join(lines_))
        emit_block(parts, f"<b>{esc(heading)}</b>", rows)

    block("Stocks in the news / on your watchlist", focus_show)
    block("High-volume stocks with technical alerts", hv_show)
    if not focus_show and not hv_show:
        parts.append("No technical alerts among the screened stocks today.")
    parts.append("<i>Indicators use daily candles (EMA20/50/200, WMA20, RSI14, Stochastic 14-3-3, Bollinger 20/2). "
                 "They lag price and fail often; treat alerts as prompts to research, not signals to trade.</i>")
    return SectionResult("technical", title, parts, ok=data is not None or not ctx.llm.available)
