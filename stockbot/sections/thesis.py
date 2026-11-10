"""Section 5: score the day's news against the investor's theses (supports / weakens / unchanged).

The AI classifies each headline; the NET SCORE and the final verdict are computed in code so the arithmetic is
transparent and repeatable. The AI also writes the strongest counter-argument to guard against confirmation bias.
"""
from __future__ import annotations

import logging

from ..context import Context, load_theses
from ..fmt import bold, clean, esc, link, truncate
from ..models import Item, SectionResult
from .common import ai_unavailable_note, kv, lines

log = logging.getLogger("stockbot.s5")

SYSTEM = """Task: act as a sceptical research assistant. The user holds one or more investment THESES. Score each of today's
news items against each thesis it is relevant to, and answer for each thesis: does today's news SUPPORT it, WEAKEN it,
or leave it UNCHANGED?
Scoring rules: verdict 'supports' only if the item is real evidence FOR the thesis (not just on-topic); 'weakens' only if it
is real evidence AGAINST; otherwise skip the item (do not list neutral items). strength: 1 = weak/indirect, 2 = clear,
3 = strong and direct. Judge the item's content, not whether you personally agree with the thesis.
Then write the STRONGEST honest counter-argument to the thesis using today's items where possible (steelman it), so the
user is not only shown confirming evidence. Finally give the Malaysian angle in plain words.
JSON schema:
{"theses": [{"thesis_id": "id from input",
   "scores": [{"id": "item id", "verdict": "supports|weakens", "strength": 1, "reason": "max 20 words"}],
   "summary": "2 sentences, plain words: what today's news says about the thesis",
   "counterargument": "the strongest case AGAINST the thesis today, 2-3 sentences",
   "malaysia_angle": "what it means for Bursa/Malaysian stocks in plain words, 1-2 sentences",
   "watch_next": ["up to 3 specific things to watch"]}]}"""

VERDICT_EMOJI = {"SUPPORTS": "⬆️", "WEAKENS": "⬇️", "UNCHANGED": "➖", "MIXED": "↔️"}
# arrows describe what happened to YOUR thesis (stronger / weaker), independent of whether it is bullish or bearish


def _net(scores: list[dict], valid_ids: set[str]) -> tuple[int, int, int, list[dict]]:
    pos = neg = 0
    clean_scores = []
    for s in scores:
        if str(s.get("id")) not in valid_ids:
            continue
        verdict = str(s.get("verdict", "")).lower()
        try:
            strength = max(1, min(3, int(s.get("strength", 1))))
        except (TypeError, ValueError):
            strength = 1
        if verdict == "supports":
            pos += strength
        elif verdict == "weakens":
            neg += strength
        else:
            continue
        clean_scores.append({**s, "verdict": verdict, "strength": strength})
    return pos - neg, pos, neg, clean_scores


def _verdict(net: int, pos: int, neg: int) -> str:
    if net >= 2:
        return "SUPPORTS"
    if net <= -2:
        return "WEAKENS"
    if pos >= 2 and neg >= 2:
        return "MIXED"
    return "UNCHANGED"


def run(ctx: Context) -> SectionResult:
    title = "🧭 <b>5. Thesis check</b>"
    theses = load_theses()
    if not theses:
        return SectionResult("thesis", title, [title + "\n\nNo theses configured (see config/theses.yaml)."])

    pool: dict[str, Item] = {}
    for it in ctx.global_items + [i for i in ctx.company_items if i.category != "INSIDER"] + ctx.gov_items:
        pool.setdefault(it.id, it)
    items = list(pool.values())[: ctx.cfg("limits", "thesis_items", default=60)]
    if not items:
        return SectionResult("thesis", title, [title + "\n\nNo new items today to score."])

    t_block = "\n\n".join(
        f"THESIS {t['id']}: {t['title']}\nStatement: {' '.join(str(t.get('statement', '')).split())}\n"
        f"Supporting evidence looks like: {'; '.join(t.get('supports_if', []))}\n"
        f"Weakening evidence looks like: {'; '.join(t.get('weakens_if', []))}\n"
        f"Malaysian channels: {'; '.join(t.get('malaysia_channels', []))}" for t in theses)
    i_block = "\n".join(f"[{i.id}] {truncate(i.title, 170)} ({i.source})" for i in items)
    data = ctx.llm_json(SYSTEM, f"THESES:\n{t_block}\n\nTODAY'S ITEMS:\n{i_block}", tier="smart", max_tokens=4500)

    parts = [title]
    if not data or not isinstance(data.get("theses"), list):
        parts.append(ai_unavailable_note(ctx))
        parts.append("Thesis scoring needs the AI, so it is skipped today.")
        return SectionResult("thesis", title, parts, ok=False, error="AI unavailable")

    lookup = {i.id: i for i in items}
    by_id = {str(t.get("thesis_id")): t for t in data["theses"] if isinstance(t, dict)}
    for t in theses:
        r = by_id.get(t["id"])
        if not r:
            parts.append(f"<b>{esc(t['title'])}</b>: no result returned by the AI today.")
            continue
        net, pos, neg, scores = _net(r.get("scores") or [], set(lookup))
        verdict = _verdict(net, pos, neg)
        ctx.state.record_thesis(t["id"], ctx.today.isoformat(), {"net": net, "pos": pos, "neg": neg, "verdict": verdict})
        hist = ctx.state.thesis_history(t["id"], 7)
        trend = " ".join(f"{h['net']:+d}" for _, h in hist) if len(hist) > 1 else ""

        def evidence(kind: str) -> str:
            rows = sorted([s for s in scores if s["verdict"] == kind], key=lambda s: -s["strength"])[:2]
            out = []
            for s in rows:
                it = lookup[str(s["id"])]
                out.append(f"• {link(truncate(it.title, 90), it.url) if it.url else esc(truncate(it.title, 90))} "
                           f"<i>({'+' if kind == 'supports' else '−'}{s['strength']}: {clean(s.get('reason'))})</i>")
            return "\n".join(out)

        meaning = {"SUPPORTS": "today's news strengthens your thesis", "WEAKENS": "today's news weakens your thesis",
                   "UNCHANGED": "no real change to your thesis", "MIXED": "strong evidence on both sides"}[verdict]
        head = (f"{VERDICT_EMOJI[verdict]} <b>{esc(t['title'])}</b>\nToday: <b>{verdict}</b> ({meaning}). "
                f"Net score {net:+d} ({pos} for, {neg} against)")
        if trend:
            head += f"\nLast {len(hist)} days (net score): {esc(trend)}"
        parts.append(lines(
            head,
            kv("Summary", r.get("summary")),
            ("<b>Evidence for:</b>\n" + evidence("supports")) if any(s["verdict"] == "supports" for s in scores) else "",
            ("<b>Evidence against:</b>\n" + evidence("weakens")) if any(s["verdict"] == "weakens" for s in scores) else "",
            f"🔁 <b>Strongest counter-argument:</b> {clean(r.get('counterargument'))}" if r.get("counterargument") else "",
            f"🇲🇾 <b>Malaysia angle:</b> {clean(r.get('malaysia_angle'))}" if r.get("malaysia_angle") else "",
            ("👀 <b>Watch next:</b> " + "; ".join(clean(w) for w in (r.get("watch_next") or [])[:3])) if r.get("watch_next") else "",
        ))
        if verdict in ("SUPPORTS", "WEAKENS"):
            for s in sorted(scores, key=lambda s: -s["strength"])[:5]:
                it = lookup[str(s["id"])]
                if it.code:
                    ctx.add_focus(it.code, f"Thesis '{t['title'][:30]}': {verdict.lower()}", 2)
    parts.append("<i>⬆️ = today's news strengthens your thesis, ⬇️ = weakens it, ➖ = no real change. "
                 "A thesis is a hypothesis, not a forecast; the counter-argument is there to keep you honest.</i>")
    return SectionResult("thesis", title, parts)
