"""RON's confluence engine — fuse the agents into one weighted read RON can act on.

Each agent casts a directional vote (up / down / none) with a strength. The votes are weighted
by how ACCURATE that agent has actually been on the daily meeting scoreboard — so voices that
read gold well over time count for more, and ones that don't fade. The result is a single score
in [-1, +1], a direction, and a trade-likelihood %.

The front page shows this as RON's "final decision" confluence. The trading loop uses it only as a
small, bounded nudge to the conviction it already requires (see rsibot.check_watcher_entries):
strong agreement makes a high-conviction call a touch easier, strong disagreement makes it harder.
It never forces a trade and is capped (FUSION_MAX) and reversible (FUSION_ENABLED=0). Demo only.
"""

from __future__ import annotations

import time

# agent label -> meeting-scoreboard member name (for accuracy weighting)
WEIGHT_KEY = {
    "Trend agent": "Trend agent",
    "Pattern agent": "Pattern agent",
    "Session agent": "Session agent",
    "News & Macro": "News & Macro",
}
BULL = {"breakout up", "double bottom", "bull flag", "possible inverse H&S"}
BEAR = {"breakout down", "double top", "bear flag", "possible head & shoulders"}
MIN_W = 0.2       # floor so a new/poor agent still has a small say
DEFAULT_W = 0.5   # before it has a track record


def _weight(scores: dict, who: str) -> float:
    t = ((scores or {}).get("tally") or {}).get(who) or {}
    n = t.get("acc_n", 0)
    if not n:
        return DEFAULT_W
    return max(MIN_W, t.get("acc", 0) / n)


def _votes(sym: str, g: dict) -> list[dict]:
    """Each agent's directional vote: who, dir(+1/-1/0), strength[0..1]."""
    out = []
    mtf = next((r for r in (g.get("ron_multitf") or {}).get("rows", []) if r.get("symbol") == sym), None)
    pat = next((r for r in (g.get("ron_patterns") or {}).get("rows", []) if r.get("symbol") == sym), None)
    brief = g.get("_briefing") or {}
    sess = (g.get("ron_sessions") or {})
    cur = ((sess.get("current") or {}).get("label"))
    rows = ((sess.get("by_symbol", {}).get(sym, {}) or {}).get("rows", []))
    srow = next((r for r in rows if r.get("session") == cur), None)

    # Trend agent
    state = (mtf or {}).get("state", "")
    if "up" in state:
        out.append({"who": "Trend agent", "dir": 1, "strength": 0.8})
    elif "down" in state:
        out.append({"who": "Trend agent", "dir": -1, "strength": 0.8})
    else:
        out.append({"who": "Trend agent", "dir": 0, "strength": 0.0})

    # Pattern agent
    p = (pat or {}).get("pattern"); struct = (pat or {}).get("structure", "")
    if p in BULL:
        out.append({"who": "Pattern agent", "dir": 1, "strength": 0.6})
    elif p in BEAR:
        out.append({"who": "Pattern agent", "dir": -1, "strength": 0.6})
    elif struct == "uptrend":
        out.append({"who": "Pattern agent", "dir": 1, "strength": 0.4})
    elif struct == "downtrend":
        out.append({"who": "Pattern agent", "dir": -1, "strength": 0.4})
    else:
        out.append({"who": "Pattern agent", "dir": 0, "strength": 0.0})

    # Session agent (needs a little history)
    wr = srow.get("win_rate") if srow else None
    n = srow.get("n") if srow else 0
    if wr is not None and n >= 4:
        if wr >= 55:
            out.append({"who": "Session agent", "dir": 1, "strength": min(1.0, (wr - 50) / 50)})
        elif wr <= 45:
            out.append({"who": "Session agent", "dir": -1, "strength": min(1.0, (50 - wr) / 50)})
        else:
            out.append({"who": "Session agent", "dir": 0, "strength": 0.0})
    else:
        out.append({"who": "Session agent", "dir": 0, "strength": 0.0})

    # News & Macro
    tone = (brief.get("gold") or {}).get("tone")
    if tone == "pos":
        out.append({"who": "News & Macro", "dir": 1, "strength": 0.5})
    elif tone == "neg":
        out.append({"who": "News & Macro", "dir": -1, "strength": 0.5})
    else:
        out.append({"who": "News & Macro", "dir": 0, "strength": 0.0})
    return out


def compute_one(sym: str, g: dict, scores: dict | None) -> dict:
    votes = _votes(sym, g)
    num = 0.0; den = 0.0; wsum = 0.0; contribs = []
    for v in votes:
        w = _weight(scores, v["who"])
        wsum += w
        s = v["strength"]
        if s > 0 and v["dir"] != 0:
            num += w * v["dir"] * s
            den += w * s
        contribs.append({"who": v["who"], "dir": v["dir"], "weight": round(w, 2), "strength": s})
    score = (num / den) if den > 0 else 0.0
    conf = (den / wsum) if wsum > 0 else 0.0
    direction = 1 if score > 0.15 else -1 if score < -0.15 else 0
    likelihood = round(50 + 50 * abs(score) * conf) if direction != 0 else None
    if likelihood is not None:
        likelihood = max(50, min(95, likelihood))
    return {"symbol": sym, "score": round(score, 3), "conf": round(conf, 3),
            "direction": direction, "likelihood": likelihood,
            "contributions": contribs, "as_of": time.time()}


def compute(g: dict, scores: dict | None, symbols: list[str]) -> dict:
    return {sym: compute_one(sym, g, scores) for sym in symbols}


def nudge(fusion: dict | None, dsign: int, fusion_max: float) -> float:
    """Bounded conviction adjustment for a prospective trade in direction dsign (+1/-1).

    Agreement with a confident, accurate panel → small positive nudge; disagreement → negative.
    Returns a delta in [-fusion_max, +fusion_max].
    """
    if not fusion or not fusion.get("direction"):
        return 0.0
    agree = 1.0 if ((fusion["direction"] > 0 and dsign > 0) or (fusion["direction"] < 0 and dsign < 0)) else -1.0
    mag = min(1.0, abs(fusion.get("score", 0.0))) * fusion.get("conf", 0.0)
    return max(-fusion_max, min(fusion_max, fusion_max * agree * mag))
