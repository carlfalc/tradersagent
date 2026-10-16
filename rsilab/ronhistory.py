"""Persistent candle history for RON's statistical-edge engine.

Appends closed candles per instrument to results/history/<SYM>.jsonl so the stats have a
growing dataset to learn from. Dedups by timestamp and only ever appends strictly-newer
candles, so it survives restarts and never double-counts. The longer the bot runs, the more
history accrues and the smarter the edge engine gets — this file is that memory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class HistoryStore:
    def __init__(self, root: Path):
        self.dir = Path(root) / "history"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._last_t: dict[str, str] = {}

    def _path(self, sym: str) -> Path:
        return self.dir / f"{sym}.jsonl"

    def _tail_time(self, sym: str) -> str:
        last = ""
        try:
            with open(self._path(sym)) as f:
                for line in f:
                    if line.strip():
                        last = line
            if last:
                return str(json.loads(last).get("t") or "")
        except Exception:
            pass
        return ""

    def record(self, sym: str, candle) -> bool:
        """Append one closed candle (object with .t/.o/.h/.l/.c) if strictly newer. True if written."""
        t = str(getattr(candle, "t", "") or "")
        if not t:
            return False
        last = self._last_t.get(sym)
        if last is None:
            last = self._tail_time(sym)
            self._last_t[sym] = last
        if t <= last:
            return False
        try:
            with open(self._path(sym), "a") as f:
                f.write(json.dumps({"t": t, "o": candle.o, "h": candle.h,
                                    "l": candle.l, "c": candle.c}) + "\n")
            self._last_t[sym] = t
            return True
        except Exception:
            return False

    def load(self, sym: str, limit: int | None = None) -> list[dict]:
        try:
            with open(self._path(sym)) as f:
                rows = [json.loads(ln) for ln in f if ln.strip()]
        except FileNotFoundError:
            return []
        except Exception:
            return []
        return rows[-limit:] if limit else rows

    def bulk_merge(self, sym: str, candles) -> int:
        """Merge a batch of candles (Candle objects or dicts) into history: dedup by
        timestamp, sort oldest-first, rewrite the file atomically. Returns total rows."""
        merged = {r["t"]: r for r in self.load(sym)}
        for c in candles:
            if isinstance(c, dict):
                t = str(c.get("t") or "")
                row = {"t": t, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]}
            else:
                t = str(getattr(c, "t", "") or "")
                row = {"t": t, "o": c.o, "h": c.h, "l": c.l, "c": c.c}
            if t:
                merged[t] = row
        rows = sorted(merged.values(), key=lambda r: r["t"])
        try:
            tmp = self._path(sym).with_suffix(".tmp")
            with open(tmp, "w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            os.replace(tmp, self._path(sym))
            self._last_t[sym] = rows[-1]["t"] if rows else ""
        except Exception:
            pass
        return len(rows)

    def count(self, sym: str) -> int:
        try:
            with open(self._path(sym)) as f:
                return sum(1 for ln in f if ln.strip())
        except Exception:
            return 0
