from __future__ import annotations

import os

from ib_async import IB

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7496  # TWS Live Trading socket port
DEFAULT_CLIENT_ID = 1


class IBKRClient:
    """Thin wrapper around an ib_async IB connection to a locally running TWS instance.

    TWS must already be open, logged into the target account, and configured under
    File > Global Configuration > API > Settings with "Enable ActiveX and Socket
    Clients" checked and 127.0.0.1 listed as a trusted IP.
    """

    def __init__(self, host: str | None = None, port: int | None = None, client_id: int | None = None):
        self.host = host or os.environ.get("IBKR_HOST", DEFAULT_HOST)
        self.port = int(port or os.environ.get("IBKR_PORT", DEFAULT_PORT))
        self.client_id = int(client_id or os.environ.get("IBKR_CLIENT_ID", DEFAULT_CLIENT_ID))
        self.ib = IB()

    def connect(self, timeout: float = 10) -> IB:
        self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=timeout)
        return self.ib

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    def __enter__(self) -> "IBKRClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    def account_summary(self):
        return self.ib.accountSummary()

    def account_values(self):
        return self.ib.accountValues()

    def positions(self):
        return self.ib.positions()
