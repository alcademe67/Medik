"""Local browser dashboard for the trading bot — a visual screen instead of the log.

Serves a small, self-contained web page on 127.0.0.1 (localhost ONLY — never
exposed to the network, because it shows balances and can halt the bot) with:

  * the account balance and today's / all-time P/L
  * open positions with LIVE unrealized profit/loss
  * each watchlist coin's latest price, regime, and buy/sell signal
  * recent closed trades
  * a big STOP button

It is READ-ONLY over the trading core. It reads the same SQLite state the engine
already writes (`bot.state`) plus the newest per-coin signal the engine hands it
each tick (`STATUS`). The ONLY action it can take is to create the emergency STOP
file — the exact same brake the engine already honors — so the dashboard can
never place, size, or alter an order. No secrets are ever served: API keys live
only in the environment and are never read here.

Built on the Python standard library only (http.server) — no extra packages to
install. The engine starts it automatically (see live_engine.run); just open
http://localhost:8787 in your browser. It can also be run on its own:

    python -m bot.dashboard
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot import config, state

logger = logging.getLogger(__name__)


class LiveStatus:
    """Thread-safe holder for the newest snapshot the engine computes each tick.

    The engine (asyncio thread) writes via set_account / set_coin; the HTTP
    handler threads read via snapshot(). A lock keeps reads and writes from
    interleaving. This holds only volatile display data — positions, trades and
    P/L come from the SQLite state, which is the source of truth.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._account: dict = {"balance": None, "live": False, "updated_at": None}
        self._coins: dict[str, dict] = {}

    def set_account(self, *, balance: float | None = None, live: bool = False) -> None:
        with self._lock:
            self._account = {
                "balance": (float(balance) if balance is not None else None),
                "live": bool(live),
                "updated_at": time.time(),
            }

    def set_coin(self, symbol: str, price: float, regime: str, signal: str, atr: float) -> None:
        with self._lock:
            self._coins[symbol] = {
                "price": float(price),
                "regime": str(regime),
                "signal": str(signal),
                "atr": float(atr),
                "updated_at": time.time(),
            }

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "account": dict(self._account),
                "coins": {k: dict(v) for k, v in self._coins.items()},
            }


# Single shared instance the engine writes to and the server reads from.
STATUS = LiveStatus()


def request_stop(stop_path: str | None = None) -> bool:
    """Create the emergency STOP file — the same brake `live_engine` checks each
    tick. Returns True on success. Kept as a tiny function so it can be unit
    tested without a live HTTP server."""
    path = stop_path or config.EMERGENCY_STOP_PATH
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("stop requested from the dashboard\n")
    logger.critical("dashboard: STOP requested — wrote %s", path)
    return True


def build_state(status: LiveStatus | None = None, *, state_path: str | None = None,
                stop_path: str | None = None) -> dict:
    """Assemble the full dashboard payload as a plain dict (JSON-ready).

    Pure and side-effect free (except reading the SQLite state), so it can be
    unit tested directly. Open positions are priced against the newest tick
    price to show LIVE unrealized P/L, net of the entry fee already paid and an
    estimate of the exit fee still to come.
    """
    status = status or STATUS
    stop_path = stop_path or config.EMERGENCY_STOP_PATH
    snap = status.snapshot()
    coins = snap["coins"]
    acct = snap["account"]

    positions = []
    for p in state.open_positions(state_path):
        info = coins.get(p.symbol, {})
        current = info.get("price") or p.entry_price
        value_now = current * p.size
        gross = (current - p.entry_price) * p.size
        est_exit_fee = value_now * config.FEE_RATE
        net = gross - (p.entry_fee or 0.0) - est_exit_fee
        pct = ((current / p.entry_price - 1.0) * 100.0) if p.entry_price else 0.0
        positions.append({
            "symbol": p.symbol, "size": p.size, "entry": p.entry_price,
            "current": current, "value": value_now,
            "unrealized_gross": gross, "unrealized_net": net, "pct": pct,
            "stop": p.stop, "target": p.target,
            "signal": info.get("signal"), "opened_at": p.opened_at,
        })
    positions.sort(key=lambda x: x["symbol"])

    watchlist = sorted(
        (
            {"symbol": sym, "price": c.get("price"), "regime": c.get("regime"),
             "signal": c.get("signal"), "updated_at": c.get("updated_at")}
            for sym, c in coins.items()
        ),
        key=lambda w: w["symbol"],
    )

    trades = [
        {"symbol": t.get("symbol"), "entry": t.get("entry_price"), "exit": t.get("exit_price"),
         "reason": t.get("reason"), "pnl": t.get("pnl"), "gross": t.get("gross_pnl"),
         "fees": t.get("fees"), "closed_at": t.get("closed_at")}
        for t in state.recent_trades(15, state_path)
    ]

    st = state.stats(state_path)
    return {
        "account": {
            "balance": acct.get("balance"),
            "live": acct.get("live"),
            "updated_at": acct.get("updated_at"),
            "realized_today": state.today_realized_pnl(state_path),
            "open_positions": st["open_positions"],
            "max_positions": config.RISK.max_open_positions,
            "orders_today": state.orders_today(state_path),
            "max_daily_orders": config.RISK.max_daily_orders,
            "quote": config.QUOTE_CURRENCY,
        },
        "stats": st,
        "positions": positions,
        "watchlist": watchlist,
        "trades": trades,
        "stop_active": os.path.exists(stop_path),
        "server_time": time.time(),
    }


class _Handler(BaseHTTPRequestHandler):
    # Silence the default per-request stderr logging (keeps the console clean).
    def log_message(self, *_a) -> None:  # noqa: D401
        return

    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Belt-and-braces: this is localhost-only, but forbid embedding anyway.
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser navigated away mid-response — harmless

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(_PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                self._json(build_state())
            except Exception as exc:  # noqa: BLE001 - a read hiccup must not kill the server
                logger.warning("dashboard /api/state error: %s", exc)
                self._json({"error": str(exc)}, 500)
        elif path == "/healthz":
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/stop":
            try:
                request_stop()
                self._json({"ok": True, "stop_active": True})
            except OSError as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
        else:
            self._json({"error": "not found"}, 404)


def _make_server(host: str | None = None, port: int | None = None) -> ThreadingHTTPServer | None:
    host = host or config.DASHBOARD_HOST
    port = int(port or config.DASHBOARD_PORT)
    try:
        return ThreadingHTTPServer((host, port), _Handler)
    except OSError as exc:
        logger.warning(
            "dashboard could not bind %s:%s (%s) — continuing WITHOUT the dashboard "
            "(trading is unaffected). Is it already running, or the port in use?",
            host, port, exc,
        )
        return None


def _browser_url(host, port) -> str:
    """The address to open in a browser. A server bound to 0.0.0.0 / 127.0.0.1 is
    reached as `localhost` from the same machine."""
    host = str(host)
    friendly = "localhost" if host in ("", "0.0.0.0", "127.0.0.1") else host
    return f"http://{friendly}:{port}"


def _maybe_open_browser(host, port, delay: float = 2.0) -> None:
    """Open the dashboard in the default browser a couple of seconds after the
    server binds — so the operator never has to type an address, and the first
    request can't beat the server to a 'connection refused' page. Disabled for
    headless/container runs via DASHBOARD_OPEN_BROWSER. Best-effort: a browser
    that won't open is only a convenience lost, never an error."""
    if not config.DASHBOARD_OPEN_BROWSER:
        return
    url = _browser_url(host, port)

    def _go() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
            logger.info("dashboard: opened %s in your browser", url)
        except Exception as exc:  # noqa: BLE001 - never let this bubble into the engine
            logger.debug("dashboard: could not open a browser (%s)", exc)

    threading.Thread(target=_go, name="dashboard-open", daemon=True).start()


def start_in_thread(host: str | None = None, port: int | None = None):
    """Start the dashboard HTTP server in a daemon thread. Returns the server, or
    None if the port can't be bound — trading must never fail because the
    dashboard couldn't start, so a bind error is logged and swallowed."""
    server = _make_server(host, port)
    if server is None:
        return None
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    bound_host, bound_port = server.server_address
    logger.info("dashboard READY — open %s in your browser", _browser_url(bound_host, bound_port))
    _maybe_open_browser(bound_host, bound_port)
    return server


def main() -> None:
    """Run the dashboard as a standalone server (python -m bot.dashboard).

    The engine normally hosts the dashboard in-process (see live_engine); this
    entrypoint is for running it on its own, e.g. a container. Standalone it
    still shows positions, trades and stats from the SQLite state, but the live
    per-coin signals and balance only populate while the trading engine is also
    running and feeding them (a separate process does not share STATUS).
    """
    from bot.logging_setup import setup_logging

    setup_logging()
    state.init_db()
    server = _make_server()
    if server is None:
        raise SystemExit(1)
    bound_host, bound_port = server.server_address
    logger.info("dashboard: serving on %s (standalone)", _browser_url(bound_host, bound_port))
    _maybe_open_browser(bound_host, bound_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


# The whole UI in one self-contained page (no external files, no CDN). Data is
# fetched from /api/state every few seconds; the STOP button POSTs to /api/stop.
# NOTE: a plain string (NOT an f-string) — the ${...} below are JS template
# literals, not Python placeholders.
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Medik Bot — Dashboard</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--text);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--border);
position:sticky;top:0;background:var(--bg);z-index:5;}
h1{font-size:17px;margin:0;font-weight:600;}
.badge{padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700;white-space:nowrap;}
.badge.live{background:rgba(248,81,73,.15);color:var(--red);border:1px solid var(--red);}
.badge.paper{background:rgba(88,166,255,.15);color:var(--blue);border:1px solid var(--blue);}
.spacer{flex:1;}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle;}
.dot.ok{background:var(--green);}.dot.bad{background:var(--red);}
#conn{font-size:12px;color:var(--muted);}
.stop-btn{background:var(--red);color:#fff;border:none;padding:9px 18px;border-radius:8px;
font-weight:700;cursor:pointer;font-size:14px;}
.stop-btn:hover{filter:brightness(1.12);}
main{padding:20px;max-width:1100px;margin:0 auto;}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px;}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 16px;}
.card .label{font-size:12px;color:var(--muted);margin-bottom:6px;}
.card .value{font-size:21px;font-weight:700;}
section{margin-bottom:26px;}
h2{font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin:0 0 10px;}
.tablewrap{overflow-x:auto;}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);
border-radius:10px;overflow:hidden;font-size:14px;}
th,td{text-align:right;padding:9px 12px;border-bottom:1px solid var(--border);white-space:nowrap;}
th:first-child,td:first-child{text-align:left;}
th{color:var(--muted);font-weight:600;font-size:12px;}
tr:last-child td{border-bottom:none;}
.pos{color:var(--green);}.neg{color:var(--red);}
.sig{font-weight:700;padding:2px 8px;border-radius:6px;font-size:12px;}
.sig.BUY{color:var(--green);background:rgba(63,185,80,.12);}
.sig.SELL{color:var(--red);background:rgba(248,81,73,.12);}
.sig.HOLD{color:var(--muted);background:rgba(139,148,158,.12);}
.muted{color:var(--muted);}
.empty{color:var(--muted);padding:16px;text-align:center;background:var(--panel);
border:1px solid var(--border);border-radius:10px;}
.stopbanner{background:rgba(210,153,34,.15);border:1px solid var(--amber);color:var(--amber);
padding:10px 14px;border-radius:8px;margin-bottom:18px;font-weight:600;}
.foot{font-size:12px;color:var(--muted);margin-top:8px;}
</style>
</head>
<body>
<header>
  <h1>🤖 Medik Trading Bot</h1>
  <span id="mode" class="badge paper">—</span>
  <span class="spacer"></span>
  <span id="conn"><span class="dot bad"></span>connecting…</span>
  <button class="stop-btn" onclick="doStop()">■ STOP</button>
</header>
<main>
  <div id="stopbanner"></div>
  <div class="cards" id="cards"></div>
  <section><h2>Open positions</h2><div id="positions"></div></section>
  <section><h2>Watchlist — live signals</h2><div id="watchlist"></div></section>
  <section><h2>Recent trades</h2><div id="trades"></div></section>
  <p class="foot">Updates every 3s · localhost only · closing this tab does NOT stop the bot.</p>
</main>
<script>
const fmt=(n,d=2)=>(n===null||n===undefined||isNaN(n))?"—":Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const sgn=(n,d=4)=>(n===null||n===undefined||isNaN(n))?"—":((n>=0?"+":"")+fmt(n,d));
const cls=n=>(n>0?"pos":(n<0?"neg":""));

async function refresh(){
  try{
    const r=await fetch('/api/state',{cache:'no-store'});
    if(!r.ok) throw new Error('bad status');
    render(await r.json());
    document.getElementById('conn').innerHTML='<span class="dot ok"></span>connected';
  }catch(e){
    document.getElementById('conn').innerHTML='<span class="dot bad"></span>waiting for the bot…';
  }
}

function render(s){
  const a=s.account||{}, st=s.stats||{}, q=a.quote||'USDT';
  const mode=document.getElementById('mode');
  mode.textContent=a.live?'LIVE — real money':'PAPER — simulated';
  mode.className='badge '+(a.live?'live':'paper');

  document.getElementById('stopbanner').innerHTML=s.stop_active
    ?'<div class="stopbanner">■ STOP is active — the bot is halting / halted. Re-launch start_bot to run again.</div>':'';

  const cards=[
    ['Balance',fmt(a.balance,2)+' '+q],
    ['Open positions',(a.open_positions??0)+' / '+(a.max_positions??0)],
    ['P/L today',sgn(a.realized_today,4)+' '+q],
    ['All-time net',sgn(st.total_pnl,4)+' '+q],
    ['Win rate',fmt(st.win_rate,1)+'%'],
    ['Orders today',(a.orders_today??0)+' / '+(a.max_daily_orders??0)],
  ];
  document.getElementById('cards').innerHTML=cards.map(c=>
    `<div class="card"><div class="label">${c[0]}</div><div class="value">${c[1]}</div></div>`).join('');

  const P=s.positions||[];
  document.getElementById('positions').innerHTML=P.length?`<div class="tablewrap"><table>
    <tr><th>Coin</th><th>Size</th><th>Entry</th><th>Now</th><th>Change</th><th>Unreal. P/L</th><th>Stop</th><th>Target</th></tr>
    ${P.map(p=>`<tr><td>${p.symbol}</td><td>${fmt(p.size,4)}</td><td>${fmt(p.entry,6)}</td>
      <td>${fmt(p.current,6)}</td><td class="${cls(p.pct)}">${sgn(p.pct,2)}%</td>
      <td class="${cls(p.unrealized_net)}">${sgn(p.unrealized_net,4)} ${q}</td>
      <td class="muted">${fmt(p.stop,6)}</td><td class="muted">${fmt(p.target,6)}</td></tr>`).join('')}
    </table></div>`:'<div class="empty">No open positions.</div>';

  const W=s.watchlist||[];
  document.getElementById('watchlist').innerHTML=W.length?`<div class="tablewrap"><table>
    <tr><th>Coin</th><th>Price</th><th>Regime</th><th>Signal</th></tr>
    ${W.map(w=>{const sig=(w.signal||'HOLD');const rg=w.regime||'';const warm=rg==='warming-up';
      return `<tr><td>${w.symbol}</td><td>${fmt(w.price,6)}</td>
      <td class="muted">${warm?'⏳ warming-up':rg}</td>
      <td><span class="sig ${sig}">${sig}</span></td></tr>`;}).join('')}
    </table></div>`:'<div class="empty">Waiting for the first signal…</div>';

  const T=s.trades||[];
  document.getElementById('trades').innerHTML=T.length?`<div class="tablewrap"><table>
    <tr><th>Coin</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Net P/L</th></tr>
    ${T.map(t=>`<tr><td>${t.symbol}</td><td>${fmt(t.entry,6)}</td><td>${fmt(t.exit,6)}</td>
      <td class="muted">${t.reason||''}</td><td class="${cls(t.pnl)}">${sgn(t.pnl,4)} ${q}</td></tr>`).join('')}
    </table></div>`:'<div class="empty">No closed trades yet.</div>';
}

async function doStop(){
  if(!confirm('Stop the bot now?\\n\\nIt halts on the next check. Open positions are NOT auto-sold — you can manage them on KuCoin.')) return;
  try{await fetch('/api/stop',{method:'POST'});alert('STOP sent. The bot is shutting down.');}
  catch(e){alert('Could not reach the bot — it may already be stopped.');}
}

refresh(); setInterval(refresh,3000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
