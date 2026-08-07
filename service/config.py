"""Configuration for the background trading service, loaded from
environment variables (.env). See .env.example for every key this reads.

MODE has exactly two valid values:

  PAPER -- fully autonomous. Connects on the paper port (7497), verifies
           every managed account id starts with "D" (IBKR's paper prefix),
           and places bracket orders with no per-trade confirmation. This
           is the same guarded behavior as examples/autotrade_paper.py,
           run continuously by the supervisor instead of once.

  LIVE  -- NOT autonomous for order submission. Connects on the live port
           (7496), runs the full scan/score/risk-engine pipeline against
           real account data, and queues qualifying setups to the
           journal's pending_orders table with a desktop alert. Nothing is
           submitted, modified, or cancelled automatically -- a human runs
           examples/review_pending_orders.py to actually place anything.
           This is a structural limit, not a missing feature: there is no
           code path in this service that calls ib.placeOrder while
           connected to a live account.

Defaults to PAPER if MODE is unset or unrecognized, since an unrecognized
value should never silently fall through to live-anything.

SCAN_ENABLED gates the pipeline independently of MODE, and **defaults to
false** (owner decision, 2026-08-07). The gate and pullback strategies the
pipeline runs were both backtested to negative expectancy, and the account's
adopted strategy is QQQ buy-and-hold -- which is deliberately inert. Leaving
the scan loop running would produce alerts for setups that have already been
decided against, and the failure mode of buy-and-hold is intervening.

With SCAN_ENABLED=false the supervisor still connects, reconnects, and runs
health checks; it just never calls run_cycle. Set SCAN_ENABLED=true to turn
scanning back on -- e.g. to run a newly validated strategy, which per
CLAUDE.md must first be re-run through net_of_commission.py.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

VALID_MODES = ("PAPER", "LIVE")

_TRUTHY = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ServiceConfig:
    mode: str
    scan_enabled: bool
    host: str
    port: int
    client_id: int
    poll_interval_seconds: int
    log_dir: Path
    market_open_hour_et: int
    market_open_minute_et: int
    market_close_hour_et: int
    market_close_minute_et: int
    health_check_every_n_cycles: int


def load_config() -> ServiceConfig:
    raw_mode = os.environ.get("MODE", "PAPER").strip().upper()
    mode = raw_mode if raw_mode in VALID_MODES else "PAPER"

    default_port = 7497 if mode == "PAPER" else 7496
    port = int(os.environ.get("IBKR_PORT", default_port))

    # Opt-IN, not opt-out: an unset or misspelled value leaves scanning off,
    # which is the safe direction given the pipeline's strategies tested
    # negative and the adopted strategy is buy-and-hold.
    scan_enabled = os.environ.get("SCAN_ENABLED", "false").strip().lower() in _TRUTHY

    return ServiceConfig(
        mode=mode,
        scan_enabled=scan_enabled,
        host=os.environ.get("IBKR_HOST", "127.0.0.1"),
        port=port,
        client_id=int(os.environ.get("IBKR_SERVICE_CLIENT_ID", 77)),
        poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", 300)),
        log_dir=Path(os.environ.get("SERVICE_LOG_DIR", REPO_ROOT / "logs")),
        market_open_hour_et=9,
        market_open_minute_et=30,
        market_close_hour_et=16,
        market_close_minute_et=0,
        health_check_every_n_cycles=int(os.environ.get("HEALTH_CHECK_EVERY_N_CYCLES", 3)),
    )
