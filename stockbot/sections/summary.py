"""Section 7: one summary card per key stock, combining news, calendar and technicals."""
from __future__ import annotations

import json
import logging

from ..context import Context, StockFocus
from ..fmt import clean, esc, link, truncate
from ..models import SectionResult
from .common import ai_unavailable_note, dir_emoji

log = logging.getLogger("stockbot.s7")

SYSTEM = """Task: write one summary card per stock for a Malaysian retail investor, using ONLY the facts provided for that stock
(news items, calendar/report timing, pre-computed technical indicators, sector, price). Fields:
- what_happened: the key facts in 1-2 plain sentences.
- why_affected: why this news matters for THIS stock specifically (business exposure, size of the deal relative to the company
  if known, sector effect). If the link is indirect or speculative, say so.
- direction: 'Bull', 'Bear' or 'Neutral' for the next few weeks. If news and technicals disagree, or evidence is thin, choose
  'Neutral' and explain the conflict. Do not default to Bull.
- confidence: low, medium or high (low if there is only one weak source).
- time_horizon: one of 'days', '1-4 weeks', '1-3 months', '3+ months', with a few words on why.
- technical: 1-2 plain sentences from the indicators provided (say 'no chart data' if missing).
- check_next: 2 to 4 concrete things the investor should check next (filings to read, numbers to verify, dates to watch, risks).
Never give buy/sell instructions. Use only stock codes from the input.
JSON schema:
{"cards": [{"code": "code", "what_happened": "", "why_affected": "", "direction": "Bull|Bear|Neutral", "confidence": "low|medium|high",
  "time_horizon": "", "technical": "", "check_next": ["", ""]}]}"""


def _rank(ctx: Context, f: StockFocus) -> float:
    s = float(f.importance)
    t = ctx.tech.get(f.code)
    if t and t.is_alert:
        s += 2
    if any(r.code == f.code for r in ctx.reports_due):
        s += 1.5
    if f.code in ctx.watchlist:
        s += 2
    # Size/liquidity: a RM100m penny stock's news is rarely as relevant to a swing trader as a large-cap's.
    mcap = (ctx.market_row(f.code) or {}).get("mcap_m") or 0
    s += 1.5 if mcap >= 2000 else 0.5 if mcap >= 500 else -1.0 if mcap < 100 else 0.0
    return s


def _facts(ctx: Context, f: StockFocus) -> dict:
    row = ctx.market_row(f.code) or {}
    d: dict = {"code": f.code, "name": f.name, "sector": f.sector or row.get("sector", ""),
               "price": row.get("price"), "change_pct_today": row.get("change_pct"),
               "market_cap_rm_million": row.get("mcap_m"),
               "why_in_focus": [truncate(r, 100) for r in f.reasons[:4]],
               "news": [{"type": i.get("type"), "headline": truncate(str(i.get("title")), 140),
                         "plain": truncate(str(i.get("plain") or ""), 160), "direction": i.get("direction")}
                        for i in sorted(f.items, key=lambda i: -(i.get("importance") or 0))[:4]]}
    t = ctx.tech.get(f.code)
    d["technicals"] = t.compact() if t else None
    due = [r for r in ctx.reports_due if r.code == f.code]
    if due:
        d["results_expected"] = f"~{due[0].expected.isoformat()} (estimate; Bursa deadline {due[0].deadline.isoformat()})"
    return d


def run(ctx: Context) -> SectionResult:
    title = "🔎 <b>7. Key stocks: summary cards</b>"
    # Only stocks with a real reason beyond a stray technical alert qualify.
    cands = [f for f in ctx.focus.values() if f.reasons and (f.items or f.code in ctx.watchlist
                                                              or any(r.code == f.code for r in ctx.reports_due))]
    cands.sort(key=lambda f: -_rank(ctx, f))
    top = cands[: ctx.cfg("limits", "key_stocks", default=8)]
    if not top:
        return SectionResult("summary", title, [title + "\n\nNo stock had enough news or catalysts today to justify a card."])

    facts = [_facts(ctx, f) for f in top]
    data = ctx.llm_json(SYSTEM, "Stocks:\n" + json.dumps(facts, ensure_ascii=False, default=str), tier="smart", max_tokens=6000)
    cards = {str(c.get("code")): c for c in (data or {}).get("cards", []) if isinstance(c, dict)}

    parts = [title]
    if not data:
        parts.append(ai_unavailable_note(ctx))
    for f in top:
        c = cards.get(f.code)
        row = ctx.market_row(f.code) or {}
        px = f" · RM{row['price']:.3f} ({row['change_pct']:+.1f}%)" if row.get("price") and row.get("change_pct") is not None else ""
        header = f"<b>{esc(f.name)}</b> ({esc(f.code)}){px}"
        if c:
            direction = str(c.get("direction", "Neutral")).strip().title()
            checks = "\n".join(f"  • {clean(x)}" for x in (c.get("check_next") or [])[:4])
            parts.append("\n".join(x for x in [
                f"{dir_emoji(direction)} {header}",
                f"<b>Direction:</b> {esc(direction)} · <b>Confidence:</b> {clean(c.get('confidence'))} · "
                f"<b>Horizon:</b> {clean(c.get('time_horizon'))}",
                f"<b>What happened:</b> {clean(c.get('what_happened'))}",
                f"<b>Why this stock:</b> {clean(c.get('why_affected'))}",
                f"<b>Technicals:</b> {clean(c.get('technical'))}",
                f"<b>Check next:</b>\n{checks}" if checks else "",
            ] if x))
        else:
            # Fallback card: facts only, no AI interpretation.
            t = ctx.tech.get(f.code)
            lines = [f"⚪ {header}", "<b>In focus because:</b> " + esc("; ".join(f.reasons[:3]))]
            if t:
                lines.append("<b>Technicals:</b> " + esc(f"trend {t.trend}, RSI {t.rsi:.0f}" if t.rsi else f"trend {t.trend}")
                             + (" · " + esc("; ".join(t.alerts[:2])) if t.alerts else ""))
            seen_types: set[str] = set()
            links = []
            for i in f.items:
                label = str(i.get("type") or "source")
                if i.get("url") and label not in seen_types and len(links) < 2:
                    seen_types.add(label)
                    links.append(link(label, i["url"]))
            if links:
                lines.append(" · ".join(links))
            parts.append("\n".join(lines))
    parts.append("<i>Not financial advice. Direction is an AI reading of the facts above, not a prediction. "
                 "Verify the filings yourself before acting.</i>")
    return SectionResult("summary", title, parts, ok=data is not None or not ctx.llm.available)
