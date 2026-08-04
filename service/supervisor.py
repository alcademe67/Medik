"""Background trading service entry point.

    python service/supervisor.py

Designed to be launched by Windows Task Scheduler at boot/logon (see
service/SETUP_WINDOWS.md) via service/run_supervisor.bat. Runs until killed;
Task Scheduler's own "restart on failure" setting is the outermost safety
net if this process itself dies unexpectedly (every code path below tries
hard not to let that happen -- see the per-cycle and per-reconnect
try/except blocks).

Loop structure:
  outer loop  -- (re)connect to TWS/IB Gateway, retrying indefinitely with
                 backoff if it's not reachable yet (requirement: "retry
                 automatically until connected")
  inner loop  -- while connected: periodic health checks, one pipeline
                 cycle per poll interval during market hours, sleep,
                 repeat. A cycle that raises is logged and alerted, not
                 fatal -- the loop continues (requirement: crash recovery
                 without killing the whole service over one bad cycle).
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ibkr.client import IBKRClient
from service import health
from service.alerts import alert_critical
from service.config import load_config
from service.logging_setup import setup_logging
from service.market_hours import market_is_open
from service.pipeline import run_cycle

RECONNECT_ALERT_EVERY_N_FAILURES = 3
OUTER_RETRY_SLEEP_SECONDS = 60
MODE_MISMATCH_RECHECK_SECONDS = 1800


def _verify_account_matches_mode(ib, mode: str) -> tuple[bool, str]:
    accounts = ib.managedAccounts()
    is_paper = bool(accounts) and all(a.startswith("D") for a in accounts)
    if mode == "PAPER" and not is_paper:
        return False, f"MODE=PAPER but connected accounts {accounts} are not paper ids"
    if mode == "LIVE" and is_paper:
        return False, f"MODE=LIVE but connected accounts {accounts} look like paper ids"
    return True, ""


def main() -> None:
    config = load_config()
    logger = setup_logging(config.log_dir, name="service")

    logger.info("=" * 70)
    logger.info(f"Medik trading service starting -- MODE={config.mode}")
    if config.mode == "LIVE":
        logger.info(
            "LIVE mode: pipeline runs fully autonomously through scan -> score -> "
            "risk engine, then QUEUES qualifying setups for human review. "
            "No order is ever submitted, modified, or cancelled automatically."
        )
    else:
        logger.info("PAPER mode: fully autonomous, including order placement, on the paper account only.")
    logger.info(f"host={config.host} port={config.port} poll_interval={config.poll_interval_seconds}s")
    logger.info("=" * 70)

    consecutive_connect_failures = 0

    while True:  # outer reconnect loop
        client = IBKRClient(host=config.host, port=config.port, client_id=config.client_id)
        try:
            ib = client.connect(timeout=10, retries=5, backoff=2.0)
        except Exception as exc:
            consecutive_connect_failures += 1
            logger.error(f"connect failed (attempt group {consecutive_connect_failures}): {exc}")
            if consecutive_connect_failures % RECONNECT_ALERT_EVERY_N_FAILURES == 1:
                alert_critical(config.log_dir, logger, "Cannot reach TWS/IB Gateway",
                                f"{consecutive_connect_failures} attempt groups failed: {exc}")
            time.sleep(OUTER_RETRY_SLEEP_SECONDS)
            continue

        consecutive_connect_failures = 0
        logger.info(f"connected. managed accounts: {ib.managedAccounts()}")

        ok, reason = _verify_account_matches_mode(ib, config.mode)
        if not ok:
            alert_critical(config.log_dir, logger, "Account/mode mismatch -- service paused", reason)
            client.disconnect()
            time.sleep(MODE_MISMATCH_RECHECK_SECONDS)
            continue

        cycle_count = 0
        paused = False
        while ib.isConnected():
            cycle_count += 1

            if cycle_count == 1 or cycle_count % config.health_check_every_n_cycles == 0:
                checks = [
                    ("connectivity", health.check_connectivity(ib)),
                    ("account", health.check_account_health(ib)),
                    ("market_data", health.check_market_data_health(ib)),
                ]
                failed = [(name, msg) for name, (ok, msg) in checks if not ok]
                if failed:
                    paused = True
                    detail = "; ".join(f"{name}: {msg}" for name, msg in failed)
                    alert_critical(config.log_dir, logger, "Health check failed -- trading paused", detail)
                else:
                    if paused:
                        logger.info("health checks recovered -- resuming trading")
                    paused = False
                    logger.info("health checks OK: " + "; ".join(f"{n}={m}" for n, (_, m) in checks))

            if paused:
                logger.warning("paused (last health check failed) -- skipping cycle")
            elif not market_is_open(config):
                if cycle_count == 1:
                    logger.info("market closed -- idling until next session")
            else:
                try:
                    result = run_cycle(ib, config.mode, logger)
                    if not result.ran:
                        logger.info(f"cycle skipped: {result.reason}")
                    else:
                        logger.info(
                            f"cycle done: evaluated={result.candidates_evaluated} "
                            f"placed={result.new_orders_placed} queued={result.new_orders_queued}"
                        )
                        if result.new_orders_queued:
                            alert_critical(
                                config.log_dir, logger, "New trades ready for review",
                                f"{len(result.new_orders_queued)} queued: {result.new_orders_queued}. "
                                "Run examples/review_pending_orders.py to act on them.",
                            )
                except Exception:
                    tb = traceback.format_exc()
                    logger.error(f"cycle crashed:\n{tb}")
                    alert_critical(config.log_dir, logger, "Trading cycle crashed (recovering)",
                                    tb.splitlines()[-1] if tb else "unknown error")

            time.sleep(config.poll_interval_seconds)

        logger.warning("disconnected from TWS/IB Gateway -- will attempt to reconnect")
        alert_critical(config.log_dir, logger, "Disconnected from TWS/IB Gateway", "Reconnecting...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        # Top-level catch-all: log the fatal error and exit non-zero so
        # Task Scheduler's "restart on failure" setting can restart the
        # process. Every code path above tries to avoid ever reaching here.
        logging_fallback = setup_logging(load_config().log_dir, name="service")
        logging_fallback.critical(f"FATAL, service exiting:\n{traceback.format_exc()}")
        sys.exit(1)
