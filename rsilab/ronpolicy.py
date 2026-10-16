"""RON's self-learning trading policy.

This is the "point of difference": RON does not trade a fixed rulebook. After every REAL
closed trade (the broker's realised outcome — not RON's optimistic paper call) RON updates
a per-instrument policy and changes how it trades next:

  * conviction floor   — how sure RON must be before it opens. Ratchets UP after losses,
                         eases back toward base when it's winning again.
  * require_trend      — RON turns its OWN trend filter on when counter-trend entries bleed,
                         and off again when they stop bleeding. The user never has to touch it.
  * blocked direction  — if one side (buys or sells) keeps losing, RON stops taking that side
                         for a probation window, then tries it again carefully.
  * post-stop cooldown — after a stop RON waits before re-entering; consecutive stops lengthen
                         the wait. This is what kills the flip-flop churn.
  * circuit breaker    — if an instrument is deeply net-negative with a poor hit-rate, RON
                         pauses it entirely for a spell, then resumes at a raised conviction.

Everything is derived from a rolling window of real closes and persisted to
results/ron_policy.json, so the learning survives restarts. It is honest, rules-based
adaptation — not a black box, not magic — and every adjustment is explained in plain English
on the dashboard so you can see exactly what RON learned and why.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# ---- tunables (sane defaults; all per-instrument) -----------------------------------------
WINDOW = 20            # how many recent real closes we learn from, per instrument
BASE_CONV_STEP = 0.03  # raise the conviction floor by this much after a bad patch
EASE_STEP = 0.02       # relax it back toward base when winning
CONV_CAP = 0.90        # never demand more than this
SHORT_LOOK = 8         # "recent patch" length for the conviction ratchet
CT_MIN = 4             # min counter-trend closes before we judge the trend filter
CT_WINRATE_ON = 0.45   # counter-trend win-rate below this → turn trend filter ON
CT_WINRATE_OFF = 0.55  # counter-trend win-rate back above this → allow it OFF again
DIR_LOOK = 4           # a direction's last-N closes drive the direction block
COOLDOWN_BASE = 60.0   # seconds to wait after a stop before re-entering
COOLDOWN_MAX = 600.0   # cap on the escalating cooldown
BLOCK_SECS = 1800.0    # how long a losing direction stays blocked (probation)
BREAKER_LOOK = 15      # window for the circuit breaker
BREAKER_NET = -300.0   # net over that window below this (account currency) → pause instrument
BREAKER_WINRATE = 0.35 # ...and win-rate below this
BREAKER_SECS = 3600.0  # how long an instrument stays paused
TP_MIN_WINNERS = 10    # need this many winning trades before RON trusts a learned profit-take level
TP_PCTILE = 0.5        # learn the level where winners typically peak (median of their MFE, in R)
TP_FLOOR_R = 1.0       # never bank earlier than this many R


def _winrate(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    wins = sum(1 for r in rows if (r.get("pnl") or 0) > 0)
    return wins / len(rows)


def _net(rows: list[dict]) -> float:
    return sum(float(r.get("pnl") or 0) for r in rows)


class RonPolicy:
    def __init__(self, path: Path, base_conv: float = 0.72):
        self.path = Path(path)
        self.base_conv = float(base_conv)
        # per-symbol learned state + a rolling window of recent real closes
        self.state: dict[str, dict] = {}
        self.closes: dict[str, list[dict]] = {}
        self.entry_ctx: dict[str, dict] = {}   # context captured at entry, read at close
        self.lessons: list[dict] = []          # human-readable log of what RON changed
        self._load()

    # ---- persistence ----------------------------------------------------------------------
    def _blank(self) -> dict:
        return {"conv_floor": self.base_conv, "require_trend": False,
                "blocked_dir": None, "blocked_until": 0.0,
                "cooldown_until": 0.0, "cooldown_streak": 0,
                "paused_until": 0.0, "win_rate": 0.0, "net": 0.0, "n": 0, "tp_r": None}

    def _sym(self, sym: str) -> dict:
        if sym not in self.state:
            self.state[sym] = self._blank()
            self.closes.setdefault(sym, [])
        return self.state[sym]

    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
            self.state = d.get("state", {}) or {}
            self.closes = d.get("closes", {}) or {}
            self.lessons = d.get("lessons", []) or []
            # fill any missing keys introduced by later versions
            for sym, st in self.state.items():
                base = self._blank()
                base.update(st)
                self.state[sym] = base
        except Exception:
            self.state, self.closes, self.lessons = {}, {}, []

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"state": self.state, "closes": self.closes,
                                       "lessons": self.lessons[-60:], "saved": time.time()}))
            os.replace(tmp, self.path)
        except Exception:
            pass

    def _note(self, sym: str, msg: str) -> None:
        self.lessons.append({"t": time.time(), "symbol": sym, "msg": msg})
        del self.lessons[:-60]

    # ---- entry gate -----------------------------------------------------------------------
    def gate(self, sym: str, direction: str, conv: float, mtf_state: str | None,
             now: float | None = None) -> tuple[bool, str]:
        """Should RON open this call right now? Returns (allow, reason-if-blocked)."""
        now = now or time.time()
        st = self._sym(sym)
        if now < st.get("paused_until", 0):
            return False, f"paused {int((st['paused_until']-now)/60)}m (circuit breaker)"
        if now < st.get("cooldown_until", 0):
            return False, f"cooldown {int(st['cooldown_until']-now)}s after stop"
        if st.get("blocked_dir") and direction == st["blocked_dir"] and now < st.get("blocked_until", 0):
            return False, f"{direction}s blocked (losing streak)"
        if (conv or 0) < st.get("conv_floor", self.base_conv):
            return False, f"conv {conv:.2f} < floor {st['conv_floor']:.2f}"
        if st.get("require_trend") and mtf_state:
            if (direction == "long" and mtf_state == "aligned down") or \
               (direction == "short" and mtf_state == "aligned up"):
                return False, "counter-trend (RON's trend filter)"
        return True, ""

    # ---- lifecycle hooks ------------------------------------------------------------------
    def on_entry(self, sym: str, direction: str, mtf_state: str | None,
                 now: float | None = None) -> None:
        self.entry_ctx[sym] = {"dir": direction, "mtf": mtf_state, "t": now or time.time()}

    def on_close(self, sym: str, side: str, pnl: float, reason: str,
                 now: float | None = None, mfe_r: float | None = None) -> None:
        """Feed a REAL realised close in and re-learn. side is 'buy'/'sell'.
        mfe_r = how far the trade ran in our favour before it closed, in R (risk multiples).
        That's what RON learns its profit-taking level from."""
        now = now or time.time()
        st = self._sym(sym)
        ctx = self.entry_ctx.pop(sym, {})
        direction = ctx.get("dir") or ("long" if side == "buy" else "short")
        row = {"t": now, "dir": direction, "mtf": ctx.get("mtf"),
               "pnl": round(float(pnl or 0), 2), "reason": reason,
               "win": (float(pnl or 0) > 0),
               "mfe_r": (round(float(mfe_r), 2) if mfe_r is not None else None)}
        self.closes.setdefault(sym, []).append(row)
        del self.closes[sym][:-WINDOW * 3]  # keep the most recent, bounded history
        self._relearn(sym, row, now)
        self._save()

    # ---- the learning itself --------------------------------------------------------------
    def _relearn(self, sym: str, last: dict, now: float) -> None:
        st = self._sym(sym)
        rows = self.closes[sym][-WINDOW:]
        st["n"] = len(rows)
        st["win_rate"] = round(_winrate(rows), 3)
        st["net"] = round(_net(rows), 2)

        # 1) post-stop cooldown — escalates with consecutive stops, resets on a win.
        # A break-even exit is neutral: it protected the trade, so it never escalates the cooldown.
        reason_l = (last.get("reason") or "").lower()
        is_be = "break-even" in reason_l or "breakeven" in reason_l
        is_stop = (not is_be) and (("stop" in reason_l) or (last.get("pnl", 0) < 0))
        if is_be:
            st["cooldown_streak"] = 0
            st["cooldown_until"] = 0.0
            self._note(sym, "closed at break-even — protected it, no damage done")
        elif last.get("win"):
            if st["cooldown_streak"]:
                self._note(sym, "win banked — back to my normal patience")
            st["cooldown_streak"] = 0
            st["cooldown_until"] = 0.0
        elif is_stop:
            st["cooldown_streak"] = min(st["cooldown_streak"] + 1, 6)
            wait = min(COOLDOWN_BASE * (2 ** (st["cooldown_streak"] - 1)), COOLDOWN_MAX)
            st["cooldown_until"] = now + wait
            self._note(sym, f"that's {st['cooldown_streak']} stop{'s' if st['cooldown_streak']>1 else ''} in a row — I'm standing back {int(wait)}s before I take it again")

        # 2) conviction floor — ratchet up on a losing patch, ease back when winning
        recent = self.closes[sym][-SHORT_LOOK:]
        if len(recent) >= SHORT_LOOK:
            prev = st["conv_floor"]
            if _net(recent) < 0 and _winrate(recent) < 0.5:
                st["conv_floor"] = round(min(st["conv_floor"] + BASE_CONV_STEP, CONV_CAP), 3)
                if st["conv_floor"] > prev:
                    self._note(sym, f"my last {SHORT_LOOK} netted {_net(recent):+.0f} — raising my bar to {st['conv_floor']:.2f}, only stronger setups now")
            elif _net(recent) > 0 and _winrate(recent) >= 0.55 and st["conv_floor"] > self.base_conv:
                st["conv_floor"] = round(max(st["conv_floor"] - EASE_STEP, self.base_conv), 3)
                self._note(sym, f"I'm winning again — easing my bar back to {st['conv_floor']:.2f}")

        # 3) trend filter — RON turns its own on/off from counter-trend performance
        ct = [r for r in rows if (r.get("dir") == "long" and r.get("mtf") == "aligned down")
              or (r.get("dir") == "short" and r.get("mtf") == "aligned up")]
        if len(ct) >= CT_MIN:
            wr = _winrate(ct)
            if not st["require_trend"] and wr < CT_WINRATE_ON:
                st["require_trend"] = True
                self._note(sym, f"my counter-trend trades are only winning {wr*100:.0f}% ({len(ct)} of them) — I've stopped fighting the trend, but I'm watching for it to prove itself")
            elif st["require_trend"] and wr >= CT_WINRATE_OFF:
                st["require_trend"] = False
                self._note(sym, f"counter-trend's back to {wr*100:.0f}% — I'll take those setups again")

        # 4) direction block — stop taking a side that keeps losing
        for d, side_name in (("long", "long"), ("short", "short")):
            dl = [r for r in self.closes[sym] if r.get("dir") == d][-DIR_LOOK:]
            if len(dl) >= DIR_LOOK and _winrate(dl) == 0.0 and _net(dl) < 0:
                if st.get("blocked_dir") != d or now >= st.get("blocked_until", 0):
                    verb = "buying" if d == "long" else "selling"
                    st["blocked_dir"] = d
                    st["blocked_until"] = now + BLOCK_SECS
                    self._note(sym, f"my last {DIR_LOOK} {side_name}s all lost — I've stopped {verb} for {int(BLOCK_SECS/60)}m, watching for the turn")
        # clear an expired block
        if st.get("blocked_dir") and now >= st.get("blocked_until", 0):
            self._note(sym, f"{st['blocked_dir']} probation's over — I'll take them again, carefully")
            st["blocked_dir"], st["blocked_until"] = None, 0.0

        # 5) circuit breaker — pause a deeply bleeding instrument, resume pickier
        br = self.closes[sym][-BREAKER_LOOK:]
        if len(br) >= BREAKER_LOOK and _net(br) < BREAKER_NET and _winrate(br) < BREAKER_WINRATE \
                and now >= st.get("paused_until", 0):
            st["paused_until"] = now + BREAKER_SECS
            st["conv_floor"] = round(min(st["conv_floor"] + BASE_CONV_STEP * 2, CONV_CAP), 3)
            self._note(sym, f"I've bled {_net(br):+.0f} over {BREAKER_LOOK} at {_winrate(br)*100:.0f}% — I'm standing aside {int(BREAKER_SECS/60)}m, then only premium setups (bar {st['conv_floor']:.2f})")

        # 6) learned profit-taking — once RON has enough WINNERS, learn where they typically peak
        # (median favourable excursion, in R) and bank there. Until then, leave it to the trailing stop.
        winners_mfe = sorted(float(r["mfe_r"]) for r in self.closes[sym]
                             if r.get("win") and r.get("mfe_r") is not None)
        if len(winners_mfe) >= TP_MIN_WINNERS:
            idx = min(len(winners_mfe) - 1, int(TP_PCTILE * len(winners_mfe)))
            learned = max(TP_FLOOR_R, round(winners_mfe[idx], 2))
            prev = st.get("tp_r")
            st["tp_r"] = learned
            if prev is None:
                self._note(sym, f"I've learned my winners usually peak around {learned:.1f}R — I'll start banking profit there instead of giving it all back")
            elif abs((prev or 0) - learned) >= 0.3:
                self._note(sym, f"updating where I bank profit: winners now peak around {learned:.1f}R")

    def _stance(self, sym: str, st: dict, now: float) -> str:
        """RON's plain-English read of where it stands on this instrument right now."""
        if now < st.get("paused_until", 0):
            return f"I'm standing aside — it was bleeding. Back in {int((st['paused_until']-now)/60)}m, pickier."
        if now < st.get("cooldown_until", 0):
            return f"Just came out of a trade — I'm waiting {int(st['cooldown_until']-now)}s before I take it again."
        bits = [f"I need {st.get('conv_floor', self.base_conv):.2f} conviction"]
        if st.get("require_trend"):
            bits.append("the trend has to agree")
        if st.get("blocked_dir"):
            bits.append(f"I'm not {'buying' if st['blocked_dir']=='long' else 'selling'} right now")
        return "Watching for a setup — " + ", ".join(bits) + " before I act."

    # ---- dashboard snapshot ---------------------------------------------------------------
    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        instruments = []
        for sym, st in self.state.items():
            instruments.append({
                "symbol": sym,
                "stance": self._stance(sym, st, now),
                "conv_floor": round(st.get("conv_floor", self.base_conv), 2),
                "base_conv": round(self.base_conv, 2),
                "require_trend": bool(st.get("require_trend")),
                "blocked_dir": st.get("blocked_dir"),
                "paused": now < st.get("paused_until", 0),
                "paused_in_s": max(0, int(st.get("paused_until", 0) - now)),
                "cooldown_in_s": max(0, int(st.get("cooldown_until", 0) - now)),
                "cooldown_streak": st.get("cooldown_streak", 0),
                "win_rate": st.get("win_rate", 0.0),
                "net": st.get("net", 0.0),
                "n": st.get("n", 0),
                "tp_r": st.get("tp_r"),
            })
        instruments.sort(key=lambda r: r["symbol"])
        return {"base_conv": round(self.base_conv, 2),
                "instruments": instruments,
                "lessons": list(reversed(self.lessons[-24:]))}
