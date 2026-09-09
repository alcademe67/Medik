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
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_DIR = Path(r"C:\Users\Administrator\clientportal.gw")
START_MIN_BAT = GATEWAY_DIR / "start_min.bat"
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


@dataclass(frozen=True)
class SupervisorState:
    restart_times: tuple = ()               # unix seconds of recent restarts
    last_login_alert: float | None = None
    last_flapping_alert: float | None = None
    crossed_announced: bool = False         # $500 announced once until it dips back


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
        if should_alert(now, state.last_login_alert, policy.alert_cooldown_sec):
            deps.alert("gateway_login",
                       "Gateway is UP but not logged in. Log in at "
                       "https://localhost:5000 as alcademe0209 (or over Tailscale at "
                       "https://100.92.227.2:5000) to arm quotes.")
            actions.append("alert_login")
            state = replace(state, last_login_alert=now)
        else:
            actions.append("wait_login_alert")
        return state, actions

    # TICKLE — the gateway is healthy and authenticated.
    deps.tickle()
    actions.append("tickle")
    # A healthy cycle clears the restart history: a crash next week must not be
    # judged as "flapping" because of restarts that already recovered.
    state = replace(state, restart_times=())

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


def _log(msg: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        (LOG_DIR / "medik_supervisor.log").open("a", encoding="utf-8").write(line + "\n")
    except OSError:
        pass


def _alert(kind: str, msg: str) -> None:
    """Record a human-needed alert: to the log, and to a small JSON file a phone
    notifier (or a watching Claude session) can pick up. No password, no trade."""
    _log(f"ALERT[{kind}]: {msg}")
    try:
        LOG_DIR.mkdir(exist_ok=True)
        (LOG_DIR / "medik_supervisor_alert.json").write_text(
            json.dumps({"kind": kind, "msg": msg,
                        "ts": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8")
    except OSError:
        pass


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
    def alert(self, kind, msg): _alert(kind, msg)
    def read_equity(self): return read_equity()


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
            _log(f"health(responding={health.responding},auth={health.authenticated}) -> {','.join(actions)}")
        except Exception as exc:  # a supervisor that dies helps no one
            _log(f"cycle error (continuing): {type(exc).__name__}: {exc}")
        time.sleep(policy.cycle_sec)


if __name__ == "__main__":
    sys.exit(main())
