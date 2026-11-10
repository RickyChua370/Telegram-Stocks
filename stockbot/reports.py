"""Estimate when a company will next announce its quarterly results.

Method (transparent, not magic): Bursa companies are creatures of habit.
1. Pull the company's past "Quarterly rpt ... period ended dd/mm/yyyy" filings.
2. The next period ends 3 months after the latest one.
3. Expected announcement date = the date they announced the SAME quarter last year (moved to a weekday);
   if that history is missing, latest announcement + 91 days.
4. Hard ceiling = Bursa's deadline: 2 months after the quarter end.

Estimates are cached in state for ~20 days. They are estimates; the Calendar section labels them as such.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .sources import bursa
from .state import State

log = logging.getLogger("stockbot.reports")

_PERIOD = re.compile(r"ended\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.I)


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    m += 1
    import calendar
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _eom(d: date) -> date:
    import calendar
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _weekday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


@dataclass
class ReportEstimate:
    code: str
    company: str
    period_end: date
    expected: date
    deadline: date
    basis: str            # "same quarter last year" | "last report + 91 days"
    last_announced: date | None = None

    def to_dict(self) -> dict:
        return {"code": self.code, "company": self.company, "period_end": self.period_end.isoformat(),
                "expected": self.expected.isoformat(), "deadline": self.deadline.isoformat(), "basis": self.basis,
                "last_announced": self.last_announced.isoformat() if self.last_announced else None}

    @classmethod
    def from_dict(cls, d: dict) -> "ReportEstimate":
        return cls(d["code"], d["company"], date.fromisoformat(d["period_end"]), date.fromisoformat(d["expected"]),
                   date.fromisoformat(d["deadline"]), d["basis"],
                   date.fromisoformat(d["last_announced"]) if d.get("last_announced") else None)


def estimate_next_report(code: str, today: date) -> ReportEstimate | None:
    since = datetime.now(bursa.MYT) - timedelta(days=420)
    filings = bursa.fetch_announcements(bursa.CAT_RESULTS, code=code, since=since, max_pages=1, sleep=0.0)
    history: list[tuple[date, date]] = []         # (period_end, announced)
    for f in filings:
        m = _PERIOD.search(f.title)
        if not m or not f.published:
            continue
        try:
            history.append((date(int(m.group(3)), int(m.group(2)), int(m.group(1))), f.published.date()))
        except ValueError:
            continue
    if not history:
        return None
    history.sort()
    last_period, last_ann = history[-1]
    company = filings[0].company if filings else code

    nxt = _eom(_add_months(last_period, 3))
    # roll forward if that quarter was already reported in a later filing we did not see (defensive)
    while nxt + timedelta(days=62) < today - timedelta(days=5):
        nxt = _eom(_add_months(nxt, 3))
    deadline = _eom(_add_months(nxt, 2)) if nxt.day >= 28 else _add_months(nxt, 2)

    basis, expected = "last report + 91 days", last_ann + timedelta(days=91)
    for p_end, ann in history:
        if abs((p_end - _add_months(nxt, -12)).days) <= 10:
            expected = _weekday(ann.replace(year=ann.year + 1)) if not (ann.month == 2 and ann.day == 29) \
                else _weekday(ann + timedelta(days=365))
            basis = "same quarter last year"
            break
    expected = min(expected, deadline)
    return ReportEstimate(code, company, nxt, expected, deadline, basis, last_ann)


def upcoming_reports(codes: list[str], state: State, today: date, window_days: int = 7,
                     max_codes: int = 60) -> list[ReportEstimate]:
    """Estimated reporters inside [today, today + window_days]. Uses/updates the state cache."""
    out: list[ReportEstimate] = []
    cache = state.data["report_est"]
    horizon = today + timedelta(days=window_days)
    for code in list(dict.fromkeys(codes))[:max_codes]:
        entry = cache.get(code)
        est: ReportEstimate | None = None
        if entry and (today - date.fromisoformat(entry["computed"])).days <= 20:
            est = ReportEstimate.from_dict(entry["est"]) if entry.get("est") else None
            # a cached estimate whose date has passed by >3 days is stale -> recompute
            if est and est.expected < today - timedelta(days=3):
                est, entry = None, None
        else:
            entry = None
        if entry is None:
            try:
                est = estimate_next_report(code, today)
            except Exception as exc:  # noqa: BLE001
                log.warning("report estimate failed for %s: %s", code, exc)
                est = None
            cache[code] = {"computed": today.isoformat(), "est": est.to_dict() if est else None}
        if est and today <= est.expected <= horizon:
            out.append(est)
    out.sort(key=lambda e: e.expected)
    return out
