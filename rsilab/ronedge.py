"""RON EDGE — the statistical-research brain (advisory, for manual trading).

Two jobs, both learning from the growing candle history (ronhistory.HistoryStore):

  1. Time/session edges — groups history into hourly bars, then buckets by
     day-of-week x hour and by session, and reports where price has a repeatable
     directional lean: mean/median move, % up, sample size, a confidence tier, and
     a plain-English line ("NDQ100 · Wed 21:00 UTC (10:00 NZT) · +90 pts median ·
     7/10 up · n=14"). Ranked by edge strength x confidence.

  2. Chart structure (live) — support/resistance zones, order blocks, trend state +
     break-of-structure, and how close price is to the nearest level.

It states only what the data shows, flags confidence honestly by sample size, and it
gets sharper every day as more history accrues. It never places trades — it's here to
give the user a statistical edge for manual trading (and context for RON).
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone

from .ronagents import session_of

NZT_OFFSET_H = 13  # NZDT (Oct–Apr). Display convenience only; +12 in NZ winter.
_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse(t) -> datetime | None:
    s = str(t).replace("T", " ").split(".")[0].replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _nzt_hour(utc_hour: int) -> int:
    return (utc_hour + NZT_OFFSET_H) % 24


def _epoch(candle: dict):
    """Epoch seconds for a stored candle (so the dashboard can stamp events in NZT)."""
    dt = _parse(candle.get("t"))
    return dt.timestamp() if dt else None


def _conf_tier(n: int) -> str:
    return "solid" if n >= 12 else "building" if n >= 6 else "early"


def _hourly_bars(candles: list[dict]) -> list[dict]:
    """Collapse stored candles into one bar per calendar hour (UTC)."""
    bars: dict[tuple, dict] = {}
    for c in candles:
        dt = _parse(c.get("t"))
        if not dt:
            continue
        key = (dt.year, dt.month, dt.day, dt.hour)
        b = bars.get(key)
        if b is None:
            bars[key] = {"dt": dt.replace(minute=0, second=0, microsecond=0),
                         "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]}
        else:
            b["c"] = c["c"]
            b["h"] = max(b["h"], c["h"])
            b["l"] = min(b["l"], c["l"])
    return [bars[k] for k in sorted(bars)]


def time_stats(candles: list[dict]) -> list[dict]:
    """Day-of-week x hour buckets of hourly moves, ranked by edge x confidence."""
    bars = _hourly_bars(candles)
    buckets: dict[tuple, list[float]] = {}
    for b in bars:
        buckets.setdefault((b["dt"].weekday(), b["dt"].hour), []).append(b["c"] - b["o"])
    rows = []
    for (dow, hour), moves in buckets.items():   # moves are in chronological order
        n = len(moves)
        if n < 3:
            continue
        mean = sum(moves) / n
        med = statistics.median(moves)
        up = sum(1 for m in moves if m > 0)
        pct_up = 100.0 * up / n
        consistency = max(pct_up, 100 - pct_up)
        direction = "up" if pct_up >= 50 else "down"
        hits = up if direction == "up" else (n - up)
        # --- self-grading: has the edge kept working on its most recent occurrences? ---
        recent = moves[-6:]
        rn = len(recent)
        rhits = sum(1 for m in recent if (m > 0) == (direction == "up"))
        rpct = round(100.0 * rhits / rn) if rn else None
        if rn < 3:
            status = "fresh"
        elif rpct >= 60:
            status = "holding"
        elif rpct < 40:
            status = "fading"
        else:
            status = "mixed"
        score = (consistency / 100.0) * (n ** 0.5) * (abs(mean) + 1)
        if status == "fading":
            score *= 0.5      # demote edges that have stopped working lately
        rows.append({"dow": dow, "dow_name": _DOW[dow], "hour": hour, "nzt_hour": _nzt_hour(hour),
                     "n": n, "mean": round(mean, 1), "median": round(med, 1),
                     "pct_up": round(pct_up), "dir": direction, "hits": hits,
                     "consistency": round(consistency), "conf": _conf_tier(n),
                     "recent_n": rn, "recent_pct": rpct, "status": status,
                     "score": round(score, 2)})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def vol_by_time(candles: list[dict]) -> dict:
    """Average hourly range per day-of-week x hour — a volatility heatmap (when it moves)."""
    bars = _hourly_bars(candles)
    cells: dict[tuple, list[float]] = {}
    for b in bars:
        cells.setdefault((b["dt"].weekday(), b["dt"].hour), []).append(b["h"] - b["l"])
    matrix = [[None] * 24 for _ in range(7)]
    mx = 0.0
    for (dow, hour), rngs in cells.items():
        avg = sum(rngs) / len(rngs)
        matrix[dow][hour] = round(avg, 1)
        mx = max(mx, avg)
    return {"matrix": matrix, "max": round(mx, 1), "dow": _DOW}


def session_stats(candles: list[dict]) -> list[dict]:
    bars = _hourly_bars(candles)
    per: dict[str, list[float]] = {}
    for b in bars:
        per.setdefault(session_of(b["dt"].timestamp()), []).append(b["c"] - b["o"])
    rows = []
    for sess, moves in per.items():
        n = len(moves)
        if n < 3:
            continue
        up = sum(1 for m in moves if m > 0)
        pct_up = 100.0 * up / n
        rows.append({"session": sess, "n": n, "mean": round(sum(moves) / n, 1),
                     "pct_up": round(pct_up), "dir": "up" if pct_up >= 50 else "down",
                     "conf": _conf_tier(n)})
    rows.sort(key=lambda r: abs(r["mean"]) * (r["n"] ** 0.5), reverse=True)
    return rows


# ---- live chart structure --------------------------------------------------------
def _swings(vals, kind, k=2):
    out = []
    for i in range(k, len(vals) - k):
        w = vals[i - k:i + k + 1]
        if (kind == "high" and vals[i] == max(w)) or (kind == "low" and vals[i] == min(w)):
            out.append((i, vals[i]))
    return out


def _cluster(levels, tol):
    """Group nearby levels into zones; return [(centre, touches)] sorted by touches."""
    zones = []
    for lv in sorted(levels):
        for z in zones:
            if abs(lv - z["sum"] / z["n"]) <= tol:
                z["sum"] += lv
                z["n"] += 1
                break
        else:
            zones.append({"sum": lv, "n": 1})
    return sorted(((z["sum"] / z["n"], z["n"]) for z in zones), key=lambda x: -x[1])


def structure(candles: list[dict]) -> dict:
    """Support/resistance, order blocks, trend + break-of-structure from recent candles."""
    c = candles[-160:]
    if len(c) < 20:
        return {"ready": False}
    highs = [x["h"] for x in c]
    lows = [x["l"] for x in c]
    closes = [x["c"] for x in c]
    price = closes[-1]
    rng = (max(highs) - min(lows)) or 1e-9
    tol = rng * 0.05

    sh = _swings(highs, "high")
    sl = _swings(lows, "low")

    # support/resistance zones from clustered swings
    res_zones = _cluster([v for _, v in sh if v >= price], tol)
    sup_zones = _cluster([v for _, v in sl if v <= price], tol)
    resistance = res_zones[0] if res_zones else None
    support = sup_zones[0] if sup_zones else None

    # trend + break of structure, from the last two swings each side
    trend, bos = "range", None
    trend_since = None
    if len(sh) >= 2 and len(sl) >= 2:
        hh, hl = sh[-1][1] > sh[-2][1], sl[-1][1] > sl[-2][1]
        lh, ll = sh[-1][1] < sh[-2][1], sl[-1][1] < sl[-2][1]
        trend = "uptrend" if (hh and hl) else "downtrend" if (lh and ll) else "range"
        if trend != "range":
            # the current trend "started" at the earlier of the two swings that define it
            idx = min(sh[-2][0], sl[-2][0])
            trend_since = _epoch(c[idx])
    bos_at = None
    if sh and price > sh[-1][1]:
        bos = "bullish break — price took out the last swing high"
        lvl = sh[-1][1]
        for j in range(sh[-1][0] + 1, len(c)):       # first close that took the level out
            if c[j]["c"] > lvl:
                bos_at = _epoch(c[j]); break
        if bos_at is None:
            bos_at = _epoch(c[-1])
    elif sl and price < sl[-1][1]:
        bos = "bearish break — price took out the last swing low"
        lvl = sl[-1][1]
        for j in range(sl[-1][0] + 1, len(c)):
            if c[j]["c"] < lvl:
                bos_at = _epoch(c[j]); break
        if bos_at is None:
            bos_at = _epoch(c[-1])

    # order blocks: last opposite candle before a strong (>1.2*avg body) move
    bodies = [abs(x["c"] - x["o"]) for x in c]
    avg_body = (sum(bodies) / len(bodies)) or 1e-9
    obs = []
    for i in range(2, len(c) - 1):
        move = c[i]["c"] - c[i]["o"]
        if abs(move) < avg_body * 1.6:
            continue
        prev = c[i - 1]
        if move > 0 and prev["c"] < prev["o"]:      # bullish OB = last down candle before up move
            obs.append({"type": "bullish", "lo": round(prev["l"], 2), "hi": round(prev["o"], 2), "i": i})
        elif move < 0 and prev["c"] > prev["o"]:    # bearish OB = last up candle before down move
            obs.append({"type": "bearish", "lo": round(prev["o"], 2), "hi": round(prev["h"], 2), "i": i})
    obs = obs[-3:][::-1]  # most recent few

    def dist(level):
        return {"level": round(level, 2), "pts": round(abs(price - level), 1),
                "pct": round(100 * abs(price - level) / price, 2)}

    nearest = None
    cands = []
    if resistance:
        cands.append(("resistance", resistance[0]))
    if support:
        cands.append(("support", support[0]))
    for ob in obs:
        cands.append((f"{ob['type']} order block", (ob["lo"] + ob["hi"]) / 2))
    if cands:
        kind, lvl = min(cands, key=lambda kv: abs(price - kv[1]))
        nearest = {"what": kind, **dist(lvl)}
        nearest["approaching"] = nearest["pct"] <= 0.15

    def _set_at(swings, centre):
        best = None
        for i, v in swings:                 # swings are chronological; keep the latest match
            if abs(v - centre) <= tol:
                best = i
        return _epoch(c[best]) if best is not None else None

    res_set = _set_at(sh, resistance[0]) if resistance else None
    sup_set = _set_at(sl, support[0]) if support else None

    return {"ready": True, "price": round(price, 2), "trend": trend, "bos": bos,
            "trend_since": trend_since, "bos_at": bos_at,
            "resistance": ({"level": round(resistance[0], 2), "touches": resistance[1], "set_at": res_set} if resistance else None),
            "support": ({"level": round(support[0], 2), "touches": support[1], "set_at": sup_set} if support else None),
            "order_blocks": obs, "nearest": nearest}


def _now_stance(cur_edge, sess_row, struct, vol):
    """Combine current-hour edge + session + structure (+vol context) into one read."""
    votes, bits = [], []
    if cur_edge and cur_edge["n"] >= 4 and cur_edge["consistency"] >= 60 and cur_edge["status"] != "fading":
        votes.append(cur_edge["dir"])
        bits.append(f"this hour leans {cur_edge['dir']} ({cur_edge['pct_up'] if cur_edge['dir']=='up' else 100-cur_edge['pct_up']}% · n={cur_edge['n']} · {cur_edge['status']})")
    if sess_row and sess_row["n"] >= 5:
        votes.append(sess_row["dir"])
        bits.append(f"{sess_row['session']} session leans {sess_row['dir']}")
    if struct and struct.get("trend") in ("uptrend", "downtrend"):
        tdir = "up" if struct["trend"] == "uptrend" else "down"
        votes.append(tdir)
        bits.append(f"structure {struct['trend']}")
    if struct and struct.get("bos"):
        bits.append(struct["bos"])
    if vol and vol.get("regime"):
        bits.append(f"volatility {vol['regime']}")
    ups = votes.count("up")
    downs = votes.count("down")
    if ups >= 2 and ups > downs:
        stance = "bullish bias"
    elif downs >= 2 and downs > ups:
        stance = "bearish bias"
    elif votes and ups == downs:
        stance = "mixed — wait"
    elif not votes:
        stance = "no edge right now"
    else:
        stance = ("lean " + ("up" if ups > downs else "down")) if ups != downs else "mixed — wait"
    return {"stance": stance, "bits": bits}


def build_report(store, engines) -> dict:
    """Assemble the full RON EDGE report across instruments."""
    from .ronagents import VolatilityAgent
    vol_agent = VolatilityAgent()
    now = datetime.now(timezone.utc)
    dow, hour = now.weekday(), now.hour
    cur_session = session_of(now.timestamp())
    out = {"as_of": time.time(), "nzt_offset": NZT_OFFSET_H,
           "now": {"utc_hour": hour, "nzt_hour": _nzt_hour(hour), "dow_name": _DOW[dow],
                   "session": cur_session, "per": {}},
           "instruments": {}}
    for e in engines:
        sym = e.symbol
        candles = store.load(sym)
        n = len(candles)
        bars = _hourly_bars(candles)
        days = round(len(bars) / 24.0, 1) if bars else 0
        price = None
        snap = e.market.snapshot() if getattr(e, "market", None) else None
        if snap and snap.get("mid"):
            price = round(snap["mid"], 2)
        elif candles:
            price = round(candles[-1]["c"], 2)
        all_t = time_stats(candles)
        sessions = session_stats(candles)
        struct = structure(candles) if n else {"ready": False}
        try:
            vol = vol_agent.assess_one(e)
        except Exception:
            vol = None
        cur_edge = next((b for b in all_t if b["dow"] == dow and b["hour"] == hour), None)
        sess_row = next((s for s in sessions if s["session"] == cur_session), None)
        out["now"]["per"][sym] = {"name": e.name, **_now_stance(cur_edge, sess_row, struct, vol)}
        out["instruments"][sym] = {
            "name": e.name, "candles": n, "hours": len(bars), "days": days, "price": price,
            "top_times": all_t[:8], "sessions": sessions, "structure": struct,
            "vol_heatmap": vol_by_time(candles) if n else {"matrix": [], "max": 0},
            "volatility": vol,
        }
    return out
