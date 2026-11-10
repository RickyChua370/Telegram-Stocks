"""News sources: Google News RSS (free, works for any query incl. site:theedgemalaysia.com) and Bernama RSS."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser

from .. import http
from ..models import Item, make_id, norm_title

log = logging.getLogger("stockbot.news")

EDGE_SITE = "theedgemalaysia.com"


def _dt(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def google_news(query: str, *, when: str = "1d", limit: int = 10, site: str | None = None,
                topic: str = "") -> list[Item]:
    q = f"{query} site:{site}" if site else query
    q = f"{q} when:{when}"
    url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-MY&gl=MY&ceid=MY:en"
    try:
        feed = feedparser.parse(http.get(url).content)
    except Exception as exc:  # noqa: BLE001
        log.warning("Google News failed for %r: %s", query[:50], exc)
        return []
    items: list[Item] = []
    for e in feed.entries:
        title = (e.get("title") or "").strip()
        src = ((e.get("source") or {}).get("title") or "").strip()
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3].strip()
        elif " - " in title and not src:
            title, _, src = title.rpartition(" - ")
        if not title:
            continue
        items.append(Item(id=make_id("news", norm_title(title)), title=title, url=e.get("link", ""),
                          source=src or ("The Edge Malaysia" if site == EDGE_SITE else "Google News"),
                          published=_dt(e), kind="news", topic=topic))
        if len(items) >= limit:
            break
    return items


def edge_news(query: str, *, when: str = "1d", limit: int = 10, topic: str = "") -> list[Item]:
    """The Edge Malaysia headlines (via Google News site: search; The Edge has no public RSS)."""
    return google_news(query, when=when, limit=limit, site=EDGE_SITE, topic=topic)


_BERNAMA_BIZ = re.compile(r"^(business|econom|financ|market|bursa|corporate|energy|oil|commodit|trade)", re.I)


def bernama_business(limit: int = 30) -> list[Item]:
    try:
        raw = http.get("https://www.bernama.com/en/rssfeed.php").content
        # Bernama declares ISO-8859-1 but actually serves UTF-8, which garbles quotes/dashes; decode ourselves.
        text = re.sub(r'encoding="[^"]*"', "", raw.decode("utf-8", errors="replace"), count=1)
        feed = feedparser.parse(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("Bernama RSS failed: %s", exc)
        return []
    out: list[Item] = []
    for e in feed.entries:
        raw = (e.get("title") or "").strip()
        section, _, title = raw.partition(" : ")
        if not title or not _BERNAMA_BIZ.match(section.strip()):
            continue
        out.append(Item(id=make_id("news", norm_title(title)), title=title.strip(), url=e.get("link", ""),
                        source="Bernama", published=_dt(e), kind="news", topic="Bernama business"))
        if len(out) >= limit:
            break
    return out


def dedupe(items: list[Item]) -> list[Item]:
    seen: set[str] = set()
    out = []
    for it in items:
        if it.id in seen:
            continue
        seen.add(it.id)
        out.append(it)
    return out


def recent(items: list[Item], hours: int) -> list[Item]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [i for i in items if i.published is None or i.published >= cutoff]
