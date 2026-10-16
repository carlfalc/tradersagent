"""RON's Pattern Engine — the live, learning pattern feed.

The missing piece: RON already DETECTS chart patterns each candle (via PatternAgent) but used
to throw them away. This records every pattern it spots — timestamped, session-tagged, priced —
then GRADES how each one played out a while later, and AGGREGATES hit-rate by pattern × session.
That turns a sterile read into a living feed the user can watch and learn from: "double bottoms
in London pay 7/10", etc. It sharpens honestly as more events record. Demo / not advice.

Stored at results/patterns.jsonl so it survives restarts and keeps growing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .ronagents import session_of

# patterns we treat as directional + which way they imply price should go
BULLISH = {"breakout up", "double bottom", "bull flag", "possible inverse H&S"}
BEARISH = {"breakout down", "double top", "bear flag", "possible head & shoulders"}

GRADE_AFTER = float(os.getenv("PATTERN_GRADE_AFTER", "1800") or 1800)  # grade a pattern 30 min on
MIN_GAP = float(os.getenv("PATTERN_MIN_GAP", "120") or 120)          # don't re-log same pattern within this
KEEP = 600                                                            # bounded history


def _dir(pattern: str) -> int:
    if pattern in BULLISH:
        return 1
    if pattern in BEARISH:
        return -1
    return 0


class PatternRecorder:
    def __init__(self, root: Path, fname: str = "patterns.jsonl"):
        self.path = Path(root) / fname
        self.events: list[dict] = []
        self._last: dict[str, tuple[str, float]] = {}  # sym -> (pattern, t) to de-dupe
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                self.events = [json.loads(ln) for ln in f if ln.strip()][-KEEP:]
            for e in self.events:
                self._last[e["symbol"]] = (e["pattern"], e["t"])
        except Exception:
            self.events = []

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                for e in self.events[-KEEP:]:
                    f.write(json.dumps(e) + "\n")
            os.replace(tmp, self.path)
        except Exception:
            pass

    def observe(self, symbol: str, name: str, pattern: str | None, structure: str,
                note: str, price: float, now: float | None = None) -> bool:
        """Record a fresh directional pattern if it's new for this symbol. Returns True if logged."""
        now = now or time.time()
        d = _dir(pattern or "")
        if not pattern or d == 0 or not price:
            return False
        last = self._last.get(symbol)
        if last and last[0] == pattern and (now - last[1]) < max(MIN_GAP, GRADE_AFTER):
            return False  # same pattern still standing — don't spam the feed
        ev = {"t": now, "symbol": symbol, "name": name, "pattern": pattern,
              "structure": structure, "note": note, "dir": d,
              "session": session_of(now), "price": round(float(price), 2),
              "status": "open", "pts": None}
        self.events.append(ev)
        del self.events[:-KEEP]
        self._last[symbol] = (pattern, now)
        self._save()
        return True

    def grade(self, symbol: str, price: float, now: float | None = None) -> None:
        """Grade any open pattern older than GRADE_AFTER against where price went."""
        now = now or time.time()
        if not price:
            return
        changed = False
        for e in self.events:
            if e["symbol"] == symbol and e["status"] == "open" and (now - e["t"]) >= GRADE_AFTER:
                pts = (price - e["price"]) * e["dir"]  # + if price moved the way the pattern implied
                e["pts"] = round(pts, 2)
                e["status"] = "win" if pts > 0 else "loss"
                changed = True
        if changed:
            self._save()

    def summary(self, now: float | None = None) -> dict:
        now = now or time.time()
        feed = [{
            "t": e["t"], "ago_s": int(now - e["t"]), "session": e["session"],
            "name": e["name"], "pattern": e["pattern"], "dir": e["dir"],
            "price": e["price"], "note": e["note"], "status": e["status"], "pts": e["pts"],
        } for e in sorted(self.events, key=lambda x: x["t"], reverse=True)[:40]]
        # patterns that pay: hit-rate by pattern, and best session per pattern
        graded = [e for e in self.events if e["status"] in ("win", "loss")]
        by_pat: dict[str, dict] = {}
        for e in graded:
            p = by_pat.setdefault(e["pattern"], {"n": 0, "wins": 0, "sessions": {}})
            p["n"] += 1
            p["wins"] += 1 if e["status"] == "win" else 0
            s = p["sessions"].setdefault(e["session"], {"n": 0, "wins": 0})
            s["n"] += 1
            s["wins"] += 1 if e["status"] == "win" else 0
        pays = []
        for pat, p in by_pat.items():
            best_sess, best = None, -1.0
            for sess, s in p["sessions"].items():
                if s["n"] >= 2:
                    wr = s["wins"] / s["n"]
                    if wr > best:
                        best, best_sess = wr, sess
            pays.append({"pattern": pat, "n": p["n"], "wins": p["wins"],
                         "win_rate": round(p["wins"] / p["n"], 2) if p["n"] else 0.0,
                         "best_session": best_sess,
                         "best_session_hits": (f"{p['sessions'][best_sess]['wins']}/{p['sessions'][best_sess]['n']}"
                                               if best_sess else None)})
        pays.sort(key=lambda r: (r["n"], r["win_rate"]), reverse=True)
        open_n = sum(1 for e in self.events if e["status"] == "open")
        return {"feed": feed, "pays": pays, "recorded": len(self.events),
                "graded": len(graded), "open": open_n, "as_of": now}
