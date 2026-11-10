"""Rule-based classification and scoring of Bursa filings and headlines.

Rules run BEFORE the AI so that (a) the AI only sees the most relevant items, which keeps free-tier usage
low, and (b) the important categories (UMA, take-overs, profit warnings, PN17/GN3) can never be missed
because a model skipped them.
"""
from __future__ import annotations

import re

from .config import load_yaml
from .models import Item

NOISE = re.compile(
    r"net asset value|indicative optimum|\biopv\b|\betf\b|index fund|bond index|tracker|"
    r"change in (the )?(company )?secretary|change in registered|notice of (book|books) closure|"
    r"total number of (issued )?shares|annual general meeting|notice of (annual|extraordinary)|"
    r"entitlements|^appointment of|^resignation of|change in boardroom|change of (auditor|name)",
    re.I,
)

# (tag, regex, base score).  A single item can carry several tags.
RULES: list[tuple[str, re.Pattern, int]] = [(t, re.compile(rx, re.I), s) for t, rx, s in [
    ("pn17_gn3", r"practice note\s*17|\bpn\s?17\b|guidance note\s*3|\bgn\s?3\b|prescribed criteria|regularis(e|ation) plan", 9),
    ("profit_warning", r"profit warning|earnings? deviation|variance (of|in) (profit|earnings)|deviat\w+ (of|from) (the )?(profit|earnings)|"
                       r"shortfall in (profit|earnings)|expects? to (record|report|post) a? ?(net )?loss|net loss for the", 9),
    ("distress", r"default|winding[- ]up|insolven|receiver|judicial management|suspension of trading|delist|cross[- ]default|"
                 r"unable to (pay|meet)|going concern", 8),
    ("takeover", r"take-?overs?|mandatory (general )?offer|general offer|\bmgo\b|privatis|offer to (acquire|buy)|"
                 r"(unconditional|conditional) (mandatory|voluntary)|exit offer", 9),
    ("disposal", r"disposal|dispose|divest|sale of (land|property|shares|business)", 4),
    ("acquisition", r"acquisition|acquire|subscription of (shares|equity)", 3),
    ("rpt", r"related part(y|ies)|recurrent related|\brrpt\b|interested (director|major shareholder)", 3),
    ("contract", r"letter of (award|acceptance|intent)|\bloa\b|\bloi\b|\bcontracts?\b|awarded|award of|tender|"
                 r"memorandum of (understanding|agreement)|\bmou\b|supply agreement|concession|framework agreement|"
                 r"work order|charter|secures?", 5),
    ("fundraising", r"private placement|rights issue|bonus issue|share split|subdivision of shares|"
                    r"convertible|redeemable|issuance of (new )?shares", 2),
    ("buyback", r"share buy-?back", 1),
]]

SECTOR_TAGS: list[tuple[str, re.Pattern]] = [(t, re.compile(rx, re.I)) for t, rx in [
    ("data_centre", r"data cent(re|er)|hyperscal|cloud|ai infrastructure|colocation"),
    ("energy_transition", r"solar|\blss\b|renewable|hydrogen|carbon capture|\bccus\b|\bbess\b|energy storage|\bnetr\b|\bcress\b"),
    ("oil_gas", r"fpso|offshore|petronas|petros|vessel|drilling|\bepcc?i?c?\b|hook-?up|\bosv\b|oil and gas|oil & gas|upstream|downstream"),
    ("construction", r"construction|infrastructure|piling|earthworks|building works|mrt|lrt|rail"),
]]


def _project_rules() -> list[tuple[str, re.Pattern]]:
    out = []
    for p in load_yaml("projects.yaml").get("projects") or []:
        kws = [re.escape(k) for k in p.get("keywords") or []]
        if kws:
            out.append((p["name"], re.compile("|".join(kws), re.I)))
    return out


_PROJECTS = None


def project_matches(text: str) -> list[str]:
    global _PROJECTS
    if _PROJECTS is None:
        _PROJECTS = _project_rules()
    return [name for name, rx in _PROJECTS if rx.search(text)]


def is_noise(item: Item) -> bool:
    return bool(NOISE.search(item.title))


def tag_item(item: Item, *, body: str = "") -> Item:
    """Fill item.tags and item.score from title (+ body excerpt when available)."""
    hay = f"{item.title} {body or item.text}"
    tags: list[str] = []
    score = 0
    if item.category in ("UMA", "UMCO"):
        tags.append("uma")
        score = max(score, 8)
    if item.category in ("TECO",):
        tags.append("takeover")
        score = max(score, 9)
    for tag, rx, base in RULES:
        if rx.search(hay):
            if tag not in tags:
                tags.append(tag)
            score = max(score, base)
    sector_hits = [t for t, rx in SECTOR_TAGS if rx.search(hay)]
    proj = project_matches(hay)
    if proj:
        tags.append("gov_project")
    tags.extend(sector_hits)
    if "contract" in tags and (set(sector_hits) & {"data_centre", "oil_gas", "energy_transition"} or proj):
        score += 2
    item.tags = list(dict.fromkeys(tags))
    item.score = score
    return item


TAG_LABEL = {
    "uma": "Unusual market activity", "takeover": "Take-over / offer", "profit_warning": "Profit warning",
    "pn17_gn3": "PN17 / GN3", "distress": "Financial distress", "disposal": "Disposal",
    "acquisition": "Acquisition", "rpt": "Related-party transaction", "contract": "Contract",
    "fundraising": "Fund-raising", "buyback": "Share buy-back", "data_centre": "Data centre",
    "energy_transition": "Energy transition", "oil_gas": "Oil & gas", "construction": "Construction",
    "gov_project": "Government project",
}
