"""Section 1: major global news that can move Malaysian sectors."""
from __future__ import annotations

import logging

from ..config import load_yaml
from ..context import Context
from ..fmt import bold, clean, esc, link, truncate
from ..models import SectionResult
from ..sources import news
from .common import ai_unavailable_note, balanced, dir_emoji, kv, lines, source_links

log = logging.getLogger("stockbot.s1")

SYSTEM = """Task: from today's global headlines, pick the 3 to 5 themes that matter most for Malaysian stocks
(sectors such as AI/data centres, oil and gas, energy, plantation, consumer, banks, construction, tech/semiconductor,
supply chain, trade policy). Merge duplicate stories into one theme. Ignore anything with no plausible Malaysia link.
For each theme explain, in plain words for a retail investor, what it could mean for Bursa stocks.
Only name Malaysian listed companies when the link is direct and you are confident; otherwise name the sector only.
JSON schema:
{"bottom_line": "one or two sentences: the single most important takeaway today",
 "themes": [{"headline": "short plain headline", "sectors": ["sector", ...],
   "what_happened": "1-2 facts from the items",
   "so_what": "what this could mean for a Malaysian retail investor (1-2 sentences, plain words)",
   "impact": "positive|negative|mixed|unclear",
   "malaysia_exposure": ["listed company or sector names"],
   "item_ids": ["id", ...]}]}"""


def run(ctx: Context) -> SectionResult:
    cfg = load_yaml("topics.yaml").get("global") or []
    hours = ctx.cfg("lookback_hours", "news", default=30)
    items = []
    for t in cfg:
        items += news.google_news(t["query"], when="1d", limit=8, topic=t["topic"])
    items += news.bernama_business(20)
    items = news.dedupe(news.recent(items, hours))
    if not ctx.ignore_seen:
        items = [i for i in items if not ctx.state.is_seen(i.id)]
    items = balanced(items, ctx.cfg("limits", "global_news_items", default=45))
    ctx.global_items = items
    title = "🌍 <b>1. Global news that matters for Malaysia</b>"
    if not items:
        return SectionResult("global", title, [title + "\n\nNo notable new global headlines found in the last day."])

    lookup = {i.id: i for i in items}
    payload = "\n".join(f"[{i.id}] ({i.topic}) {truncate(i.title, 160)} — {i.source}" for i in items)
    data = ctx.llm_json(SYSTEM, "Today's headlines:\n" + payload, tier="smart", max_tokens=3500)

    parts = [title]
    if not data or not isinstance(data.get("themes"), list):
        parts.append(ai_unavailable_note(ctx))
        by_topic: dict[str, list[str]] = {}
        for i in items[:30]:
            by_topic.setdefault(i.topic, []).append(f"• {link(truncate(i.title, 110), i.url)} <i>({esc(i.source)})</i>")
        for topic, rows in by_topic.items():
            parts.append(bold(topic) + "\n" + "\n".join(rows[:4]))
        ctx.used_ids.update(i.id for i in items[:30])
        return SectionResult("global", title, parts, ok=data is not None)

    if data.get("bottom_line"):
        parts.append(f"💡 <b>Bottom line:</b> {clean(data['bottom_line'])}")
    for n, th in enumerate(data["themes"][:5], 1):
        sectors = ", ".join(str(s) for s in (th.get("sectors") or [])[:4])
        exposure = ", ".join(str(s) for s in (th.get("malaysia_exposure") or [])[:5])
        parts.append(lines(
            f"{dir_emoji(th.get('impact'))} <b>{n}. {clean(th.get('headline'))}</b>" + (f"  <i>[{esc(sectors)}]</i>" if sectors else ""),
            kv("What happened", th.get("what_happened")),
            kv("What it means for you", th.get("so_what")),
            kv("Malaysian exposure", exposure),
            source_links(th.get("item_ids"), lookup),
        ))
        for name in th.get("malaysia_exposure") or []:
            code = ctx.universe.resolve(str(name))
            if code:
                ctx.add_focus(code, f"Global theme: {truncate(str(th.get('headline')), 70)}", 1,
                              {"title": th.get("headline", ""), "url": "", "type": "global theme",
                               "direction": th.get("impact", ""), "importance": 2, "plain": th.get("so_what", "")})
    ctx.used_ids.update(i.id for i in items)
    ctx.global_themes = data["themes"]
    return SectionResult("global", title, parts)
