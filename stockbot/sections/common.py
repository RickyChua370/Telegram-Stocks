"""Helpers shared by sections."""
from __future__ import annotations

from collections import defaultdict

from ..fmt import clean, esc, link
from ..models import Item

DIRECTION_EMOJI = {"bullish": "🟢", "bull": "🟢", "positive": "🟢", "bearish": "🔴", "bear": "🔴", "negative": "🔴",
                   "mixed": "🟡", "neutral": "⚪", "unclear": "⚪"}


def dir_emoji(value: object) -> str:
    return DIRECTION_EMOJI.get(str(value or "").strip().lower(), "⚪")


def balanced(items: list[Item], cap: int, key=lambda i: i.topic) -> list[Item]:
    """Round-robin across groups (newest first inside each) so one noisy topic cannot crowd out the rest."""
    groups: dict[str, list[Item]] = defaultdict(list)
    for it in sorted(items, key=lambda i: i.published.timestamp() if i.published else 0, reverse=True):
        groups[key(it)].append(it)
    out: list[Item] = []
    while len(out) < cap and any(groups.values()):
        for g in list(groups.values()):
            if g and len(out) < cap:
                out.append(g.pop(0))
    return out


def source_links(item_ids, lookup: dict[str, Item], limit: int = 2) -> str:
    links = []
    for iid in item_ids or []:
        it = lookup.get(str(iid))
        if it and it.url:
            links.append(link(it.source or "source", it.url))
        if len(links) >= limit:
            break
    return " · ".join(links)


def emit_block(parts: list[str], heading: str, entries: list[str]) -> None:
    """Add a heading plus entries as separate paragraphs so Telegram message splitting never cuts an entry in half.
    The heading travels with the first entry."""
    if not entries:
        return
    parts.append(f"{heading}\n\n{entries[0]}")
    parts.extend(entries[1:])


def lines(*items: str) -> str:
    return "\n".join(i for i in items if i)


def kv(label: str, value: object) -> str:
    v = clean(value)
    return f"<i>{esc(label)}:</i> {v}" if v else ""


def ai_unavailable_note(ctx) -> str:
    return "<i>AI summaries are unavailable right now, so this shows the raw items without the plain-English explanation.</i>"
