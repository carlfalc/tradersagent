"""RON's specialist analyst agents — advisory context around the trade signal.

Three agents, each doing one job and stating only what is observably true (so nothing
is asserted that hasn't happened). They never place trades.

  SessionsAgent    — which FX/index session is live now, and which sessions each
                     instrument has actually done best in (from real closed-trade history).
  VolatilityAgent  — activity regime per instrument: candle-range expansion + live tick
                     volatility. Flags unusual bursts that often precede moves.
  MultiTFAgent     — reads RSI/trend on 15m vs 30m vs 1h and reports alignment or an
                     early reversal ("15m rolling over while 1h still up"), timestamped.
  PatternAgent     — classical price STRUCTURE from the recent candles: uptrend/downtrend
                     (higher-highs/lows vs lower), range/flag consolidation, breakouts, and
                     double top/bottom (plus a conservative head-&-shoulders check). It calls
                     only what the swings actually show — honest, not wishful.

All pure-ish: they read the engines' already-fetched data (and, for MultiTF, a throttled
candle pull per instrument), and return plain dicts the dashboard renders. Demo only.
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone

from .ha_rsi import heikin_ashi, to_candles, wilder_rsi

# ---- sessions (UTC hours, [start, end)) -----------------------------------------
# Rough but standard market hours in UTC. Overlaps are the liveliest.
_SESSIONS = [
    ("Sydney", 21, 6),
    ("Tokyo", 0, 9),
    ("London", 7, 16),
    ("New York", 12, 21),
]


def _in_session(hour: int, start: int, end: int) -> bool:
    return start <= hour < end if start < end else (hour >= start or hour < end)


def session_of(ts_epoch: float) -> str:
    """The single most-relevant session label for a moment (for bucketing trades)."""
    h = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).hour
    active = [name for name, s, e in _SESSIONS if _in_session(h, s, e)]
    if "London" in active and "New York" in active:
        return "London/NY overlap"
    if "London" in active:
        return "London"
    if "New York" in active:
        return "New York"
    if "Tokyo" in active and "Sydney" in active:
        return "Tokyo"
    return active[0] if active else "Off-hours"


class SessionsAgent:
    """Live session + which sessions each instrument has actually profited in."""

    def current(self) -> dict:
        now = time.time()
        h = datetime.now(timezone.utc).hour
        active = [name for name, s, e in _SESSIONS if _in_session(h, s, e)]
        label = session_of(now)
        return {"label": label, "active": active, "utc_hour": h}

    def best_by_symbol(self, closed_trades: list[dict]) -> dict:
        """Per-symbol per-session win-rate & pnl from real closed trades."""
        per: dict[str, dict[str, dict]] = {}
        for tr in closed_trades:
            t = tr.get("t")
            sym = tr.get("symbol")
            if t is None or not sym:
                continue
            sess = session_of(float(t))
            s = per.setdefault(sym, {}).setdefault(sess, {"n": 0, "wins": 0, "pnl": 0.0})
            pnl = float(tr.get("pnl") or 0)
            s["n"] += 1
            s["wins"] += 1 if pnl > 0 else 0
            s["pnl"] += pnl
        out = {}
        for sym, sessions in per.items():
            rows = []
            for sess, s in sessions.items():
                rows.append({"session": sess, "n": s["n"],
                             "win_rate": round(100 * s["wins"] / s["n"], 1) if s["n"] else None,
                             "pnl": round(s["pnl"], 2)})
            rows.sort(key=lambda r: r["pnl"], reverse=True)
            best = rows[0] if rows else None
            out[sym] = {"rows": rows, "best": best}
        return out

    @staticmethod
    def _rows(trades: list[dict]) -> list[dict]:
        """Per-session rows (session, n, win_rate, pnl, dir) for a set of trades."""
        agg: dict[str, dict] = {}
        for tr in trades:
            t = tr.get("t"); sym_ok = tr.get("symbol")
            if t is None or not sym_ok:
                continue
            sess = session_of(float(t))
            s = agg.setdefault(sess, {"n": 0, "wins": 0, "pnl": 0.0})
            pnl = float(tr.get("pnl") or 0)
            s["n"] += 1
            s["wins"] += 1 if pnl > 0 else 0
            s["pnl"] += pnl
        rows = []
        for sess, s in agg.items():
            wr = round(100 * s["wins"] / s["n"], 1) if s["n"] else None
            rows.append({"session": sess, "n": s["n"], "win_rate": wr,
                         "pnl": round(s["pnl"], 2),
                         "dir": ("up" if wr is not None and wr >= 55 else "down" if wr is not None and wr <= 45 else "flat")})
        rows.sort(key=lambda r: r["pnl"], reverse=True)
        return rows

    def by_period(self, closed_trades: list[dict], now: float | None = None) -> dict:
        """Per-symbol session rows split into today (NZT) / last 7d / last 30d / all."""
        now = now or time.time()
        off = int(__import__("os").getenv("NZT_OFFSET_H", "13")) * 3600
        nzt_midnight = (int((now + off) // 86400) * 86400) - off  # NZT calendar-day start, in epoch
        spans = {"today": nzt_midnight, "week": now - 7 * 86400, "month": now - 30 * 86400, "all": 0}
        per: dict[str, dict] = {}
        syms = {tr.get("symbol") for tr in closed_trades if tr.get("symbol")}
        for sym in syms:
            trs = [t for t in closed_trades if t.get("symbol") == sym and t.get("t") is not None]
            per[sym] = {k: self._rows([t for t in trs if float(t["t"]) >= cutoff]) for k, cutoff in spans.items()}
        return per

    def assess(self, closed_trades: list[dict]) -> dict:
        return {"current": self.current(), "by_symbol": self.best_by_symbol(closed_trades),
                "by_period": self.by_period(closed_trades), "as_of": time.time()}


class VolatilityAgent:
    """Activity regime from candle-range expansion + live tick volatility."""

    def assess_one(self, engine) -> dict | None:
        ha = getattr(engine, "ha_recent", None) or []
        if len(ha) < 12:
            return None
        ranges = [max(c["h"] - c["l"], 0.0) for c in ha[-30:]]
        if not ranges:
            return None
        med = statistics.median(ranges[:-1]) if len(ranges) > 3 else statistics.median(ranges)
        cur = ranges[-1]
        ratio = (cur / med) if med > 0 else 1.0
        snap = engine.market.snapshot() if getattr(engine, "market", None) else {}
        tickvol = float((snap.get("state") or {}).get("tick_volatility_60s_bps") or 0) if snap else 0.0
        if ratio >= 2.2:
            regime, spike = "spiking", True
        elif ratio >= 1.5:
            regime, spike = "elevated", False
        elif ratio >= 0.7:
            regime, spike = "normal", False
        else:
            regime, spike = "calm", False
        note = {
            "spiking": "volatility spiking — range well above normal, expect fast moves",
            "elevated": "activity picking up — range above normal",
            "normal": "normal activity",
            "calm": "quiet — range below normal, moves may be slow",
        }[regime]
        return {"symbol": engine.symbol, "name": engine.name, "regime": regime,
                "spike": spike, "range_ratio": round(ratio, 2),
                "tick_vol_bps": round(tickvol, 2), "note": note, "as_of": time.time()}

    def assess(self, engines) -> dict:
        rows = [r for r in (self.assess_one(e) for e in engines) if r]
        return {"rows": rows, "as_of": time.time()}


class MultiTFAgent:
    """RSI/trend on 15m vs 30m vs 1h: alignment, or an early reversal warning."""

    TFS = ["15m", "30m", "1h"]

    def __init__(self, rsi_len: int = 14):
        self.rsi_len = rsi_len
        self._cache: dict[str, dict] = {}
        self._last: dict[str, float] = {}

    def _tf_read(self, client, symbol: str, tf: str) -> dict | None:
        try:
            raw = client.candles(symbol, tf, limit=120)
        except Exception:
            return None
        candles = to_candles(raw)
        if len(candles) < self.rsi_len + 6:
            return None
        ha = heikin_ashi(candles)
        closes = [c.c for c in ha]
        rsivals = wilder_rsi(closes, self.rsi_len)
        # act on the last CLOSED bar
        rsi_now = rsivals[-2] if rsivals[-2] is not None else rsivals[-1]
        if rsi_now is None:
            return None
        slope = closes[-2] - closes[-5] if len(closes) >= 5 else 0.0
        if rsi_now >= 55 and slope > 0:
            trend = "up"
        elif rsi_now <= 45 and slope < 0:
            trend = "down"
        else:
            trend = "flat"
        return {"tf": tf, "rsi": round(rsi_now, 1), "trend": trend}

    def assess_one(self, client, engine, throttle_s: float = 180.0) -> dict | None:
        sym = engine.symbol
        now = time.time()
        if now - self._last.get(sym, 0) < throttle_s and sym in self._cache:
            return self._cache[sym]
        reads = {}
        for tf in self.TFS:
            r = self._tf_read(client, sym, tf)
            if r:
                reads[tf] = r
        if len(reads) < 2:
            return self._cache.get(sym)
        self._last[sym] = now
        trends = [reads[tf]["trend"] for tf in self.TFS if tf in reads]
        higher = reads.get("1h", {}).get("trend") or reads.get("30m", {}).get("trend")
        lower = reads.get("15m", {}).get("trend")
        state, warning = "mixed", None
        if trends and all(t == "up" for t in trends):
            state = "aligned up"
        elif trends and all(t == "down" for t in trends):
            state = "aligned down"
        elif higher == "up" and lower == "down":
            state, warning = "reversal risk", "15m rolling over while higher timeframe still up — watch for a pullback/short"
        elif higher == "down" and lower == "up":
            state, warning = "reversal risk", "15m turning up while higher timeframe still down — watch for a bounce/long"
        summary_bits = [f"{tf} {reads[tf]['trend']} (RSI {reads[tf]['rsi']})" for tf in self.TFS if tf in reads]
        res = {"symbol": sym, "name": engine.name, "state": state, "warning": warning,
               "reads": reads, "summary": " · ".join(summary_bits), "as_of": now}
        self._cache[sym] = res
        return res

    def assess(self, client, engines) -> dict:
        rows = [r for r in (self.assess_one(client, e) for e in engines) if r]
        return {"rows": rows, "as_of": time.time()}


class PatternAgent:
    """Classical price-structure read from recent candles — honest, swing-based.

    Detects, per instrument, the clearest of: trend structure (higher-highs/lows vs
    lower-highs/lows), a range/flag consolidation, a breakout of the recent range, and
    double top / double bottom (with a conservative head-&-shoulders check). It reports
    only what the swings actually show; if nothing clean is present it says "no clear
    pattern" rather than inventing one.
    """

    def __init__(self, swing_k: int = 2):
        self.swing_k = swing_k  # bars either side to qualify a swing high/low

    def _swings(self, vals, kind: str):
        """Return list of (index, value) local extrema. kind='high' or 'low'."""
        k = self.swing_k
        out = []
        for i in range(k, len(vals) - k):
            window = vals[i - k:i + k + 1]
            if kind == "high" and vals[i] == max(window):
                out.append((i, vals[i]))
            elif kind == "low" and vals[i] == min(window):
                out.append((i, vals[i]))
        return out

    def assess_one(self, engine) -> dict | None:
        ha = getattr(engine, "ha_recent", None) or []
        if len(ha) < 20:
            return None
        highs = [c["h"] for c in ha]
        lows = [c["l"] for c in ha]
        closes = [c["c"] for c in ha]
        last = closes[-1]
        rng = (max(highs) - min(lows)) or 1e-9
        tol = rng * 0.08  # "similar level" tolerance, 8% of the shown range

        sh = self._swings(highs, "high")
        sl = self._swings(lows, "low")

        structure, pattern, note = "choppy", None, None

        # ---- trend structure from last two swing highs & lows --------------------
        if len(sh) >= 2 and len(sl) >= 2:
            hh = sh[-1][1] > sh[-2][1]
            hl = sl[-1][1] > sl[-2][1]
            lh = sh[-1][1] < sh[-2][1]
            ll = sl[-1][1] < sl[-2][1]
            if hh and hl:
                structure = "uptrend"
            elif lh and ll:
                structure = "downtrend"
            else:
                structure = "range"

        # slope fallback so a clean trend isn't mislabelled "choppy" when swings are sparse
        if structure in ("choppy", "range") and len(closes) >= 12:
            net = closes[-1] - closes[-12]
            if net > rng * 0.35:
                structure = "uptrend"
            elif net < -rng * 0.35:
                structure = "downtrend"

        # ---- breakout of the recent range (last close vs prior 15 bars) ----------
        prior_hi = max(highs[-16:-1]) if len(highs) > 16 else max(highs[:-1])
        prior_lo = min(lows[-16:-1]) if len(lows) > 16 else min(lows[:-1])
        if last > prior_hi:
            pattern = "breakout up"
            note = "broke above the recent range — momentum breakout higher"
        elif last < prior_lo:
            pattern = "breakout down"
            note = "broke below the recent range — momentum breakdown"

        # ---- range / flag: recent contraction after a strong move -----------------
        if pattern is None and len(closes) >= 20:
            recent_rng = max(highs[-8:]) - min(lows[-8:])
            older_rng = max(highs[-20:-8]) - min(lows[-20:-8])
            move = closes[-8] - closes[-20]
            if older_rng > 0 and recent_rng < older_rng * 0.6 and abs(move) > older_rng * 0.6:
                if move > 0:
                    pattern, note = "bull flag", "tight consolidation after a push up — watch for continuation higher"
                else:
                    pattern, note = "bear flag", "tight consolidation after a push down — watch for continuation lower"

        # ---- double top / bottom -------------------------------------------------
        if pattern is None and len(sh) >= 2:
            a, b = sh[-2][1], sh[-1][1]
            if abs(a - b) <= tol and b >= max(highs) - tol:
                pattern, note = "double top", "two highs at a similar level — possible rejection / reversal lower"
        if pattern is None and len(sl) >= 2:
            a, b = sl[-2][1], sl[-1][1]
            if abs(a - b) <= tol and b <= min(lows) + tol:
                pattern, note = "double bottom", "two lows at a similar level — possible base / reversal higher"

        # ---- conservative head & shoulders (3 swing highs, middle highest) -------
        if pattern is None and len(sh) >= 3:
            l_, m_, r_ = sh[-3][1], sh[-2][1], sh[-1][1]
            if m_ > l_ and m_ > r_ and abs(l_ - r_) <= tol and (m_ - max(l_, r_)) > tol:
                pattern, note = "possible head & shoulders", "three highs with a taller middle — classic topping shape (approximate)"
        if pattern is None and len(sl) >= 3:
            l_, m_, r_ = sl[-3][1], sl[-2][1], sl[-1][1]
            if m_ < l_ and m_ < r_ and abs(l_ - r_) <= tol and (min(l_, r_) - m_) > tol:
                pattern, note = "possible inverse H&S", "three lows with a deeper middle — classic bottoming shape (approximate)"

        if pattern is None:
            note = {"uptrend": "higher highs & higher lows — trending up",
                    "downtrend": "lower highs & lower lows — trending down",
                    "range": "swinging in a range — no clear direction",
                    "choppy": "no clear pattern yet"}[structure]

        return {"symbol": engine.symbol, "name": engine.name, "structure": structure,
                "pattern": pattern, "note": note, "as_of": time.time()}

    def assess(self, engines) -> dict:
        rows = [r for r in (self.assess_one(e) for e in engines) if r]
        return {"rows": rows, "as_of": time.time()}
