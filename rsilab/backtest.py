"""Realistic backtest of the RSI-on-Heikin-Ashi strategy over MetaApi history.

Unlike TradingView's Heikin Ashi backtest (which fills at HA candle prices that
don't exist), this fills every trade at the REAL next-bar open — the honest price
you'd actually get. It's an always-in-the-market flip strategy: long on RSI
cross-up through `oversold`, short on cross-down through `overbought`.

Outputs: a plain-English summary, a per-session/per-hour breakdown (RON's future
input), a CSV of every trade, and an HTML report with the equity curve.

Not financial advice. Demo/backtest only.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .ha_rsi import Candle, heikin_ashi, signals, to_candles, wilder_rsi
from .instruments import display_name
from .metaapi import MetaApiClient, MetaApiError

# MetaApi timeframe -> seconds per bar (for pagination + session stats)
TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


@dataclass
class Trade:
    side: str          # "long" / "short"
    entry_t: str
    entry_px: float
    exit_t: str
    exit_px: float
    points: float      # signed price move captured (exit-entry for long, entry-exit for short)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_day(s: str, end: bool = False) -> datetime:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt + timedelta(hours=23, minutes=59) if end else dt


def fetch_history(client: MetaApiClient, symbol: str, timeframe: str,
                  start: datetime, end: datetime, log=print, page: int = 1000,
                  workers: int = 8) -> list[Candle]:
    """Fetch MetaApi historical candles over start..end, in PARALLEL windows.

    Instead of walking one page at a time, we split the range into ~`page`-bar
    checkpoints and fetch them concurrently, then dedupe + sort + trim. Far faster,
    and it naturally handles a broker that only serves a limited slice of history."""
    from concurrent.futures import ThreadPoolExecutor

    step = TF_SECONDS.get(timeframe, 900)
    chunk = timedelta(seconds=step * page)
    # checkpoints across the window (each asks for up to `page` bars from that time)
    checkpoints, t = [], start
    while t < end:
        checkpoints.append(t)
        t += chunk
    if not checkpoints:
        checkpoints = [start]

    raw: list[dict] = []
    seen: set[str] = set()
    empties = 0

    def pull(ts: datetime):
        try:
            batch, _ = client.history(symbol, timeframe, _iso(ts), limit=page)
            return batch or []
        except MetaApiError:
            return []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for batch in pool.map(pull, checkpoints):
            added = 0
            for r in batch:
                tt = str(r.get("time") or r.get("brokerTime") or "")
                if tt and tt not in seen:
                    seen.add(tt); raw.append(r); added += 1
            empties = empties + 1 if added == 0 else 0
            if raw:
                log(f"    …{len(raw)} candles so far")

    cs = to_candles(raw)

    def in_win(c: Candle) -> bool:
        try:
            tt = datetime.fromisoformat(c.t.replace("Z", "+00:00"))
        except ValueError:
            return True
        return start <= tt <= end
    return [c for c in cs if in_win(c)]


def session_of(iso_t: str) -> str:
    """Rough FX session by UTC hour (good enough for a first cut; RON refines later)."""
    try:
        h = datetime.fromisoformat(iso_t.replace("Z", "+00:00")).hour
    except ValueError:
        return "?"
    if 0 <= h < 7:
        return "Asian"
    if 7 <= h < 12:
        return "London"
    if 12 <= h < 16:
        return "London/NY overlap"
    if 16 <= h < 21:
        return "New York"
    return "Sydney"


def simulate(candles: list[Candle], length: int, oversold: float, overbought: float) -> list[Trade]:
    """Always-in-market flip. A signal on bar i (close) fills at bar i+1's REAL open."""
    ha = heikin_ashi(candles)
    rsi = wilder_rsi([c.c for c in ha], length)
    sig = signals(rsi, oversold, overbought)
    trades: list[Trade] = []
    pos = 0          # +1 long, -1 short, 0 flat
    entry_px = 0.0
    entry_t = ""
    for i in range(len(candles) - 1):
        s = sig[i]
        if s is None:
            continue
        want = 1 if s == "long" else -1
        if want == pos:
            continue
        fill = candles[i + 1].o            # real next-bar open
        fill_t = candles[i + 1].t
        if pos != 0:                        # close the open trade first
            pts = (fill - entry_px) if pos > 0 else (entry_px - fill)
            trades.append(Trade("long" if pos > 0 else "short", entry_t, entry_px, fill_t, fill, pts))
        pos, entry_px, entry_t = want, fill, fill_t
    return trades


def report(symbol: str, timeframe: str, candles: list[Candle], trades: list[Trade],
           length: int, oversold: float, overbought: float, contract_value: float) -> dict:
    wins = [t for t in trades if t.points > 0]
    losses = [t for t in trades if t.points <= 0]
    gross_win = sum(t.points for t in wins)
    gross_loss = sum(t.points for t in losses)  # <= 0
    net = gross_win + gross_loss
    pf = (gross_win / abs(gross_loss)) if gross_loss else float("inf")
    win_rate = (len(wins) / len(trades) * 100) if trades else 0.0
    # equity curve + max drawdown (in points)
    eq, peak, mdd = 0.0, 0.0, 0.0
    curve = []
    for t in trades:
        eq += t.points
        curve.append((t.exit_t, round(eq, 2)))
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    # session breakdown
    sess: dict[str, list[float]] = {}
    for t in trades:
        sess.setdefault(session_of(t.entry_t), []).append(t.points)
    return {
        "symbol": symbol, "name": display_name(symbol), "timeframe": timeframe,
        "params": {"length": length, "oversold": oversold, "overbought": overbought},
        "bars": len(candles),
        "from": candles[0].t if candles else None, "to": candles[-1].t if candles else None,
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "net_points": round(net, 2), "gross_win": round(gross_win, 2), "gross_loss": round(gross_loss, 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else None,
        "avg_win": round(gross_win / len(wins), 4) if wins else 0.0,
        "avg_loss": round(gross_loss / len(losses), 4) if losses else 0.0,
        "max_drawdown_points": round(mdd, 2),
        "net_usd_est": round(net * contract_value, 2),
        "contract_value": contract_value,
        "curve": curve,
        "sessions": {k: {"trades": len(v), "net_points": round(sum(v), 2),
                         "win_rate": round(sum(1 for x in v if x > 0) / len(v) * 100, 1)}
                     for k, v in sess.items()},
        "_trades": trades,
    }


def write_csv(path, trades: list[Trade]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["side", "entry_time", "entry_px", "exit_time", "exit_px", "points", "session"])
        for t in trades:
            w.writerow([t.side, t.entry_t, t.entry_px, t.exit_t, t.exit_px,
                        round(t.points, 4), session_of(t.entry_t)])


def write_html(path, rep: dict) -> None:
    """A self-contained dark report with stat tiles, the equity curve, and session breakdown."""
    curve = rep["curve"]
    # build the equity SVG path
    svg = '<div class="empty">no trades</div>'
    if len(curve) >= 2:
        ys = [c[1] for c in curve]
        lo, hi = min(ys + [0]), max(ys + [0])
        rng = (hi - lo) or 1
        W, H = 1000, 260
        pts = []
        for i, (_, y) in enumerate(curve):
            x = i / (len(curve) - 1) * W
            yy = H - (y - lo) / rng * H
            pts.append(f"{x:.1f},{yy:.1f}")
        zero_y = H - (0 - lo) / rng * H
        up = ys[-1] >= 0
        col = "#3fd68a" if up else "#ff5d6c"
        svg = (f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" class="eq">'
               f'<line x1="0" y1="{zero_y:.1f}" x2="{W}" y2="{zero_y:.1f}" class="zero"/>'
               f'<polyline points="{" ".join(pts)}" fill="none" stroke="{col}" stroke-width="2"/>'
               f'</svg>')
    def tile(label, val, sub=""):
        return f'<div class="tile"><div class="tl">{label}</div><div class="tv">{val}</div><div class="ts">{sub}</div></div>'
    pf = rep["profit_factor"]
    pf_s = "∞" if pf is None else f"{pf:.2f}"
    net_cls = "pos" if rep["net_points"] >= 0 else "neg"
    tiles = "".join([
        tile("Net (points)", f'<span class="{net_cls}">{rep["net_points"]:+.2f}</span>',
             f'≈ {rep["net_usd_est"]:+,.2f} USD @ {rep["contract_value"]}/pt'),
        tile("Trades", rep["trades"], f'{rep["wins"]}W / {rep["losses"]}L'),
        tile("Win rate", f'{rep["win_rate"]}%'),
        tile("Profit factor", pf_s),
        tile("Max drawdown", f'{rep["max_drawdown_points"]:.2f} pts'),
        tile("Avg win / loss", f'{rep["avg_win"]:+.3f} / {rep["avg_loss"]:+.3f}'),
    ])
    srows = "".join(
        f'<tr><td>{k}</td><td class="r">{v["trades"]}</td>'
        f'<td class="r {"pos" if v["net_points"]>=0 else "neg"}">{v["net_points"]:+.2f}</td>'
        f'<td class="r">{v["win_rate"]}%</td></tr>'
        for k, v in sorted(rep["sessions"].items(), key=lambda kv: -kv[1]["net_points"])
    ) or '<tr><td class="empty" colspan="4">—</td></tr>'
    trows = "".join(
        f'<tr><td>{t.entry_t[:16].replace("T"," ")}</td>'
        f'<td class="side-{t.side}">{t.side.upper()}</td>'
        f'<td class="r">{t.entry_px:g}</td><td class="r">{t.exit_px:g}</td>'
        f'<td class="r {"pos" if t.points>0 else "neg"}">{t.points:+.3f}</td></tr>'
        for t in rep["_trades"][-500:]
    ) or '<tr><td class="empty" colspan="5">no trades</td></tr>'
    p = rep["params"]
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Backtest · {rep['name']} {rep['timeframe']}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{--bg:#0a0e14;--card:#0e131a;--line:#1a222d;--fg:#e8eef5;--muted:#93a1b0;--dim:#5b6b7c;--good:#3fd68a;--bad:#ff5d6c;--mono:'SF Mono',ui-monospace,Menlo,monospace}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;padding:22px}}
h1{{font-size:20px;margin:0 0 2px}}.sub{{color:var(--muted);font:12px var(--mono);margin-bottom:18px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}}
.tl{{font:600 10px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}}
.tv{{font:800 24px var(--mono);margin:4px 0 2px;letter-spacing:-.02em}}.ts{{font:11px var(--mono);color:var(--muted)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:16px}}
.eyebrow{{font:600 10px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--dim);margin-bottom:10px}}
.eq{{width:100%;height:260px;display:block}}.zero{{stroke:var(--line);stroke-width:1}}
table{{width:100%;border-collapse:collapse;font:12px var(--mono)}}
th{{color:var(--dim);text-align:left;font-weight:400;padding:6px 8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--card)}}
td{{padding:6px 8px;border-bottom:1px solid rgba(26,34,45,.5);white-space:nowrap}}
td.r,th.r{{text-align:right}}.pos{{color:var(--good)}}.neg{{color:var(--bad)}}
.side-long{{color:var(--good)}}.side-short{{color:var(--bad)}}.empty{{color:var(--dim)}}
.scroll{{max-height:420px;overflow:auto}}.warn{{color:#f5b53d;font:12px var(--mono);margin-top:6px}}
</style></head><body>
<h1>Backtest · {rep['name']} · {rep['timeframe']} · Heikin Ashi RSI</h1>
<div class="sub">RSI({p['length']}) · long&gt;{p['oversold']} · short&lt;{p['overbought']} · {rep['bars']} bars · {str(rep['from'])[:10]} → {str(rep['to'])[:10]}</div>
<div class="tiles">{tiles}</div>
<div class="card"><div class="eyebrow">Equity curve (cumulative points)</div>{svg}
<div class="warn">Realistic fills at real next-bar open — not Heikin Ashi prices. Demo/backtest only · not financial advice.</div></div>
<div class="card"><div class="eyebrow">By session (entry time, UTC)</div>
<table><thead><tr><th>session</th><th class="r">trades</th><th class="r">net pts</th><th class="r">win %</th></tr></thead><tbody>{srows}</tbody></table></div>
<div class="card"><div class="eyebrow">Trades (last 500)</div><div class="scroll">
<table><thead><tr><th>entry</th><th>side</th><th class="r">entry px</th><th class="r">exit px</th><th class="r">points</th></tr></thead><tbody>{trows}</tbody></table></div></div>
</body></html>"""
    with open(path, "w") as f:
        f.write(html)

