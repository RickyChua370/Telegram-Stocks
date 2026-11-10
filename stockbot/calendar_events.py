"""Forward calendar: curated events (config/calendar.yaml) plus generated recurring events."""
from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, timedelta

from .config import load_yaml


@dataclass
class Event:
    key: str
    date: date
    end_date: date | None
    title: str
    category: str
    importance: int
    note: str = ""
    source: str = ""
    approx: bool = False

    @property
    def last_day(self) -> date:
        return self.end_date or self.date

    def days_from(self, today: date) -> int:
        return (self.date - today).days


def _to_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _next_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _month_iter(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def _recurring(rule: dict, start: date, end: date) -> list[Event]:
    out: list[Event] = []
    kind = rule["rule"]
    base = dict(title=rule["title"], category=rule.get("category", "Other"), importance=int(rule.get("importance", 1)),
                note=rule.get("note", ""), source=rule.get("source", ""), approx=bool(rule.get("approx", False)))
    if kind == "weekly":
        d = start
        while d <= end:
            if d.weekday() == int(rule["weekday"]):
                out.append(Event(f"{rule['title']}@{d}", d, None, **base))
            d += timedelta(days=1)
    elif kind in ("monthly_day", "pboc_lpr"):
        day = 20 if kind == "pboc_lpr" else int(rule["day"])
        for y, m in _month_iter(start, end):
            d = _next_weekday(date(y, m, min(day, _cal.monthrange(y, m)[1])))
            if start <= d <= end:
                out.append(Event(f"{rule['title']}@{d}", d, None, **base))
    return out


def load_events(start: date, end: date) -> list[Event]:
    """Events that overlap [start, end]."""
    data = load_yaml("calendar.yaml")
    events: list[Event] = []
    for row in data.get("events") or []:
        d = _to_date(row["date"])
        e = _to_date(row["end_date"]) if row.get("end_date") else None
        ev = Event(key=f"{row['title']}@{d}", date=d, end_date=e if e and e != d else None, title=row["title"],
                   category=row.get("category", "Other"), importance=int(row.get("importance", 1)),
                   note=row.get("note", ""), source=row.get("source", ""), approx=bool(row.get("approx", False)))
        if ev.last_day >= start and ev.date <= end:
            events.append(ev)
    for rule in data.get("recurring") or []:
        events.extend(_recurring(rule, start, end))
    events.sort(key=lambda e: (e.date, -e.importance))
    return events


def upcoming(today: date, horizon_days: int = 21) -> list[Event]:
    return load_events(today, today + timedelta(days=horizon_days))
