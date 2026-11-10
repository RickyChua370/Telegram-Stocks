"""Section 4: government contracts and mega-projects (data centres, NETR, ECRL, MRT3, other infrastructure)."""
from __future__ import annotations

import logging

from ..config import load_yaml
from ..context import Context
from ..fmt import bold, clean, esc, link, truncate
from ..models import Item, SectionResult
from ..sources import news
from ..tagging import project_matches
from .common import ai_unavailable_note, dir_emoji, kv, lines, source_links

log = logging.getLogger("stockbot.s4")

SYSTEM = """Task: you track Malaysian government contracts and mega-projects for a retail investor. Input: news headlines
and Bursa filings grouped by project. For each project that has genuinely NEW information, report the stage
(approval, tender, award, construction, delay, funding, other), what changed, which LISTED Malaysian companies are
named in the items as winners/contractors/suppliers (only names that appear in the input), and what it could mean for
investors. Distinguish an announcement or approval (not yet money) from a signed award (contract value known).
Skip projects with no new information. Do not invent contract values or companies.
JSON schema:
{"projects": [{"project": "project name from input", "stage": "approval|tender|award|construction|delay|funding|other",
   "update": "what is new, 1-2 sentences, facts only",
   "so_what": "what it could mean for a retail investor, plain words, 1-2 sentences",
   "companies": ["company names that appear in the input"],
   "direction": "bullish|bearish|neutral|unclear", "item_ids": ["id", ...]}],
 "quiet": ["project names with nothing new"]}"""


def run(ctx: Context) -> SectionResult:
    title = "🏛️ <b>4. Government contracts and mega-projects</b>"
    projects = load_yaml("projects.yaml").get("projects") or []
    per = ctx.cfg("limits", "gov_news_per_project", default=5)
    hours = ctx.cfg("lookback_hours", "news", default=30) * 2        # contracts move slowly: look back ~2.5 days
    groups: dict[str, list[Item]] = {}
    for p in projects:
        got = news.google_news(p["query"], when="3d", limit=per + 3, topic=p["name"])
        got += news.edge_news(p["query"], when="3d", limit=3, topic=p["name"])
        got = news.dedupe(news.recent(got, hours))
        if not ctx.ignore_seen:
            got = [g for g in got if not ctx.state.is_seen(g.id)]
        groups[p["name"]] = got[:per]

    # Link Bursa filings from Section 3 that mention a project (e.g. a contractor's Letter of Award).
    for it in ctx.company_items:
        if it.kind == "filing" and "gov_project" in it.tags:
            for name in project_matches(f"{it.title} {it.text}"):
                groups.setdefault(name, []).append(it)

    all_items = [i for g in groups.values() for i in g]
    ctx.gov_items = all_items
    if not all_items:
        return SectionResult("gov", title, [title + "\n\nNo new government-project news found in the last few days."])

    lookup = {i.id: i for i in all_items}
    blocks = []
    for name, its in groups.items():
        if not its:
            continue
        rows = "\n".join(
            f"  [{i.id}] {'FILING ' if i.kind == 'filing' else ''}{truncate(i.title, 170)}"
            + (f" ({ctx.stock_name(i.code)})" if i.code else "") for i in its)
        blocks.append(f"PROJECT: {name}\n{rows}")
    data = ctx.llm_json(SYSTEM, "\n\n".join(blocks), tier="fast", max_tokens=3500)

    parts = [title]
    if not data or not isinstance(data.get("projects"), list):
        parts.append(ai_unavailable_note(ctx))
        for name, its in groups.items():
            if its:
                parts.append(bold(name) + "\n" + "\n".join(f"• {link(truncate(i.title, 110), i.url)}" for i in its[:3]))
        ctx.used_ids.update(i.id for i in all_items)
        return SectionResult("gov", title, parts, ok=data is not None)

    shown = 0
    for p in data["projects"][:8]:
        names = ", ".join(str(c) for c in (p.get("companies") or [])[:6])
        parts.append(lines(
            f"{dir_emoji(p.get('direction'))} <b>{clean(p.get('project'))}</b>  <i>[{clean(p.get('stage'))}]</i>",
            kv("What's new", p.get("update")),
            kv("What it means for you", p.get("so_what")),
            kv("Listed names mentioned", names),
            source_links(p.get("item_ids"), lookup),
        ))
        shown += 1
        for cname in p.get("companies") or []:
            code = ctx.universe.resolve(str(cname))
            if code:
                ctx.add_focus(code, f"{p.get('project')}: {truncate(str(p.get('update')), 70)}", 3,
                              {"title": str(p.get("update")), "url": "", "type": "government project",
                               "direction": p.get("direction", ""), "importance": 3, "plain": p.get("so_what", "")})
    if data.get("quiet"):
        parts.append("<i>No new developments:</i> " + esc(", ".join(str(q) for q in data["quiet"][:8])))
    if shown == 0:
        parts.append("No new developments on tracked projects today.")
    ctx.used_ids.update(i.id for i in all_items)
    return SectionResult("gov", title, parts)
