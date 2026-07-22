from __future__ import annotations

import os
import time

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
        self.host = host if host is not None else os.environ.get("IBKR_HOST", DEFAULT_HOST)
        self.port = int(port if port is not None else os.environ.get("IBKR_PORT", DEFAULT_PORT))
        self.client_id = int(
            client_id if client_id is not None else os.environ.get("IBKR_CLIENT_ID", DEFAULT_CLIENT_ID)
        )
        self.ib = IB()

    def connect(self, timeout: float = 10, retries: int = 5, backoff: float = 2.0) -> IB:
        """Connect with exponential-backoff retries (2s, 4s, 8s, ... between
        attempts) so a TWS restart or brief network blip doesn't kill an
        unattended run. Raises the last error once retries are exhausted."""
        attempt = 0
        while True:
            try:
                self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=timeout)
                return self.ib
            except Exception:
                attempt += 1
                if attempt > retries:
                    raise
                time.sleep(backoff * 2 ** (attempt - 1))

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
