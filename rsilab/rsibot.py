"""The live RSI-on-Heikin-Ashi bot, trading NDQ100 / Gold / HK50 (and any MetaApi
symbol) on the Eightcap demo through MetaApi.

Signal (per instrument, evaluated on BAR CLOSE of the chosen timeframe):
  * pull recent candles, build Heikin Ashi, compute RSI(length)
  * long  when RSI crosses UP   through `oversold`
  * short when RSI crosses DOWN through `overbought`
  * always in the market: it flips. (Reproduces TradingView's built-in RSI Strategy.)

Reused from the jev bot, unchanged in spirit:
  * MetaApi market/limit execution, reconcile-before-trade (never stack)
  * quick-win take-profit / small-loss stop-loss per position
  * dashboard with the buys/sells/both switches, close-all / close-buys / close-sells
    tiles, live open + closed trades tables, and click-to-switch chart
  * DEMO unless METAAPI_ACCOUNT_TYPE=live + JEV_LIVE_CONFIRM; STOP kill switch; lot cap.

Demo only until proven. Not financial advice.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .core import RESULTS, console, header
from .ha_rsi import heikin_ashi, signals, to_candles, wilder_rsi
from .persist import append_jsonl, load_jsonl, read_json, write_json
from .ronagents import MultiTFAgent, PatternAgent, SessionsAgent, VolatilityAgent
from .ronedge import build_report as build_edge_report
from .ronhistory import HistoryStore
from .ronpolicy import RonPolicy
from .ronpatterns import PatternRecorder
from .ronfusion import compute as fusion_compute, nudge as fusion_nudge
from .ronfollow import TrendRecorder
from .coach import run_coach
from .instruments import SYMBOL_VARIANTS, display_name
from .metaapi import MetaApiClient, MetaApiError, MetaApiMarket, metaapi_mode
from .roncal import RonLedger
from .ronbrain import RonBrain
from .ronwatch import Watcher
from .server import serve

ROOT = Path(__file__).resolve().parent.parent
STOP_FILE = ROOT / "STOP"
FLATTEN_FILE = ROOT / "FLATTEN"
FLATTEN_BUYS_FILE = ROOT / "FLATTEN_BUYS"
FLATTEN_SELLS_FILE = ROOT / "FLATTEN_SELLS"
PRIMARY_FILE = ROOT / "PRIMARY"
DIRECTION_FILE = ROOT / "DIRECTION"

TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


def canonical_of(broker_symbol: str) -> str | None:
    for canon, variants in SYMBOL_VARIANTS.items():
        if broker_symbol == canon or broker_symbol in variants:
            return canon
    return None


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


class SymbolEngine:
    """One instrument: live price feed (dashboard), RSI-HA signal, and execution."""

    def __init__(self, client: MetaApiClient, symbol: str, max_lots: float, timeframe: str,
                 length: int, oversold: float, overbought: float, g: dict, direction: str,
                 drive: str = "rsi", ron_every_min: float = 5.0, ron_confirmed: bool = False):
        self.client, self.symbol, self.max_lots = client, symbol, max_lots
        self.name = display_name(symbol)
        self.timeframe, self.length, self.oversold, self.overbought = timeframe, length, oversold, overbought
        self.g, self.direction = g, direction
        self.drive = drive  # "rsi" = RSI-HA crosses set the target; "ron" = RON's read sets it
        self.ron = RonBrain(symbol, ron_every_min, require_confirmed=ron_confirmed) if drive == "ron" else None
        self.market = MetaApiMarket(client, symbol, poll_s=float(os.getenv("METAAPI_POLL_S", "1.0")))
        self.lock = threading.Lock()
        self.fills: list[dict] = []
        self.decisions: list[dict] = []
        self.pos_lots = 0.0
        self.unrealized = 0.0
        self.desired = 0            # +1 long / -1 short / 0 flat (target from the strategy)
        self.rsi: float | None = None
        self.last_signal: str | None = None
        self.last_cross_t: str | None = None
        self.last_fetch_ms: int | None = None
        self.last_trade_t = 0.0
        self.seq = 0
        self.ha_recent: list[dict] = []   # recent Heikin Ashi candles for the dashboard
        self.last_closed = None           # last CLOSED raw candle, for the history store

    def start(self, stop: threading.Event) -> None:
        self.market.start()
        if self.ron is not None:
            self.ron.start(stop)

    def pos_sign(self) -> int:
        return 1 if self.pos_lots > 1e-9 else -1 if self.pos_lots < -1e-9 else 0

    def flatten(self) -> None:
        try:
            self.client.flatten(self.symbol)
        except MetaApiError as exc:
            console.print(f"  [#ff5d6c]couldn't close {self.symbol}: {exc}[/]")
        self.pos_lots = 0.0

    # ---- signal -------------------------------------------------------------
    def poll_signal(self) -> None:
        """Recompute HA+RSI from recent candles; set the desired side from the last CLOSED bar."""
        t0 = time.time()
        limit = max(200, self.length + 80)
        try:
            raw = self.client.candles(self.symbol, self.timeframe, limit=limit)
        except Exception as exc:
            self._log_check(None, f"candle fetch failed: {str(exc)[:60]}")
            return
        candles = to_candles(raw)
        if len(candles) < self.length + 3:
            self._log_check(None, f"only {len(candles)} candles")
            return
        ha = heikin_ashi(candles)
        rsivals = wilder_rsi([c.c for c in ha], self.length)
        sig = signals(rsivals, self.oversold, self.overbought)
        closed = len(candles) - 1  # candles[-1] may still be forming; act on closed bars only
        self.last_closed = candles[closed - 1] if closed >= 1 else None  # last fully-closed raw candle
        desired, last_sig, last_cross = self.desired, self.last_signal, self.last_cross_t
        for i in range(1, closed):
            if sig[i] == "long":
                desired, last_sig, last_cross = 1, "long", candles[i].t
            elif sig[i] == "short":
                desired, last_sig, last_cross = -1, "short", candles[i].t
        rsi_now = rsivals[closed - 1] if 0 <= closed - 1 < len(rsivals) else (rsivals[-1] if rsivals else None)
        self.last_fetch_ms = round((time.time() - t0) * 1000)
        # keep the last ~60 Heikin Ashi candles for the dashboard chart
        ha_recent = [{"t": ha[i].t, "o": round(ha[i].o, 3), "h": round(ha[i].h, 3),
                      "l": round(ha[i].l, 3), "c": round(ha[i].c, 3),
                      "rsi": round(rsivals[i], 1) if rsivals[i] is not None else None}
                     for i in range(max(0, len(ha) - 60), len(ha))]
        with self.lock:
            # RSI sets the trade target only in rsi-drive mode; in ron mode RON sets it (below),
            # but we still compute RSI + HA candles here so the chart and signal panel stay live.
            if self.drive == "rsi":
                self.desired = desired
            self.last_signal, self.last_cross_t, self.rsi = last_sig, last_cross, rsi_now
            self.ha_recent = ha_recent
        self._log_check(rsi_now, None)

    def _log_check(self, rsi: float | None, note: str | None) -> None:
        self.seq += 1
        snap = self.market.snapshot() or {}
        side = "buy" if self.desired > 0 else "sell" if self.desired < 0 else None
        if rsi is not None and side:
            conf = 0.5 + 0.5 * (_clamp((rsi - 50) / 50) if side == "buy" else _clamp((50 - rsi) / 50))
        else:
            conf = None
        rec = {
            "block": self.seq, "side": side, "conf": round(conf, 3) if conf is not None else None,
            "status": "ok" if note is None else "error", "ms": self.last_fetch_ms,
            "t": time.time(), "mid": snap.get("mid"),
            "state": {**(snap.get("state") or {}),
                      "rsi": round(rsi, 1) if rsi is not None else None,
                      "oversold": self.oversold, "overbought": self.overbought,
                      "signal": self.last_signal},
            "action": note or (f"target {'LONG' if self.desired > 0 else 'SHORT' if self.desired < 0 else 'FLAT'}"
                               f" · RSI {round(rsi, 1) if rsi is not None else '–'}"),
        }
        with self.lock:
            self.decisions.append(rec)
            del self.decisions[:-250]

    # ---- execution ----------------------------------------------------------
    def paused(self) -> bool:
        return (ROOT / f"PAUSE_{self.symbol}").exists()

    def step(self) -> None:
        """Reconcile against the broker and trade to match the desired side, honoring the direction filter."""
        if self.g["halted"] or not self.market.market_open:
            return
        if self.paused():  # per-instrument stop: close it once and stay out until resumed
            if self.pos_sign() != 0:
                self.g.setdefault("_close_reason", {})[self.symbol] = "paused"
                self.flatten()
            return
        direction = self.g.get("direction", self.direction)
        want = self.desired
        if want > 0 and direction == "sell":
            want = 0
        if want < 0 and direction == "buy":
            want = 0
        try:
            live_lots, _ = self.client.net_position(self.symbol)
        except MetaApiError:
            live_lots = self.pos_lots
        live = 1 if live_lots > 1e-9 else -1 if live_lots < -1e-9 else 0
        self.pos_lots = live_lots
        if want == live:
            return
        try:
            if live != 0:
                self.g.setdefault("_close_reason", {})[self.symbol] = "signal flip" if want != 0 else "signal exit"
                self.client.flatten(self.symbol)
                self.pos_lots = 0.0
            if want != 0:
                side = "buy" if want > 0 else "sell"
                self.client.market_order(self.symbol, side, self.max_lots)
                self.pos_lots = want * self.max_lots
                mid = (self.market.snapshot() or {}).get("mid")
                self.fills.append({"t": time.time(), "side": side, "px": mid,
                                   "qty": self.max_lots, "kind": "market", "call": self.seq, "wait_s": 0})
                console.print(f"  {time.strftime('%H:%M:%S')}  {self.symbol:<7} RSI {round(self.rsi or 0,1):<5} "
                              f"→  market {side} {self.max_lots:g} lots")
        except MetaApiError as exc:
            console.print(f"  [#ff5d6c]{self.symbol} order rejected: {str(exc)[:70]}[/]")
            return
        self.last_trade_t = time.time()

    # ---- dashboard ----------------------------------------------------------
    def summary(self) -> dict:
        return {"symbol": self.symbol, "name": self.name, "pos": self.pos_sign(), "lots": self.pos_lots,
                "unrealized": round(self.unrealized, 2),
                "bias": ("long" if self.desired > 0 else "short" if self.desired < 0 else "flat"),
                "bias_conf": None, "market_open": self.market.market_open,
                "calls": len(self.decisions), "trades": len(self.fills),
                "last_side": ("buy" if self.desired > 0 else "sell" if self.desired < 0 else None),
                "rsi": round(self.rsi, 1) if self.rsi is not None else None,
                "paused": self.paused(),
                "ron": (self.ron.current() if self.ron is not None else None),
                "avg_ms": self.last_fetch_ms}

    def detail(self) -> dict:
        return {"decisions": self.decisions[-200:], "fills": self.fills[-100:],
                "ha": self.ha_recent,
                "ticks": self.market.recent_ticks(), "counts": {"ok": len(self.decisions), "late": 0,
                "throttled": 0, "error": 0},
                "blocks": len(self.decisions), "rate_per_s": 0.0,
                "last_ms": self.last_fetch_ms, "avg_ms": self.last_fetch_ms,
                "hits": 0, "scored": 0,
                "brain": {"bias": self.last_signal, "reason": f"RSI({self.length}) HA {self.timeframe}",
                          "model": "RSI", "t": time.time(), "degraded": False, "error": None}}


def run_rsibot(symbols: list[str], timeframe: str = "15m", length: int = 2,
               oversold: float = 58, overbought: float = 30, lots: float | None = None,
               direction: str = "both", max_loss: float | None = None,
               take_profit: float | None = None, stop_loss: float | None = None,
               signal_poll_s: float = 20.0, port: int = 8765, open_browser: bool = True,
               minutes: float = 0.0, signal_source: str = "rsi", ron_confirmed: bool = False,
               ron_every_min: float = 5.0, ron_watch_conv: float = 0.70,
               webhook_port: int = 8799, webhook_token: str | None = None,
               trend_filter: bool = False, watcher_exec_conv: float = 0.72) -> None:
    symbols = [s.strip().upper() for s in symbols if s.strip()]
    if not symbols:
        raise SystemExit("  no symbols given")
    timeframe = timeframe.strip().lower()
    if timeframe not in TF_SECONDS:
        raise SystemExit(f"  --timeframe must be one of: {', '.join(TF_SECONDS)}")
    direction = direction if direction in ("both", "buy", "sell") else "both"
    take_profit = float(take_profit if take_profit is not None else os.getenv("TAKE_PROFIT_USD", "25") or 0)
    stop_loss = float(stop_loss if stop_loss is not None else os.getenv("STOP_LOSS_USD", "20") or 0)
    tp_on, sl_on = take_profit > 0, stop_loss > 0
    max_lots = float(lots if lots is not None else os.getenv("MAX_LOTS", "0.01"))
    max_loss = float(max_loss if max_loss is not None else os.getenv("MAX_DAILY_LOSS_USD", "0"))
    loss_on = max_loss > 0
    # --- watcher risk model (Carl's spec): size each trade to a fixed $ risk, move to
    # break-even once +$ in favour, then trail so winners run. All tunable via .env. ---
    RISK_USD = float(os.getenv("RISK_PER_TRADE", "250") or 0)      # $ risked to the stop per trade
    BE_AT_USD = float(os.getenv("BREAKEVEN_AT_USD", "300") or 0)   # +$ in favour → stop jumps to entry
    TRAIL_R = float(os.getenv("TRAIL_R", "1.0") or 0)             # after BE, trail this many R behind the peak
    LOT_CAP = float(os.getenv("LOT_CAP", "0.5") or 0)            # hard safety cap on lots (never blow up)
    SAFE_LOTS = float(os.getenv("SAFE_LOTS", "0.05") or 0)      # fallback when risk can't be sized
    MIN_STOP_PTS = float(os.getenv("MIN_STOP_PTS", "3.0") or 0)  # stop can't be tighter than this (anti-churn)
    try:
        mode = metaapi_mode()
    except MetaApiError as exc:
        raise SystemExit(f"  {exc}")
    mode_label = {"demo": "MetaApi DEMO · fake money", "live": "MetaApi LIVE · REAL MONEY"}[mode]
    names = ", ".join(f"{display_name(s)} ({s})" for s in symbols)
    dir_label = {"both": "buys & sells", "buy": "buys only", "sell": "sells only"}[direction]
    signal_source = signal_source if signal_source in ("rsi", "ron", "webhook", "watcher") else "rsi"
    if signal_source == "watcher":
        # RON executes its OWN high-conviction calls, with its own stop/target. The fixed
        # $ TP/SL is turned off — RON's per-call levels are the only exit.
        tp_on = sl_on = False
        strat_note = f"RON executes its own calls · conviction ≥ {watcher_exec_conv:g} · RON's stop/target"
    elif signal_source == "ron":
        strat_note = f"RON drives · {'confirmed-only' if ron_confirmed else 'any fresh read'} · RSI({length}) HA {timeframe} shown"
    elif signal_source == "webhook":
        strat_note = f"TradingView webhook drives · RSI({length}) HA {timeframe} shown"
    else:
        strat_note = f"RSI({length}) Heikin Ashi {timeframe} · long>{oversold:g} · short<{overbought:g}"
    tpsl = ([f"take-profit ${take_profit:g}"] if tp_on else []) + ([f"stop ${stop_loss:g}"] if sl_on else [])
    size_label = (f"~${RISK_USD:g} risk/trade · BE at +${BE_AT_USD:g} then trail"
                  if signal_source == "watcher" else f"{max_lots:g} lots each")
    header("THE RSI-HA BOT", f"{names} · {mode_label} · {dir_label} · {strat_note} · "
           f"{size_label} · {' · '.join(tpsl) if tpsl else 'no TP/SL'} · "
           f"{'daily loss $%.0f' % max_loss if loss_on else 'no daily loss limit'}")
    if not loss_on:
        console.print("  [#f5b53d]Daily loss limit is OFF. STOP kill switch and per-instrument lot cap still active.[/]")
    if mode == "live":
        console.print("  [bold #ff5d6c]LIVE MODE: real money.[/] Kill switch: create a file named STOP.")

    try:
        client = MetaApiClient()
    except MetaApiError as exc:
        raise SystemExit(f"  MetaApi problem: {exc}\n  (Check METAAPI_TOKEN / METAAPI_ACCOUNT_ID in .env.)")
    # The broker connection can be flaky / still re-establishing — wait and retry rather than dying.
    info, tries = None, 10
    for attempt in range(1, tries + 1):
        try:
            info = client.account_information()
            break
        except MetaApiError as exc:
            msg = str(exc).lower()
            transient = "504" in msg or "not connected" in msg or "timeout" in msg or "502" in msg or "503" in msg
            if transient and attempt < tries:
                console.print(f"  waiting for the broker connection to come up… ({attempt}/{tries})")
                time.sleep(8)
                continue
            raise SystemExit(
                f"  MetaApi problem: {exc}\n"
                f"  The account isn't connected to the broker right now. Open https://app.metaapi.cloud,\n"
                f"  check the account is DEPLOYED and shows 'connected', then run this again.\n"
                f"  If it's on a different region, set METAAPI_REGION in .env (london / new-york / …).")
    currency = info.get("currency", "USD")
    equity_start = float(info.get("equity") or info.get("balance") or 0)
    console.print(f"  MetaApi {mode} account connected · equity {equity_start:,.2f} {currency}")

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "loop.json"
    stop = threading.Event()
    g = {"halted": None, "direction": direction, "open_positions": [], "closed_trades": [],
         "_open_prev": {}, "_close_reason": {}}

    for f in (FLATTEN_FILE, FLATTEN_BUYS_FILE, FLATTEN_SELLS_FILE, PRIMARY_FILE, DIRECTION_FILE):
        f.unlink(missing_ok=True)
    for f in ROOT.glob("PAUSE_*"):  # clear stale per-instrument pauses from a previous run
        f.unlink(missing_ok=True)
    for f in RESULTS.glob("webhook_*.json"):  # don't re-fire a stale webhook alert on boot
        f.unlink(missing_ok=True)

    signal_source = signal_source if signal_source in ("rsi", "ron", "webhook", "watcher") else "rsi"
    ledger = RonLedger(RESULTS / "ron_ledger.jsonl")
    g["ron_ledger"] = ledger
    g["signal_source"] = signal_source
    engines = [SymbolEngine(client, s, max_lots, timeframe, length, oversold, overbought, g, direction,
                            drive=signal_source, ron_every_min=ron_every_min, ron_confirmed=ron_confirmed)
               for s in symbols]
    # RON's independent watcher — always on, advisory only, grades itself so it gets smarter.
    # alerts_path makes its alert history persist to disk and reload on restart.
    watch_ledger = RonLedger(RESULTS / "ron_watch_ledger.jsonl")
    ALERTS_FILE = RESULTS / "ron_alerts.jsonl"
    watchers = [Watcher(e, watch_ledger, min_conv=ron_watch_conv, alerts_path=ALERTS_FILE) for e in engines]
    g["ron_alerts"] = []
    # RON's specialist analyst agents — advisory context (sessions / volatility / multi-timeframe)
    sessions_agent = SessionsAgent()
    vol_agent = VolatilityAgent()
    mtf_agent = MultiTFAgent()
    pattern_agent = PatternAgent()
    pattern_rec = PatternRecorder(RESULTS)  # live pattern feed + grader + session hit-rates
    trend_rec = TrendRecorder(RESULTS)      # trend-onset learner: when real (volume-backed) trends form
    g["ron_sessions"] = {}
    g["ron_volatility"] = {}
    g["ron_multitf"] = {}
    g["ron_patterns"] = {}
    g["ron_pattern_feed"] = {}
    g["trend_skips"] = 0
    # RON EDGE — persistent candle history + statistical-research report (results/edge.json)
    history = HistoryStore(RESULTS)
    EDGE_FILE = RESULTS / "edge.json"
    g["_last_edge"] = 0.0
    # RON COACH (#5) — advisory LLM review on a schedule; off unless OPENAI_API_KEY is set
    COACH_FILE = RESULTS / "coach.json"
    COACH_EVERY_MIN = float(os.getenv("COACH_EVERY_MIN", "30") or 30)
    g["_last_coach"] = 0.0
    # watcher-execution state: RON trades its own calls to their own stop/target, one per instrument
    watcher_by_sym = {w.sym: w for w in watchers}
    wt_state = {e.symbol: {"active": False, "dir": 0, "stop": None, "target": None, "id": None,
                           "entry": None, "be": False, "R": None, "peak": None} for e in engines}
    # RON's self-learning policy — reads every REAL close and adapts its own conviction floor,
    # trend requirement, direction blocks, post-stop cooldown and a circuit breaker. Persists to disk.
    policy = RonPolicy(RESULTS / "ron_policy.json", base_conv=watcher_exec_conv)
    g["ron_policy"] = policy
    # Agent-fusion nudge: strong, accurate agent agreement eases a call slightly; disagreement
    # tightens it. Bounded by FUSION_MAX and fully reversible with FUSION_ENABLED=0.
    FUSION_ENABLED = os.getenv("FUSION_ENABLED", "1").strip().lower() not in ("0", "false", "off", "")
    FUSION_MAX = float(os.getenv("FUSION_MAX", "0.06") or 0.06)

    def _mtf_state(sym: str):
        for row in (g.get("ron_multitf", {}).get("rows", []) or []):
            if row.get("symbol") == sym:
                return row.get("state")
        return None

    def size_for_risk(sym: str, entry: float, stop: float) -> tuple[float, float]:
        """Lots sized so that being stopped loses ~RISK_USD. Returns (lots, est_risk_usd).
        Falls back to a SAFE small size (never oversized) if the broker spec is unavailable."""
        dist = abs((entry or 0) - (stop or 0))
        if RISK_USD <= 0 or dist <= 0:
            return (SAFE_LOTS, 0.0)
        vpp = None
        try:
            vpp = client.value_per_point(sym)
        except Exception:
            vpp = None
        if not vpp or vpp <= 0:
            console.print(f"  [#f5b53d]{sym}: couldn't read contract spec — using safe {SAFE_LOTS:g} lots. "
                          f"Check the first trade's risk on the dashboard.[/]")
            return (SAFE_LOTS, 0.0)
        raw = RISK_USD / (dist * vpp)
        lots = max(0.01, min(round(raw, 2), LOT_CAP))     # clamp: broker min .. hard cap
        est = lots * dist * vpp
        return (lots, round(est, 2))
    if os.getenv("FLATTEN_ON_START", "true").lower() != "false":
        for e in engines:
            try:
                n = e.client.flatten(e.symbol)
            except Exception:
                pass
    for e in engines:
        e.start(stop)

    # TradingView webhook receiver — its own token-protected server on a separate port,
    # bound to 127.0.0.1 (front it with a Cloudflare Tunnel / nginx+TLS; the dashboard stays private).
    if signal_source == "webhook":
        from .webhook import serve_webhook, read_signal
        wh_token = webhook_token or os.getenv("WEBHOOK_TOKEN", "")
        serve_webhook(webhook_port, wh_token, [e.symbol for e in engines],
                      log=lambda m: console.print(f"  [dim]{m}[/]"))
        console.print(f"  webhook receiver on 127.0.0.1:{webhook_port} · POST /webhook · token required")
    webhook_applied: dict[str, float] = {}

    # ---- persistence so the dashboard's history survives restarts -----------------------
    CLOSED_FILE = RESULTS / "closed_trades.jsonl"
    EQUITY_FILE = RESULTS / "equity_curve.jsonl"
    BASELINE_FILE = RESULTS / "equity_baseline.json"
    # A persistent baseline keeps the equity/P&L curve continuous across restarts
    # (equity_start would otherwise reset to the current equity every launch).
    base = read_json(BASELINE_FILE)
    if base and isinstance(base, dict) and base.get("equity_start"):
        baseline = float(base["equity_start"])
    else:
        baseline = equity_start
        write_json(BASELINE_FILE, {"equity_start": baseline, "created": time.time()})
    # reload closed-trade history and equity curve from disk
    g["closed_trades"] = load_jsonl(CLOSED_FILE, 200)
    eq_hist = load_jsonl(EQUITY_FILE, 2400)
    seeded_curve = [[float(p["t"]), round(float(p["equity"]) - baseline, 3),
                     round(float(p["equity"]) - baseline, 3)]
                    for p in eq_hist if p.get("t") is not None and p.get("equity") is not None]

    acct = {"equity": equity_start, "balance": float(info.get("balance") or equity_start),
            "currency": currency, "equity_start": baseline, "day": datetime.now(timezone.utc).date(),
            "day_start_equity": equity_start, "equity_curve": seeded_curve, "_last_eq_persist": 0.0}
    started = time.time()

    def record_close(symbol, side, lots_, pnl, reason):
        rec = {"t": time.time(), "symbol": symbol, "name": display_name(symbol),
               "side": side, "lots": round(abs(float(lots_ or 0)), 2),
               "pnl": round(float(pnl or 0), 2), "reason": reason}
        g["closed_trades"].append(rec)
        del g["closed_trades"][:-200]
        append_jsonl(CLOSED_FILE, rec)  # persist so trade history survives restarts
        if signal_source in ("ron", "watcher"):  # grade RON's decision against the realised outcome
            ledger.grade(symbol, pnl)
        if signal_source == "watcher":  # RON learns from every REAL close and adapts its own policy
            try:
                mfe = g.get("_mfe_r", {}).pop(symbol, None)
                policy.on_close(symbol, side, pnl, reason, time.time(), mfe_r=mfe)
            except Exception:
                pass

    def refresh_account():
        try:
            i = client.account_information()
            acct["equity"] = float(i.get("equity") or i.get("balance") or acct["equity"])
            acct["balance"] = float(i.get("balance") or acct["balance"])
        except MetaApiError:
            pass
        try:
            positions = client.positions()
        except Exception:
            positions = []
        agg = {e.symbol: [0.0, 0.0] for e in engines}
        opens = []
        for p in positions:
            canon = canonical_of(p.get("symbol", ""))
            if canon in agg:
                sign = 1 if p.get("type") == "POSITION_TYPE_BUY" else -1
                agg[canon][0] += sign * float(p.get("volume") or 0)
                agg[canon][1] += float(p.get("profit") or 0)
            opens.append({"id": p.get("id"), "symbol": canon, "name": display_name(canon or ""),
                          "side": "buy" if p.get("type") == "POSITION_TYPE_BUY" else "sell",
                          "lots": round(float(p.get("volume") or 0), 2),
                          "entry": float(p.get("openPrice") or 0),
                          "pnl": round(float(p.get("profit") or 0), 2), "t": p.get("time")})
        opens.sort(key=lambda o: str(o.get("t") or ""))
        cur = {o["id"]: o for o in opens if o.get("id")}
        closed_ids = g.setdefault("_closed_ids", set())
        for pid, inf in g.get("_open_prev", {}).items():
            if pid not in cur and pid not in closed_ids:   # record each real close exactly once
                closed_ids.add(pid)
                reason = g["_close_reason"].pop(inf["symbol"], None) or "closed"
                record_close(inf["symbol"], inf["side"], inf["lots"], inf["pnl"], reason)
        if len(closed_ids) > 3000:
            g["_closed_ids"] = set(list(closed_ids)[-1500:])
        g["_open_prev"] = cur
        g["open_positions"] = opens
        for e in engines:
            e.pos_lots, e.unrealized = agg[e.symbol][0], agg[e.symbol][1]

    def check_wins():
        if g["halted"] or not (tp_on or sl_on):
            return
        for e in engines:
            if e.pos_sign() == 0:
                continue
            u = e.unrealized
            hit = "take-profit" if (tp_on and u >= take_profit) else ("stop-loss" if (sl_on and u <= -stop_loss) else None)
            if not hit:
                continue
            side = "sell" if e.pos_sign() > 0 else "buy"
            qty = abs(e.pos_lots)
            g["_close_reason"][e.symbol] = hit
            try:
                e.client.flatten(e.symbol)
            except MetaApiError:
                continue
            e.pos_lots, e.last_trade_t = 0.0, time.time()
            e.desired = 0  # banked; wait for the next fresh cross before re-entering
            e.fills.append({"t": time.time(), "side": side, "px": (e.market.snapshot() or {}).get("mid"),
                            "qty": qty, "kind": hit, "call": None, "wait_s": 0})
            mark = "✔" if hit == "take-profit" else "✖"
            console.print(f"  {time.strftime('%H:%M:%S')}  {e.symbol:<7} {mark} {hit} {u:+.2f} {currency}")

    def check_watcher_entries():
        """watcher mode: RON opens its own high-conviction call (with its own stop/target)."""
        if signal_source != "watcher" or g["halted"]:
            return
        now = time.time()
        dd = g.get("direction", direction)
        for e in engines:
            st = wt_state[e.symbol]
            if st["active"] or e.pos_sign() != 0 or e.paused() or not e.market.market_open:
                continue
            w = watcher_by_sym.get(e.symbol)
            a = (w.recent[0] if (w and w.recent) else None)
            if not a or a.get("result") is not None:           # only a live, ungraded call
                continue
            if a.get("id") == st["id"]:                         # already evaluated this exact call
                continue
            if now - (a.get("t") or 0) > 90:                    # ignore a stale call
                continue
            dsign = 1 if a["dir"] == "long" else -1
            if (dsign > 0 and dd == "sell") or (dsign < 0 and dd == "buy"):
                st["id"] = a.get("id")                          # respect direction filter; mark seen
                continue
            # RON's self-learning gate: conviction floor, trend requirement, direction block,
            # cooldown and circuit breaker — all adapted from RON's own real results.
            eff_conv = a.get("conv") or 0
            if FUSION_ENABLED:
                _delta = fusion_nudge((g.get("ron_fusion") or {}).get(e.symbol), dsign, FUSION_MAX)
                if _delta:
                    eff_conv = max(0.0, min(1.0, eff_conv + _delta))
            allow, why = policy.gate(e.symbol, a["dir"], eff_conv, _mtf_state(e.symbol), now)
            if not allow:
                st["id"] = a.get("id")                          # mark seen so we don't spam-evaluate
                console.print(f"  [dim]{time.strftime('%H:%M:%S')}  {e.symbol:<7} RON held back: {why}[/]")
                continue
            side = "buy" if dsign > 0 else "sell"
            entry_px = (e.market.snapshot() or {}).get("mid") or a.get("entry")
            # anti-churn: never let the stop be tighter than MIN_STOP_PTS, or spread eats it instantly
            stop_px = a.get("stop")
            if entry_px and stop_px and abs(entry_px - stop_px) < MIN_STOP_PTS:
                stop_px = round(entry_px - MIN_STOP_PTS if dsign > 0 else entry_px + MIN_STOP_PTS, 2)
            lots, est_risk = size_for_risk(e.symbol, entry_px, stop_px)
            try:
                e.client.flatten(e.symbol)
                # place the initial stop at the broker too — a hard backstop that caps the ~$risk
                # even if the bot is ever down; the BE + trail then tighten it in software.
                e.client.market_order(e.symbol, side, lots, stop_loss=stop_px)
            except MetaApiError as exc:
                console.print(f"  [#ff5d6c]{e.symbol} RON entry rejected: {str(exc)[:60]}[/]")
                continue
            e.pos_lots, e.last_trade_t = dsign * lots, now
            R = abs((entry_px or 0) - (stop_px or 0)) or None
            st.update(active=True, dir=dsign, stop=stop_px, target=a.get("target"),
                      id=a.get("id"), entry=entry_px, be=False, R=R, peak=entry_px)
            g.setdefault("_mfe_r", {})[e.symbol] = 0.0  # reset favourable-excursion tracker
            e.fills.append({"t": now, "side": side, "px": entry_px,
                            "qty": lots, "kind": "RON entry", "call": None, "wait_s": 0})
            ledger.log_decision(e.symbol, a["dir"], "watch", None, a.get("conv"))
            policy.on_entry(e.symbol, a["dir"], _mtf_state(e.symbol), now)  # context for learning on close
            console.print(f"  {time.strftime('%H:%M:%S')}  {e.symbol:<7} RON {side} conv {a.get('conv')} "
                          f"{lots:g} lots (~${est_risk:g} risk) -> stop {a.get('stop')} "
                          f"BE@+${BE_AT_USD:g} then trail")

    def check_watcher_exits():
        """watcher mode: close a RON-executed position when price hits RON's own stop or target."""
        if signal_source != "watcher" or g["halted"]:
            return
        for e in engines:
            st = wt_state[e.symbol]
            if not st["active"]:
                continue
            if e.pos_sign() == 0:   # closed elsewhere (manual/flatten) — clear the state
                st["active"] = False
                continue
            snap = e.market.snapshot() or {}
            mid = snap.get("mid")
            if not mid or st["stop"] is None:
                continue
            d = st["dir"]
            R = st.get("R")
            # track how far this trade has run in our favour (max favourable excursion), in R —
            # this is what RON learns its profit-taking level from.
            if R and st.get("entry") is not None:
                st["peak"] = (max(st["peak"], mid) if d > 0 else min(st["peak"], mid)) if st.get("peak") is not None else mid
                mfe_r = (st["peak"] - st["entry"]) * d / R
                g.setdefault("_mfe_r", {})[e.symbol] = round(mfe_r, 2)
            # break-even by DOLLARS: once the open trade is +BE_AT_USD in our favour, pull the stop
            # to entry so it can't become a loss (Carl: "always be in a profitable situation").
            if not st.get("be") and BE_AT_USD > 0 and e.unrealized >= BE_AT_USD:
                st["stop"], st["be"] = st["entry"], True
                console.print(f"  {time.strftime('%H:%M:%S')}  {e.symbol:<7} ⇧ break-even locked "
                              f"@ {st['entry']:.2f} (+${e.unrealized:.0f}) — can't lose from here, letting it run")
            # after break-even, trail the stop behind the best price. Once the trade reaches the
            # level where RON has learned its winners usually peak (tp_r), tighten the trail hard so
            # it banks most of the move — but still lets a runner run (RON-learned profit-taking, #6).
            if st.get("be") and R:
                tp_r = (policy.state.get(e.symbol, {}) or {}).get("tp_r")
                prog_r = (mid - st["entry"]) * d / R
                mult = 0.3 if (tp_r and prog_r >= tp_r) else TRAIL_R  # tight trail at the learned peak
                trail = (st["peak"] - mult * R) if d > 0 else (st["peak"] + mult * R)
                st["stop"] = max(st["entry"], trail) if d > 0 else min(st["entry"], trail)
            hit_s = (mid <= st["stop"]) if d > 0 else (mid >= st["stop"])
            if not hit_s:
                continue
            reason = ("trailing profit" if (st.get("be") and e.unrealized > 1)
                      else "break-even" if st.get("be") else "RON stop")
            g["_close_reason"][e.symbol] = reason
            try:
                e.client.flatten(e.symbol)
            except MetaApiError:
                continue
            e.pos_lots, e.last_trade_t = 0.0, time.time()
            e.desired = 0
            st["active"] = False
            mark = "✔" if (reason != "RON stop") else "✖"
            console.print(f"  {time.strftime('%H:%M:%S')}  {e.symbol:<7} {mark} {reason} @ {mid:.2f} "
                          f"({e.unrealized:+.0f})")

    def risk_check():
        if g["halted"]:
            return
        today = datetime.now(timezone.utc).date()
        if today != acct["day"]:
            acct["day"], acct["day_start_equity"] = today, acct["equity"]
        if loss_on and acct["equity"] - acct["day_start_equity"] < -max_loss:
            g["halted"] = f"daily loss limit (${max_loss:,.0f})"
            console.print(f"\n  [bold #f5b53d]HALTED: {g['halted']}[/]. Flattening.")
            for e in engines:
                e.flatten()
        elif STOP_FILE.exists():
            g["halted"] = "kill switch (STOP file)"
            console.print("\n  [bold #f5b53d]HALTED: STOP file[/]. Flattening.")
            for e in engines:
                e.flatten()

    def write(status: str):
        primary = engines[0]
        try:
            if PRIMARY_FILE.exists():
                want = PRIMARY_FILE.read_text().strip()
                primary = next((e for e in engines if e.symbol == want), engines[0])
        except Exception:
            pass
        net = acct["equity"] - acct["equity_start"]
        acct["equity_curve"].append([round(time.time(), 2), round(net, 3), round(net, 3)])
        del acct["equity_curve"][:-2400]
        # persist the equity point (throttled ~30s) so the P&L curve survives restarts
        nowt = time.time()
        if nowt - acct.get("_last_eq_persist", 0) > 30:
            append_jsonl(EQUITY_FILE, {"t": round(nowt, 1), "equity": round(acct["equity"], 2)})
            acct["_last_eq_persist"] = nowt
        det = primary.detail()
        label = mode_label + (f" · HALTED: {g['halted']}" if g["halted"]
                              else "" if primary.market.market_open else " · MARKET CLOSED")
        payload = {
            "status": status, "venue": "metaapi", "symbol": primary.symbol, "coin": primary.symbol,
            "venue_label": f"{primary.name} · MetaApi", "mode": mode, "mode_label": label,
            "market_open": primary.market.market_open, "pace": signal_poll_s, "interval": signal_poll_s,
            "rate_per_s": 0.0, "late_ms": 0, "strategy_note": strat_note,
            "execution": "market", "maker_wait": 0, "started": started, "updated": time.time(),
            "model": f"RSI({length}) · HA {timeframe}", "notional": max_lots, "unit": "lots",
            "currency": acct["currency"], "maker_fee_bps": 0, "taker_fee_bps": 0,
            "counts": det["counts"], "blocks": det["blocks"], "last_ms": det["last_ms"], "avg_ms": det["avg_ms"],
            "hits": det["hits"], "scored": det["scored"], "brain": det["brain"], "order": None,
            "equity": round(acct["equity"], 2), "brain_on": False, "brain_source": "off",
            "direction": g.get("direction", direction), "strategy_mode": "rsi",
            "signal_source": signal_source, "ron_calibration": ledger.snapshot(),
            "ron_alerts": g.get("ron_alerts", []),
            "ron_watch_calibration": watch_ledger.snapshot(),
            "ron_sessions": g.get("ron_sessions", {}),
            "ron_volatility": g.get("ron_volatility", {}),
            "ron_multitf": g.get("ron_multitf", {}),
            "ron_patterns": g.get("ron_patterns", {}),
            "ron_pattern_feed": g.get("ron_pattern_feed", {}),
            "ron_fusion": g.get("ron_fusion", {}),
            "ron_trend": g.get("ron_trend", {}),
            "trend_filter": bool(trend_filter), "trend_skips": g.get("trend_skips", 0),
            "ron_policy": policy.snapshot(),
            "ron_coach": read_json(COACH_FILE) or {},
            "open_positions": g.get("open_positions", []),
            "closed_trades": g.get("closed_trades", [])[-100:][::-1],
            "balance": round(acct["balance"], 2), "decisions": det["decisions"], "fills": det["fills"],
            "book": {"pos": primary.pos_sign(), "qty": primary.pos_lots, "gross": round(net, 3), "fees": 0.0,
                     "net": round(net, 3), "trades": len(primary.fills), "equity": acct["equity_curve"]},
            "ticks": det["ticks"], "ha_candles": det.get("ha", []),
            "instruments": [e.summary() for e in engines],
        }
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str))
        os.replace(tmp, out)

    def writer():
        tick = 0
        while not stop.is_set():
            try:
                try:
                    d = DIRECTION_FILE.read_text().strip() if DIRECTION_FILE.exists() else direction
                    g["direction"] = d if d in ("both", "buy", "sell") else direction
                except Exception:
                    pass
                if FLATTEN_FILE.exists():
                    for e in engines:
                        g["_close_reason"][e.symbol] = "manual close"
                        try:
                            e.client.flatten(e.symbol); e.pos_lots = 0.0; e.desired = 0
                        except Exception:
                            pass
                    FLATTEN_FILE.unlink(missing_ok=True)
                    console.print("  ⚑ close-all from dashboard")
                for sentinel, side, want_sign, lbl in ((FLATTEN_BUYS_FILE, "buy", 1, "BUYS"),
                                                       (FLATTEN_SELLS_FILE, "sell", -1, "SELLS")):
                    if sentinel.exists():
                        for e in engines:
                            if e.pos_sign() == want_sign:
                                g["_close_reason"][e.symbol] = f"manual close {lbl.lower()}"; e.desired = 0
                        try:
                            res = engines[0].client.close_all(side=side)
                        except Exception as exc:
                            res = {"error": str(exc)[:80]}
                        for e in engines:
                            if e.pos_sign() == want_sign:
                                e.pos_lots = 0.0
                        sentinel.unlink(missing_ok=True)
                        refresh_account()
                        console.print(f"  ⚑ close-all {lbl} — {res}")
                holding = any(e.pos_sign() for e in engines)
                if tick % (1 if holding else 3) == 0:
                    refresh_account()
                check_wins()
                check_watcher_exits()
                risk_check()
                if tick % 2 == 0:  # RON watches continuously (~2s), advisory only
                    for w in watchers:
                        try:
                            w.assess()
                        except Exception:
                            pass
                    alerts = sorted((a for w in watchers for a in w.recent),
                                    key=lambda a: a["t"], reverse=True)[:30]
                    g["ron_alerts"] = alerts
                    check_watcher_entries()  # RON opens its own high-conviction calls (watcher mode)
                write("running")
            except Exception as exc:
                console.print(f"  [dim]status update skipped: {str(exc)[:80]}[/]")
            tick += 1
            stop.wait(1.0)

    threading.Thread(target=writer, daemon=True).start()
    serve(port, open_browser, page="home.html")  # new consolidated front page (old dash at /dashboard/loop.html)
    console.print(f"  [dim]{len(engines)} markets · RSI on {timeframe} bars · signals every ~{signal_poll_s:g}s[/]")

    # signal + execution loop
    try:
        with ThreadPoolExecutor(max_workers=max(2, len(engines))) as pool:
            while not minutes or time.time() - started < minutes * 60:
                list(pool.map(lambda e: e.poll_signal(), engines))  # RSI/HA + chart, always
                for e in engines:
                    if signal_source == "ron" and e.ron is not None:
                        r = e.ron.current()
                        b = r.get("bias")
                        e.desired = 1 if b == "long" else -1 if b == "short" else 0
                    elif signal_source == "webhook":
                        got = read_signal(RESULTS, e.symbol)  # apply each fresh alert once
                        if got:
                            dsign, ts = got
                            if ts > webhook_applied.get(e.symbol, 0.0):
                                webhook_applied[e.symbol] = ts
                                # trend filter: skip a signal that fights the multi-TF trend
                                conflict = False
                                if trend_filter and dsign != 0:
                                    for row in (g.get("ron_multitf", {}).get("rows", []) or []):
                                        if row.get("symbol") == e.symbol:
                                            st = row.get("state")
                                            if (dsign > 0 and st == "aligned down") or (dsign < 0 and st == "aligned up"):
                                                conflict = True
                                            break
                                if conflict:
                                    g["trend_skips"] = g.get("trend_skips", 0) + 1
                                    e.desired = 0  # stay flat rather than trade against the trend
                                    console.print(f"  [#f5b53d]trend-filter: skipped {e.symbol} "
                                                  f"{'long' if dsign > 0 else 'short'} (fights multi-TF trend)[/]")
                                else:
                                    e.desired = dsign
                    if signal_source != "watcher":  # watcher mode: entries/exits handled in the writer thread
                        e.step()
                    # log RON's decision the moment a RON-driven position is open and not yet on the ledger
                    if signal_source == "ron" and e.ron is not None and e.pos_sign() != 0 and not ledger.is_open(e.symbol):
                        r = e.ron.current()
                        ledger.log_decision(e.symbol, r.get("direction") or ("long" if e.pos_sign() > 0 else "short"),
                                            r.get("ron_state"), r.get("as_of"), r.get("raw_conf"))
                # RON analyst agents — advisory context, refreshed each signal pass
                try:
                    g["ron_sessions"] = sessions_agent.assess(g.get("closed_trades", []))
                    g["ron_volatility"] = vol_agent.assess(engines)
                    g["ron_multitf"] = mtf_agent.assess(client, engines)  # multi-TF self-throttles its API calls
                    g["ron_patterns"] = pattern_agent.assess(engines)
                    # Pattern Engine: record every fresh pattern RON spots, grade the older ones.
                    for e in engines:
                        res = pattern_agent.assess_one(e)
                        px = (e.market.snapshot() or {}).get("mid") or getattr(e.last_closed, "c", None)
                        if res and px:
                            pattern_rec.observe(e.symbol, e.name, res.get("pattern"), res.get("structure"),
                                                res.get("note"), px)
                            pattern_rec.grade(e.symbol, px)
                    g["ron_pattern_feed"] = pattern_rec.summary()
                    # Trend learning: relative VOLUME (tick volume, throttled+guarded) → record/grade
                    # trend onsets so the Trend agent learns WHEN real, volume-backed trends form.
                    _psym = engines[0].symbol if engines else None
                    try:
                        if _psym and time.time() - g.get("_last_relvol", 0) > 60:
                            g["_last_relvol"] = time.time()
                            _rows = client.candles(_psym, "3m", 40)
                            _vols = [float(c.get("tickVolume") or c.get("volume") or 0) for c in (_rows or [])]
                            _vols = [v for v in _vols if v > 0]
                            if len(_vols) >= 8:
                                import statistics as _st
                                _med = _st.median(_vols[:-1]) or 1.0
                                g["_relvol_val"] = round(_vols[-1] / _med, 2) if _med else None
                    except Exception:
                        pass
                    _tvol = g.get("_relvol_val")
                    if _tvol is None:
                        _vr = next((r for r in (g.get("ron_volatility") or {}).get("rows", []) if r.get("symbol") == _psym), None)
                        _tvol = _vr.get("range_ratio") if _vr else None
                    for e in engines:
                        _mtf = next((r for r in (g.get("ron_multitf") or {}).get("rows", []) if r.get("symbol") == e.symbol), None)
                        _px = (e.market.snapshot() or {}).get("mid") or getattr(e.last_closed, "c", None)
                        if _mtf:
                            trend_rec.observe(e.symbol, _mtf.get("state"), _px, _tvol)
                            trend_rec.grade(e.symbol, _px)
                    g["ron_trend"] = trend_rec.summary(_psym)
                    # Fusion: weight each agent by its scoreboard accuracy, fuse into one read.
                    _sc = read_json(RESULTS / "meeting_scores.json") or {}
                    g["_briefing"] = read_json(RESULTS / "briefing.json") or g.get("_briefing") or {}
                    g["ron_fusion"] = fusion_compute(g, _sc, [e.symbol for e in engines])
                except Exception as exc:
                    console.print(f"  [dim]ron agents skipped: {str(exc)[:70]}[/]")
                # RON EDGE: record each new closed candle, then rebuild the research report (throttled)
                try:
                    for e in engines:
                        if e.last_closed is not None:
                            history.record(e.symbol, e.last_closed)
                    if time.time() - g.get("_last_edge", 0) > 90:
                        _edge_rep = build_edge_report(history, engines)
                        write_json(EDGE_FILE, _edge_rep)
                        g["_edge_report"] = _edge_rep
                        g["_last_edge"] = time.time()
                except Exception as exc:
                    console.print(f"  [dim]ron edge skipped: {str(exc)[:70]}[/]")
                # RON COACH: advisory LLM review every COACH_EVERY_MIN, in a background thread so
                # it never blocks trading. Only runs if an API key is set; writes results/coach.json.
                try:
                    if os.getenv("OPENAI_API_KEY", "").strip() and \
                            time.time() - g.get("_last_coach", 0) > COACH_EVERY_MIN * 60:
                        g["_last_coach"] = time.time()
                        threading.Thread(target=lambda: run_coach(RESULTS), daemon=True).start()
                except Exception as exc:
                    console.print(f"  [dim]ron coach skipped: {str(exc)[:70]}[/]")
                # RON NEWS & MACRO: hourly gold briefing + real scheduled-events (US dollar / CPI).
                # maybe_run self-throttles (hourly + a 9am NZT morning brief); we gate thread churn
                # to ~5 min and run it in the background so it never blocks trading.
                try:
                    if time.time() - g.get("_last_news", 0) > 300:
                        g["_last_news"] = time.time()
                        rep = g.get("_edge_report") or {}
                        _prim = engines[0].symbol if engines else None
                        _instr = (rep.get("instruments") or {}).get(_prim) or {}
                        _st = _instr.get("structure") or {}
                        _pc = {"price": _st.get("price"), "trend": _st.get("trend"),
                               "support": (_st.get("support") or {}).get("level") if _st.get("support") else None,
                               "resistance": (_st.get("resistance") or {}).get("level") if _st.get("resistance") else None,
                               "session": (rep.get("now") or {}).get("session"),
                               "recent_sessions": ((g.get("ron_sessions") or {}).get("by_symbol", {}).get(_prim, {}) or {}).get("rows")}
                        from .ronnews import maybe_run as _news_run
                        from .ronmeeting import maybe_meet as _meet_run
                        threading.Thread(target=lambda: (_news_run(RESULTS, _pc), _meet_run(RESULTS, g)), daemon=True).start()
                except Exception as exc:
                    console.print(f"  [dim]ron news skipped: {str(exc)[:70]}[/]")
                # AUDNZD analysis (no trading) — same engines on the cross's candles, in the
                # background so it never blocks gold. Writes results/audnzd.json for its own page.
                try:
                    if time.time() - g.get("_last_pair", 0) > 90:
                        g["_last_pair"] = time.time()
                        from .ronpair import maybe_build as _pair_build
                        threading.Thread(target=lambda: _pair_build(client, RESULTS), daemon=True).start()
                except Exception as exc:
                    console.print(f"  [dim]ron pair skipped: {str(exc)[:70]}[/]")
                stop.wait(signal_poll_s)
    except KeyboardInterrupt:
        pass
    stop.set()
    for e in engines:
        e.market.stop()
    if os.getenv("FLATTEN_ON_EXIT", "true").lower() != "false":
        for e in engines:
            e.flatten()
    console.print("\n  session over · equity {:.2f} {}\n".format(acct["equity"], currency))
