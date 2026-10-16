"""RON's News & Macro agent — the daily gold briefing + scheduled-events feed.

Two honest sources, combined:

  1. A REAL economic calendar (ForexFactory's free weekly JSON via faireconomy.media) — the
     actual high/medium-impact US events, their scheduled time, forecast and previous. Times
     are converted to NZT. For each we attach a plain if/then: a hot print lifts the US dollar
     and usually presses gold down; a soft print drops the dollar and usually lifts gold.

  2. An LLM macro read (OpenAI, same key as the coach) that frames, in plain English, what the
     US dollar / gold / geopolitics picture means for the next 24 hours across every session —
     seeded with today's real price/structure and the real calendar so it stays grounded.

It writes results/briefing.json, which the front page reads. It runs on an HOURLY cadence (the
US-dollar/news read) with a fuller refresh at ~9am NZT (the morning brief), and from 7am Monday
NZT it frames the weekend's gap risk. It also keeps a small rolling memory of which THEMES it has
been weighting (war, CPI, Fed...), so over time it stays focused on what actually moves gold.

Honest by design: the calendar is real; the narrative is expert framing, not live breaking-news
scraping. If the key or network is unavailable it still writes the real calendar + a plain note.
Advisory only — RON decides. Demo account.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

API_URL = "https://api.openai.com/v1/chat/completions"
NEWS_MODEL = os.getenv("NEWS_MODEL", os.getenv("COACH_MODEL", "gpt-4o-mini"))
CAL_URL = os.getenv("FF_CALENDAR_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
NZT_OFFSET_H = int(os.getenv("NZT_OFFSET_H", "13"))  # NZDT Oct–Apr; +12 in NZ winter

HOURLY_S = 3600.0
SESSION_WINDOWS = [
    ("Sydney", "9:00 am – 6:00 pm"),
    ("Tokyo / Asian", "12:00 pm – 6:00 pm"),
    ("London", "8:00 pm – 4:30 am"),
    ("London / NY overlap", "2:30 am – 5:00 am"),
    ("New York", "2:30 am – 9:00 am"),
]


def _nzt(dt_utc: datetime) -> datetime:
    return dt_utc + timedelta(hours=NZT_OFFSET_H)


def _nzt_str(dt_utc: datetime) -> str:
    d = _nzt(dt_utc)
    h = d.hour % 12 or 12
    ap = "am" if d.hour < 12 else "pm"
    return f"{h}:{d.minute:02d} {ap} NZT"


# ---- real economic calendar ------------------------------------------------------

def _parse_ff_date(s: str):
    """ForexFactory dates look like 2026-10-11T13:30:00-04:00 (or with Z)."""
    try:
        s = s.strip()
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _event_ifthen(title: str) -> tuple[str, str]:
    """Plain if/then for gold, by event type. Hot US data → stronger USD → gold down."""
    t = title.lower()
    inflation = any(k in t for k in ("cpi", "pce", "inflation", "ppi"))
    jobs = any(k in t for k in ("payroll", "nonfarm", "nfp", "unemployment", "jobless", "employment"))
    growth = any(k in t for k in ("gdp", "retail sales", "ism", "pmi", "durable"))
    rates = any(k in t for k in ("rate", "fomc", "fed funds", "interest rate"))
    if inflation:
        return ("Inflation higher than forecast → dollar strengthens → gold likely DOWN. RON trades with-trend short only, small size.",
                "Inflation cooler than forecast → dollar sells off → gold likely UP, often sharply. A prime long setup.")
    if jobs:
        return ("Jobs stronger than forecast → dollar firms → gold likely DOWN.",
                "Jobs weaker than forecast → dollar eases → gold likely UP.")
    if rates:
        return ("A hawkish/higher-rate surprise → dollar up → gold likely DOWN.",
                "A dovish/lower-rate surprise → dollar down → gold likely UP.")
    if growth:
        return ("Stronger growth than forecast → dollar firms → gold likely DOWN.",
                "Weaker growth than forecast → dollar eases → gold likely UP.")
    return ("A stronger-than-forecast US print tends to lift the dollar → gold down.",
            "A weaker-than-forecast US print tends to ease the dollar → gold up.")


def fetch_events(now: float | None = None, hours_ahead: int = 24) -> list[dict]:
    """Real upcoming USD high/medium-impact events in the next `hours_ahead`, NZT-stamped."""
    if requests is None:
        return []
    now = now or time.time()
    try:
        r = requests.get(CAL_URL, timeout=12, headers={"User-Agent": "rsi-bot/1.0"})
        r.raise_for_status()
        rows = r.json()
    except Exception:
        return []
    out = []
    end = now + hours_ahead * 3600
    for ev in rows if isinstance(rows, list) else []:
        try:
            if (ev.get("country") or ev.get("currency")) != "USD":
                continue
            impact = (ev.get("impact") or "").capitalize()
            if impact not in ("High", "Medium"):
                continue
            dt = _parse_ff_date(ev.get("date") or "")
            if not dt:
                continue
            ts = dt.timestamp()
            if ts < now - 1800 or ts > end:  # keep next 24h (+ a 30-min grace)
                continue
            above, below = _event_ifthen(ev.get("title") or "")
            fc = ev.get("forecast") or ""
            prev = ev.get("previous") or ""
            out.append({
                "name": ev.get("title") or "US event",
                "time": _nzt_str(dt),
                "ts": ts,
                "impact": "HIGH IMPACT" if impact == "High" else "MEDIUM",
                "forecast": (f"Forecast {fc}" + (f" · previous {prev}" if prev else "")) if fc else (f"Previous {prev}" if prev else ""),
                "above": above, "below": below,
            })
        except Exception:
            continue
    out.sort(key=lambda e: e["ts"])
    return out[:6]


# ---- LLM macro narrative ---------------------------------------------------------

_SCHEMA_HINT = """Return ONLY a JSON object with EXACTLY these keys:
{
 "headline": "one or two sentences: the single most important thing for gold in the next 24h",
 "usd":  {"verdict":"short tag e.g. headwind for gold / tailwind for gold / neutral","tone":"neg|pos|warn","body":"2-3 plain sentences on the US dollar and what it means for gold"},
 "gold": {"verdict":"short tag e.g. range, break-pending / bullish / bearish","tone":"pos|neg|warn","body":"2-3 plain sentences on gold's own drivers and levels"},
 "geo":  {"verdict":"short tag e.g. watch / calm / elevated","tone":"warn|pos|neg","body":"2-3 plain sentences on geopolitics/news that could move gold"},
 "sessions_24h": [ {"session":"Sydney","window":"9:00 am – 6:00 pm","expect":"LEAN LONG|LEAN SHORT|WAIT|QUIET|CAREFUL","tone":"pos|neg|muted","why":"one sentence"} , ... one per session in this order: Sydney, Tokyo / Asian, London, London / NY overlap, New York ],
 "themes": ["short theme tags you are currently weighting, e.g. US dollar, CPI, Fed, Middle East"]
}
No markdown, no commentary outside the JSON."""

_SYS = ("You are RON's macro analyst for spot gold (XAUUSD). Gold is driven most by the US dollar "
        "(DXY, Fed policy, US data like CPI/PCE/NFP, real yields), by risk sentiment, and by geopolitics "
        "(wars, Middle East tension, elections, central-bank buying). A STRONGER US dollar usually pushes "
        "gold DOWN; a weaker dollar usually lifts it. Be honest and plain. Never promise profit or certainty; "
        "note uncertainty. All session times are New Zealand time (NZT). Base your read on the real context "
        "given. "
        "IMPORTANT — WEEKEND: if 'weekend_ahead' or 'monday_open' is true, the gold market is closed or just "
        "reopening. In that case focus the headline and the 'geo' block on what has happened or is pending over "
        "the weekend (geopolitics, conflict, big US-dollar or data news) that could GAP gold at the Monday "
        "Australian-session open (~7am NZT), and state plainly whether it leaves gold susceptible to BUYING "
        "(gap up) or SELLING (gap down), with your reasoning. "
        "Output strictly the requested JSON.")


def _llm_brief(context: dict) -> dict | None:
    key = os.getenv("OPENAI_API_KEY")
    if not key or requests is None:
        return None
    user = ("Here is today's real context (times are NZT):\n"
            + json.dumps(context, default=str, indent=1)
            + "\n\n" + _SCHEMA_HINT)
    body = {"model": NEWS_MODEL, "max_tokens": 700, "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": _SYS},
                         {"role": "user", "content": user}]}
    try:
        r = requests.post(API_URL, timeout=40,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json=body)
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"]
        data = json.loads(txt)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _fallback_brief(events: list[dict], price_ctx: dict) -> dict:
    """Deterministic honest briefing when the LLM is unavailable — real calendar still shows."""
    nxt = events[0]["name"] + " at " + events[0]["time"] if events else "no high-impact US events scheduled"
    return {
        "headline": f"Macro narrative is offline (no AI key / call failed) — but the real calendar is live. Next up: {nxt}.",
        "usd": {"verdict": "see calendar", "tone": "warn",
                "body": "The AI dollar read is off right now. Watch the scheduled US events below — a hotter print tends to lift the dollar and press gold, a softer print tends to lift gold."},
        "gold": {"verdict": "see levels", "tone": "warn",
                 "body": f"Gold is near {price_ctx.get('price','—')} with support/resistance mapped on the market read. Direction likely waits on the next US data."},
        "geo": {"verdict": "manual", "tone": "warn",
                "body": "Geopolitical read needs the AI narrative (or a news feed) — not available right now."},
        "sessions_24h": [{"session": s, "window": w, "expect": "WATCH", "tone": "muted",
                          "why": "AI session outlook is off; trade RON's live session edge."} for s, w in SESSION_WINDOWS],
        "themes": ["US dollar", "US data calendar"],
    }


# ---- public entrypoint -----------------------------------------------------------

def build_briefing(results: Path, price_ctx: dict | None = None, now: float | None = None,
                   morning: bool = False) -> dict:
    now = now or time.time()
    events = fetch_events(now)
    price_ctx = price_ctx or {}
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    weekday = _nzt(dt).weekday()  # 0 Mon .. 6 Sun
    context = {
        "today_nzt": _nzt(dt).strftime("%A %d %B %Y, %H:%M") + " NZT",
        "morning_brief": morning,
        "weekend_ahead": weekday == 4,  # Friday NZT
        "monday_open": weekday == 0,    # Monday NZT
        "gold_price": price_ctx.get("price"),
        "gold_trend": price_ctx.get("trend"),
        "support": price_ctx.get("support"),
        "resistance": price_ctx.get("resistance"),
        "current_session": price_ctx.get("session"),
        "recent_sessions": price_ctx.get("recent_sessions"),
        "real_calendar_next_24h": [{"name": e["name"], "time": e["time"], "impact": e["impact"],
                                     "forecast": e["forecast"]} for e in events],
        "session_windows_nzt": [{"session": s, "window": w} for s, w in SESSION_WINDOWS],
    }
    brief = _llm_brief(context)
    ok = bool(brief)
    if not brief:
        brief = _fallback_brief(events, price_ctx)
    # always attach the REAL calendar events (don't trust the LLM to reproduce them)
    brief["events"] = events
    brief["as_of"] = now
    brief["ok"] = ok
    brief["model"] = NEWS_MODEL if ok else "offline"
    brief["morning"] = morning
    return brief


# ---- generic pair briefing (e.g. AUDNZD) -----------------------------------------

def _llm_json(system: str, user: str, max_tokens: int = 700) -> dict | None:
    key = os.getenv("OPENAI_API_KEY")
    if not key or requests is None:
        return None
    try:
        r = requests.post(API_URL, timeout=40,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"model": NEWS_MODEL, "max_tokens": max_tokens, "temperature": 0.4,
                                "response_format": {"type": "json_object"},
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": user}]})
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception:
        return None


def _pair_ifthen(title: str, ccy: str, base: str, quote: str) -> tuple[str, str]:
    """If/then for a cross pair. A stronger BASE currency lifts the pair; stronger QUOTE sinks it."""
    t = title.lower()
    kind = ("rate decision / policy" if any(k in t for k in ("rate", "rbnz", "rba", "cash rate", "ocr"))
            else "inflation" if any(k in t for k in ("cpi", "inflation", "ppi"))
            else "jobs" if any(k in t for k in ("employ", "job", "unemploy", "payroll"))
            else "growth" if any(k in t for k in ("gdp", "retail", "trade", "pmi", "business"))
            else "data")
    if ccy == base:  # e.g. AUD on AUDNZD
        return (f"Hot {base} {kind} → {base} strengthens → {base}{quote} likely UP.",
                f"Soft {base} {kind} → {base} eases → {base}{quote} likely DOWN.")
    else:            # quote, e.g. NZD on AUDNZD
        return (f"Hot {quote} {kind} → {quote} strengthens → {base}{quote} likely DOWN.",
                f"Soft {quote} {kind} → {quote} eases → {base}{quote} likely UP.")


def fetch_events_for(currencies: list[str], base: str, quote: str,
                     now: float | None = None, hours_ahead: int = 24) -> list[dict]:
    if requests is None:
        return []
    now = now or time.time()
    try:
        r = requests.get(CAL_URL, timeout=12, headers={"User-Agent": "rsi-bot/1.0"})
        r.raise_for_status()
        rows = r.json()
    except Exception:
        return []
    want = set(currencies)
    out, end = [], now + hours_ahead * 3600
    for ev in rows if isinstance(rows, list) else []:
        try:
            ccy = ev.get("country") or ev.get("currency")
            if ccy not in want:
                continue
            if (ev.get("impact") or "").capitalize() not in ("High", "Medium"):
                continue
            dt = _parse_ff_date(ev.get("date") or "")
            if not dt:
                continue
            ts = dt.timestamp()
            if ts < now - 1800 or ts > end:
                continue
            above, below = _pair_ifthen(ev.get("title") or "", ccy, base, quote)
            fc, prev = ev.get("forecast") or "", ev.get("previous") or ""
            out.append({"name": f"{ccy} · {ev.get('title') or 'event'}", "time": _nzt_str(dt), "ts": ts,
                        "impact": "HIGH IMPACT" if (ev.get("impact") or "").capitalize() == "High" else "MEDIUM",
                        "forecast": (f"Forecast {fc}" + (f" · prev {prev}" if prev else "")) if fc else (f"Prev {prev}" if prev else ""),
                        "above": above, "below": below})
        except Exception:
            continue
    out.sort(key=lambda e: e["ts"])
    return out[:8]


_PAIR_SYS = (
 "You are RON's macro analyst for the {pair} currency cross ({base} vs {quote}). The pair RISES when "
 "{base} strengthens relative to {quote}, and FALLS when {quote} strengthens relative to {base}. "
 "Key drivers: the RBA vs RBNZ interest-rate differential and policy tone; Australian data (CPI, jobs, GDP, "
 "trade balance, and Chinese demand for Australian commodities like iron ore); New Zealand data (CPI, jobs, "
 "GDP, and dairy/GDT auction prices); risk sentiment; and domestic politics in each country. Be honest and "
 "plain; never promise profit or certainty; note uncertainty. All times are New Zealand time (NZT). "
 "If 'weekend_ahead' or 'monday_open' is true the FX market is closed/just reopening — focus on what happened "
 "or is pending over the weekend in either country (politics, data, central-bank comments) that could GAP the "
 "pair at Monday's open, and say plainly whether it leaves {pair} susceptible to BUYING (gap up) or SELLING "
 "(gap down). Output strictly the requested JSON.")

_PAIR_SCHEMA = """Return ONLY a JSON object with EXACTLY these keys:
{
 "headline":"1-2 sentences: the single most important thing for this pair in the next 24h",
 "au":{"verdict":"short tag e.g. AUD firm / AUD soft / neutral","tone":"pos|neg|warn","body":"2-3 plain sentences on the Australian side (RBA, AU data, China) and what it means for the pair"},
 "nz":{"verdict":"short tag e.g. NZD firm / NZD soft / neutral","tone":"pos|neg|warn","body":"2-3 plain sentences on the New Zealand side (RBNZ, NZ data, dairy) and what it means for the pair"},
 "cross":{"verdict":"short tag e.g. lean long / lean short / range","tone":"pos|neg|warn","body":"2-3 sentences on the rate differential & risk flows driving the cross right now"},
 "sessions_24h":[{"session":"Sydney","window":"9:00 am – 6:00 pm","expect":"LEAN AUD|LEAN NZD|WAIT|QUIET","tone":"pos|neg|muted","why":"one sentence"}, ... for Sydney, Tokyo / Asian, London, London / NY overlap, New York],
 "themes":["short tags you are weighting, e.g. RBA, RBNZ, China, dairy, risk"]
}
Note tone: pos = AUDNZD up (AUD-favourable), neg = AUDNZD down (NZD-favourable). No markdown, only JSON."""


def build_pair_briefing(results: Path, pair: str = "AUDNZD", base: str = "AUD", quote: str = "NZD",
                        price_ctx: dict | None = None, now: float | None = None) -> dict:
    now = now or time.time()
    events = fetch_events_for([base, quote], base, quote, now)
    price_ctx = price_ctx or {}
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    weekday = _nzt(dt).weekday()
    context = {
        "pair": pair, "base": base, "quote": quote,
        "today_nzt": _nzt(dt).strftime("%A %d %B %Y, %H:%M") + " NZT",
        "weekend_ahead": weekday == 4, "monday_open": weekday == 0,
        "price": price_ctx.get("price"), "trend": price_ctx.get("trend"),
        "support": price_ctx.get("support"), "resistance": price_ctx.get("resistance"),
        "current_session": price_ctx.get("session"),
        "real_calendar_next_24h": [{"name": e["name"], "time": e["time"], "impact": e["impact"], "forecast": e["forecast"]} for e in events],
        "session_windows_nzt": [{"session": s, "window": w} for s, w in SESSION_WINDOWS],
    }
    sys = _PAIR_SYS.format(pair=pair, base=base, quote=quote)
    brief = _llm_json(sys, json.dumps(context, default=str, indent=1) + "\n\n" + _PAIR_SCHEMA)
    ok = bool(brief)
    if not brief:
        nxt = (events[0]["name"] + " at " + events[0]["time"]) if events else "no high-impact AU/NZ events scheduled"
        brief = {"headline": f"AI narrative offline — real calendar still live. Next: {nxt}.",
                 "au": {"verdict": "see calendar", "tone": "warn", "body": "AI read off; watch the scheduled Australian events below."},
                 "nz": {"verdict": "see calendar", "tone": "warn", "body": "AI read off; watch the scheduled New Zealand events below."},
                 "cross": {"verdict": "manual", "tone": "warn", "body": "Trade the pair's live structure on the chart until the AI read returns."},
                 "sessions_24h": [{"session": s, "window": w, "expect": "WATCH", "tone": "muted", "why": "AI outlook off."} for s, w in SESSION_WINDOWS],
                 "themes": ["RBA", "RBNZ"]}
    brief["events"] = events
    brief["as_of"] = now
    brief["ok"] = ok
    brief["pair"] = pair
    brief["model"] = NEWS_MODEL if ok else "offline"
    return brief


def maybe_run(results: Path, price_ctx: dict | None = None, now: float | None = None) -> dict | None:
    """Throttle to hourly, with a fuller 9am NZT morning brief. Returns the briefing if it ran."""
    now = now or time.time()
    out = results / "briefing.json"
    state = results / ".briefing_state.json"
    try:
        last = json.loads(state.read_text())
    except Exception:
        last = {"last_run": 0.0, "last_morning_day": None}
    dt_nzt = _nzt(datetime.fromtimestamp(now, tz=timezone.utc))
    day = dt_nzt.strftime("%Y-%m-%d")
    is_morning_slot = dt_nzt.hour == 9 and last.get("last_morning_day") != day
    due = (now - last.get("last_run", 0)) >= HOURLY_S
    if not (due or is_morning_slot):
        return None
    brief = build_briefing(results, price_ctx, now, morning=is_morning_slot)
    try:
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(brief, default=str))
        os.replace(tmp, out)
    except Exception:
        pass
    last["last_run"] = now
    if is_morning_slot:
        last["last_morning_day"] = day
    try:
        state.write_text(json.dumps(last))
    except Exception:
        pass
    return brief
