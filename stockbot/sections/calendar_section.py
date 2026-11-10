"""Section 2: forward calendar of scheduled catalysts, policy tracker and companies reporting soon."""
from __future__ import annotations

import logging
from datetime import timedelta

from .. import calendar_events, reports
from ..config import load_yaml
from ..context import Context
from ..fmt import bold, clean, esc, link, truncate
from ..models import SectionResult
from ..sources import news
from .common import ai_unavailable_note, emit_block, kv, lines, source_links

log = logging.getLogger("stockbot.s2")

CATEGORY_EMOJI = {"BNM": "🏦", "Fed": "🇺🇸", "OPEC+": "🛢️", "PBoC": "🇨🇳", "DOSM": "📊", "Policy": "📜", "Index": "🔀"}

SYSTEM = """Task: build the 'what is coming' briefing for a Malaysian retail investor. Input has three parts:
EVENTS (numbered, scheduled catalysts), POLICY NEWS (headlines about Malaysian budget/policy/subsidies, with ids),
and REPORTS (companies expected to announce results soon; dates are estimates based on last year's timing).
1. For each event that matters (importance 2 or 3), explain in plain words why a retail investor should care and what
   sectors/stocks are usually sensitive. Index events: explain forced buying/selling by index funds in simple terms.
2. From POLICY NEWS, report the latest development and the next date or stage ONLY if the headlines state it.
3. Summarise how to think about reporting season (results can move prices; expectations vs actual).
Never invent dates. Say 'date not confirmed' when unsure.
JSON schema:
{"week_ahead": "2-3 sentences: the most important things in the next 7 days",
 "events": [{"n": 1, "so_what": "plain explanation, 1-2 sentences"}],
 "policy": [{"topic": "topic name", "latest": "latest development (facts)", "next": "next date/stage if stated, else empty",
   "so_what": "plain-words meaning for a retail investor", "item_ids": ["id"]}],
 "reports_note": "1-2 sentences on the reporting-season outlook and what to check"}"""


def _fmt_date(d) -> str:
    return d.strftime("%a %d %b")


def run(ctx: Context) -> SectionResult:
    title = "🗓️ <b>2. Forward calendar: catalysts coming up</b>"
    horizon = ctx.cfg("calendar", "horizon_days", default=21)
    window = ctx.cfg("calendar", "report_window_days", default=7)
    events = calendar_events.upcoming(ctx.today, horizon)

    # Policy headlines (Budget 2027, 13MP, subsidies...)
    policy_cfg = load_yaml("topics.yaml").get("policy") or []
    pol_items = []
    for t in policy_cfg:
        pol_items += news.google_news(t["query"], when="2d", limit=5, topic=t["topic"])
        pol_items += news.edge_news(t["query"], when="2d", limit=2, topic=t["topic"])
    pol_items = news.dedupe(pol_items)[:24]
    lookup = {i.id: i for i in pol_items}

    # Companies expected to report soon: watchlist + stocks in today's news + most liquid names
    max_c = ctx.cfg("calendar", "report_candidates", default=60)
    cand = list(dict.fromkeys(ctx.watchlist + sorted(ctx.focus, key=lambda c: -ctx.focus[c].importance)
                              + ctx.liquid_codes(40)))[:max_c]
    lookahead = max(window, 21)
    est_all = reports.upcoming_reports(cand, ctx.state, ctx.today, lookahead, max_c)
    due = [r for r in est_all if (r.expected - ctx.today).days <= window]
    later = [r for r in est_all if (r.expected - ctx.today).days > window]
    ctx.reports_due = due
    for r in due:
        ctx.add_focus(r.code, f"Results expected ~{_fmt_date(r.expected)}", 1)

    ev_lines = [f"{n}. {e.date.isoformat()}{'→' + e.end_date.isoformat() if e.end_date else ''}"
                f"{' (approx)' if e.approx else ''} [{e.category}] importance {e.importance}: {e.title}. {e.note}"
                for n, e in enumerate(events, 1)]
    pol_lines = [f"[{i.id}] ({i.topic}) {truncate(i.title, 150)}" for i in pol_items]
    rep_lines = [f"{r.company} ({r.code}) expected ~{r.expected.isoformat()} (basis: {r.basis}; Bursa deadline {r.deadline.isoformat()})"
                 for r in due[:25]]
    user = ("TODAY: " + ctx.today.isoformat() + "\n\nEVENTS:\n" + ("\n".join(ev_lines) or "none") +
            "\n\nPOLICY NEWS:\n" + ("\n".join(pol_lines) or "none") +
            "\n\nREPORTS:\n" + ("\n".join(rep_lines) or "none"))
    data = ctx.llm_json(SYSTEM, user, tier="smart", max_tokens=3500) if (events or pol_items or due) else None
    notes = {int(e.get("n", 0)): e.get("so_what", "") for e in (data or {}).get("events", []) if isinstance(e, dict)}

    parts = [title]
    if data and data.get("week_ahead"):
        parts.append(f"💡 <b>Next 7 days:</b> {clean(data['week_ahead'])}")
    elif not data and (events or pol_items or due):
        parts.append(ai_unavailable_note(ctx))

    # ---- scheduled events (weekly recurring items collapsed into one line) ----
    weekly = [e for e in events if e.title.startswith("MOF weekly")]
    shown = [e for e in events if e not in weekly]
    if shown:
        week_end = ctx.today + timedelta(days=7)
        for label, sel in (("This week", [e for e in shown if e.date <= week_end]),
                           ("Later", [e for e in shown if e.date > week_end])):
            if not sel:
                continue
            entries: list[str] = []
            for e in sel:
                n = events.index(e) + 1
                when = _fmt_date(e.date) + (f" → {_fmt_date(e.end_date)}" if e.end_date else "")
                days = e.days_from(ctx.today)
                tag = "today" if days == 0 else f"in {days}d" if days > 0 else "ongoing"
                head = (f"{CATEGORY_EMOJI.get(e.category, '•')} <b>{esc(when)}{' ~' if e.approx else ''}</b> ({tag}) "
                        f"{esc(e.title)}")
                note = notes.get(n) or (e.note if not data else "")
                entries.append(head + (f"\n   <i>Why it matters:</i> {clean(note)}" if note else "") +
                               (f"\n   {link('source', e.source)}" if e.source and e.importance >= 3 else ""))
            emit_block(parts, f"<b>{label}</b>", entries)
        if any(e.approx for e in shown):
            parts.append("<i>~ = estimated or typical timing, not officially confirmed. Check the source before trading around it.</i>")
    if weekly:
        parts.append("⛽ MOF announces fuel prices every Wednesday (BUDI95 RON95 stays at RM1.99/litre; unsubsidised prices float weekly).")

    # ---- policy tracker ----
    if data and isinstance(data.get("policy"), list) and data["policy"]:
        block = ["<b>📜 Policy tracker (Budget 2027, 13MP, subsidies)</b>"]
        for p in data["policy"][:5]:
            block.append(lines(
                f"• <b>{clean(p.get('topic'))}</b>",
                kv("Latest", p.get("latest")), kv("Next", p.get("next")), kv("What it means for you", p.get("so_what")),
                source_links(p.get("item_ids"), lookup, 1)))
        parts.append("\n".join(block))

    # ---- results coming up ----
    if due:
        block = [f"<b>📑 Results expected in the next {window} days</b> <i>(estimated from last year's timing)</i>"]
        for r in due[:15]:
            block.append(f"• <b>{esc(ctx.stock_name(r.code))}</b> ({esc(r.code)}): ~{_fmt_date(r.expected)} "
                         f"· quarter ended {r.period_end.strftime('%d %b')} · Bursa deadline {r.deadline.strftime('%d %b')}")
        if data and data.get("reports_note"):
            block.append(f"<i>How to think about it:</i> {clean(data['reports_note'])}")
        parts.append("\n".join(block))
    elif cand:
        parts.append(f"📑 None of the {len(cand)} stocks checked are expected to report in the next {window} days.")
    if later:
        nxt = ", ".join(f"{esc(ctx.stock_name(r.code))} ~{r.expected.strftime('%d %b')}" for r in later[:10])
        parts.append(f"<b>Reporting a little later (up to {lookahead} days):</b> {nxt}")

    if len(parts) == 1:
        parts.append("No scheduled catalysts found in the window.")
    ctx.used_ids.update(i.id for i in pol_items)
    return SectionResult("calendar", title, parts, ok=data is not None or not ctx.llm.available)
