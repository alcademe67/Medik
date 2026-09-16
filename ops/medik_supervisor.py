"""MEDIK connection supervisor — keeps the trading stack self-healing and hands-free.

WHY THIS EXISTS
    The bot (examples/medik_etf_live.py) already self-heals its OWN process via
    run_medik_etf.bat: if it exits at preflight because the gateway is down, the
    wrapper retries every 5 minutes. But nothing was healing the GATEWAY itself.
    On 2026-09-09 the Client Portal Gateway crashed on every boot (OpenJDK 25's
    Netty event loop could not open its AF_UNIX loopback self-pipe on the default
    temp path — "Unable to establish loopback connection"). The bot then retried
    into a gateway that would never come back, and the day was lost silently.

    This supervisor closes that gap. It watches the gateway, restarts it when it
    dies (using start_min.bat, which now carries the temp-dir fix), and keeps the
    authenticated session from idling out — so that when the bot retries, it finds
    a live gateway waiting.

WHAT IT HEALS AUTONOMOUSLY (no human)
    * Gateway process crashed or wedged  -> relaunch it, with backoff so a truly
      broken machine is not hammered forever.
    * Authenticated session going idle    -> tickle it every cycle.

WHAT IT CANNOT HEAL — so it ALERTS instead of pretending
    * Gateway up but NOT logged in -> only Ali's browser login at
      https://localhost:5000 (alcademe0209) can authenticate it. Detected and
      alerted (debounced). This module NEVER handles a password.
    * Gateway restarted too many times in a row -> escalate to a human; a boot
      loop means something the supervisor cannot fix (reboot / Winsock / creds).

WHAT IT WILL NEVER DO
    * Place, modify or cancel an order. There is deliberately no code path here
      that writes to any order endpoint — see tests/test_medik_supervisor.py,
      which fails the build if one is ever added. The ONLY thing authorised to
      trade is the bot, via its 19 gates, and only on genuinely real-time data.
    * Fabricate readiness. Below the ~$500 real-time-data minimum the feed is
      delayed and the bot idles by design; the supervisor reports that state
      honestly and never tries to force it live.

DESIGN
    The decision logic is pure (decide_gateway_action / should_restart /
    should_alert / run_cycle) and fully unit-tested. All I/O — probing the
    gateway, restarting it, reading equity, raising alerts — is injected as a
    `deps` object, so the tests never touch the network or the process table.
    Stdlib only: this runs unattended and a missing import at 06:45 is a lost day.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_DIR = Path(r"C:\Users\Administrator\clientportal.gw")
START_MIN_BAT = GATEWAY_DIR / "start_min.bat"
BOT_BAT = REPO_ROOT / "run_medik_etf.bat"
TWS_HOST, TWS_PORT = "127.0.0.1", 7496
BASE_URL = "https://localhost:5000/v1/api"
ACCOUNT = os.environ.get("MEDIK_ETF_ACCOUNT", "U26953060")

LOG_DIR = REPO_ROOT / "logs"
STOP_FILE = REPO_ROOT / "STOP_MEDIK"

# ---- the three gateway states, and the one action each demands
HEAL_RESTART = "RESTART_GATEWAY"       # not responding -> relaunch the process
ALERT_LOGIN = "ALERT_GATEWAY_LOGIN"    # responding but not authenticated -> human
TICKLE = "TICKLE"                      # authenticated -> keep the session alive


@dataclass(frozen=True)
class GatewayHealth:
    """What a single probe of the gateway found."""
    responding: bool       # the HTTP server answered at all (even 401)
    authenticated: bool    # the brokerage session is logged in and usable


@dataclass(frozen=True)
class RestartPolicy:
    cycle_sec: int = 60
    cooldown_sec: int = 90          # a restart needs ~20s to boot; don't double-fire
    max_attempts: int = 5           # ...within
    window_sec: int = 1800          # ...this rolling 30-minute window -> then escalate
    alert_cooldown_sec: int = 1800  # 30 min between repeats of the same human-needed alert
    equity_threshold: float = 500.0 # IBKR real-time-data minimum
    max_reauth: int = 3             # /iserver/reauthenticate tries before it's a human's job
    reauth_cooldown_sec: int = 30   # reauth is async; give it a beat between tries
    bot_relaunch_cooldown_sec: int = 300  # a relaunched bot needs time to boot before judging again
    max_bot_restarts: int = 4       # bot relaunches within window_sec before escalating to a human


@dataclass(frozen=True)
class SupervisorState:
    restart_times: tuple = ()               # unix seconds of recent restarts
    reauth_times: tuple = ()                # unix seconds of recent reauthenticate tries
    last_login_alert: float | None = None
    last_flapping_alert: float | None = None
    crossed_announced: bool = False         # $500 announced once until it dips back
    bot_start_times: tuple = ()             # unix seconds of recent bot relaunches
    last_bot_alert: float | None = None     # debounce the bot-flapping human alert
    last_tws_alert: float | None = None     # debounce the TWS-API-down human alert


# ----------------------------------------------------------------- pure logic

def decide_gateway_action(health: GatewayHealth) -> str:
    """Map an observed gateway state onto the single correct action.

    Not responding is the only thing a restart can fix. A responding-but-
    unauthenticated gateway must NOT be restarted — restarting throws away a
    login the human is about to give (or just gave) and achieves nothing; the
    fix there is a person, so it is an alert, not a heal.
    """
    if not health.responding:
        return HEAL_RESTART
    if not health.authenticated:
        return ALERT_LOGIN
    return TICKLE


def should_restart(now: float, restart_times, policy: RestartPolicy) -> tuple[bool, str]:
    """(may_restart, reason_code). reason_code in {ok, cooldown, flapping}."""
    recent = [t for t in restart_times if now - t <= policy.window_sec]
    if recent and (now - max(recent)) < policy.cooldown_sec:
        return False, "cooldown"
    if len(recent) >= policy.max_attempts:
        return False, "flapping"
    return True, "ok"


def should_alert(now: float, last_alert: float | None, cooldown_sec: int) -> bool:
    """True if enough time has passed to repeat a human-needed alert."""
    return last_alert is None or (now - last_alert) >= cooldown_sec


def prune(restart_times, now: float, window_sec: int) -> tuple:
    return tuple(t for t in restart_times if now - t <= window_sec)


def run_cycle(health: GatewayHealth, state: SupervisorState, now: float,
              policy: RestartPolicy, deps) -> tuple[SupervisorState, list]:
    """One supervision step. Returns (new_state, actions_taken).

    `deps` supplies the side effects: restart(), tickle(), alert(kind, msg),
    read_equity() -> float | None. Keeping them injected is what makes this the
    unit under test rather than a thing that can only be checked in production.
    """
    actions: list[str] = []
    decision = decide_gateway_action(health)

    if decision == HEAL_RESTART:
        ok, why = should_restart(now, state.restart_times, policy)
        if ok:
            deps.restart()
            actions.append("restart_gateway")
            state = replace(state, restart_times=prune(state.restart_times, now, policy.window_sec) + (now,))
        elif why == "flapping":
            if should_alert(now, state.last_flapping_alert, policy.alert_cooldown_sec):
                deps.alert("gateway_flapping",
                           "Gateway has crashed and failed to stay up repeatedly — "
                           "it needs a human (reboot, or check the login). Auto-restart paused.")
                actions.append("alert_flapping")
                state = replace(state, last_flapping_alert=now)
            else:
                actions.append("wait_flapping")
        else:  # cooldown
            actions.append("wait_cooldown")
        return state, actions

    if decision == ALERT_LOGIN:
        # Try to recover WITHOUT a human first. /iserver/reauthenticate revives
        # the brokerage session as long as the browser SSO cookie is still valid
        # — which covers the common mid-day session drop. Only a full SSO expiry
        # (~daily) or a reboot genuinely needs the owner's browser login, and
        # that is the sole thing this cannot do (it never handles a password).
        recent_reauth = prune(state.reauth_times, now, policy.window_sec)
        in_cooldown = bool(recent_reauth) and (now - max(recent_reauth)) < policy.reauth_cooldown_sec
        if len(recent_reauth) < policy.max_reauth and not in_cooldown:
            recovered = deps.reauthenticate()
            state = replace(state, reauth_times=recent_reauth + (now,))
            if recovered:
                actions.append("reauthenticate_ok")
                # session is back — clear the reauth history and let the next
                # cycle fall through to TICKLE
                state = replace(state, reauth_times=(), last_login_alert=None)
            else:
                actions.append("reauthenticate_try")
            return state, actions
        if in_cooldown:
            actions.append("wait_reauth_cooldown")
            return state, actions
        # reauth budget exhausted — the SSO itself has expired; only a human login fixes it
        if should_alert(now, state.last_login_alert, policy.alert_cooldown_sec):
            deps.alert("gateway_login",
                       "Gateway session expired and auto-reauthenticate failed — the "
                       "browser login has lapsed. Log in at https://localhost:5000 as "
                       "alcademe0209 (or over Tailscale at https://100.92.227.2:5000).")
            actions.append("alert_login")
            state = replace(state, last_login_alert=now)
        else:
            actions.append("wait_login_alert")
        return state, actions

    # TICKLE — the gateway is healthy and authenticated.
    deps.tickle()
    actions.append("tickle")
    # A healthy cycle clears the restart AND reauth history: a crash or drop next
    # week must not be judged against attempts that already recovered.
    state = replace(state, restart_times=(), reauth_times=())

    equity = deps.read_equity()
    if equity is not None:
        if equity >= policy.equity_threshold and not state.crossed_announced:
            deps.alert("equity_threshold",
                       f"Equity crossed ${policy.equity_threshold:.0f}: ${equity:.2f}. "
                       "The bot will verify the feed is genuinely real-time before it trades.")
            actions.append("alert_equity")
            state = replace(state, crossed_announced=True)
        elif equity < policy.equity_threshold and state.crossed_announced:
            # dipped back under — re-arm the announcement for the next crossing
            state = replace(state, crossed_announced=False)
    return state, actions


def should_start_bot(now: float, bot_start_times, policy: RestartPolicy) -> tuple[bool, str]:
    """(may_start, reason_code). Mirrors should_restart but for the bot process:
    a fresh relaunch gets bot_relaunch_cooldown_sec to boot before we judge it
    again, and too many relaunches in the window means a human is needed."""
    recent = [t for t in bot_start_times if now - t <= policy.window_sec]
    if recent and (now - max(recent)) < policy.bot_relaunch_cooldown_sec:
        return False, "cooldown"
    if len(recent) >= policy.max_bot_restarts:
        return False, "flapping"
    return True, "ok"


def _second_sunday(year: int, month: int) -> int:
    first_sun = 1 + (6 - datetime(year, month, 1).weekday()) % 7
    return first_sun + 7


def _first_sunday(year: int, month: int) -> int:
    return 1 + (6 - datetime(year, month, 1).weekday()) % 7


def is_regular_session(now_utc: datetime) -> bool:
    """True during the US regular session (Mon-Fri 09:30-16:00 ET), computing the
    ET offset from US DST rules directly so this needs no tz database (the
    supervisor is stdlib-only and must not fail on a missing tzdata). Holidays
    are NOT modelled — on a holiday the bot simply stays up idle rather than
    exiting, so it never looks like a crash; the only failure this must avoid is
    the after-close 'clean exit' being mistaken for a flap."""
    naive = now_utc.astimezone(timezone.utc).replace(tzinfo=None)
    y = naive.year
    edt = datetime(y, 3, _second_sunday(y, 3), 7) <= naive < datetime(y, 11, _first_sunday(y, 11), 6)
    et = naive - timedelta(hours=4 if edt else 5)
    if et.weekday() >= 5:
        return False
    return dtime(9, 30) <= et.time() < dtime(16, 0)


def run_ancillary_cycle(state: SupervisorState, now: float,
                        policy: RestartPolicy, deps) -> tuple[SupervisorState, list]:
    """Heal the two things the gateway cycle does not: the BOT PROCESS and the
    TWS ORDER PATH. Kept separate from run_cycle so the gateway logic (and its
    24 tests) stay exactly as they were.

    Only acts DURING MARKET HOURS. Outside the regular session the bot exits on
    purpose ('clean exit -- done for the day') and TWS may be closed — relaunching
    then produced a false 'bot flapping' alert (2026-09-16). When the market is
    closed a down bot/TWS is expected, so we heal nothing, clear any stale flap
    history, and stay silent.

    * Bot not running        -> relaunch it (run_medik_etf.bat), with the same
      cooldown/flap discipline as the gateway; escalate to a human if it will
      not stay up.
    * TWS API socket down     -> ALERT only. Bringing 7496 back needs a TWS login,
      which needs Ali's 2FA — no script can supply that, so we never pretend to
      fix it, we surface it fast and debounced.
    """
    actions: list[str] = []

    if not deps.market_open():
        # market closed: a down bot/TWS is the expected state. Clear stale history
        # so tomorrow's open starts clean, and do not relaunch or alert.
        if state.bot_start_times or state.last_bot_alert is not None or state.last_tws_alert is not None:
            state = replace(state, bot_start_times=(), last_bot_alert=None, last_tws_alert=None)
        actions.append("market_closed_idle")
        return state, actions

    if not deps.bot_running():
        ok, why = should_start_bot(now, state.bot_start_times, policy)
        if ok:
            deps.start_bot()
            actions.append("start_bot")
            state = replace(state, bot_start_times=prune(state.bot_start_times, now, policy.window_sec) + (now,))
        elif why == "flapping":
            if should_alert(now, state.last_bot_alert, policy.alert_cooldown_sec):
                deps.alert("bot_flapping",
                           "The ETF bot keeps exiting and will not stay up — needs a human. "
                           "Check logs/medik_etf_*.log (often TWS API down or a bad config).")
                actions.append("alert_bot_flapping")
                state = replace(state, last_bot_alert=now)
            else:
                actions.append("wait_bot_flapping")
        else:
            actions.append("wait_bot_cooldown")
    else:
        # a healthy bot clears its relaunch history and re-arms the alert
        if state.bot_start_times or state.last_bot_alert is not None:
            state = replace(state, bot_start_times=(), last_bot_alert=None)

    if not deps.tws_api_up():
        if should_alert(now, state.last_tws_alert, policy.alert_cooldown_sec):
            deps.alert("tws_api_down",
                       "TWS order path is DOWN — port 7496 not listening, so the bot cannot "
                       "place orders. Open TWS, log in as alcademe67, then File > Global "
                       "Configuration > API > Settings: enable ActiveX and Socket Clients, "
                       "port 7496, allow 127.0.0.1, uncheck Read-Only API.")
            actions.append("alert_tws_down")
            state = replace(state, last_tws_alert=now)
        else:
            actions.append("wait_tws_alert")
    else:
        if state.last_tws_alert is not None:
            state = replace(state, last_tws_alert=None)  # re-arm once it recovers

    return state, actions


# ------------------------------------------------------------------- real I/O

def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # loopback self-signed cert; never leaves the box
    return ctx


def probe_gateway(base_url: str = BASE_URL, timeout: float = 6.0) -> GatewayHealth:
    """Probe /tickle and classify the gateway into a GatewayHealth.

    Connection refused / timeout  -> not responding (process down)   -> restart.
    Any HTTP answer, incl. 401     -> responding; authenticated iff the body's
    iserver.authStatus.authenticated is true.
    """
    req = urllib.request.Request(base_url + "/tickle", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
            body = json.loads(r.read().decode("utf-8") or "null") or {}
        authed = bool(((body.get("iserver") or {}).get("authStatus") or {}).get("authenticated"))
        return GatewayHealth(responding=True, authenticated=authed)
    except urllib.error.HTTPError:
        return GatewayHealth(responding=True, authenticated=False)
    except (urllib.error.URLError, TimeoutError, OSError):
        return GatewayHealth(responding=False, authenticated=False)


def read_equity(base_url: str = BASE_URL, account: str = ACCOUNT,
                timeout: float = 8.0) -> float | None:
    """Net liquidation for the account, or None if it cannot be read cleanly."""
    url = f"{base_url}/portfolio/{account}/summary"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout,
                                    context=_ssl_ctx()) as r:
            data = json.loads(r.read().decode("utf-8") or "null") or {}
        v = data.get("netliquidation")
        if isinstance(v, dict):
            v = v.get("amount")
        return float(v) if v is not None else None
    except Exception:
        return None


def restart_gateway() -> None:
    """Relaunch the gateway via start_min.bat (self-detaches, carries the fix)."""
    if not START_MIN_BAT.exists():
        _log(f"CANNOT RESTART: {START_MIN_BAT} not found")
        return
    subprocess.Popen(["cmd", "/c", str(START_MIN_BAT)], cwd=str(GATEWAY_DIR),
                     creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    _log("issued gateway restart via start_min.bat")


def tws_api_up(host: str = TWS_HOST, port: int = TWS_PORT, timeout: float = 3.0) -> bool:
    """True iff TWS's API socket is accepting connections. A plain TCP connect —
    it never sends an API handshake, so it cannot disturb the bot's own session."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def market_open_now() -> bool:
    return is_regular_session(datetime.now(timezone.utc))


def bot_process_running() -> bool:
    """True iff a live examples/medik_etf_live.py python process exists. On any
    doubt returns True, so the supervisor never spawns a DUPLICATE bot — the bot
    also holds a singleton lock, but not launching is the safer failure."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*medik_etf_live.py*' } | "
             "Measure-Object).Count"],
            capture_output=True, text=True, timeout=20)
        s = (out.stdout or "").strip()
        return not (s.isdigit() and int(s) == 0)
    except Exception:
        return True  # cannot tell -> assume running, never risk a duplicate


def start_bot() -> None:
    """Relaunch the bot via its own wrapper (run_medik_etf.bat carries the live
    flags and its own 5-minute preflight retry loop). Never touches config."""
    if not BOT_BAT.exists():
        _log(f"CANNOT START BOT: {BOT_BAT} not found")
        return
    subprocess.Popen(["cmd", "/c", str(BOT_BAT)], cwd=str(REPO_ROOT),
                     creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    _log("issued bot start via run_medik_etf.bat")


def reauthenticate(base_url: str = BASE_URL, timeout: float = 8.0) -> bool:
    """Ask the gateway to re-establish the brokerage session, then confirm.

    POST /iserver/reauthenticate revives a dropped session using the browser
    SSO cookie WITHOUT a password — it works only while that cookie is still
    valid. It is asynchronous, so we give it a beat and then re-probe. Returns
    True only if the session is genuinely authenticated afterwards. This never
    handles a credential; a lapsed SSO simply returns False and becomes a
    human-login alert.
    """
    try:
        urllib.request.urlopen(
            urllib.request.Request(base_url + "/iserver/reauthenticate", data=b"", method="POST"),
            timeout=timeout, context=_ssl_ctx()).read()
    except Exception:
        return False
    time.sleep(4)
    healed = probe_gateway(base_url, timeout)
    if healed.authenticated:
        _log("reauthenticate: session recovered without a human login")
    return healed.authenticated


def _log(msg: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        (LOG_DIR / "medik_supervisor.log").open("a", encoding="utf-8").write(line + "\n")
    except OSError:
        pass


# Durable phone channel (the owner's existing ntfy topic, also used by
# ConnectivityKeeper) so alerts reach the phone even with NO Claude session open.
# Only ever carries "a human needs to do X" text — never a credential or a trade.
NTFY_URL = os.environ.get("MEDIK_NTFY_URL", "https://ntfy.sh/alca-kraken-f07c84d02fd2a45f")


def _notify_phone(kind: str, msg: str) -> None:
    """Best-effort push to the owner's ntfy topic. A failure here must never
    disturb the supervisor loop, so every error is swallowed."""
    try:
        req = urllib.request.Request(
            NTFY_URL, data=msg.encode("utf-8"), method="POST",
            headers={"Title": f"MEDIK: {kind}", "Priority": "high", "Tags": "warning"})
        urllib.request.urlopen(req, timeout=6).read()
    except Exception:
        pass


def _alert(kind: str, msg: str) -> None:
    """Record a human-needed alert: to the log, to a small JSON file a watching
    Claude session can pick up, AND to the owner's phone via ntfy so it lands
    even when nothing is watching. No password, no trade."""
    _log(f"ALERT[{kind}]: {msg}")
    try:
        LOG_DIR.mkdir(exist_ok=True)
        (LOG_DIR / "medik_supervisor_alert.json").write_text(
            json.dumps({"kind": kind, "msg": msg,
                        "ts": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8")
    except OSError:
        pass
    _notify_phone(kind, msg)


LOCK_FILE = LOG_DIR / "medik_supervisor.lock"


def _pid_alive(pid: int) -> bool:
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def _acquire_singleton() -> bool:
    """True if we are the only supervisor. Prevents the Startup entry and a
    manual start from running two watchdogs that would race to restart the
    gateway (two gateways fighting for port 5000)."""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        if LOCK_FILE.exists():
            old = LOCK_FILE.read_text(encoding="utf-8").strip()
            if old.isdigit() and int(old) != os.getpid() and _pid_alive(int(old)):
                return False
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return True  # a lock-file glitch must never stop the watchdog itself


class _LiveDeps:
    """The production side effects, wired to the real gateway."""
    def restart(self): restart_gateway()
    def tickle(self):
        try:
            urllib.request.urlopen(urllib.request.Request(BASE_URL + "/tickle", data=b"", method="POST"),
                                   timeout=6.0, context=_ssl_ctx()).read()
        except Exception:
            pass
    def reauthenticate(self): return reauthenticate()
    def alert(self, kind, msg): _alert(kind, msg)
    def read_equity(self): return read_equity()
    def bot_running(self): return bot_process_running()
    def start_bot(self): start_bot()
    def tws_api_up(self): return tws_api_up()
    def market_open(self): return market_open_now()


def main() -> int:
    policy = RestartPolicy()
    state = SupervisorState()
    deps = _LiveDeps()
    if not _acquire_singleton():
        _log("another supervisor is already running — exiting (singleton).")
        return 0
    _log(f"supervisor started (cycle={policy.cycle_sec}s, account={ACCOUNT})")
    while True:
        if STOP_FILE.exists():
            _log("STOP_MEDIK present — supervisor exiting cleanly.")
            return 0
        try:
            health = probe_gateway()
            now = time.time()
            state, actions = run_cycle(health, state, now, policy, deps)
            state, anc = run_ancillary_cycle(state, now, policy, deps)
            _log(f"health(responding={health.responding},auth={health.authenticated}) "
                 f"-> {','.join(actions + anc)}")
        except Exception as exc:  # a supervisor that dies helps no one
            _log(f"cycle error (continuing): {type(exc).__name__}: {exc}")
        time.sleep(policy.cycle_sec)


if __name__ == "__main__":
    sys.exit(main())
