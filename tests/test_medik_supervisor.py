"""Tests for the MEDIK connection supervisor.

These are the "backtest" for everything this session established: that the
gateway crash is healed automatically, that the two human-only steps are
alerted (not faked), that a boot loop escalates instead of hammering, that the
$500 crossing is announced exactly once, and — the safety invariant — that the
supervisor can never place a trade.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops.medik_supervisor import (  # noqa: E402
    ALERT_LOGIN,
    HEAL_RESTART,
    TICKLE,
    GatewayHealth,
    RestartPolicy,
    SupervisorState,
    decide_gateway_action,
    is_regular_session,
    prune,
    run_ancillary_cycle,
    run_cycle,
    should_alert,
    should_restart,
    should_start_bot,
)

POLICY = RestartPolicy()


class FakeDeps:
    """Records side effects instead of performing them."""
    def __init__(self, equity=None, reauth=False, bot_running=True, tws_up=True,
                 market_open=True):
        self.calls = []
        self.alerts = []
        self._equity = equity
        self._reauth = reauth      # what reauthenticate() should return
        self._bot_running = bot_running
        self._tws_up = tws_up
        self._market_open = market_open

    def bot_running(self):
        self.calls.append("bot_running")
        return self._bot_running

    def start_bot(self):
        self.calls.append("start_bot")

    def tws_api_up(self):
        self.calls.append("tws_api_up")
        return self._tws_up

    def market_open(self):
        self.calls.append("market_open")
        return self._market_open

    def restart(self):
        self.calls.append("restart")

    def tickle(self):
        self.calls.append("tickle")

    def reauthenticate(self):
        self.calls.append("reauthenticate")
        return self._reauth

    def alert(self, kind, msg):
        self.alerts.append((kind, msg))

    def read_equity(self):
        self.calls.append("read_equity")
        return self._equity


# ------------------------------------------------------- decide_gateway_action

def test_decide_not_responding_restarts():
    assert decide_gateway_action(GatewayHealth(False, False)) == HEAL_RESTART

def test_decide_responding_unauthed_alerts():
    assert decide_gateway_action(GatewayHealth(True, False)) == ALERT_LOGIN

def test_decide_authed_tickles():
    assert decide_gateway_action(GatewayHealth(True, True)) == TICKLE


# --------------------------------------------------------------- should_restart

def test_restart_ok_when_no_history():
    assert should_restart(1000.0, (), POLICY) == (True, "ok")

def test_restart_blocked_during_cooldown():
    ok, why = should_restart(1000.0, (995.0,), POLICY)  # 5s ago < 90s cooldown
    assert ok is False and why == "cooldown"

def test_restart_flapping_after_max_in_window():
    # max_attempts restarts spread past cooldown but inside the window
    times = tuple(1000.0 - 100 * i for i in range(POLICY.max_attempts))
    ok, why = should_restart(1000.0 + POLICY.cooldown_sec + 1, times, POLICY)
    assert ok is False and why == "flapping"

def test_restart_ok_again_once_window_passes():
    old = tuple(0.0 + 100 * i for i in range(POLICY.max_attempts))
    now = 100000.0  # far beyond the window -> old restarts pruned
    assert should_restart(now, old, POLICY) == (True, "ok")


# ----------------------------------------------------------------- should_alert

def test_alert_first_time_always_allowed():
    assert should_alert(1000.0, None, POLICY.alert_cooldown_sec) is True

def test_alert_debounced_within_cooldown():
    assert should_alert(1000.0, 1000.0 - 10, POLICY.alert_cooldown_sec) is False

def test_alert_allowed_after_cooldown():
    assert should_alert(1000.0, 1000.0 - POLICY.alert_cooldown_sec, POLICY.alert_cooldown_sec) is True


def test_prune_drops_stale_entries():
    # window is 1800s; at now=5000 only t >= 3200 survives
    assert prune((0.0, 3300.0, 4800.0), 5000.0, POLICY.window_sec) == (3300.0, 4800.0)


# --------------------------------------------------------------------- run_cycle

def test_cycle_down_gateway_restarts_and_records():
    deps = FakeDeps()
    state, actions = run_cycle(GatewayHealth(False, False), SupervisorState(), 1000.0, POLICY, deps)
    assert deps.calls == ["restart"]
    assert "restart_gateway" in actions
    assert state.restart_times == (1000.0,)

def test_cycle_down_gateway_respects_cooldown():
    deps = FakeDeps()
    st = SupervisorState(restart_times=(995.0,))
    state, actions = run_cycle(GatewayHealth(False, False), st, 1000.0, POLICY, deps)
    assert deps.calls == []                 # did NOT restart (cooldown)
    assert actions == ["wait_cooldown"]

def test_cycle_flapping_escalates_once():
    deps = FakeDeps()
    times = tuple(1000.0 - 100 * i for i in range(POLICY.max_attempts))
    now = 1000.0 + POLICY.cooldown_sec + 1
    st = SupervisorState(restart_times=times)
    state, actions = run_cycle(GatewayHealth(False, False), st, now, POLICY, deps)
    assert deps.calls == []                 # never restarts while flapping
    assert deps.alerts and deps.alerts[0][0] == "gateway_flapping"
    assert state.last_flapping_alert == now
    # second consecutive flapping cycle inside cooldown does not re-alert
    deps2 = FakeDeps()
    _, actions2 = run_cycle(GatewayHealth(False, False), state, now + 5, POLICY, deps2)
    assert deps2.alerts == [] and actions2 == ["wait_flapping"]

def test_cycle_unauthed_tries_reauth_before_bothering_a_human():
    deps = FakeDeps(reauth=True)            # SSO cookie still valid -> recovers
    state, actions = run_cycle(GatewayHealth(True, False), SupervisorState(), 1000.0, POLICY, deps)
    assert deps.calls == ["reauthenticate"]
    assert actions == ["reauthenticate_ok"]
    assert deps.alerts == []                # never alerted a human
    assert state.reauth_times == ()         # recovery clears the history

def test_cycle_unauthed_reauth_fail_records_a_try():
    deps = FakeDeps(reauth=False)           # SSO cookie gone -> reauth fails
    state, actions = run_cycle(GatewayHealth(True, False), SupervisorState(), 1000.0, POLICY, deps)
    assert deps.calls == ["reauthenticate"]
    assert actions == ["reauthenticate_try"]
    assert deps.alerts == []                # not a human's problem yet
    assert state.reauth_times == (1000.0,)

def test_cycle_reauth_respects_cooldown():
    st = SupervisorState(reauth_times=(1000.0,))   # last try 10s ago (< 30s cooldown)
    deps = FakeDeps(reauth=False)
    _, actions = run_cycle(GatewayHealth(True, False), st, 1010.0, POLICY, deps)
    assert deps.calls == [] and actions == ["wait_reauth_cooldown"]

def test_cycle_reauth_exhausted_escalates_to_human_login():
    # max_reauth tries already spent, all past cooldown, none recovered
    st = SupervisorState(reauth_times=(900.0, 950.0, 1000.0))
    deps = FakeDeps(reauth=False)
    state, actions = run_cycle(GatewayHealth(True, False), st, 1040.0, POLICY, deps)
    assert deps.calls == []                 # reauth budget spent -> doesn't retry
    assert deps.alerts and deps.alerts[0][0] == "gateway_login"
    assert "alert_login" in actions
    # and it debounces the human alert on the next exhausted cycle
    deps2 = FakeDeps(reauth=False)
    _, actions2 = run_cycle(GatewayHealth(True, False), state, 1050.0, POLICY, deps2)
    assert deps2.alerts == [] and actions2 == ["wait_login_alert"]

def test_cycle_healthy_clears_reauth_history():
    deps = FakeDeps(equity=480.0)
    st = SupervisorState(reauth_times=(990.0, 995.0))
    state, _ = run_cycle(GatewayHealth(True, True), st, 1000.0, POLICY, deps)
    assert state.reauth_times == ()

def test_cycle_healthy_tickles_and_clears_restart_history():
    deps = FakeDeps(equity=480.0)
    st = SupervisorState(restart_times=(990.0, 995.0))
    state, actions = run_cycle(GatewayHealth(True, True), st, 1000.0, POLICY, deps)
    assert "tickle" in deps.calls
    assert state.restart_times == ()        # healthy cycle resets flap history
    assert deps.alerts == []                # below threshold -> no equity alert

def test_cycle_announces_500_once():
    deps = FakeDeps(equity=501.23)
    state, actions = run_cycle(GatewayHealth(True, True), SupervisorState(), 1000.0, POLICY, deps)
    assert deps.alerts and deps.alerts[0][0] == "equity_threshold"
    assert state.crossed_announced is True
    # next cycle still above -> no repeat
    deps2 = FakeDeps(equity=502.0)
    state2, _ = run_cycle(GatewayHealth(True, True), state, 1100.0, POLICY, deps2)
    assert deps2.alerts == []

def test_cycle_rearms_500_after_dip():
    deps = FakeDeps(equity=501.0)
    state, _ = run_cycle(GatewayHealth(True, True), SupervisorState(), 1000.0, POLICY, deps)
    assert state.crossed_announced is True
    deps2 = FakeDeps(equity=498.0)          # dipped back under
    state2, _ = run_cycle(GatewayHealth(True, True), state, 1100.0, POLICY, deps2)
    assert state2.crossed_announced is False
    deps3 = FakeDeps(equity=503.0)          # crosses again -> announces again
    state3, _ = run_cycle(GatewayHealth(True, True), state2, 1200.0, POLICY, deps3)
    assert deps3.alerts and deps3.alerts[0][0] == "equity_threshold"


# ------------------------------------------------------- safety / session facts

def test_supervisor_source_has_no_order_path():
    """Invariant: the supervisor can never trade. Fail the build if an order
    endpoint or an order-placing call is ever introduced into this module."""
    src = (Path(__file__).resolve().parent.parent / "ops" / "medik_supervisor.py").read_text(encoding="utf-8")
    lowered = src.lower()
    for forbidden in ("placeorder", "/iserver/account/", "reqmktdata", "/orders", "whatif"):
        assert forbidden not in lowered, f"supervisor must not reference {forbidden!r}"

def test_gateway_launcher_carries_the_afunix_fix():
    """The 2026-09-09 crash fix must be baked into the canonical launcher so a
    reboot or a fresh scheduled run comes up healthy."""
    bat = Path(r"C:\Users\Administrator\clientportal.gw\start_cpgw.bat")
    if not bat.exists():          # skip cleanly on a machine without the gateway
        return
    text = bat.read_text(encoding="utf-8").lower()
    assert "unixdomain.tmpdir" in text and "c:\\tmp" in text


# ------------------------------------------------- ancillary: bot + TWS healing

def test_should_start_bot_ok_when_no_recent():
    ok, why = should_start_bot(1000.0, (), POLICY)
    assert ok and why == "ok"

def test_should_start_bot_cooldown_after_recent_start():
    ok, why = should_start_bot(1000.0, (1000.0 - 10,), POLICY)  # 10s ago < 300s
    assert not ok and why == "cooldown"

def test_should_start_bot_flapping_escalates():
    now = 10_000.0
    # max relaunches, most recent one PAST the cooldown and all inside the window
    recent = tuple(now - 301 * (i + 1) for i in range(POLICY.max_bot_restarts))
    ok, why = should_start_bot(now, recent, POLICY)
    assert not ok and why == "flapping"

def test_ancillary_relaunches_dead_bot():
    d = FakeDeps(bot_running=False, tws_up=True)
    state, actions = run_ancillary_cycle(SupervisorState(), 1000.0, POLICY, d)
    assert "start_bot" in d.calls
    assert "start_bot" in actions
    assert state.bot_start_times and state.bot_start_times[-1] == 1000.0

def test_ancillary_healthy_bot_no_relaunch_and_clears_history():
    d = FakeDeps(bot_running=True, tws_up=True)
    prior = SupervisorState(bot_start_times=(500.0,), last_bot_alert=400.0)
    state, actions = run_ancillary_cycle(prior, 1000.0, POLICY, d)
    assert "start_bot" not in d.calls
    assert state.bot_start_times == () and state.last_bot_alert is None

def test_ancillary_bot_flapping_alerts_not_hammers():
    now = 10_000.0
    recent = tuple(now - 301 * (i + 1) for i in range(POLICY.max_bot_restarts))
    d = FakeDeps(bot_running=False, tws_up=True)
    state, actions = run_ancillary_cycle(SupervisorState(bot_start_times=recent), now, POLICY, d)
    assert "start_bot" not in d.calls          # did NOT relaunch again
    assert any(k == "bot_flapping" for k, _ in d.alerts)
    assert state.last_bot_alert == now

def test_ancillary_tws_down_alerts():
    d = FakeDeps(bot_running=True, tws_up=False)
    state, actions = run_ancillary_cycle(SupervisorState(), 1000.0, POLICY, d)
    assert any(k == "tws_api_down" for k, _ in d.alerts)
    assert "alert_tws_down" in actions
    assert state.last_tws_alert == 1000.0

def test_ancillary_tws_down_debounced():
    d = FakeDeps(bot_running=True, tws_up=False)
    prior = SupervisorState(last_tws_alert=1000.0)
    state, actions = run_ancillary_cycle(prior, 1000.0 + 60, POLICY, d)  # 60s < 1800s
    assert d.alerts == []                       # debounced, no repeat
    assert "wait_tws_alert" in actions

def test_ancillary_tws_recovers_rearms_alert():
    d = FakeDeps(bot_running=True, tws_up=True)
    prior = SupervisorState(last_tws_alert=1000.0)
    state, actions = run_ancillary_cycle(prior, 2000.0, POLICY, d)
    assert state.last_tws_alert is None         # re-armed for the next outage

def test_ancillary_never_places_orders():
    # the safety invariant, extended to the ancillary path
    d = FakeDeps(bot_running=False, tws_up=False)
    run_ancillary_cycle(SupervisorState(), 1000.0, POLICY, d)
    assert not any("order" in c.lower() or "trade" in c.lower() for c in d.calls)


# ------------------------------------- market-hours gate on ancillary healing

from datetime import datetime, timezone  # noqa: E402

def test_market_closed_does_not_relaunch_bot_or_alert():
    # the 2026-09-16 false-flap fix: after close the bot exits on purpose
    d = FakeDeps(bot_running=False, tws_up=False, market_open=False)
    state, actions = run_ancillary_cycle(SupervisorState(bot_start_times=(1.0, 2.0)), 1000.0, POLICY, d)
    assert "start_bot" not in d.calls          # never relaunches while closed
    assert d.alerts == []                       # never alerts while closed
    assert actions == ["market_closed_idle"]
    assert state.bot_start_times == ()          # stale flap history cleared

def test_market_open_still_heals():
    d = FakeDeps(bot_running=False, tws_up=True, market_open=True)
    state, actions = run_ancillary_cycle(SupervisorState(), 1000.0, POLICY, d)
    assert "start_bot" in d.calls               # open -> healing active as before

def test_is_regular_session_weekday_midday_edt():
    # 2026-09-16 is a Wednesday; 17:00 UTC = 13:00 EDT -> open
    assert is_regular_session(datetime(2026, 9, 16, 17, 0, tzinfo=timezone.utc)) is True

def test_is_regular_session_after_close():
    # 20:30 UTC = 16:30 EDT -> closed
    assert is_regular_session(datetime(2026, 9, 16, 20, 30, tzinfo=timezone.utc)) is False

def test_is_regular_session_weekend():
    # 2026-09-19 is a Saturday, midday -> closed
    assert is_regular_session(datetime(2026, 9, 19, 17, 0, tzinfo=timezone.utc)) is False

def test_is_regular_session_est_winter():
    # 2026-01-14 Wednesday; 15:00 UTC = 10:00 EST -> open (offset 5, not 4)
    assert is_regular_session(datetime(2026, 1, 14, 15, 0, tzinfo=timezone.utc)) is True
    # 14:00 UTC = 09:00 EST -> before the 09:30 open
    assert is_regular_session(datetime(2026, 1, 14, 14, 0, tzinfo=timezone.utc)) is False
