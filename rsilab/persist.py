"""Tiny append-only JSONL persistence so RON's visible history survives restarts.

Closed trades, RON alerts and the equity curve are written here as they happen and
reloaded on start, so the dashboard shows a continuous record instead of resetting
each time the service restarts. Best-effort by design: any IO error is swallowed —
persistence must never be able to break trading or the dashboard.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def append_jsonl(path: Path, obj: dict) -> None:
    try:
        Path(path).parent.mkdir(exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(obj, default=str) + "\n")
    except Exception:
        pass


def load_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    try:
        with open(path) as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except FileNotFoundError:
        return []
    except Exception:
        return []
    if limit:
        lines = lines[-limit:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def write_json(path: Path, obj) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, default=str))
        os.replace(tmp, p)
    except Exception:
        pass
