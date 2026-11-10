"""Persistent run state (JSON). Stored on a separate git branch when running in GitHub Actions."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SEEN_TTL_DAYS = 10
FLAG_TTL_DAYS = 45
THESIS_KEEP_DAYS = 60


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"seen": {}, "thesis": {}, "report_est": {}, "flags": {}, "runs": []}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass  # corrupt state is not worth failing the digest over
        for key in ("seen", "thesis", "report_est", "flags"):
            self.data.setdefault(key, {})
        self.data.setdefault("runs", [])
        self._pending_seen: dict[str, str] = {}

    # ---- seen items -------------------------------------------------
    def is_seen(self, item_id: str) -> bool:
        return item_id in self.data["seen"]

    def mark_seen(self, ids) -> None:
        today = date.today().isoformat()
        for i in ids:
            self._pending_seen[i] = today

    # ---- flags (e.g. PN17/GN3 names seen recently) -------------------
    def set_flag(self, code: str, kind: str, name: str, title: str, when: str) -> None:
        self.data["flags"][f"{kind}:{code}"] = {"kind": kind, "code": code, "name": name, "title": title, "last_seen": when}

    def flags(self, kind: str, within_days: int = 30) -> list[dict]:
        cutoff = (date.today() - timedelta(days=within_days)).isoformat()
        rows = [f for f in self.data["flags"].values() if f["kind"] == kind and f["last_seen"] >= cutoff]
        return sorted(rows, key=lambda r: r["last_seen"], reverse=True)

    # ---- thesis history ----------------------------------------------
    def record_thesis(self, thesis_id: str, day: str, record: dict) -> None:
        self.data["thesis"].setdefault(thesis_id, {})[day] = record

    def thesis_history(self, thesis_id: str, days: int = 7) -> list[tuple[str, dict]]:
        hist = self.data["thesis"].get(thesis_id, {})
        return sorted(hist.items())[-days:]

    # ---- persistence ----------------------------------------------------
    def commit(self, run_summary: dict | None = None) -> None:
        self.data["seen"].update(self._pending_seen)
        self._pending_seen = {}
        cutoff = (date.today() - timedelta(days=SEEN_TTL_DAYS)).isoformat()
        self.data["seen"] = {k: v for k, v in self.data["seen"].items() if v >= cutoff}
        fcut = (date.today() - timedelta(days=FLAG_TTL_DAYS)).isoformat()
        self.data["flags"] = {k: v for k, v in self.data["flags"].items() if v["last_seen"] >= fcut}
        tcut = (date.today() - timedelta(days=THESIS_KEEP_DAYS)).isoformat()
        for tid, hist in self.data["thesis"].items():
            self.data["thesis"][tid] = {d: r for d, r in hist.items() if d >= tcut}
        if run_summary:
            now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.data["runs"] = (self.data["runs"] + [{"at": now_utc, **run_summary}])[-30:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
