"""Command line: python -m stockbot <run|check|chatid|models|calendar>"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from . import __version__
from .config import load_settings
from .fmt import strip_tags
from .llm import LLMClient, LLMUnavailable, list_gemini_models
from .notify import Telegram, TelegramError
from .pipeline import DISPLAY_ORDER, deliver, run_digest


def _cmd_run(args) -> int:
    settings = load_settings()
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    if only:
        bad = [s for s in only if s not in DISPLAY_ORDER]
        if bad:
            print(f"Unknown section(s): {bad}. Choose from: {', '.join(DISPLAY_ORDER)}")
            return 2
    messages, results, ctx = run_digest(settings, only=only, dry_run=args.dry_run, ignore_seen=args.ignore_seen,
                                        hours=args.hours)
    if args.dry_run:
        for i, m in enumerate(messages, 1):
            print(f"\n{'=' * 20} message {i}/{len(messages)} ({len(m)} chars) {'=' * 20}")
            print(m if args.html else strip_tags(m))
    else:
        deliver(settings, messages, ctx, dry_run=False)
        print(f"Sent {len(messages)} message(s).")
    meta = ctx.run_meta
    print(f"\nDone in {meta.get('seconds')}s | AI calls: {meta.get('ai_calls')} | failed sections: {meta.get('sections_failed') or 'none'}",
          file=sys.stderr)
    if ctx.llm.failures:
        print("AI provider problems: " + " || ".join(ctx.llm.failures[-3:]), file=sys.stderr)
    return 0


def _cmd_chatid(_args) -> int:
    s = load_settings()
    if not s.telegram_token:
        print("Set TELEGRAM_BOT_TOKEN first (see .env.example).")
        return 2
    tg = Telegram(s.telegram_token, "")
    try:
        me = tg.whoami()
        chats = tg.recent_chats()
    except TelegramError as exc:
        print(f"Telegram error: {exc}")
        return 1
    print(f"Bot: @{me.get('username')}")
    if not chats:
        print("No messages found. Open your bot in Telegram, press Start / send 'hi', then run this again.")
        return 1
    for c in chats:
        label = c.get("username") or c.get("title") or c.get("first_name") or ""
        print(f"chat_id = {c['id']}   ({c.get('type')}, {label})")
    return 0


def _cmd_models(_args) -> int:
    s = load_settings()
    if not s.gemini_key:
        print("GEMINI_API_KEY is not set.")
        return 2
    try:
        names = list_gemini_models(s.gemini_key)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not list models: {type(exc).__name__}")
        return 1
    print("Gemini models your key can call (generateContent):")
    for n in sorted(names):
        if "flash" in n or "pro" in n:
            print("  ", n)
    print(f"\nCurrently configured: fast={s.gemini_fast}  smart={s.gemini_smart}")
    return 0


def _cmd_calendar(_args) -> int:
    from . import calendar_events
    s = load_settings()
    today = datetime.now(s.tz).date()
    for e in calendar_events.upcoming(today, 60):
        end = f" → {e.end_date}" if e.end_date else ""
        print(f"{e.date}{end}  {'~' if e.approx else ' '} [{e.category:6}] {e.title}")
    return 0


def _cmd_check(_args) -> int:
    s = load_settings()
    ok = True
    print(f"stockbot {__version__}\n")
    print("Telegram:")
    if not s.telegram_token:
        print("  ✗ TELEGRAM_BOT_TOKEN missing")
        ok = False
    else:
        tg = Telegram(s.telegram_token, s.telegram_chat_id)
        try:
            print(f"  ✓ bot reachable: @{tg.whoami().get('username')}")
        except TelegramError as exc:
            print(f"  ✗ {exc}")
            ok = False
        print("  ✓ TELEGRAM_CHAT_ID set" if s.telegram_chat_id else "  ✗ TELEGRAM_CHAT_ID missing (run: python -m stockbot chatid)")
        ok = ok and bool(s.telegram_chat_id)
    print("\nAI providers (free tiers):")
    llm = LLMClient.from_settings(s)
    if not llm.available:
        print("  ✗ none configured. Set GEMINI_API_KEY (free) and/or GROQ_API_KEY (free). The bot runs without AI but only shows raw items.")
        ok = False
    for p in llm.providers:
        one = LLMClient([p])
        try:
            res = one.complete_json("Reply with JSON only.", 'Return {"ok": true}', tier="fast", max_tokens=50)
            print(f"  ✓ {p.name} responded ({p.fast_model}) -> {res}")
        except LLMUnavailable as exc:
            print(f"  ✗ {p.name}: {str(exc)[:200]}")
            ok = False
    print("\nData sources:")
    from .sources import bursa, news, prices
    uni = bursa.load_universe()
    print(f"  {'✓' if uni.names else '✗'} Bursa company list: {len(uni.names)} securities")
    mk = bursa.load_market_table()
    print(f"  {'✓' if len(mk) else '✗'} Market table: {len(mk)} stocks")
    gn = news.google_news("Bursa Malaysia", when="2d", limit=3)
    print(f"  {'✓' if gn else '✗'} Google News: {len(gn)} headlines")
    hist = prices.fetch_history(["1155"], "3mo")
    print(f"  {'✓' if hist else '✗'} Yahoo Finance prices (Maybank 1155.KL): {'ok' if hist else 'no data'}")
    ok = ok and bool(uni.names) and len(mk) > 0 and bool(hist)
    print("\n" + ("All good." if ok else "Some checks failed: see ✗ above."))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="stockbot", description="Daily Bursa Malaysia briefing bot for Telegram")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="build the digest and send it to Telegram")
    r.add_argument("--dry-run", action="store_true", help="print instead of sending; does not update state")
    r.add_argument("--only", help="comma list of sections: global,calendar,company,gov,thesis,technical,summary")
    r.add_argument("--ignore-seen", action="store_true", help="include items already shown in previous digests")
    r.add_argument("--hours", type=float, help="look back this many hours for filings (default: since last run)")
    r.add_argument("--html", action="store_true", help="with --dry-run, print raw Telegram HTML")
    r.set_defaults(fn=_cmd_run)
    sub.add_parser("check", help="verify keys and data sources").set_defaults(fn=_cmd_check)
    sub.add_parser("chatid", help="find your Telegram chat id").set_defaults(fn=_cmd_chatid)
    sub.add_parser("models", help="list Gemini models your key can use").set_defaults(fn=_cmd_models)
    sub.add_parser("calendar", help="print the next 60 days of calendar events").set_defaults(fn=_cmd_calendar)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    return args.fn(args)
