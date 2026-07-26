"""`python -m bot.golive` — the human gate before any live trading.

Runs the startup health checks, prints the safety checklist and your risk
configuration, then requires you to (a) attest to several items you alone
can confirm and (b) type an exact phrase. Only then does it write the
go-live token the engine requires. It NEVER places an order and NEVER
prints secrets.
"""

import asyncio
import time

from bot import config, preflight

CONFIRM_PHRASE = "I ACCEPT THE RISK"

# Things the API can't verify — only you can. All must be 'y' to proceed.
ATTESTATIONS = [
    "My KuCoin API key has TRADE permission enabled.",
    "My KuCoin API key does NOT have withdrawal/transfer permission.",
    "I have BACKTESTED this strategy and it showed an edge after fees.",
    "I have PAPER-TRADED this strategy and I'm satisfied with how it behaves.",
    "I am only risking money I can afford to lose entirely.",
]


async def _run() -> None:
    print("\n=== KuCoin live-trading go-live check ===\n")
    print("Running startup health checks...\n")
    checks = await preflight.run_health_checks()
    for c in checks:
        print(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name} — {c.detail}")

    if not preflight.all_passed(checks):
        print("\n❌ Not all health checks passed. Fix the above and re-run.")
        print("   Live trading NOT enabled.\n")
        return

    r = config.RISK
    print("\nRisk configuration that will apply:")
    print(f"  • risk per trade      : {r.max_risk_per_trade * 100:.2f}% of equity (to the stop)")
    print(f"  • max position size   : {r.max_position_pct:.1f}% of equity (notional per trade)")
    print(f"  • max open positions  : {r.max_open_positions}")
    print(f"  • daily loss limit    : {r.daily_loss_limit_pct:.1f}%")
    print(f"  • daily order cap     : {r.max_daily_orders} entries/day")
    print(f"  • pause after losses  : {r.max_consecutive_losses} in a row")
    print(f"  • stop-loss / target  : {r.stop_atr_mult}×ATR / {r.take_profit_rr}:1")
    print(f"  • symbol              : {config.TRADE_SYMBOL}")

    print("\nConfirm each item (type 'y' for yes):")
    for item in ATTESTATIONS:
        if input(f"  - {item}  [y/N] ").strip().lower() != "y":
            print("\n❌ Not confirmed. Live trading NOT enabled.\n")
            return

    print()
    if input(f'Type exactly "{CONFIRM_PHRASE}" to arm live trading: ').strip() != CONFIRM_PHRASE:
        print("\n❌ Phrase did not match. Live trading NOT enabled.\n")
        return

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(config.GOLIVE_TOKEN_PATH, "w") as fh:
        fh.write(f"go-live confirmed {stamp}\n")
    print(f"\n✅ Go-live confirmed (token: {config.GOLIVE_TOKEN_PATH}).")
    print("   The engine will trade live ONLY if LIVE_TRADING=true as well.")
    print(f"   To disarm at any time, delete {config.GOLIVE_TOKEN_PATH}.\n")


if __name__ == "__main__":
    asyncio.run(_run())
