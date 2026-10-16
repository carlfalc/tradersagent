# TradingView webhook — finishing steps (tomorrow, ~15 min)

The receiver is **built and tested**. It's a token-protected endpoint that turns your
TradingView RSI Strategy alerts into trade signals for the bot. Three things remain, all
with you at the keyboard: copy the code to the VPS, expose one HTTPS URL, paste the alert.

> **Honest note:** the webhook only changes *how* the signal reaches the bot. It does **not**
> change the strategy's edge. If the RSI-HA strategy is losing on real fills, it loses the same
> way through the webhook. Worth watching the closed-trades tab either way.

---

## What changed in the code
- `rsilab/webhook.py` — new. Token-protected receiver on `127.0.0.1` (its own port, default 8799),
  separate from the dashboard so the dashboard stays private. Maps a TradingView ticker
  (`NAS100`/`US100` → `NDQ100`, `XAUUSD`, `HK50`) to the running symbol and writes a signal file.
- `rsilab/rsibot.py` — new `--signal webhook` mode: applies each fresh alert once.
- `rsilab/__main__.py` — flags `--signal webhook`, `--webhook-port`, `--webhook-token`.
- `.env.example` — `WEBHOOK_TOKEN` / `WEBHOOK_PORT`.

## Step 1 — get the updated code onto the VPS
From your Mac (in the rsi-bot folder):
```
scp rsilab/webhook.py rsilab/rsibot.py rsilab/__main__.py .env.example WEBHOOK.md root@188.166.150.48:/root/rsi-bot/
```

## Step 2 — set the secret token (on the VPS)
```
ssh root@188.166.150.48
cd ~/rsi-bot
TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
echo "WEBHOOK_TOKEN=$TOKEN" >> .env
echo "  >>> YOUR TOKEN (copy it, you'll paste it into TradingView):  $TOKEN"
```

## Step 3 — switch the service to webhook mode
```
sed -i 's#bot --port 8766 --no-open#bot --port 8766 --no-open --signal webhook#' /etc/systemd/system/rsibot.service
systemctl daemon-reload && systemctl restart rsibot && sleep 6 && systemctl status rsibot --no-pager | head -6
```
Expect `active (running)`, and in the logs a line: `webhook receiver on 127.0.0.1:8799`.

## Step 4 — expose ONE HTTPS URL with a Cloudflare Tunnel (free, no domain)
TradingView only sends to ports 80/443 over HTTPS, so we need a public HTTPS front for the
webhook port only (8799 — NOT the dashboard on 8766).
```
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
cloudflared tunnel --url http://127.0.0.1:8799
```
It prints a URL like `https://random-words.trycloudflare.com`. Your webhook URL is that **+ `/webhook`**:
```
https://random-words.trycloudflare.com/webhook
```
(For a permanent URL later we'd use a named tunnel or a domain — the quick tunnel URL changes
each restart, which is fine for testing. Tell me tomorrow if you want the permanent version and
I'll turn it into its own systemd service.)

## Step 5 — set the TradingView alert
On your RSI Strategy alert (Create Alert → Notifications tab):
- **Webhook URL:** the `https://….trycloudflare.com/webhook` from step 4
- **Message** (Settings tab, Message box) — paste this JSON exactly:
```
{"token":"PASTE_YOUR_TOKEN_HERE","symbol":"{{ticker}}","action":"{{strategy.order.action}}"}
```
Replace `PASTE_YOUR_TOKEN_HERE` with the token from step 2. TradingView fills in `{{ticker}}`
and `{{strategy.order.action}}` (buy/sell) automatically on each signal.

Set the alert **Condition** to your RSI Strategy, and trigger on **Order fills only**.

## Step 6 — confirm
Fire a test: in TradingView hit "Test" on the alert, or wait for a live cross. On the VPS:
```
journalctl -u rsibot -n 20 --no-pager | grep webhook
```
You should see `webhook · NDQ100 → long` (etc). That signal then drives the next trade.

### Quick sanity check without TradingView (from the VPS, uses your token):
```
source ~/.local/bin/env
curl -s -XPOST http://127.0.0.1:8799/webhook -H 'Content-Type: application/json' \
  -d "{\"token\":\"$(grep '^WEBHOOK_TOKEN=' .env | cut -d= -f2)\",\"symbol\":\"NAS100\",\"action\":\"buy\"}"
```
Expect: `{"ok": true, "symbol": "NDQ100", "dir": "long"}`

## To switch back to local RSI mode
```
sed -i 's# --signal webhook##' /etc/systemd/system/rsibot.service
systemctl daemon-reload && systemctl restart rsibot
```
