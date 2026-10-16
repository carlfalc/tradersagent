# TradersAgent · Agent RON

A self-learning trading-intelligence platform ("Agent RON") running at **tradersagent.co**.
Trades **gold (XAUUSD)** on an Eightcap demo account via MetaApi, with a consolidated live dashboard,
a learning agent stack, and an **analysis-only** read on **AUD/NZD**.

## What's inside

**Backend (`rsilab/`)** — Python, run via `uv`:
- `rsibot.py` — the main loop: RSI/Heikin-Ashi signals, RON watcher execution, risk sizing, break-even/trailing.
- `ronagents.py` — specialist analyst agents (sessions, volatility, multi-timeframe, patterns).
- `ronpatterns.py` — pattern engine: records, grades and aggregates chart-pattern hit-rates.
- `ronfollow.py` — trend-learning engine: records trend onsets with volume, grades follow-through.
- `ronfusion.py` — fuses the agents (weighted by scoreboard accuracy) into one conviction nudge.
- `ronedge.py` — statistical time/session edges + live market structure (support/resistance, trend, NZT stamps).
- `ronpolicy.py` — RON's self-learning policy (conviction floor, trend filter, cooldown, learned TP).
- `ronnews.py` — hourly macro/news briefing + real economic calendar (gold/USD and AUD/NZD).
- `ronmeeting.py` — daily 9am team meeting + accountability scoreboard.
- `ronpair.py` — AUDNZD analysis pipeline (analysis only, no trading).
- `coach.py` — advisory LLM coach + supervisor.
- `metaapi.py` — MetaApi REST client (quotes, candles, orders).
- `server.py` — tiny dashboard web server + control endpoints.

**Dashboard (`dashboard/`)** — single-page HTML (light theme):
- `home.html` — consolidated front page (market read, agents, patterns, sessions, heatmap, briefing, meeting).
- `audnzd.html` — AUD/NZD analysis page.
- `world.html` — world market clocks.
- `ron.html`, `edge.html`, `coach.html` — detailed views.

## Setup
1. `cp .env.example .env` and fill in your keys (MetaApi token/account, OpenAI key, etc.).
2. Install deps: `uv sync`.
3. Run: `uv run python -m rsilab bot --timeframe 3m --port 8766 --signal watcher`.
4. The dashboard serves on `127.0.0.1:8766`; front it with nginx + TLS for public access.

## Notes
- Demo / educational. Not financial advice.
- No secrets are committed — see `.gitignore`.
