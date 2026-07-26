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

import asyncio
import os
from dataclasses import dataclass

from bot import config, kucoin_client


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    # Required checks gate live trading; advisory ones are informational only.
    # Per-coin tradability is advisory: one halted coin in a watchlist must not
    # force the whole engine to paper mode — that coin is simply skipped at
    # order time (execution.validate_buy re-checks tradability before every buy).
    required: bool = True


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

    for sym in config.TRADE_SYMBOLS:
        try:
            info = await kucoin_client.get_symbol_info(sym)
            checks.append(Check(f"{sym} tradable", info["enable_trading"], "enableTrading flag", required=False))
            checks.append(
                Check(f"{sym} min size known", info["base_min_size"] > 0,
                      f"min {info['base_min_size']} base", required=False)
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(f"{sym} tradable", False, str(exc), required=False))

    return checks


def all_passed(checks: list[Check]) -> bool:
    """True if every REQUIRED check passed. Advisory checks (e.g. a single
    coin being tradable) are reported but do not gate live trading."""
    required = [c for c in checks if c.required]
    return bool(required) and all(c.passed for c in required)


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


async def _main() -> None:
    """`python -m bot.preflight` — run the read-only health checks and show
    the current live-trading gate status. Places no orders; arms nothing."""
    print("\n=== KuCoin health check (read-only) ===\n")
    checks = await run_health_checks()
    for c in checks:
        mark = "PASS" if c.passed else "FAIL"
        tag = "" if c.required else " (advisory — skips just this coin)"
        print(f"  [{mark}] {c.name}{tag} — {c.detail}")

    allowed, reason = live_allowed()
    print(f"\nHealth checks : {'ALL PASS ✅' if all_passed(checks) else 'SOME FAILED ❌'}")
    print(f"Live gate     : {'OPEN' if allowed else 'CLOSED'} — {reason}")
    print("\nThis only checked your setup. It placed no orders and armed nothing.")
    await kucoin_client.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
