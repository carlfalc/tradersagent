"""Heikin Ashi candles + Wilder RSI + the RSI Strategy signals.

This reproduces TradingView's built-in "RSI Strategy" exactly, as applied to a
Heikin Ashi chart:

    length     = 2      (RSI period)
    overSold   = 58     (long when RSI crosses UP through this)
    overBought = 30     (short when RSI crosses DOWN through this)

    vrsi = ta.rsi(close, length)          # close = HA close on a HA chart
    if ta.crossover(vrsi, overSold):   -> go long
    if ta.crossunder(vrsi, overBought):-> go short   (always in the market: it flips)

Everything here is pure functions over a list of OHLC candles, so it can be unit
tested and used by both the backtest and the live engine. Signals are evaluated on
BAR CLOSE (TradingView default, calc_on_every_tick = false).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Candle:
    t: str            # ISO time of the bar
    o: float
    h: float
    l: float
    c: float


def to_candles(raw: list[dict]) -> list[Candle]:
    """MetaApi historical candles -> Candle list, sorted oldest-first."""
    out = []
    for r in raw:
        try:
            out.append(Candle(
                t=str(r.get("time") or r.get("brokerTime") or ""),
                o=float(r["open"]), h=float(r["high"]),
                l=float(r["low"]), c=float(r["close"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda c: c.t)
    return out


def heikin_ashi(candles: list[Candle]) -> list[Candle]:
    """Standard Heikin Ashi transform.
        HA_close = (O+H+L+C)/4
        HA_open  = (prev_HA_open + prev_HA_close)/2   (seed: (O+C)/2)
        HA_high  = max(H, HA_open, HA_close)
        HA_low   = min(L, HA_open, HA_close)
    """
    ha: list[Candle] = []
    for i, c in enumerate(candles):
        ha_close = (c.o + c.h + c.l + c.c) / 4.0
        if i == 0:
            ha_open = (c.o + c.c) / 2.0
        else:
            ha_open = (ha[-1].o + ha[-1].c) / 2.0
        ha_high = max(c.h, ha_open, ha_close)
        ha_low = min(c.l, ha_open, ha_close)
        ha.append(Candle(t=c.t, o=ha_open, h=ha_high, l=ha_low, c=ha_close))
    return ha


def wilder_rsi(closes: list[float], length: int) -> list[float | None]:
    """Wilder's RSI (RMA smoothing), matching TradingView's ta.rsi().
    Returns a list the same length as `closes`; leading values are None until
    enough data exists. Uses alpha = 1/length (Wilder), seeded with the first
    `length` deltas' simple average, then RMA — this is what TradingView does."""
    n = len(closes)
    rsi: list[float | None] = [None] * n
    if n <= length:
        return rsi
    gains, losses = [], []
    for i in range(1, length + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length
    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        if g == 0:
            return 0.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)
    rsi[length] = _rsi(avg_gain, avg_loss)
    for i in range(length + 1, n):
        d = closes[i] - closes[i - 1]
        gain = max(d, 0.0)
        loss = max(-d, 0.0)
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length
        rsi[i] = _rsi(avg_gain, avg_loss)
    return rsi


def signals(rsi: list[float | None], oversold: float, overbought: float) -> list[str | None]:
    """Per-bar signal from the RSI Strategy rules, evaluated on that bar's close.
        "long"  when RSI crosses UP through oversold
        "short" when RSI crosses DOWN through overbought
        None    otherwise (hold whatever you have)
    A crossover needs the previous bar below/at the level and this bar above it."""
    out: list[str | None] = [None] * len(rsi)
    for i in range(1, len(rsi)):
        prev, cur = rsi[i - 1], rsi[i]
        if prev is None or cur is None:
            continue
        if prev <= oversold and cur > oversold:
            out[i] = "long"
        elif prev >= overbought and cur < overbought:
            out[i] = "short"
    return out
