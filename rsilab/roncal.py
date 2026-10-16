"""RON's calibration ledger — the honest path from 'not_calibrated' to calibrated.

Every trade RON drives is written down BEFORE its outcome is known (a decision line),
then graded when the position closes (an outcome line). From the graded history we
compute, per instrument:
  * n            — graded trades
  * win_rate     — realised hit-rate (this becomes RON's *calibrated* confidence)
  * brier        — calibration error of RON's raw confidence vs. what happened
  * calibrated   — win_rate once n >= MIN_N, else None (not enough evidence yet)

Lines are hash-chained (each carries the previous line's hash) so the ledger is
tamper-evident, exactly like the Jevmind concept. Stored at results/ron_ledger.jsonl.

Demo only. Calibration needs dozens of graded trades before it means anything.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

MIN_N = 15  # graded trades before we trust a calibrated number for an instrument


def _hash(prev: str, rec: dict) -> str:
    payload = prev + json.dumps(rec, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


class RonLedger:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.records: list[dict] = []
        self._prev = "0" * 12
        self._open: dict[str, str] = {}  # symbol -> open decision id awaiting grade
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    self.records.append(r)
                    self._prev = r.get("hash", self._prev)
        except FileNotFoundError:
            return
        except Exception:
            return
        # rebuild any ungraded opens
        graded = {r["ref"] for r in self.records if r.get("type") == "outcome"}
        for r in self.records:
            if r.get("type") == "decision" and r["id"] not in graded:
                self._open[r["symbol"]] = r["id"]

    def _append(self, rec: dict) -> dict:
        rec["prev"] = self._prev
        rec["hash"] = _hash(self._prev, rec)
        self._prev = rec["hash"]
        self.records.append(rec)
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            pass
        return rec

    def log_decision(self, symbol: str, direction: str, ron_state, as_of, raw_conf) -> str:
        """Record a RON-driven entry before its outcome is known. Returns the decision id."""
        with self.lock:
            did = f"{symbol}-{int(time.time()*1000)}"
            self._append({"type": "decision", "id": did, "t": time.time(), "symbol": symbol,
                          "direction": direction, "ron_state": ron_state, "as_of": as_of,
                          "raw_conf": raw_conf})
            self._open[symbol] = did
            return did

    def grade(self, symbol: str, pnl: float) -> None:
        """Grade the open RON decision for this symbol against the realised P&L."""
        with self.lock:
            did = self._open.pop(symbol, None)
            if not did:
                return
            self._append({"type": "outcome", "ref": did, "t": time.time(), "symbol": symbol,
                          "pnl": round(float(pnl or 0), 2), "win": 1 if (pnl or 0) > 0 else 0})

    def snapshot(self) -> dict:
        with self.lock:
            decs = {r["id"]: r for r in self.records if r.get("type") == "decision"}
            outs = [r for r in self.records if r.get("type") == "outcome"]
            per: dict[str, dict] = {}
            allg = {"n": 0, "wins": 0, "brier_sum": 0.0, "pnl": 0.0}
            for o in outs:
                d = decs.get(o.get("ref"))
                if not d:
                    continue
                sym = d["symbol"]
                p = per.setdefault(sym, {"n": 0, "wins": 0, "brier_sum": 0.0, "pnl": 0.0})
                win = int(o.get("win", 0))
                raw = d.get("raw_conf")
                for bucket in (p, allg):
                    bucket["n"] += 1
                    bucket["wins"] += win
                    bucket["pnl"] += float(o.get("pnl") or 0)
                    if raw is not None:
                        bucket["brier_sum"] += (float(raw) - win) ** 2
            def finish(b):
                n = b["n"]
                return {"n": n, "wins": b["wins"],
                        "win_rate": round(100 * b["wins"] / n, 1) if n else None,
                        "brier": round(b["brier_sum"] / n, 3) if n else None,
                        "pnl": round(b["pnl"], 2),
                        "calibrated": round(b["wins"] / n, 3) if n >= MIN_N else None}
            recent = []
            for o in outs[-30:][::-1]:
                d = decs.get(o.get("ref")) or {}
                recent.append({"t": o.get("t"), "symbol": o.get("symbol"),
                               "direction": d.get("direction"), "raw_conf": d.get("raw_conf"),
                               "pnl": o.get("pnl"), "win": o.get("win")})
            return {"overall": finish(allg), "per_symbol": {k: finish(v) for k, v in per.items()},
                    "recent": recent, "min_n": MIN_N, "open": dict(self._open)}

    def is_open(self, symbol: str) -> bool:
        with self.lock:
            return symbol in self._open

    def calibrated_conf(self, symbol: str) -> float | None:
        """RON's calibrated confidence for a symbol = realised win-rate once enough evidence."""
        snap = self.snapshot()
        s = snap["per_symbol"].get(symbol)
        return s.get("calibrated") if s else None
