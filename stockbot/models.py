"""Shared data structures."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime


def make_id(*parts: object) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:10]


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:90]


@dataclass
class Item:
    """One news article, Bursa filing or results row."""

    id: str
    title: str
    url: str
    source: str
    published: datetime | None = None
    kind: str = "news"          # news | filing | results
    category: str = ""          # Bursa category code (UMA, TECO, ...) for filings
    company: str = ""
    code: str = ""              # Bursa stock code when resolvable
    tags: list[str] = field(default_factory=list)
    text: str = ""              # excerpt of the filing body, or article summary
    topic: str = ""
    score: int = 0              # rule-based priority (higher = more important)


@dataclass
class SectionResult:
    key: str
    title: str
    parts: list[str]            # Telegram-HTML paragraphs
    ok: bool = True
    error: str = ""
