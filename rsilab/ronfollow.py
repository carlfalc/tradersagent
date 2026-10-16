"""RON's Trend-Learning engine — makes the Trend agent actually learn.

The Trend agent used to just read 15m/30m/1h alignment. This turns it into a learner:

  1. Every time the trend FLIPS into alignment (up or down), that onset is recorded — with the
     EXACT time, the session, the NZT hour, the weekday, and the relative VOLUME at that moment.
  2. A while later each onset is GRADED: did price actually follow through, or was it a fakeout?
  3. Those graded onsets aggregate into reliability by SESSION, by NZT HOUR, by WEEKDAY and by
     VOLUME regime — so RON learns *when* real trends form and whether a volume surge (the closest
     honest proxy for institutional participation) confirms them.

The result feeds the dashboard ("up since 3:58am NZT · this window follows through 72% · on 1.8×
volume") and the fusion weight. Times are the real candle timestamps — accurate, never rounded.

Stored at results/trend_events.jsonl so it survives restarts and keeps sharpening. Demo only.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .ronagents import session_of

GRADE_AFTER = float(os.getenv("TREND_GRADE_AFTER", "1800") or 1800)  # grade 30 min after onset
KEEP = 800
NZT_OFFSET_H = int(os.getenv("NZT_OFFSET_H", "13"))
VOL_SURGE = float(os.getenv("TREND_VOL_SURGE", "1.5") or 1.5)  # rel-vol >= this = a surge
_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _dir_of(state: str) -> str | None:
    s = (state or "").lower()
    if "up" in s:
        return "up"
    if "down" in s:
        return "down"
    return None


def _nzt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=NZT_OFFSET_H)


def _hour_band(h: int) -> str:
    return f"{h:02d}:00–{(h + 1) % 24:02d}:00"


class TrendRecorder:
    def __init__(self, root: Path, fname: str = "trend_events.jsonl"):
        self.path = Path(root) / fname
        self.events: list[dict] = []
        self.cur: dict[str, dict] = {}   # sym -> {dir, since, rel_vol, price}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                self.events = [json.loads(ln) for ln in f if ln.strip()][-KEEP:]
            for e in self.events:
                if e.get("status") == "open" or e["symbol"] not in self.cur:
                    self.cur[e["symbol"]] = {"dir": e["dir"], "since": e["t"],
                                             "rel_vol": e.get("rel_vol"), "price": e.get("price")}
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

    def observe(self, symbol: str, state: str, price: float | None,
                rel_vol: float | None = None, now: float | None = None) -> bool:
        """Record a trend ONSET when alignment flips. Returns True if a new onset was logged."""
        now = now or time.time()
        d = _dir_of(state)
        prev = self.cur.get(symbol)
        prev_dir = prev["dir"] if prev else None
        if d is None:
            # trend dissolved into mixed/range — close the current trend read (keep for history)
            if prev_dir in ("up", "down"):
                self.cur[symbol] = {"dir": None, "since": now, "rel_vol": rel_vol, "price": price}
            return False
        if d == prev_dir:
            return False  # same trend still standing
        # a genuine onset (or a flip): log it with exact time + context
        dt = _nzt(now)
        ev = {"t": now, "symbol": symbol, "dir": d, "price": round(float(price), 2) if price else None,
              "session": session_of(now), "nzt_hour": dt.hour, "weekday": dt.weekday(),
              "rel_vol": round(float(rel_vol), 2) if rel_vol else None,
              "status": "open", "pts": None, "prev_dir": prev_dir}
        self.events.append(ev)
        del self.events[:-KEEP]
        self.cur[symbol] = {"dir": d, "since": now, "rel_vol": rel_vol, "price": price}
        self._save()
        return True

    def grade(self, symbol: str, price: float | None, now: float | None = None) -> None:
        now = now or time.time()
        if not price:
            return
        changed = False
        for e in self.events:
            if (e["symbol"] == symbol and e["status"] == "open" and e.get("price")
                    and (now - e["t"]) >= GRADE_AFTER):
                dirv = 1 if e["dir"] == "up" else -1
                pts = (price - e["price"]) * dirv   # + if price followed the trend
                e["pts"] = round(pts, 2)
                e["status"] = "followed" if pts > 0 else "faded"
                changed = True
        if changed:
            self._save()

    @staticmethod
    def _rate(rows: list[dict]) -> dict:
        n = len(rows)
        w = sum(1 for e in rows if e["status"] == "followed")
        return {"n": n, "followed": w, "rate": round(100 * w / n) if n else None}

    def summary(self, symbol: str | None = None, now: float | None = None) -> dict:
        now = now or time.time()
        graded = [e for e in self.events if e["status"] in ("followed", "faded")
                  and (symbol is None or e["symbol"] == symbol)]

        def by(keyfn):
            out: dict = {}
            for e in graded:
                out.setdefault(keyfn(e), []).append(e)
            return {k: self._rate(v) for k, v in out.items()}

        by_session = by(lambda e: e["session"])
        by_hour = by(lambda e: e["nzt_hour"])
        by_weekday = by(lambda e: _DOW[e["weekday"]])
        hi = [e for e in graded if (e.get("rel_vol") or 0) >= VOL_SURGE]
        lo = [e for e in graded if (e.get("rel_vol") or 0) < VOL_SURGE]
        by_volume = {"surge": self._rate(hi), "quiet": self._rate(lo)}

        # current trend read for this symbol
        cur = self.cur.get(symbol) if symbol else None
        current = None
        if cur and cur.get("dir"):
            dt = _nzt(cur["since"])
            sess = session_of(cur["since"])
            # quality = reliability of this session + this volume regime, blended with overall
            sess_rate = (by_session.get(sess) or {}).get("rate")
            hour_rate = (by_hour.get(dt.hour) or {}).get("rate")
            relv = cur.get("rel_vol")
            vol_rate = (by_volume["surge"] if (relv or 0) >= VOL_SURGE else by_volume["quiet"]).get("rate")
            parts = [r for r in (sess_rate, hour_rate, vol_rate) if r is not None]
            quality = round(sum(parts) / len(parts)) if parts else None
            current = {
                "dir": cur["dir"], "since": cur["since"],
                "since_nzt": dt.strftime("%-I:%M %p") if hasattr(dt, "strftime") else None,
                "duration_min": int((now - cur["since"]) / 60),
                "session": sess, "nzt_hour": dt.hour, "hour_band": _hour_band(dt.hour),
                "rel_vol": relv, "vol_surge": (relv or 0) >= VOL_SURGE,
                "quality": quality, "session_rate": sess_rate, "hour_rate": hour_rate, "vol_rate": vol_rate,
            }

        # "best windows" — sessions & hours ranked by follow-through (min sample)
        best_sessions = sorted(
            [{"session": k, **v} for k, v in by_session.items() if v["n"] >= 3],
            key=lambda r: (r["rate"] or 0, r["n"]), reverse=True)
        best_hours = sorted(
            [{"hour": k, "band": _hour_band(k), **v} for k, v in by_hour.items() if v["n"] >= 2],
            key=lambda r: (r["rate"] or 0, r["n"]), reverse=True)[:6]

        return {"current": current, "by_session": by_session, "by_weekday": by_weekday,
                "by_volume": by_volume, "best_sessions": best_sessions, "best_hours": best_hours,
                "graded": len(graded), "as_of": now}
