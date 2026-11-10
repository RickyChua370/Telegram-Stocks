"""Run context shared by all sections (data caches, accumulated 'stocks in focus', LLM access)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from .config import Settings, load_yaml
from .llm import LLMClient, LLMUnavailable
from .models import Item
from .sources.bursa import Universe
from .state import State

log = logging.getLogger("stockbot.ctx")

SYSTEM_BASE = (
    "You are a careful market analyst writing a daily briefing for a Malaysian RETAIN INVESTOR: a non-professional "
    "who trades Bursa Malaysia stocks over days to a few weeks. "
    "Rules: (1) Plain English, short sentences; explain any jargon in a few words. "
    "(2) Always say what it MEANS for a retail investor, not only what happened. "
    "(3) Never give personal financial advice or promise outcomes; use words like 'could', 'may', 'looks'. "
    "(4) Separate facts (from the provided items) from your inference, and say when evidence is thin. "
    "(5) Do NOT invent facts, numbers, dates, companies or stock codes that are not in the input. "
    "(6) Refer to input items only by their id. (7) Plain text only: no markdown, no asterisks, no bullet symbols. "
    "(8) Reply with ONLY one valid JSON object that follows the requested schema."
).replace("RETAIN INVESTOR", "RETAIL INVESTOR")


@dataclass
class StockFocus:
    code: str
    name: str
    sector: str = ""
    reasons: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)       # {title,url,type,direction,importance,plain}
    importance: int = 0

    def add(self, reason: str, importance: int, item: dict | None = None) -> None:
        if reason and reason not in self.reasons:
            self.reasons.append(reason)
        if item and all(i.get("url") != item.get("url") or i.get("title") != item.get("title") for i in self.items):
            self.items.append(item)
        self.importance += importance


@dataclass
class Context:
    settings: Settings
    state: State
    llm: LLMClient
    universe: Universe
    market: pd.DataFrame
    now: datetime
    watchlist: list[str]
    ignore_seen: bool = False
    since_filings: datetime | None = None
    focus: dict[str, StockFocus] = field(default_factory=dict)
    global_items: list[Item] = field(default_factory=list)
    global_themes: list[dict] = field(default_factory=list)
    company_items: list[Item] = field(default_factory=list)
    gov_items: list[Item] = field(default_factory=list)
    insider_summaries: list[dict] = field(default_factory=list)
    tech: dict = field(default_factory=dict)
    reports_due: list = field(default_factory=list)
    used_ids: set[str] = field(default_factory=set)       # items shown today -> marked as seen after sending
    run_meta: dict = field(default_factory=dict)

    @property
    def today(self) -> date:
        return self.now.date()

    # ---- helpers ------------------------------------------------------------------
    def cfg(self, *path, default=None):
        return self.settings.get(*path, default=default)

    def stock_name(self, code: str) -> str:
        return self.universe.name(code)

    def sector_of(self, code: str) -> str:
        if self.market is None or self.market.empty:
            return ""
        row = self.market[self.market["code"] == code]
        return row.iloc[0]["sector"] if not row.empty else ""

    def market_row(self, code: str) -> dict | None:
        if self.market is None or self.market.empty:
            return None
        row = self.market[self.market["code"] == code]
        return row.iloc[0].to_dict() if not row.empty else None

    def liquid_codes(self, top_n: int) -> list[str]:
        t = self.cfg("technicals", default={}) or {}
        m = self.market
        if m is None or m.empty:
            return []
        m = m[(m["price"] >= t.get("min_price", 0.3)) & (m["mcap_m"].fillna(0) >= t.get("min_market_cap_m", 300))]
        return list(m.sort_values("turnover", ascending=False).head(top_n)["code"])

    def add_focus(self, code: str, reason: str, importance: int = 1, item: dict | None = None) -> None:
        if not code:
            return
        f = self.focus.get(code)
        if f is None:
            f = StockFocus(code=code, name=self.stock_name(code), sector=self.sector_of(code))
            self.focus[code] = f
        f.add(reason, importance, item)

    def llm_json(self, system_extra: str, user: str, *, tier: str = "fast", max_tokens: int = 4096):
        """Call the LLM chain; returns parsed JSON or None when AI is unavailable (callers must fall back)."""
        if not self.llm.available:
            return None
        try:
            return self.llm.complete_json(SYSTEM_BASE + "\n" + system_extra, user, tier=tier, max_tokens=max_tokens)
        except LLMUnavailable as exc:
            log.warning("AI unavailable for this step: %s", exc)
            return None


def load_theses() -> list[dict]:
    return load_yaml("theses.yaml").get("theses") or []
