"""KuCoin spot-trading automation toolkit."""

from .client import KuCoinAPIError, KuCoinClient, MissingCredentials
from .orders import OrderRejected

__all__ = [
    "KuCoinClient",
    "KuCoinAPIError",
    "MissingCredentials",
    "OrderRejected",
]
