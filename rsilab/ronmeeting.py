"""RON's daily team meeting + accountability scoreboard.

Once a day (~9am NZT) RON, the coach and each specialist agent each put a CALL on the day ahead —
bullish, bearish or range — with a confidence and a one-line reason, read from the live signals.
The calls are written to results/meeting.json for the front page.

Accountability: each meeting records the gold price at the time. At the NEXT meeting we score the
PREVIOUS day's calls against what gold actually did over the ~24h, and keep a running tally per
member (results/meeting_scores.json). Over weeks you can see who reads gold best — and later RON
can weight the sharpest voices more heavily in its own decisions.

Pure-ish and fully guarded: reads the in-memory signal dict `g` and the edge report; never trades.
Demo / advisory only.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

NZT_OFFSET_H = int(os.getenv("NZT_OFFSET_H", "13"))
FLAT_PCT = 0.0015  # <0.15% move over the day = "range" (so range calls can be right)

MEMBERS = [
    ("RON (decides)", "🧠"),
    ("Coach", "🤝"),
    ("Trend agent", "📈"),
    ("Session agent", "🕐"),
    ("Pattern agent", "🧩"),
    ("News & Macro", "🌍"),
]
BULL_PATTERNS = {"breakout up", "double bottom", "bull flag", "possible inverse H&S"}
BEAR_PATTERNS = {"breakout down", "double top", "bear flag", "possible head & shoulders"}


def _nzt(dt_utc: datetime) -> datetime:
    return dt_utc + timedelta(hours=NZT_OFFSET_H)


def _call(direction: str) -> tuple[str, str]:
    """Map an internal direction to a display label + tone."""
    if direction == "bull":
        return "Bullish", "pos"
    if direction == "bear":
        return "Bearish", "neg"
    return "Range / wait", "muted"


def _prim(g: dict):
    rep = g.get("_edge_report") or {}
    instrs = rep.get("instruments") or {}
    return (next(iter(instrs), None)), rep


def _gold_price(g: dict):
    _, rep = _prim(g)
    sym, _ = _prim(g)
    st = ((rep.get("instruments") or {}).get(sym) or {}).get("structure") or {}
    return st.get("price")


def _derive_calls(g: dict) -> list[dict]:
    sym, rep = _prim(g)
    instr = (rep.get("instruments") or {}).get(sym) or {}
    st = instr.get("structure") or {}
    per = ((rep.get("now") or {}).get("per") or {}).get(sym) or {}
    mtf = next((r for r in (g.get("ron_multitf") or {}).get("rows", []) if r.get("symbol") == sym), None)
    pat = next((r for r in (g.get("ron_patterns") or {}).get("rows", []) if r.get("symbol") == sym), None)
    coach = (g.get("ron_coach") or {}).get("note") or ""
    brief = g.get("_briefing") or {}

    out = []

    # --- RON: its live bias / stance ---
    stance = (per.get("stance") or "").lower()
    trend = (st.get("trend") or "").lower()
    if "bull" in stance or "up" in stance or "uptrend" in trend:
        d, conf, why = "bull", 65, "Structure and my session edge lean up."
    elif "bear" in stance or "down" in stance or "downtrend" in trend:
        d, conf, why = "bear", 65, "Structure and my session edge lean down."
    else:
        d, conf, why = "range", 55, "No clean edge yet — I'll trade the session as it shows."
    out.append({"dir": d, "conf": conf, "why": why})

    # --- Coach: light sentiment from its note ---
    cl = coach.lower()
    bull = sum(cl.count(k) for k in ("long", "bull", "upside", " up"))
    bear = sum(cl.count(k) for k in ("short", "bear", "downside", " down"))
    if not coach:
        out.append({"dir": "range", "conf": 50, "why": "Not enough closed trades to call it yet."})
    elif bull > bear:
        out.append({"dir": "bull", "conf": 58, "why": "My read of the results leans long."})
    elif bear > bull:
        out.append({"dir": "bear", "conf": 58, "why": "My read of the results leans short."})
    else:
        out.append({"dir": "range", "conf": 52, "why": "Mixed — I'd keep size down."})

    # --- Trend agent: multi-timeframe alignment ---
    state = (mtf or {}).get("state", "")
    if "up" in state:
        out.append({"dir": "bull", "conf": 70, "why": (mtf or {}).get("summary", "Higher timeframes aligned up.")})
    elif "down" in state:
        out.append({"dir": "bear", "conf": 70, "why": (mtf or {}).get("summary", "Higher timeframes aligned down.")})
    else:
        out.append({"dir": "range", "conf": 50, "why": (mtf or {}).get("summary") or "Timeframes mixed."})

    # --- Session agent: the live session's historical lean ---
    cur = ((g.get("ron_sessions") or {}).get("current") or {}).get("label") or (rep.get("now") or {}).get("session") or "the session"
    rows = ((g.get("ron_sessions") or {}).get("by_symbol", {}).get(sym, {}) or {}).get("rows", [])
    srow = next((r for r in rows if r.get("session") == cur), None)
    wr = srow.get("win_rate") if srow else None
    if wr is not None and wr >= 55:
        out.append({"dir": "bull", "conf": min(75, int(wr)), "why": f"{cur} has paid {wr}% — I lean with it."})
    elif wr is not None and wr <= 45:
        out.append({"dir": "bear", "conf": min(70, int(100 - wr)), "why": f"{cur} has been weak ({wr}%) — careful."})
    else:
        out.append({"dir": "range", "conf": 50, "why": f"{cur}: not enough edge yet — trade it live."})

    # --- Pattern agent ---
    p = (pat or {}).get("pattern")
    if p in BULL_PATTERNS:
        out.append({"dir": "bull", "conf": 60, "why": f"A {p} is showing — bias up."})
    elif p in BEAR_PATTERNS:
        out.append({"dir": "bear", "conf": 60, "why": f"A {p} is showing — bias down."})
    else:
        struct = (pat or {}).get("structure", "")
        d2 = "bull" if struct == "uptrend" else "bear" if struct == "downtrend" else "range"
        out.append({"dir": d2, "conf": 52, "why": (pat or {}).get("note") or "No clean pattern yet."})

    # --- News & Macro: from the latest briefing ---
    gold = (brief.get("gold") or {})
    tone = gold.get("tone")
    if tone == "pos":
        out.append({"dir": "bull", "conf": 58, "why": gold.get("verdict") or "Macro backdrop leans gold up."})
    elif tone == "neg":
        out.append({"dir": "bear", "conf": 58, "why": gold.get("verdict") or "Macro backdrop leans gold down."})
    else:
        out.append({"dir": "range", "conf": 50, "why": (brief.get("headline") or "Waiting on the next data to set direction.")[:90]})

    return out


def _consensus(calls: list[dict]) -> str:
    b = sum(1 for c in calls if c["dir"] == "bull")
    s = sum(1 for c in calls if c["dir"] == "bear")
    r = len(calls) - b - s
    if b > s and b >= r:
        return f"📗 Consensus: a GREEN (bullish) day — {b} of {len(calls)} lean long. Net: lean long, respect the scheduled data."
    if s > b and s >= r:
        return f"📕 Consensus: a RED (bearish) day — {s} of {len(calls)} lean short. Net: favour shorts with the trend."
    return f"📙 Consensus: a RANGE / mixed day — {b} up, {s} down, {r} neutral. Net: trade the session edges, keep size measured."


def _score_pending(scores: dict, price_now) -> None:
    """Score the previous meeting's calls against the ~24h move, update the running tally."""
    pend = scores.get("pending")
    if not pend or price_now is None or not pend.get("price_at"):
        return
    p0 = pend["price_at"]
    move = (price_now - p0) / p0 if p0 else 0.0
    if move > FLAT_PCT:
        actual = "bull"
    elif move < -FLAT_PCT:
        actual = "bear"
    else:
        actual = "range"
    tally = scores.setdefault("tally", {})
    for who, direction in (pend.get("calls") or {}).items():
        t = tally.setdefault(who, {"acc": 0, "acc_n": 0})
        t["acc_n"] += 1
        if direction == actual:
            t["acc"] += 1
    scores["pending"] = None


def maybe_meet(results: Path, g: dict, now: float | None = None) -> dict | None:
    """Build the meeting at ~9am NZT (and once on first run). Returns the meeting if it ran."""
    now = now or time.time()
    out = results / "meeting.json"
    scores_path = results / "meeting_scores.json"
    try:
        scores = json.loads(scores_path.read_text())
    except Exception:
        scores = {"tally": {}, "pending": None, "last_day": None}

    dt_nzt = _nzt(datetime.fromtimestamp(now, tz=timezone.utc))
    day = dt_nzt.strftime("%Y-%m-%d")
    first_ever = not out.exists()
    is_slot = dt_nzt.hour == 9 and scores.get("last_day") != day
    if not (is_slot or first_ever):
        return None

    # expose the latest briefing to the call-deriver (if present)
    try:
        g["_briefing"] = json.loads((results / "briefing.json").read_text())
    except Exception:
        g.setdefault("_briefing", {})

    price_now = _gold_price(g)
    if is_slot:  # score yesterday before casting today's calls
        _score_pending(scores, price_now)

    calls = _derive_calls(g)
    tally = scores.get("tally", {})
    members = []
    pend_calls = {}
    for (who, ic), c in zip(MEMBERS, calls):
        label, tone = _call(c["dir"])
        t = tally.get(who, {})
        members.append({"who": who, "ic": ic, "call": label, "tone": tone,
                        "conf": c["conf"], "why": c["why"],
                        "acc": t.get("acc", 0), "acc_n": t.get("acc_n", 0)})
        pend_calls[who] = c["dir"]

    meeting = {"as_of": now, "day": day, "consensus": _consensus(calls), "members": members}
    try:
        tmp = out.with_suffix(".tmp"); tmp.write_text(json.dumps(meeting, default=str)); os.replace(tmp, out)
    except Exception:
        pass

    # set the new pending for scoring at the next meeting
    scores["pending"] = {"day": day, "price_at": price_now, "calls": pend_calls}
    if is_slot:
        scores["last_day"] = day
    elif first_ever and scores.get("last_day") is None:
        scores["last_day"] = None  # let the real 9am slot still fire today
    try:
        scores_path.write_text(json.dumps(scores, default=str))
    except Exception:
        pass
    return meeting
