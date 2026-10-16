"""RON's independent watcher — advisory only, always on.

Runs continuously on the live tick feed (~every 2s) for every instrument, whether or
not the bot is trading. It scores a conviction + direction from momentum, noise, trend,
RSI and (when present) RON's GAINEDGE read, and fires a high-conviction ALERT:
direction, entry, suggested stop, target, timestamp, and a plain-English reason.

Then it GRADES ITSELF: every alert is paper-tracked against price — target hit = win,
stop hit = loss, horizon expiry = graded by which way price actually moved. Graded calls
feed a hash-chained ledger, so RON's conviction is calibrated to its real hit-rate over
time. That is what makes it get smarter: the number is earned, never asserted, and
capped below 100% on purpose because certainty does not exist.

Advisory only — it never places a trade. Not financial advice.
"""

from __future__ import annotations

import math
import time


class Watcher:
    def __init__(self, engine, ledger, min_conv: float = 0.70,
                 cooldown_s: float = 150.0, horizon_s: float = 1800.0, alerts_path=None):
        self.e = engine
        self.sym = engine.symbol
        self.ledger = ledger
        self.min_conv = min_conv
        self.cooldown_s = cooldown_s
        self.horizon_s = horizon_s
        self.open: dict | None = None      # the live, ungraded alert
        self.last_fire = 0.0
        self.last_dir = None
        self.alerts_path = alerts_path     # if set, alerts persist here and reload on restart
        self.recent: list[dict] = []       # rolling recent alerts for the dashboard
        if alerts_path is not None:
            self._load_recent()

    def _load_recent(self) -> None:
        """Reload this symbol's recent alert history from disk so the dashboard survives restarts."""
        from .persist import load_jsonl
        recs = load_jsonl(self.alerts_path, 800)
        results = {r["id"]: r.get("result") for r in recs if r.get("type") == "resolve"}
        mine = []
        for r in recs:
            if r.get("type") == "alert" and r.get("symbol") == self.sym:
                a = {k: r[k] for k in ("id", "t", "symbol", "name", "dir", "conv",
                                       "entry", "stop", "target", "reason") if k in r}
                a["result"] = results.get(r["id"])
                mine.append(a)
        mine.sort(key=lambda a: a.get("t") or 0, reverse=True)
        self.recent = mine[:40]

    # ---- conviction ----------------------------------------------------------
    def _conviction(self, st: dict, rsi, ron_bias):
        ret30 = float(st.get("return_30s_bps") or 0)
        ret5 = float(st.get("return_5s_bps") or 0)
        vol = float(st.get("tick_volatility_60s_bps") or 0)
        direction = "long" if ret30 > 0 else "short" if ret30 < 0 else None
        if direction is None:
            return None, 0.0, {}
        mom = math.tanh(abs(ret30) / (vol * 2 + 3))                 # momentum vs noise, 0..1
        agree5 = 1.0 if (ret5 > 0) == (ret30 > 0) else 0.0          # 5s confirms 30s
        rsi_ok = 0.0
        if rsi is not None:
            if direction == "long" and rsi > 55:
                rsi_ok = min(1.0, (rsi - 55) / 25)
            elif direction == "short" and rsi < 45:
                rsi_ok = min(1.0, (45 - rsi) / 25)
        ron_ok = 1.0 if ron_bias == direction else (0.0 if ron_bias in ("long", "short") else 0.4)
        conv = 0.45 + 0.28 * mom + 0.10 * agree5 + 0.10 * rsi_ok + 0.07 * ron_ok
        conv = max(0.50, min(0.93, conv))                          # honest cap — never 100%
        return direction, conv, {"mom": mom, "agree5": agree5, "rsi_ok": rsi_ok,
                                 "ron_ok": ron_ok, "vol": vol}

    def _reason(self, direction, d):
        bits = []
        bits.append("strong momentum vs low noise" if d["mom"] > 0.6
                    else "momentum building" if d["mom"] > 0.35 else "early momentum")
        if d["agree5"]:
            bits.append("5s confirms 30s")
        if d["rsi_ok"] > 0.3:
            bits.append("RSI backing it")
        if d["ron_ok"] >= 1.0:
            bits.append("RON agrees")
        head = "watch for buys" if direction == "long" else "watch for sells"
        return f"{head} — " + ", ".join(bits)

    # ---- one assessment tick -------------------------------------------------
    def assess(self):
        if not self.e.market.market_open:
            return
        snap = self.e.market.snapshot()
        if not snap:
            return
        mid = snap.get("mid")
        st = snap.get("state") or {}
        if not mid:
            return
        ron_bias = self.e.ron.current().get("bias") if getattr(self.e, "ron", None) else None
        self._resolve(mid)  # grade the open alert first

        direction, conv, drivers = self._conviction(st, self.e.rsi, ron_bias)
        now = time.time()
        if not direction or conv < self.min_conv:
            return
        if self.open and self.open["dir"] == direction:
            return                                     # already flagged this way
        if now - self.last_fire < self.cooldown_s and direction == self.last_dir:
            return                                     # cooldown

        vol_bps = max(float(st.get("tick_volatility_60s_bps") or 0), 2.0)
        spread = mid * float(st.get("spread_bps") or 0) / 1e4
        stop_dist = max(mid * (vol_bps * 3.0) / 1e4, spread * 2, mid * 0.0004)
        target_dist = stop_dist * 1.8
        entry = mid
        stop = entry - stop_dist if direction == "long" else entry + stop_dist
        target = entry + target_dist if direction == "long" else entry - target_dist

        did = self.ledger.log_decision(self.sym, direction, "watch", None, round(conv, 3))
        alert = {"id": did, "t": now, "symbol": self.sym, "name": self.e.name, "dir": direction,
                 "conv": round(conv, 3), "entry": round(entry, 3), "stop": round(stop, 3),
                 "target": round(target, 3), "reason": self._reason(direction, drivers),
                 "result": None}
        self.open = alert
        self.last_fire, self.last_dir = now, direction
        self.recent.insert(0, alert)
        del self.recent[40:]
        if self.alerts_path is not None:
            from .persist import append_jsonl
            append_jsonl(self.alerts_path, {"type": "alert", **alert})

    def _resolve(self, mid):
        a = self.open
        if not a:
            return
        hit_t = (mid >= a["target"]) if a["dir"] == "long" else (mid <= a["target"])
        hit_s = (mid <= a["stop"]) if a["dir"] == "long" else (mid >= a["stop"])
        expired = time.time() - a["t"] > self.horizon_s
        if not (hit_t or hit_s or expired):
            return
        if hit_t:
            win, res = True, "target hit"
        elif hit_s:
            win, res = False, "stopped"
        else:
            moved = (mid - a["entry"]) if a["dir"] == "long" else (a["entry"] - mid)
            win, res = (moved > 0), ("expired +" if moved > 0 else "expired −")
        self.ledger.grade(self.sym, 1.0 if win else -1.0)
        a["result"] = res
        if self.alerts_path is not None:
            from .persist import append_jsonl
            append_jsonl(self.alerts_path, {"type": "resolve", "id": a["id"], "result": res, "t": time.time()})
        self.open = None
