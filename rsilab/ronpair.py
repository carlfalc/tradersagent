"""AUDNZD (and any cross) ANALYSIS pipeline — the gold-style read, without trading.

RON does NOT place trades on this pair. This module just runs the same analysis engines on the
pair's live candles — market structure (with NZT-stamped trend/break/level times), chart patterns
(recorded + graded), the multi-timeframe trend read + trend-quality learner, volatility, the
candle-based session/time edges + volatility heatmap, a fused advisory bias, and a dedicated
AUD+NZD economic/news briefing (RBA vs RBNZ, both countries' data, politics, cross flows). It
writes results/audnzd.json for the AUDNZD dashboard page. Reuses the gold engines; separate files
so nothing collides with gold. Demo / advisory only.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from .ha_rsi import to_candles, heikin_ashi
from . import ronedge
from .ronagents import PatternAgent, VolatilityAgent, MultiTFAgent, session_of
from .ronpatterns import PatternRecorder
from .ronfollow import TrendRecorder
from .ronfusion import compute_one
from . import ronnews

BRIEF_EVERY_S = 3600.0
_state: dict = {}
_pat = PatternAgent()
_vol = VolatilityAgent()
_mtf = MultiTFAgent()


class _Shim:
    """Minimal engine-like object so the analyst agents can read a non-traded symbol."""
    def __init__(self, symbol, name, ha_recent):
        self.symbol = symbol
        self.name = name
        self.ha_recent = ha_recent
        self.market = None


def _cds(candles):
    return [{"t": c.t, "o": c.o, "h": c.h, "l": c.l, "c": c.c} for c in candles]


def _rel_vol(raw):
    vols = [float(c.get("tickVolume") or c.get("volume") or 0) for c in (raw or [])]
    vols = [v for v in vols if v > 0]
    if len(vols) < 8:
        return None
    med = statistics.median(vols[:-1]) or 1.0
    return round(vols[-1] / med, 2) if med else None


def build(client, results: Path, symbol: str = "AUDNZD", name: str = "AUD/NZD",
          base: str = "AUD", quote: str = "NZD", now: float | None = None) -> dict:
    now = now or time.time()
    st = _state.setdefault(symbol, {
        "rec": PatternRecorder(results, fname=f"patterns_{symbol.lower()}.jsonl"),
        "trend": TrendRecorder(results, fname=f"trend_events_{symbol.lower()}.jsonl"),
        "last_brief": 0.0, "briefing": {},
    })
    try:
        raw = client.candles(symbol, "3m", 400)
    except Exception:
        raw = []
    if not raw:
        return {"ready": False, "symbol": symbol, "name": name, "as_of": now,
                "note": "no candles for this pair yet", "nzt_offset": ronedge.NZT_OFFSET_H}
    candles = to_candles(raw)
    cds = _cds(candles)
    ha = heikin_ashi(candles)
    ha_recent = [{"h": c.h, "l": c.l, "c": c.c} for c in ha][-60:]
    shim = _Shim(symbol, name, ha_recent)

    structure = ronedge.structure(cds)
    price = structure.get("price") if structure.get("ready") else (cds[-1]["c"] if cds else None)
    sessions = ronedge.session_stats(cds)
    top_times = ronedge.time_stats(cds)[:8]
    heatmap = ronedge.vol_by_time(cds)

    # patterns — detect, record, grade, summarise
    pat = None
    try:
        pat = _pat.assess_one(shim)
        if pat and price:
            st["rec"].observe(symbol, name, pat.get("pattern"), pat.get("structure"), pat.get("note"), price, now)
            st["rec"].grade(symbol, price, now)
    except Exception:
        pass
    feed = st["rec"].summary(now)

    vol = None
    try:
        vol = _vol.assess_one(shim)
    except Exception:
        pass
    mtf = None
    try:
        mtf = _mtf.assess_one(client, shim)
    except Exception:
        pass

    relv = _rel_vol(raw)
    try:
        if mtf:
            st["trend"].observe(symbol, mtf.get("state"), price, relv, now)
            st["trend"].grade(symbol, price, now)
    except Exception:
        pass
    trend = st["trend"].summary(symbol, now)

    # advisory fused bias (no trade) — reuse the fusion engine on a small signal dict
    g = {"ron_multitf": {"rows": [mtf] if mtf else []},
         "ron_patterns": {"rows": [pat] if pat else []},
         "ron_sessions": {"current": {"label": session_of(now)}, "by_symbol": {}},
         "_briefing": {}}
    try:
        fusion = compute_one(symbol, g, None)
    except Exception:
        fusion = None

    # AUD+NZD economic/news briefing — hourly
    if now - st.get("last_brief", 0) >= BRIEF_EVERY_S or not st.get("briefing"):
        pc = {"price": price, "trend": structure.get("trend"), "session": session_of(now),
              "support": (structure.get("support") or {}).get("level") if structure.get("support") else None,
              "resistance": (structure.get("resistance") or {}).get("level") if structure.get("resistance") else None}
        try:
            st["briefing"] = ronnews.build_pair_briefing(results, symbol, base, quote, pc, now)
            st["last_brief"] = now
        except Exception:
            pass

    return {
        "ready": True, "symbol": symbol, "name": name, "base": base, "quote": quote,
        "as_of": now, "nzt_offset": ronedge.NZT_OFFSET_H, "session": session_of(now),
        "price": price, "candles": len(cds),
        "structure": structure, "sessions": sessions, "top_times": top_times, "heatmap": heatmap,
        "volatility": vol, "multitf": mtf, "pattern": pat, "pattern_feed": feed,
        "trend": trend, "fusion": fusion, "rel_vol": relv,
        "briefing": st.get("briefing", {}),
    }


def maybe_build(client, results: Path, now: float | None = None, every_s: float = 90.0) -> dict | None:
    """Throttled entry for the bot loop: rebuild the AUDNZD analysis every `every_s`, write audnzd.json."""
    now = now or time.time()
    st = _state.setdefault("_run", {"last": 0.0})
    if now - st["last"] < every_s:
        return None
    st["last"] = now
    import json, os
    payload = build(client, results, now=now)
    try:
        out = results / "audnzd.json"
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str))
        os.replace(tmp, out)
    except Exception:
        pass
    return payload
