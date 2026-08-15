"""IBKR integration: TWS connection, orders, historical data, bar cache.

`IBKRClient` is exported lazily (PEP 562) rather than imported at package
import time. Eagerly importing it pulled `ib_async` into every consumer of
this package, including `ibkr.cache` -- which reads JSON files off disk and
has no business requiring a broker API library. The backtests import the
cache and should run without TWS installed at all.
"""
from __future__ import annotations

__all__ = ["IBKRClient"]


def __getattr__(name: str):
    if name == "IBKRClient":
        from .client import IBKRClient

        return IBKRClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
