"""Read-only FastAPI dashboard: balance, open positions, P/L, win rate,
status, recent trades. Serves a small self-refreshing HTML page plus a
JSON API. It never places orders and never shows secrets.

    uvicorn bot.dashboard:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from bot import config, kucoin_client, preflight, state

app = FastAPI(title="Medik trading dashboard")


@app.on_event("startup")
async def _startup() -> None:
    state.init_db()


@app.get("/api/status")
async def api_status() -> dict:
    allowed, reason = preflight.live_allowed()
    try:
        balance = await kucoin_client.get_available_balance(config.QUOTE_CURRENCY)
    except kucoin_client.KucoinError:
        balance = None
    return {
        "mode": "LIVE" if allowed else "PAPER",
        "gate_reason": reason,
        "symbol": config.TRADE_SYMBOL,
        "quote": config.QUOTE_CURRENCY,
        "balance": balance,
        "stats": state.stats(),
        "open_positions": [vars(p) for p in state.open_positions()],
        "recent_trades": state.recent_trades(20),
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    s = await api_status()
    st = s["stats"]
    bal = f"{s['balance']:.2f} {s['quote']}" if s["balance"] is not None else "—"
    rows = "".join(
        f"<tr><td>{t['symbol']}</td><td>{t['reason']}</td>"
        f"<td>{t['entry_price']:.6f}</td><td>{t['exit_price']:.6f}</td>"
        f"<td style='color:{'#0a0' if t['pnl'] >= 0 else '#c00'}'>{t['pnl']:+.4f}</td></tr>"
        for t in s["recent_trades"]
    ) or "<tr><td colspan=5>no trades yet</td></tr>"
    positions = "".join(
        f"<tr><td>{p['symbol']}</td><td>{p['size']}</td><td>{p['entry_price']:.6f}</td>"
        f"<td>{p['stop']:.6f}</td><td>{p['target']:.6f}</td></tr>"
        for p in s["open_positions"]
    ) or "<tr><td colspan=5>none</td></tr>"
    badge = "#0a0" if s["mode"] == "LIVE" else "#888"
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=15><title>Medik dashboard</title>
<style>body{{font-family:system-ui;margin:2rem;max-width:900px}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0}}
td,th{{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;font-size:14px}}
.kpi{{display:inline-block;margin-right:2rem}}.kpi b{{font-size:1.4rem}}</style></head>
<body>
<h1>Medik trading bot
<span style="background:{badge};color:#fff;padding:.1rem .6rem;border-radius:6px;font-size:1rem">
{s['mode']}</span></h1>
<p style="color:#666">{s['gate_reason']} · {s['symbol']}</p>
<div class=kpi>Balance<br><b>{bal}</b></div>
<div class=kpi>Total P/L<br><b>{st['total_pnl']:+.4f}</b></div>
<div class=kpi>Win rate<br><b>{st['win_rate']:.1f}%</b></div>
<div class=kpi>Trades<br><b>{st['total_trades']}</b></div>
<div class=kpi>Open<br><b>{st['open_positions']}</b></div>
<h3>Open positions</h3><table>
<tr><th>Symbol</th><th>Size</th><th>Entry</th><th>Stop</th><th>Target</th></tr>{positions}</table>
<h3>Recent trades</h3><table>
<tr><th>Symbol</th><th>Reason</th><th>Entry</th><th>Exit</th><th>P/L</th></tr>{rows}</table>
<p style="color:#999;font-size:12px">Read-only. Auto-refreshes every 15s.</p>
</body></html>"""
