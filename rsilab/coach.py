"""RON's LLM coach — an advisory review layer (#5).

Every so often this reads RON's REAL trade ledger + its self-learned policy and asks a Claude
model to write a short, honest, plain-English coaching note: what's working, what's leaking,
any time/session patterns, and a suggestion or two. It is ADVISORY ONLY — it never touches
execution. RON's deterministic rules stay in charge; the coach just helps the human see the wood
for the trees, and can propose tweaks for Carl to approve.

Needs OPENAI_API_KEY in the environment (a paid key from platform.openai.com). If the key
is missing or the call fails, it writes the reason into coach.json and the bot carries on.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = os.getenv("COACH_MODEL", "gpt-4o-mini")


def _load_jsonl(path: Path, limit: int) -> list[dict]:
    try:
        with open(path) as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
        return rows[-limit:]
    except Exception:
        return []


def _hhmm(ts) -> str:
    try:
        import datetime
        return datetime.datetime.utcfromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def _session(ts) -> str:
    """Rough FX session from the trade's UTC hour, so the coach can name the window."""
    try:
        import datetime
        h = datetime.datetime.utcfromtimestamp(float(ts)).hour
    except Exception:
        return "?"
    if 0 <= h < 7:   return "Asian"
    if 7 <= h < 12:  return "London"
    if 12 <= h < 16: return "London/NY"
    if 16 <= h < 21: return "New York"
    return "Sydney"  # 21-24


def _bucket(rows: list[dict]) -> dict:
    n = len(rows)
    wins = sum(1 for r in rows if (r.get("pnl") or 0) > 0)
    net = sum(float(r.get("pnl") or 0) for r in rows)
    return {"n": n, "win_rate": (round(wins / n, 2) if n else None), "net": round(net, 2)}


def _group(rows: list[dict], keyfn) -> dict:
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(keyfn(r), []).append(r)
    return {k: _bucket(v) for k, v in sorted(out.items())}


def _summarise(results_dir: Path) -> dict:
    """Comprehensive, factual digest of RON's REAL trading — enough for the coach to name
    the exact exit reason / session / direction that's leaking, with numbers."""
    closed = _load_jsonl(results_dir / "closed_trades.jsonl", 500)
    policy, edge = {}, {}
    try:
        policy = json.loads((results_dir / "ron_policy.json").read_text())
    except Exception:
        policy = {}
    try:
        edge = json.loads((results_dir / "edge.json").read_text())
    except Exception:
        edge = {}
    recent = closed[-40:]  # the newer, gold-only era
    def rows_fmt(rows):
        return [{"time_utc": _hhmm(c.get("t")), "session": _session(c.get("t")),
                 "side": c.get("side"), "pnl": c.get("pnl"), "reason": c.get("reason")}
                for c in rows]
    return {
        "note_on_data": "all-time includes older pre-fix trades; 'recent' (last 40) reflects the "
                        "current gold-only $250-risk setup. Weight recent more.",
        "all_time": _bucket(closed),
        "recent": _bucket(recent),
        "all_time_by_exit_reason": _group(closed, lambda r: r.get("reason") or "?"),
        "recent_by_exit_reason": _group(recent, lambda r: r.get("reason") or "?"),
        "all_time_by_session": _group(closed, lambda r: _session(r.get("t"))),
        "recent_by_session": _group(recent, lambda r: _session(r.get("t"))),
        "all_time_by_direction": _group(closed, lambda r: r.get("side") or "?"),
        "recent_trades": rows_fmt(recent[-25:]),
        "policy_state": policy.get("state", {}),
        "policy_lessons": (policy.get("lessons", []) or [])[-15:],
        "edge_now": edge.get("now", {}) if isinstance(edge, dict) else {},
    }


PROMPT = """You are RON, an honest trading coach reviewing your OWN real (demo-account) results on Gold (XAUUSD).
Below is a detailed, factual digest of your REAL closed trades broken down by exit reason, session and
direction (both all-time and recent), plus your self-learned policy and current edge read.

Write a SHORT review for the trader (Carl), first person ("I"), and BE SPECIFIC — cite the numbers:
1. Name the exact exit reason(s), session(s) and/or direction that are leaking money vs working — quote the net and count (e.g. "my shorts in London are -340 over 12").
2. Call out the clearest pattern in the breakdowns (which session/time, stop-outs vs trailing profits, longs vs shorts).
3. One or two concrete suggestions tied to those specifics — framed as suggestions, not commands.

Weight the RECENT (gold-only) numbers more than all-time, since all-time includes older pre-fix trades; say so if recent and all-time disagree. Be honest and non-promotional, never claim certainty or guarantee profit, and if a bucket's sample is tiny say so. No more than ~160 words, no headers — short sentences or bullets.

DIGEST:
{digest}
"""


def run_coach(results_dir, model: str | None = None, api_key: str | None = None) -> dict:
    """Generate one coaching note and write results/coach.json. Returns the note dict."""
    results_dir = Path(results_dir)
    out = results_dir / "coach.json"
    key = api_key or os.getenv("OPENAI_API_KEY", "").strip()
    model = model or DEFAULT_MODEL
    now = time.time()
    if not key:
        note = {"generated_at": now, "ok": False,
                "note": "Coach is off — no OPENAI_API_KEY set on the server yet.", "model": model}
        _write(out, note)
        return note
    if requests is None:
        note = {"generated_at": now, "ok": False, "note": "requests library unavailable.", "model": model}
        _write(out, note)
        return note
    digest = _summarise(results_dir)
    if (digest.get("all_time", {}).get("n") or 0) < 3:
        note = {"generated_at": now, "ok": True, "model": model,
                "note": "Too few real trades so far to review honestly — I'll have a proper read once a few more close.",
                "digest": digest}
        _write(out, note)
        _append_log(results_dir, note)
        return note
    body = {"model": model, "max_tokens": 400,
            "messages": [{"role": "user",
                          "content": PROMPT.format(digest=json.dumps(digest, default=str, indent=1))}]}
    try:
        r = requests.post(API_URL, headers={"Authorization": f"Bearer {key}",
                          "Content-Type": "application/json"}, json=body, timeout=60)
        data = r.json()
        if r.status_code != 200:
            msg = (data.get("error", {}) or {}).get("message", f"HTTP {r.status_code}")
            note = {"generated_at": now, "ok": False, "model": model,
                    "note": f"Coach call failed: {msg}. (If it's a model name, set COACH_MODEL in .env.)"}
            _write(out, note)
            return note
        text = "".join(ch.get("message", {}).get("content", "")
                       for ch in data.get("choices", [])).strip()
        at = digest.get("all_time", {})
        note = {"generated_at": now, "ok": True, "model": model, "note": text or "(empty)",
                "stats": {"trades": at.get("n"), "win_rate": at.get("win_rate"),
                          "net_aud": at.get("net")}}
        _write(out, note)
        _append_log(results_dir, note)
        return note
    except Exception as exc:
        note = {"generated_at": now, "ok": False, "model": model, "note": f"Coach error: {str(exc)[:160]}"}
        _write(out, note)
        return note


def _write(path: Path, note: dict) -> None:
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(note, default=str))
        os.replace(tmp, path)
    except Exception:
        pass


def _append_log(results_dir: Path, note: dict, cap: int = 100) -> None:
    """Keep a running history of real coaching notes (newest first) for the coach-log page."""
    if not note.get("note"):
        return
    log_path = Path(results_dir) / "coach_log.json"
    try:
        existing = json.loads(log_path.read_text()) if log_path.exists() else []
        if not isinstance(existing, list):
            existing = []
    except Exception:
        existing = []
    existing.insert(0, note)
    existing = existing[:cap]
    try:
        tmp = log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, default=str))
        os.replace(tmp, log_path)
    except Exception:
        pass
