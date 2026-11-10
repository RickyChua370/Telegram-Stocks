"""Orchestrates one daily digest run."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from .config import Settings, load_watchlist
from .context import Context
from .fmt import esc
from .llm import LLMClient
from .models import SectionResult
from .notify import Telegram
from .sections import calendar_section, company_news, global_news, gov_contracts, summary, technical, thesis
from .sources import bursa, prices
from .state import State

log = logging.getLogger("stockbot.pipeline")

# Execution order matters: later sections reuse what earlier ones discovered (stocks in focus, items to score).
RUN_ORDER = ["global", "company", "gov", "calendar", "thesis", "technical", "summary"]
# Order the reader sees.
DISPLAY_ORDER = ["global", "calendar", "company", "gov", "thesis", "technical", "summary"]
RUNNERS = {
    "global": global_news.run, "calendar": calendar_section.run, "company": company_news.run,
    "gov": gov_contracts.run, "thesis": thesis.run, "technical": technical.run, "summary": summary.run,
}
TITLES = {
    "global": "1. Global news", "calendar": "2. Forward calendar", "company": "3. Company news",
    "gov": "4. Government contracts", "thesis": "5. Thesis check", "technical": "6. Technical alerts",
    "summary": "7. Key stock cards",
}


def _since(settings: Settings, state: State, now: datetime, hours: float | None) -> datetime:
    """Filings window: since the last successful run (so weekends/holidays are covered), at least 30h."""
    if hours:
        return now - timedelta(hours=hours)
    base = now - timedelta(hours=settings.get("lookback_hours", "filings", default=26) + 4)
    runs = state.data.get("runs") or []
    if runs:
        try:
            last = datetime.fromisoformat(runs[-1]["at"]).astimezone(now.tzinfo)
            return max(min(base, last - timedelta(hours=1)), now - timedelta(hours=80))
        except (ValueError, KeyError):
            pass
    return base


def build_context(settings: Settings, *, ignore_seen: bool = False, hours: float | None = None) -> Context:
    state = State(settings.state_path)
    now = datetime.now(settings.tz)
    log.info("Loading Bursa company list and market table...")
    universe = bursa.load_universe()
    market = bursa.load_market_table()
    ctx = Context(settings=settings, state=state, llm=LLMClient.from_settings(settings), universe=universe,
                  market=market, now=now, watchlist=load_watchlist(), ignore_seen=ignore_seen)
    ctx.since_filings = _since(settings, state, now, hours)
    log.info("Universe %d | market rows %d | filings since %s | AI providers: %s",
             len(universe.names), len(market), ctx.since_filings.strftime("%d %b %H:%M"),
             ", ".join(p.name for p in ctx.llm.providers) or "none (raw mode)")
    return ctx


def header(ctx: Context) -> str:
    klci = prices.klci_snapshot()
    when = ctx.now.strftime("%a %d %b %Y, %I:%M %p MYT").replace(" 0", " ")
    lines = [f"📊 <b>Daily Bursa Brief</b> · {esc(when)}"]
    if klci:
        arrow = "🔺" if klci["change_pct"] >= 0 else "🔻"
        lines.append(f"{arrow} FBM KLCI {klci['level']:,.2f} ({klci['change_pct']:+.2f}%) as of {klci['date'].strftime('%d %b')}")
    if ctx.watchlist:
        lines.append(f"Watchlist: {esc(', '.join(ctx.watchlist[:12]))}")
    lines.append("<i>Sections: 1 Global · 2 Calendar · 3 Company · 4 Gov projects · 5 Thesis · 6 Technicals · 7 Key stocks</i>")
    return "\n".join(lines)


def run_digest(settings: Settings, *, only: list[str] | None = None, dry_run: bool = False,
               ignore_seen: bool = False, hours: float | None = None) -> tuple[list[str], list[SectionResult], Context]:
    started = time.time()
    ctx = build_context(settings, ignore_seen=ignore_seen or dry_run, hours=hours)
    wanted = [k for k in RUN_ORDER if not only or k in only]
    results: dict[str, SectionResult] = {}
    for key in wanted:
        t0 = time.time()
        try:
            results[key] = RUNNERS[key](ctx)
            log.info("Section %-9s ok=%s in %.1fs", key, results[key].ok, time.time() - t0)
        except Exception as exc:  # noqa: BLE001 - one broken section must not kill the digest
            log.exception("Section %s failed", key)
            results[key] = SectionResult(key, TITLES[key], [f"⚠️ <b>{esc(TITLES[key])}</b> could not be built today "
                                                           f"({esc(type(exc).__name__)}). The other sections are unaffected."],
                                         ok=False, error=str(exc))
    parts = [header(ctx)]
    for key in DISPLAY_ORDER:
        if key in results:
            parts.extend(results[key].parts)
    parts.append("<i>Sources: Bursa filings via KLSE Screener, Google News / The Edge / Bernama, Yahoo Finance. "
                 "AI-written summaries can contain mistakes. This is information, not investment advice.</i>")
    ctx.run_meta = {"seconds": round(time.time() - started), "ai_calls": ctx.llm.calls,
                    "sections_failed": [k for k, r in results.items() if not r.ok]}
    from .fmt import chunk_parts
    return chunk_parts(parts), list(results.values()), ctx


def deliver(settings: Settings, messages: list[str], ctx: Context, *, dry_run: bool) -> None:
    if dry_run:
        return
    tg = Telegram(settings.telegram_token, settings.telegram_chat_id)
    if not tg.configured:
        raise SystemExit("Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see .env.example).")
    for i, msg in enumerate(messages):
        tg.send_html(msg)
        if i < len(messages) - 1:
            time.sleep(1.1)
    ctx.state.mark_seen(ctx.used_ids)
    ctx.state.commit(ctx.run_meta)
