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
    prune,
    run_cycle,
    should_alert,
    should_restart,
)

POLICY = RestartPolicy()


class FakeDeps:
    """Records side effects instead of performing them."""
    def __init__(self, equity=None, reauth=False):
        self.calls = []
        self.alerts = []
        self._equity = equity
        self._reauth = reauth      # what reauthenticate() should return

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
