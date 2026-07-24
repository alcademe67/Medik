"""`python -m bot.balances` — show every non-zero balance in your KuCoin
account, across BOTH wallets (Funding/main and Trading). Read-only: it
places no orders and changes nothing. Answers "where is my money?".
"""

import asyncio

from bot import kucoin_client


def _account_label(kind: str) -> str:
    return {"trade": "Trading", "main": "Funding"}.get(kind, kind or "?")


async def _main() -> None:
    print("\n=== KuCoin account balances (read-only) ===\n")
    try:
        balances = await kucoin_client.fetch_balances()
    except kucoin_client.AuthError as exc:
        print(f"❌ {exc}")
        await kucoin_client.aclose()
        return
    except kucoin_client.KucoinError as exc:
        print(f"❌ Could not reach KuCoin: {exc}")
        await kucoin_client.aclose()
        return

    if not balances:
        print("No funds found in any account.")
        print("If you expected money here, double-check in the KuCoin app → Assets.")
    else:
        print(f"{'Coin':<8}{'Wallet':<10}{'Balance':>20}{'Available':>20}")
        print("-" * 58)
        for b in balances:
            print(
                f"{b['currency']:<8}{_account_label(b['type']):<10}"
                f"{b['balance']:>20}{b['available']:>20}"
            )
        print()
        print("To TRADE a coin, it must sit in your Trading wallet.")
        print("Move funds in the KuCoin app: Assets → Transfer → Funding → Trading.")

    await kucoin_client.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
