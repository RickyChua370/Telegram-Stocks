"""Section 3: company-level news.

Sources: Bursa filings (via KLSE Screener) + recent results + The Edge Malaysia / Bernama headlines.
Pipeline: gather -> resolve stock codes -> rule-based tag/score -> parse insider trades deterministically ->
AI explains the top items in plain English -> render.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from ..config import load_yaml
from ..context import Context
from ..fmt import bold, clean, esc, link, truncate
from ..models import Item, SectionResult, make_id
from ..sources import bursa, news
from ..tagging import TAG_LABEL, is_noise, tag_item
from .common import ai_unavailable_note, dir_emoji, emit_block, lines

log = logging.getLogger("stockbot.s3")

SYSTEM = """Task: you are given today's Bursa Malaysia filings, results and company headlines, each with an id.
Rate each item's importance (1-5) for a retail investor who holds stocks for days to weeks, and explain it in plain words.
Importance guide: 5 = could clearly move the share price or signals serious risk; 3 = worth knowing; 1 = routine.
Be careful with direction: a contract win is not automatically bullish if the value is tiny relative to the company;
a profit warning, PN17/GN3 or UMA query is a risk flag. Use the facts given (values, percentages) and say when
important details are missing. Do not guess numbers.
JSON schema:
{"market_takeaway": "1-2 sentences: the pattern across today's company news",
 "items": [{"id": "id from input", "importance": 1, "headline": "max 14 words, plain",
   "plain_english": "what it means for a retail investor, 1-2 sentences",
   "direction": "bullish|bearish|neutral|unclear", "horizon": "days|weeks|months"}]}"""

GROUPS = [
    ("risk", "⚠️ Risk flags (UMA, profit warnings, PN17/GN3, distress)", {"uma", "profit_warning", "pn17_gn3", "distress"}),
    ("corporate", "🤝 Take-overs, disposals and related-party deals", {"takeover", "disposal", "acquisition", "rpt", "fundraising"}),
    ("contract", "🏗️ Contract wins", {"contract"}),
    ("insider", "👤 Director and substantial-shareholder trades", {"insider"}),
    ("results", "📈 Results released", {"results"}),
    ("news", "📰 Other company headlines (The Edge / Bernama)", set()),
]
ALWAYS_SHOW = {"uma", "profit_warning", "pn17_gn3", "distress", "takeover"}


# ----------------------------------------------------------------------------- gathering
def _bodies(urls: list[str], workers: int = 4) -> dict[str, str]:
    if not urls:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        texts = list(pool.map(lambda u: bursa.fetch_announcement_text(u, 1400), urls))
    return dict(zip(urls, texts))


def _liquid_rank(ctx: Context) -> dict[str, int]:
    return {c: i for i, c in enumerate(ctx.liquid_codes(400))}


def _gather_filings(ctx: Context) -> tuple[list[Item], list[Item]]:
    since = ctx.since_filings
    uma = bursa.fetch_announcements(bursa.CAT_UMA, since=since, max_pages=2)
    teco = bursa.fetch_announcements(bursa.CAT_TAKEOVER, since=since, max_pages=2)
    general = (bursa.fetch_announcements(bursa.CAT_GENERAL, since=since, max_pages=8)
               + bursa.fetch_announcements(bursa.CAT_SPECIAL, since=since, max_pages=2)
               + bursa.fetch_announcements(bursa.CAT_QUERY, since=since, max_pages=2))
    holdings = bursa.fetch_announcements(bursa.CAT_SHAREHOLDING, since=since, max_pages=6)
    for it in uma + teco + general + holdings:
        it.code = ctx.universe.resolve(it.company) or ""
    if not ctx.ignore_seen:
        keep = lambda it: not ctx.state.is_seen(it.id)  # noqa: E731
        uma, teco, general, holdings = ([i for i in grp if keep(i)] for grp in (uma, teco, general, holdings))

    rank = _liquid_rank(ctx)
    wl = set(ctx.watchlist)
    prio = lambda it: (0 if it.code in wl else 1, rank.get(it.code, 9999))  # noqa: E731

    # General / special / query: drop routine noise, read the body of the most relevant, then tag.
    general = [g for g in general if not is_noise(g)]
    general.sort(key=prio)
    to_read = general[:70]
    texts = _bodies([g.url for g in to_read])
    tagged: list[Item] = []
    for g in to_read:
        body = texts.get(g.url, "")
        g.text = bursa.core_text(g.title, body)[:500]
        tag_item(g, body=body)
        if g.tags:
            tagged.append(g)
    for it in uma + teco:
        body = bursa.fetch_announcement_text(it.url, 900)
        it.text = bursa.core_text(it.title, body)[:500]
        tag_item(it, body=body)
        tagged.append(it)
    return _merge_duplicates(tagged), holdings


def _merge_duplicates(items: list[Item]) -> list[Item]:
    """The same notice is often filed several times (e.g. daily take-over dealing disclosures): show it once."""
    seen: dict[tuple[str, str], Item] = {}
    counts: dict[tuple[str, str], int] = {}
    for it in items:
        key = (it.code or it.company, " ".join(it.title.lower().split()))
        if key in seen:
            counts[key] += 1
            continue
        seen[key] = it
        counts[key] = 1
    out = []
    for key, it in seen.items():
        if counts[key] > 1:
            it.title = f"{it.title} (filed {counts[key]} times today)"
        out.append(it)
    return out


def _insider_items(ctx: Context, holdings: list[Item]) -> list[Item]:
    """Parse director / substantial-shareholder notices and keep the financially meaningful ones."""
    rank = _liquid_rank(ctx)
    wl = set(ctx.watchlist)
    cand = [h for h in holdings if h.code and (h.code in rank or h.code in wl)]
    cand.sort(key=lambda h: (0 if h.code in wl else 1, rank.get(h.code, 9999)))
    cand = cand[:45]
    texts = _bodies([h.url for h in cand])
    by_code: dict[str, list[dict]] = {}
    for h in cand:
        rec = bursa.parse_shareholding(h.title, texts.get(h.url, ""))
        if not rec or rec["side"] == "other":
            continue
        price = (ctx.market_row(h.code) or {}).get("price")
        value = rec["value_rm"] or (rec["shares"] * price if price else None)
        if not value or value < 300_000:
            continue
        rec.update(value_rm=value, url=h.url, code=h.code, company=h.company, est=rec["value_rm"] is None)
        by_code.setdefault(h.code, []).append(rec)
    items: list[Item] = []
    for code, recs in by_code.items():
        buys = sum(r["value_rm"] for r in recs if r["side"] == "buy")
        sells = sum(r["value_rm"] for r in recs if r["side"] == "sell")
        recs.sort(key=lambda r: -r["value_rm"])
        bits = []
        for r in recs[:3]:
            stake = f"; total stake now {r['stake_after_pct']:.2f}%" if r["stake_after_pct"] is not None else ""
            val = f"RM{r['value_rm']:,.0f}{' (est.)' if r['est'] else ''}"
            bits.append(f"{r['role']} {r['person'] or 'n/a'} {'BOUGHT' if r['side'] == 'buy' else 'SOLD'} "
                        f"{r['shares']:,.0f} shares ({val}){stake}")
        net = buys - sells
        side = "net BUYING" if net > 0 else "net SELLING"
        title = f"Insiders {side} (RM{abs(net):,.0f}): " + " | ".join(bits)
        it = Item(id=make_id("insider", code, ctx.today), title=title, url=recs[0]["url"], source="Bursa filing",
                  published=ctx.now, kind="filing", category="INSIDER", company=recs[0]["company"], code=code,
                  tags=["insider"], text=title)
        it.score = 4 + (2 if abs(net) >= 2_000_000 else 0) + (1 if len(recs) >= 2 and (buys == 0 or sells == 0) else 0)
        items.append(it)
    items.sort(key=lambda i: -i.score)
    return items[:10]


def _results_items(ctx: Context) -> list[Item]:
    rows = bursa.fetch_recent_results()
    since_day = (ctx.since_filings or ctx.now).date()
    rank = _liquid_rank(ctx)
    wl = set(ctx.watchlist)
    out: list[Item] = []
    for r in rows:
        if r["announced"].date() < since_day or not r["code"]:
            continue
        yoy, qoq = r["yoy_pct"], r["qoq_pct"]
        material = (yoy is not None and abs(yoy) >= 40) or (qoq is not None and abs(qoq) >= 60) \
            or r["code"] in wl or r["code"] in rank and rank[r["code"]] < 250
        if not material:
            continue
        mkt = ctx.market_row(r["code"]) or {}
        reaction = f"; share price today {mkt['change_pct']:+.1f}%" if mkt.get("change_pct") is not None else ""
        pct = lambda v: "n/a" if v is None else f"{v:+.0f}%"  # noqa: E731
        # source figures are RM thousand; put the minus sign before "RM"
        rm = lambda v: "n/a" if v is None else f"{'-' if v < 0 else ''}RM{abs(v) * 1000:,.0f}"  # noqa: E731
        np_ = r["net_profit"]
        title = (f"{r['name']} Q{r['quarter']} (to {r['q_date']}): net profit {rm(np_)}, revenue {rm(r['revenue'])}; "
                 f"vs last year {pct(yoy)}, vs last quarter {pct(qoq)}{reaction}")
        it = Item(id=make_id("res", r["code"], r["q_date"]), title=title, url=r["url"], source="KLSE Screener",
                  published=r["announced"], kind="results", company=r["name"], code=r["code"], tags=["results"], text=title)
        it.score = 5 + (1 if (yoy is not None and abs(yoy) >= 100) else 0) + (1 if (np_ is not None and np_ < 0) else 0)
        if not ctx.ignore_seen and ctx.state.is_seen(it.id):
            continue
        out.append(it)
    out.sort(key=lambda i: (-i.score, rank.get(i.code, 9999)))
    return out[:12]


def _edge_items(ctx: Context) -> list[Item]:
    hours = ctx.cfg("lookback_hours", "news", default=30)
    queries = load_yaml("topics.yaml").get("company_edge") or []
    got: list[Item] = []
    for q in queries:
        got += news.edge_news(q, when="1d", limit=6, topic="The Edge")
    got += [b for b in news.bernama_business(30)]
    got = news.dedupe(news.recent(got, hours))
    out = []
    for it in got:
        if not ctx.ignore_seen and ctx.state.is_seen(it.id):
            continue
        codes = ctx.universe.find_companies(it.title)
        it.code = codes[0] if codes else ""
        it.company = ctx.stock_name(it.code) if it.code else ""
        tag_item(it)
        if it.code or it.score >= 5:
            out.append(it)
    out.sort(key=lambda i: -i.score)
    return out[:20]


# ----------------------------------------------------------------------------- rendering
def _group_of(it: Item) -> str:
    tagset = set(it.tags)
    for key, _label, tags in GROUPS:
        if tags and tagset & tags:
            return key
    return "news"


def _facts(it: Item) -> str:
    return " ".join(it.text.split())[:300] if it.kind != "news" else ""


def run(ctx: Context) -> SectionResult:
    title = "🏢 <b>3. Company news</b>"
    filings, holdings = _gather_filings(ctx)
    insiders = _insider_items(ctx, holdings)
    results = _results_items(ctx)
    edge = _edge_items(ctx)

    rank = _liquid_rank(ctx)
    pool = filings + insiders + results + edge
    # rule-based priority, then liquidity (more tradable names first)
    pool.sort(key=lambda i: (-i.score, rank.get(i.code, 9999)))
    pool = pool[: ctx.cfg("limits", "company_llm_items", default=40)]
    ctx.company_items = pool

    for it in pool:                                   # PN17 / GN3 tracker for the next 30 days
        if "pn17_gn3" in it.tags and it.code:
            ctx.state.set_flag(it.code, "pn17_gn3", ctx.stock_name(it.code), truncate(it.title, 80), ctx.today.isoformat())

    if not pool:
        return SectionResult("company", title, [title + "\n\nNo notable company announcements found in this window."])

    lookup = {i.id: i for i in pool}
    payload = "\n".join(
        f"[{i.id}] {ctx.stock_name(i.code) if i.code else (i.company or 'n/a')}"
        f"{f' ({i.code})' if i.code else ''} | {','.join(TAG_LABEL.get(t, t) for t in i.tags) or i.kind} | "
        f"{truncate(i.title, 190)}" + (f" | {_facts(i)}" if _facts(i) and i.kind != 'results' and i.category != 'INSIDER' else "")
        for i in pool)
    data = ctx.llm_json(SYSTEM, "Items:\n" + payload, tier="smart", max_tokens=5000)
    ai = {str(r.get("id")): r for r in (data or {}).get("items", []) if isinstance(r, dict)}

    grouped: dict[str, list[tuple[Item, dict]]] = {k: [] for k, _, _ in GROUPS}
    for it in pool:
        rec = ai.get(it.id, {})
        imp = int(rec.get("importance", 0) or 0) if data else it.score // 2
        always = bool(set(it.tags) & ALWAYS_SHOW)
        if data and imp < 3 and not always:
            continue
        if not data and it.score < 4 and not always:
            continue
        grouped[_group_of(it)].append((it, rec))
        if it.code:
            ctx.add_focus(it.code, truncate(rec.get("headline") or it.title, 90), max(imp, 1) if data else 2,
                          {"title": rec.get("headline") or it.title, "url": it.url, "type": _group_of(it),
                           "direction": rec.get("direction", ""), "importance": imp,
                           "plain": rec.get("plain_english", "")})

    parts = [title]
    if data and data.get("market_takeaway"):
        parts.append(f"💡 <b>Pattern today:</b> {clean(data['market_takeaway'])}")
    elif not data:
        parts.append(ai_unavailable_note(ctx))

    caps = {"risk": 8, "corporate": 6, "contract": 6, "insider": 5, "results": 6, "news": 5}
    for key, label, _ in GROUPS:
        rows = grouped[key][: caps[key]]
        if not rows:
            continue
        block: list[str] = []
        for it, rec in rows:
            who = f"{esc(ctx.stock_name(it.code))} ({esc(it.code)})" if it.code else esc(it.company or "Market")
            facts_first = key in ("insider", "results")        # these titles ARE the facts (parsed from the filing)
            if rec.get("headline"):
                head = clean(rec["headline"])
            else:
                head = "" if facts_first else esc(truncate(it.title, 150))
            entry = [f"{dir_emoji(rec.get('direction'))} <b>{who}</b>" + (f": {head}" if head else "")]
            if facts_first:
                entry.append(f"<i>Facts:</i> {esc(truncate(it.title, 380))}")
            elif not rec.get("headline") and _facts(it):
                entry.append(f"<i>Details:</i> {esc(truncate(_facts(it), 300))}")
            if rec.get("plain_english"):
                entry.append(f"<i>What it means:</i> {clean(rec['plain_english'])}")
            tail = []
            if rec.get("horizon"):
                tail.append(f"Horizon: {esc(rec['horizon'])}")
            tail.append(link(it.source if it.source != "Bursa filing" else "Bursa filing", it.url) if it.url else "")
            entry.append(" · ".join(t for t in tail if t))
            block.append("\n".join(e for e in entry if e))
            ctx.used_ids.add(it.id)
        emit_block(parts, f"<b>{esc(label)}</b>", block)

    pn = ctx.state.flags("pn17_gn3", 30)
    if pn:
        names = ", ".join(f"{esc(r['name'])} ({esc(r['code'])}, {r['last_seen'][5:]})" for r in pn[:10])
        parts.append("🧾 <b>PN17 / GN3 watch (news in the last 30 days):</b> " + names +
                     "\n<i>PN17/GN3 means the company hit Bursa's financial-distress criteria and must submit a plan to fix it; "
                     "these stocks are higher risk.</i>")
    if len(parts) == 1:
        parts.append("Nothing in today's filings crossed the importance threshold.")
    return SectionResult("company", title, parts, ok=data is not None or not ctx.llm.available)
