"""Bursa Malaysia data via KLSE Screener (bursamalaysia.com blocks automated access).

What this gives us for free:
- the full list of listed companies (code <-> name)         -> `load_universe`
- Bursa announcements by category, with paging              -> `fetch_announcements`
- announcement body text                                    -> `fetch_announcement_text`
- recently announced quarterly results with YoY/QoQ growth  -> `fetch_recent_results`
- a market-wide table with price, volume, sector, market cap -> `load_market_table`

These are scraped HTML/JSON endpoints that can change without notice. Every function degrades to an
empty result on failure so one broken source never kills the daily digest. Personal use only; please
respect KLSE Screener's terms and keep request volume low (the code sleeps between pages).
"""
from __future__ import annotations

import html as htmllib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from bs4 import BeautifulSoup

from .. import http
from ..models import Item

log = logging.getLogger("stockbot.bursa")

BASE = "https://www.klsescreener.com"
MYT = ZoneInfo("Asia/Kuala_Lumpur")
AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01"}

# KLSE Screener category filter values (verified against the site's own dropdown).
CAT_SHAREHOLDING = "SH,CHSH"
CAT_TAKEOVER = "TECO"
CAT_UMA = "UMA,UMCO"
CAT_RESULTS = "FA,FRCO"
CAT_GENERAL = "GA,GACO"
CAT_SPECIAL = "SA,SACO"
CAT_QUERY = "RQ,RQCO"

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


# --------------------------------------------------------------------------- universe
@dataclass
class Universe:
    names: dict[str, str] = field(default_factory=dict)        # code -> short name (e.g. "1155" -> "MAYBANK")
    descs: dict[str, str] = field(default_factory=dict)        # code -> full name
    _by_norm: dict[str, str] = field(default_factory=dict)

    def build_index(self) -> None:
        self._by_norm = {}
        for code, desc in self.descs.items():
            self._by_norm.setdefault(_norm_company(desc), code)
            self._by_norm.setdefault(_norm_company(self.names.get(code, "")), code)

    def resolve(self, text: str) -> str | None:
        """Map a company name (full or colloquial) to a Bursa code, or None if not confident."""
        key = _norm_company(text)
        if not key:
            return None
        if key in self._by_norm:
            return self._by_norm[key]
        # "Gamuda Bhd wins ..." style: look for a known name as a whole-word prefix of the text
        words = key.split()
        for n in range(min(4, len(words)), 0, -1):
            cand = " ".join(words[:n])
            if len(cand) >= 4 and cand in self._by_norm:
                return self._by_norm[cand]
        # "YTL Power" -> "YTL POWER INTERNATIONAL": accept a unique company whose name starts with the text
        if len(key) >= 6:
            hits = {code for norm, code in self._by_norm.items() if norm.startswith(key + " ")}
            if len(hits) == 1:
                return hits.pop()
        return None

    def find_companies(self, text: str, limit: int = 3) -> list[str]:
        """Listed companies mentioned in free text (headlines). Conservative: ignores very generic names."""
        words = _norm_company(text).split()
        found: list[str] = []
        for n in (4, 3, 2, 1):
            for i in range(len(words) - n + 1):
                gram = " ".join(words[i:i + n])
                if gram in _GENERIC or (n == 1 and len(gram) < 5):
                    continue
                code = self._by_norm.get(gram)
                if code and code not in found:
                    found.append(code)
                    if len(found) >= limit:
                        return found
        return found

    def name(self, code: str) -> str:
        return self.names.get(code) or self.descs.get(code) or code


# Company-name words that are also ordinary news words: never treat them alone as a company mention.
_GENERIC = {
    "malaysia", "malaysian", "bank", "energy", "power", "capital", "land", "asia", "global", "green", "star", "media",
    "best", "great", "first", "united", "national", "world", "digital", "smart", "gold", "plus", "tech", "life",
    "property", "properties", "industries", "industrial", "resources", "international", "development", "oil", "gas",
    "ocean", "sun", "sky", "grand", "prime", "royal", "premium", "pacific", "east", "west", "north", "south",
    "central", "general", "advance", "alliance", "apex", "crest", "edge", "focus", "market", "tower",
}

_STOP = re.compile(r"\b(berhad|bhd|sdn|holdings?|group|corporation|corp|company|co|ltd|limited|plc|the)\b")


def _norm_company(text: str) -> str:
    t = htmllib.unescape(text or "").upper().replace("&", " AND ").lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = _STOP.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


# The site serves both minified ({code:'1',name:'A'}) and pretty-printed ({code: '1', name:'A'}) HTML.
_OPT = re.compile(r"\{\s*code:\s*'([^']+)'\s*,\s*name:\s*'((?:[^'\\]|\\.)*)'\s*,\s*description:\s*'((?:[^'\\]|\\.)*)'\s*\}")


def load_universe() -> Universe:
    uni = Universe()
    try:
        page = http.get(f"{BASE}/v2/announcements").text
    except Exception as exc:  # noqa: BLE001
        log.warning("Universe load failed: %s", exc)
        return uni
    for code, name, desc in _OPT.findall(page):
        clean = lambda s: htmllib.unescape(s.replace("\\'", "'")).strip()  # noqa: E731
        uni.names[code] = clean(name)
        uni.descs[code] = clean(desc)
    uni.build_index()
    if len(uni.names) < 500:
        log.warning("Universe looks incomplete (%d securities): KLSE Screener page layout may have changed; "
                    "stock-code matching will be degraded.", len(uni.names))
    else:
        log.info("Universe: %d listed securities", len(uni.names))
    return uni


# --------------------------------------------------------------------------- announcements
def _parse_when(day: str, mon: str, ago: str, now: datetime) -> datetime:
    month = _MONTHS.get(mon[:3].title(), now.month)
    year = now.year
    hh = mm = 0
    m = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?", ago or "", re.I)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if m.group(3):
            hh = hh % 12 + (12 if m.group(3).lower() == "pm" else 0)
    try:
        dt = datetime(year, month, int(day), hh, mm, tzinfo=MYT)
    except ValueError:
        return now
    if dt > now + timedelta(days=2):          # e.g. "28 Dec" seen in early January
        dt = dt.replace(year=year - 1)
    return dt


def _parse_cards(html: str, now: datetime) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Item] = []
    for a in soup.select("a.announcement-item"):
        href = a.get("href", "")
        ann_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not ann_id:
            continue
        def txt(sel: str) -> str:
            node = a.select_one(sel)
            return node.get_text(" ", strip=True) if node else ""
        tag = a.select_one(".category-tag")
        cat_cls = next((c[4:] for c in (tag.get("class") or []) if c.startswith("cat-")), "") if tag else ""
        out.append(Item(
            id=f"ann{ann_id}",
            title=txt(".title"),
            url=BASE + href if href.startswith("/") else href,
            source="Bursa filing",
            published=_parse_when(txt(".day") or "1", txt(".month") or "Jan", txt(".time-ago"), now),
            kind="filing",
            category=cat_cls,
            company=txt(".company-label"),
            text=txt(".snippet"),
        ))
    return out


def fetch_announcements(category: str, *, code: str | None = None, since: datetime | None = None,
                        max_pages: int = 5, sleep: float = 0.4) -> list[Item]:
    now = datetime.now(MYT)
    out: list[Item] = []
    for page in range(1, max_pages + 1):
        params = {"category": category, "page": page}
        if code:
            params["code"] = code
        try:
            data = http.get(f"{BASE}/v2/announcements", params=params, headers=AJAX).json()
        except Exception as exc:  # noqa: BLE001
            log.warning("Announcements %s p%d failed: %s", category, page, exc)
            break
        cards = _parse_cards(data.get("html", ""), now)
        if not cards:
            break
        out.extend(cards)
        if since and all(c.published and c.published < since for c in cards):
            break
        time.sleep(sleep)
    if since:
        out = [c for c in out if c.published is None or c.published >= since]
    seen: set[str] = set()
    return [c for c in out if not (c.id in seen or seen.add(c.id))]


def core_text(title: str, text: str) -> str:
    """Strip the repeated title and boilerplate header so only the announcement substance remains."""
    t = (text or "").strip()
    if title and t.startswith(title):
        t = t[len(title):].strip()
    if "Type Announcement Subject" in t and " Description " in t:
        t = t.split(" Description ", 1)[1].strip()
    return t


def fetch_announcement_text(url: str, max_chars: int = 1400) -> str:
    try:
        soup = BeautifulSoup(http.get(url).text, "html.parser")
    except Exception as exc:  # noqa: BLE001
        log.debug("Announcement text failed %s: %s", url, exc)
        return ""
    node = soup.select_one(".bursa-ann") or soup.select_one("#main")
    if not node:
        return ""
    text = " ".join(node.get_text(" ", strip=True).split())
    return text[:max_chars]


# --------------------------------------------------------------------------- shareholding notices
def parse_shareholding(title: str, text: str) -> dict | None:
    """Extract the key facts from a Changes in Director's / Substantial Shareholder's Interest notice.

    Returns None when the notice does not look like one we can parse. Values are what the filing says;
    nothing is estimated here.
    """
    if not text:
        return None
    is_director = "director" in title.lower()
    is_substantial = "s-hldr" in title.lower() or "substantial" in title.lower()
    if not (is_director or is_substantial):
        return None
    person = title.rsplit(" - ", 1)[-1].strip() if " - " in title else ""
    m = re.search(r"(\d[\d,]*)\s+(Acquired|Disposed|Subscri\w+|Transfer\w*|Allot\w+|Exercise\w*|Vested|Award\w*|Bonus|Others?)\b",
                  text, re.I)
    if not m:
        return None
    shares = float(m.group(1).replace(",", ""))
    kind = m.group(2).lower()
    side = "buy" if kind.startswith(("acquir", "subscri", "allot", "exercise", "vested", "award", "bonus")) else \
           "sell" if kind.startswith("dispos") else "other"
    cons = re.search(r"Consideration \(if any\)\s*RM\s*([\d,]+(?:\.\d+)?)", text, re.I)
    value = float(cons.group(1).replace(",", "")) if cons else None
    circ = re.search(r"Circumstances by reason of which change has occurred\s*(.*?)\s*Nature of interest", text, re.I)
    d_pct = re.search(r"Direct \(%\)\s*([\d.]+)", text)
    i_pct = re.search(r"Indirect/deemed interest \(%\)\s*([\d.]+)", text)
    total_pct = None
    if d_pct or i_pct:
        total_pct = round(float(d_pct.group(1)) if d_pct else 0.0, 3) + round(float(i_pct.group(1)) if i_pct else 0.0, 3)
    return {
        "role": "Director" if is_director else "Substantial shareholder",
        "person": person.title() if person.isupper() else person,
        "side": side,
        "action": kind,
        "shares": shares,
        "value_rm": value,
        "circumstance": (circ.group(1)[:90] if circ else ""),
        "stake_after_pct": total_pct,
    }


# --------------------------------------------------------------------------- results
def fetch_recent_results(max_rows: int = 150) -> list[dict]:
    """Recently announced quarterly results with growth figures (RM thousands as reported)."""
    try:
        soup = BeautifulSoup(http.get(f"{BASE}/v2/financial-reports", timeout=45).text, "html.parser")
    except Exception as exc:  # noqa: BLE001
        log.warning("Financial reports failed: %s", exc)
        return []
    table = soup.find("table")
    if not table:
        return []
    now = datetime.now(MYT)
    rows = []
    for tr in table.find_all("tr")[1:max_rows + 1]:
        tds = tr.find_all("td")
        if len(tds) < 10:
            continue
        a = tr.find("a", href=re.compile(r"/stocks/view/"))
        m = re.search(r"/stocks/view/([^/\"?]+)", a["href"]) if a else None
        day_mon = tds[0].get_text(" ", strip=True).split()
        if len(day_mon) < 2:
            continue
        rows.append({
            "announced": _parse_when(day_mon[0], day_mon[1], "", now),
            "name": tds[1].get_text(" ", strip=True),
            "code": m.group(1) if m else "",
            "quarter": tds[2].get_text(strip=True),
            "q_date": tds[3].get_text(strip=True),
            "revenue": _num(tds[4].get_text()),
            "net_profit": _num(tds[6].get_text()),
            "qoq_pct": _signed_pct(tds[7].get_text(" ", strip=True)),
            "yoy_pct": _signed_pct(tds[8].get_text(" ", strip=True)),
            "url": BASE + a["href"] if a else "",
        })
    return rows


def _num(s: str) -> float | None:
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _signed_pct(s: str) -> float | None:
    m = re.search(r"([\d.,]+)\s*%", s)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    return -val if "↓" in s or s.strip().startswith("-") else val


# --------------------------------------------------------------------------- market table
def load_market_table() -> pd.DataFrame:
    """All Main/ACE Market stocks with price, change %, volume, sector and market cap."""
    cols = ["code", "name", "sector", "market", "price", "change_pct", "volume", "mcap_m", "turnover"]
    try:
        soup = BeautifulSoup(http.get(f"{BASE}/v2/screener/quote_results", timeout=60).text, "html.parser")
    except Exception as exc:  # noqa: BLE001
        log.warning("Market table failed: %s", exc)
        return pd.DataFrame(columns=cols)
    table = soup.find("table")
    if not table:
        return pd.DataFrame(columns=cols)
    heads = [th.get_text(" ", strip=True) for th in table.find_all("th")]
    idx = {h: i for i, h in enumerate(heads)}
    need = ["Name", "Code", "Category", "Price", "Change%", "Volume", "MCap.(M)"]
    if not all(n in idx for n in need):
        log.warning("Market table layout changed: %s", heads)
        return pd.DataFrame(columns=cols)
    rows = []
    for tr in table.find_all("tr")[1:]:
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) <= max(idx[n] for n in need):
            continue
        code = tds[idx["Code"]]
        if not re.fullmatch(r"\d{4,5}", code):
            continue                                   # warrants, structured products
        cat = tds[idx["Category"]]
        market = "Main" if "Main Market" in cat else "ACE" if "ACE Market" in cat else "Other"
        sector = re.sub(r",?\s*(Main|ACE|Leap) Market.*$", "", cat).strip()
        price, vol = _num(tds[idx["Price"]]), _num(tds[idx["Volume"]])
        chg = _num(tds[idx["Change%"]].replace("%", ""))
        rows.append({
            "code": code, "name": re.sub(r"\s*\[.*?\]", "", tds[idx["Name"]]).strip(),
            "sector": sector, "market": market, "price": price, "change_pct": chg, "volume": vol,
            "mcap_m": _num(tds[idx["MCap.(M)"]]),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["turnover"] = df["price"].fillna(0) * df["volume"].fillna(0)
    return df[cols]
