"""Telegram-HTML formatting helpers. Telegram supports only a small HTML subset."""
from __future__ import annotations

import html
import re


def esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def clean(text: object) -> str:
    """LLM output -> safe Telegram text: strip markdown markers, escape HTML."""
    s = str(text if text is not None else "")
    s = re.sub(r"[*`#]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return esc(s)


def link(label: object, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>'


def bold(text: object) -> str:
    return f"<b>{esc(text)}</b>"


def truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def chunk_parts(parts: list[str], limit: int = 3800) -> list[str]:
    """Pack paragraphs into messages no longer than `limit` chars (never splitting inside a paragraph
    unless a single paragraph is itself too long)."""
    messages: list[str] = []
    current = ""
    for part in parts:
        pieces = [part] if len(part) <= limit else _split_long(part, limit)
        for piece in pieces:
            if current and len(current) + 2 + len(piece) > limit:
                messages.append(current)
                current = piece
            else:
                current = f"{current}\n\n{piece}" if current else piece
    if current:
        messages.append(current)
    return messages


def _hard_split(line: str, limit: int) -> list[str]:
    """Split one over-long line at word boundaries (falls back to a raw cut if there is no space)."""
    out = []
    while len(line) > limit:
        cut = line.rfind(" ", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    if line:
        out.append(line)
    return out


def _split_long(text: str, limit: int) -> list[str]:
    out, cur = [], ""
    lines = []
    for line in text.split("\n"):
        lines.extend(_hard_split(line, limit) if len(line) > limit else [line])
    for line in lines:
        if len(cur) + 1 + len(line) > limit and cur:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))
