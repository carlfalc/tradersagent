"""rsilab command line.

  uv run python -m rsilab backtest --symbols NDQ100,XAUUSD,HK50 --timeframe 15m \
      --from 2026-04-01 --to 2026-09-29

Flags:
  --symbols       comma list of canonical symbols (default NDQ100,XAUUSD,HK50)
  --timeframe     1m | 5m | 15m | 30m | 1h | 4h | 1d   (default 15m)
  --from --to     YYYY-MM-DD window (default: last 90 days)
  --length        RSI length (default 2)
  --oversold      long when RSI crosses up through this (default 58)
  --overbought    short when RSI crosses down through this (default 30)
  --contract-value  $ per 1.0 price move per lot, for the USD estimate (default 1.0)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .backtest import (TF_SECONDS, fetch_history, report, simulate,
                       write_csv, write_html)
from .instruments import display_name
from .metaapi import MetaApiClient, MetaApiError
from .rsibot import run_rsibot

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"


def _day(s: str, end: bool = False) -> datetime:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt + timedelta(hours=23, minutes=59) if end else dt


def cmd_backtest(a) -> None:
    tf = a.timeframe.strip().lower()
    if tf not in TF_SECONDS:
        raise SystemExit(f"  --timeframe must be one of: {', '.join(TF_SECONDS)}")
    end = _day(a.to, end=True) if a.to else datetime.now(timezone.utc)
    start = _day(a.frm) if a.frm else end - timedelta(days=90)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    try:
        client = MetaApiClient()
    except MetaApiError as exc:
        raise SystemExit(f"  MetaApi problem: {exc}\n  (Check METAAPI_TOKEN / METAAPI_ACCOUNT_ID / METAAPI_REGION in .env.)")
    RESULTS.mkdir(exist_ok=True)

    print(f"\n  RSI-HA backtest · {tf} · {start:%Y-%m-%d} → {end:%Y-%m-%d}")
    print(f"  RSI({a.length}) · long>{a.oversold} · short<{a.overbought} · realistic fills (real next-bar open)\n")
    for sym in symbols:
        print(f"  {display_name(sym)} ({sym}) — fetching history…")
        try:
            candles = fetch_history(client, sym, tf, start, end, log=print)
        except MetaApiError as exc:
            print(f"    ! {sym}: {exc}\n")
            continue
        if len(candles) < a.length + 5:
            print(f"    ! {sym}: only {len(candles)} candles returned — the broker may not have that "
                  f"history for {tf}. Try a shorter range or a higher timeframe.\n")
            continue
        cov0, cov1 = str(candles[0].t)[:10], str(candles[-1].t)[:10]
        print(f"    got {len(candles)} candles covering {cov0} → {cov1}")
        if cov0 > start.strftime("%Y-%m-%d"):
            print(f"    (broker only served history back to {cov0} for {tf} — that's its limit, not a bug)")
        trades = simulate(candles, a.length, a.oversold, a.overbought)
        rep = report(sym, tf, candles, trades, a.length, a.oversold, a.overbought, a.contract_value)
        base = RESULTS / f"backtest_{sym}_{tf}"
        write_csv(base.with_suffix(".csv"), trades)
        write_html(base.with_suffix(".html"), rep)
        pf = "∞" if rep["profit_factor"] is None else rep["profit_factor"]
        print(f"    {rep['bars']} bars · {rep['trades']} trades · win {rep['win_rate']}% · "
              f"net {rep['net_points']:+.2f} pts (≈{rep['net_usd_est']:+,.0f} USD) · PF {pf} · "
              f"maxDD {rep['max_drawdown_points']:.2f} pts")
        best = max(rep["sessions"].items(), key=lambda kv: kv[1]["net_points"], default=None)
        if best:
            print(f"    best session: {best[0]} ({best[1]['net_points']:+.2f} pts, {best[1]['win_rate']}% win)")
        print(f"    report: {base.with_suffix('.html')}\n")
    print("  Done. Open the .html reports to see equity curves & session breakdowns.")
    print("  Reminder: realistic fills here will differ from TradingView's Heikin-Ashi backtest.\n")


def cmd_backfill(a) -> None:
    """Seed RON EDGE's history store with past candles so the stats have data today."""
    from .backtest import TF_SECONDS, fetch_history
    from .ronhistory import HistoryStore
    tf = a.timeframe.strip().lower()
    if tf not in TF_SECONDS:
        raise SystemExit(f"  --timeframe must be one of: {', '.join(TF_SECONDS)}")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=a.days)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    try:
        client = MetaApiClient()
    except MetaApiError as exc:
        raise SystemExit(f"  MetaApi problem: {exc}")
    RESULTS.mkdir(exist_ok=True)
    store = HistoryStore(RESULTS)
    print(f"\n  RON EDGE backfill · {tf} · last {a.days} days\n")
    for sym in symbols:
        print(f"  {display_name(sym)} ({sym}) — fetching…")
        try:
            candles = fetch_history(client, sym, tf, start, end, log=print)
        except MetaApiError as exc:
            print(f"    ! {sym}: {exc}\n")
            continue
        total = store.bulk_merge(sym, candles)
        print(f"    added {len(candles)} candles · history now {total} rows\n")
    print("  Done. Refresh RON EDGE — best-times, sessions and the heatmap should now have data.\n")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="rsilab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("backfill", help="seed RON EDGE history with past candles")
    bf.add_argument("--symbols", default="NDQ100,XAUUSD,HK50")
    bf.add_argument("--timeframe", default="1h")
    bf.add_argument("--days", type=int, default=45)
    bt = sub.add_parser("backtest", help="realistic backtest of the RSI-HA strategy")
    bt.add_argument("--symbols", default="NDQ100,XAUUSD,HK50")
    bt.add_argument("--timeframe", default="15m")
    bt.add_argument("--from", dest="frm", default=None)
    bt.add_argument("--to", dest="to", default=None)
    bt.add_argument("--length", type=int, default=2)
    bt.add_argument("--oversold", type=float, default=58)
    bt.add_argument("--overbought", type=float, default=30)
    bt.add_argument("--contract-value", type=float, default=1.0, dest="contract_value")

    bot = sub.add_parser("bot", help="live RSI-HA bot on the Eightcap demo, with dashboard")
    bot.add_argument("--symbols", default="XAUUSD")  # Gold-only focus (HK50/NDQ100 dropped from execution)
    bot.add_argument("--timeframe", default="15m")
    bot.add_argument("--length", type=int, default=2)
    bot.add_argument("--oversold", type=float, default=58)
    bot.add_argument("--overbought", type=float, default=30)
    bot.add_argument("--lots", type=float, default=None)
    bot.add_argument("--direction", default="both", choices=["both", "buy", "sell"])
    bot.add_argument("--max-loss", type=float, default=None, dest="max_loss")
    bot.add_argument("--take-profit", type=float, default=None, dest="take_profit")
    bot.add_argument("--stop-loss", type=float, default=None, dest="stop_loss")
    bot.add_argument("--signal-poll", type=float, default=20.0, dest="signal_poll",
                     help="seconds between signal re-checks (default 20)")
    bot.add_argument("--signal", default="rsi", choices=["rsi", "ron", "webhook", "watcher"],
                     help="what drives trades: rsi (RSI-HA crosses), ron (GAINEDGE RON's read), "
                          "webhook (TradingView alerts), or watcher (RON executes its own calls w/ its stop+target)")
    bot.add_argument("--ron-confirmed", action="store_true", dest="ron_confirmed",
                     help="RON mode: only trade RON's fresh+confirmed setups (default: any fresh directional read)")
    bot.add_argument("--ron-every", type=float, default=5.0, dest="ron_every",
                     help="RON mode: minutes between RON reads (default 5)")
    bot.add_argument("--ron-watch-conv", type=float, default=0.70, dest="ron_watch_conv",
                     help="min conviction (0-1) for RON's watcher to fire an alert (default 0.70)")
    bot.add_argument("--webhook-port", type=int, default=8799, dest="webhook_port",
                     help="port for the TradingView webhook receiver (127.0.0.1 only; default 8799)")
    bot.add_argument("--webhook-token", default=None, dest="webhook_token",
                     help="shared secret for the webhook (default: WEBHOOK_TOKEN in .env)")
    bot.add_argument("--trend-filter", action="store_true", dest="trend_filter",
                     help="skip a signal that fights the multi-timeframe trend (counter-trend guard)")
    bot.add_argument("--watcher-conv", type=float, default=0.72, dest="watcher_exec_conv",
                     help="watcher mode: min RON conviction (0-1) to actually open a trade (default 0.72)")
    bot.add_argument("--minutes", type=float, default=0.0, help="0 = run until stopped (Ctrl+C)")
    bot.add_argument("--port", type=int, default=8766)  # 8766 so it never clashes with the jev bot on 8765
    bot.add_argument("--no-open", action="store_true", help="don't auto-open the dashboard")

    a = ap.parse_args(argv)
    if a.cmd == "backfill":
        cmd_backfill(a)
    elif a.cmd == "backtest":
        cmd_backtest(a)
    elif a.cmd == "bot":
        run_rsibot([s for s in a.symbols.split(",")], timeframe=a.timeframe, length=a.length,
                   oversold=a.oversold, overbought=a.overbought, lots=a.lots, direction=a.direction,
                   max_loss=a.max_loss, take_profit=a.take_profit, stop_loss=a.stop_loss,
                   signal_poll_s=a.signal_poll, port=a.port, open_browser=not a.no_open, minutes=a.minutes,
                   signal_source=a.signal, ron_confirmed=a.ron_confirmed, ron_every_min=a.ron_every,
                   ron_watch_conv=a.ron_watch_conv, webhook_port=a.webhook_port,
                   webhook_token=a.webhook_token, trend_filter=a.trend_filter,
                   watcher_exec_conv=a.watcher_exec_conv)


if __name__ == "__main__":
    main(sys.argv[1:])
