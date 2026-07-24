"""Startup health checks and the live-trading gate.

`run_health_checks()` verifies everything that is *machine-checkable*
before the bot is allowed to trade live. Two required safety properties
can't be introspected from the KuCoin API — that the key has TRADE
permission and does NOT have withdrawal permission — so those are
attested by the operator in `bot.golive`, and separately guaranteed by
construction: this codebase never calls any withdrawal/transfer endpoint.

`live_allowed()` is the single gate the engine consults. Live requires
BOTH LIVE_TRADING=true AND the go-live token written by `bot.golive`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from bot import config, kucoin_client


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


async def run_health_checks() -> list[Check]:
    """Run the machine-checkable startup checks, in order, stopping early
    when a failure makes later checks meaningless."""
    checks: list[Check] = []

    try:
        ts = await kucoin_client.get_server_time()
        checks.append(Check("KuCoin reachable", True, f"server time {ts}"))
    except Exception as exc:  # noqa: BLE001 - report any failure as a failed check
        checks.append(Check("KuCoin reachable", False, str(exc)))
        return checks

    keys_present = all(
        [config.KUCOIN_API_KEY, config.KUCOIN_API_SECRET, config.KUCOIN_API_PASSPHRASE]
    )
    checks.append(Check("API keys present", keys_present, "loaded from .env" if keys_present else "missing"))
    if not keys_present:
        return checks

    try:
        bal = await kucoin_client.get_available_balance(config.QUOTE_CURRENCY)
        checks.append(Check("API keys valid (auth OK)", True, "authenticated read succeeded"))
        checks.append(
            Check(f"{config.QUOTE_CURRENCY} balance available", bal > 0, f"{bal:.2f} {config.QUOTE_CURRENCY}")
        )
    except kucoin_client.AuthError:
        checks.append(Check("API keys valid (auth OK)", False, "KuCoin rejected the credentials"))
        return checks
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("API keys valid (auth OK)", False, str(exc)))
        return checks

    try:
        info = await kucoin_client.get_symbol_info(config.TRADE_SYMBOL)
        checks.append(Check(f"{config.TRADE_SYMBOL} tradable", info["enable_trading"], "enableTrading flag"))
        checks.append(
            Check(f"{config.TRADE_SYMBOL} min size known", info["base_min_size"] > 0,
                  f"min {info['base_min_size']} base")
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check(f"{config.TRADE_SYMBOL} tradable", False, str(exc)))

    return checks


def all_passed(checks: list[Check]) -> bool:
    return bool(checks) and all(c.passed for c in checks)


def golive_confirmed() -> bool:
    """True if the operator has run `python -m bot.golive` successfully."""
    return os.path.exists(config.GOLIVE_TOKEN_PATH)


def live_allowed() -> tuple[bool, str]:
    """The engine's gate. Returns (allowed, human reason)."""
    if not config.LIVE_TRADING:
        return False, "LIVE_TRADING is false (paper mode)"
    if not golive_confirmed():
        return False, "go-live not confirmed — run `python -m bot.golive`"
    return True, "live trading armed"
