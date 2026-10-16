"""TradingView webhook receiver — turns TradingView strategy alerts into trade signals.

It runs its OWN tiny HTTP server on a SEPARATE port (default 8799), bound to 127.0.0.1.
A public HTTPS front (a Cloudflare Tunnel, or nginx + Let's Encrypt) forwards TradingView's
alert POST to it — so the trading dashboard on 8766 stays private and only this one endpoint
is ever exposed. Every request must carry the shared secret token or it is rejected.

On a valid alert it writes a per-instrument signal file that the bot reads:

    results/webhook_<CANON>.json  = {"dir": "long"|"short"|"flat", "ts": <epoch>, "raw": {...}}

The bot, when run with `--signal webhook`, applies each fresh signal once (on the next loop).

IMPORTANT (honest framing): this is signal TRANSPORT only. It changes how the signal reaches
the bot — not the strategy's edge. A losing strategy loses the same whether the cross is
computed locally or delivered by TradingView. It never places an order itself.

TradingView alert message (Condition → Alert → Message box), as JSON:
    {"token":"YOUR_TOKEN","symbol":"{{ticker}}","action":"{{strategy.order.action}}"}
"""

from __future__ import annotations

import hmac
import json
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import RESULTS
from .instruments import SYMBOL_VARIANTS

MAX_BODY = 8192  # a TradingView alert is tiny; refuse anything larger

# action word -> canonical direction
_LONG = {"long", "buy", "bull", "1", "+1", "up"}
_SHORT = {"short", "sell", "bear", "-1", "down"}
_FLAT = {"flat", "close", "exit", "0", "none", "flatten"}


def _variants(sym: str) -> set[str]:
    """Every ticker string that should resolve to this active symbol."""
    out = {sym, sym.split(".")[0]}
    out.update(SYMBOL_VARIANTS.get(sym, []))
    return {v.upper() for v in out}


def resolve_symbol(raw: str, active: list[str]) -> str | None:
    """Map a TradingView ticker to whichever ACTIVE engine symbol it belongs to.

    Resolving against the running set (not the global registry) avoids the NAS100/NDQ100
    ambiguity: if the bot trades 'NDQ100', a TradingView 'NAS100'/'US100' alert lands on it.
    """
    if not raw:
        return None
    s = "".join(ch for ch in raw.upper() if ch.isalnum() or ch in "._")
    base = s.split(".")[0]
    for sym in active:
        vs = _variants(sym)
        if s in vs or base in vs:
            return sym
    return None


def _resolve_dir(raw: str) -> str | None:
    a = (raw or "").strip().lower()
    if a in _LONG:
        return "long"
    if a in _SHORT:
        return "short"
    if a in _FLAT:
        return "flat"
    return None


def write_signal(results_dir: Path, canon: str, direction: str, raw: dict) -> Path:
    results_dir.mkdir(exist_ok=True)
    p = results_dir / f"webhook_{canon}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"dir": direction, "ts": time.time(), "raw": raw}))
    import os
    os.replace(tmp, p)
    return p


def read_signal(results_dir: Path, canon: str):
    """Return (sign, ts) for the latest webhook signal, or None. sign is +1/-1/0."""
    p = results_dir / f"webhook_{canon}.json"
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    s = {"long": 1, "short": -1, "flat": 0}.get(d.get("dir"))
    if s is None:
        return None
    return s, float(d.get("ts") or 0)


class _WebhookHandler(BaseHTTPRequestHandler):
    token = ""
    active: list[str] = []  # the symbols the bot is actually trading
    log = None  # optional callable(str)

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _note(self, msg: str) -> None:
        """Log without ever letting a logging failure break the response."""
        fn = type(self).log
        if not fn:
            return
        try:
            fn(msg)
        except Exception:
            pass

    def do_GET(self):
        # a plain health check — never reveals anything, useful to confirm the tunnel is up
        if self.path.split("?")[0] in ("/", "/health", "/webhook"):
            return self._json(200, {"ok": True, "service": "rsi-bot webhook", "hint": "POST /webhook"})
        return self._json(404, {"ok": False})

    def do_POST(self):
        if self.path.split("?")[0] != "/webhook":
            return self._json(404, {"ok": False, "error": "post to /webhook"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self._json(400, {"ok": False, "error": "bad body size"})
        raw_body = self.rfile.read(n)
        try:
            data = json.loads(raw_body.decode("utf-8", "replace"))
            if not isinstance(data, dict):
                raise ValueError
        except Exception:
            return self._json(400, {"ok": False, "error": "body must be JSON object"})

        # token check first, constant-time, before we act on anything
        supplied = str(data.get("token") or "")
        if not self.token or not hmac.compare_digest(supplied, self.token):
            self._json(403, {"ok": False, "error": "forbidden"})
            self._note("webhook REJECTED - bad/missing token")
            return

        canon = resolve_symbol(str(data.get("symbol") or ""), self.active)
        direction = _resolve_dir(str(data.get("action") or data.get("dir") or ""))
        if not canon:
            return self._json(422, {"ok": False, "error": f"unknown symbol {data.get('symbol')!r}"})
        if not direction:
            return self._json(422, {"ok": False, "error": f"unknown action {data.get('action')!r}"})

        safe_raw = {k: data[k] for k in ("symbol", "action", "dir", "price", "comment") if k in data}
        write_signal(RESULTS, canon, direction, safe_raw)
        self._json(200, {"ok": True, "symbol": canon, "dir": direction})
        self._note(f"webhook {canon} -> {direction}")
        return

    def log_message(self, *args):  # keep the terminal clean
        pass


def serve_webhook(port: int, token: str, active: list[str], log=None) -> ThreadingHTTPServer:
    if not token:
        raise SystemExit("  webhook needs a secret token — set WEBHOOK_TOKEN in .env or pass --webhook-token")
    handler = type("Handler", (_WebhookHandler,),
                   {"token": token, "active": list(active),
                    "log": staticmethod(log) if log else None})
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        raise SystemExit(f"  webhook port {port} is busy — pass --webhook-port {port + 1}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
