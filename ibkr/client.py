from __future__ import annotations

import os
import time

from ib_async import IB

from ibkr.accounts import belongs_to, tag_map

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

    def account_summary(self, account: str = ""):
        """Summary rows, optionally for one account.

        Unfiltered these span every account on the login. Callers that
        reduce them to {tag: value} must pass an account or use
        ibkr.accounts.tag_map, or two accounts collapse into whichever
        row arrived last.
        """
        return [r for r in self.ib.accountSummary() if belongs_to(r, account)]

    def account_values(self, account: str = ""):
        return [r for r in self.ib.accountValues() if belongs_to(r, account)]

    def account_tags(self, account: str = "") -> dict:
        """{tag: value} for one account — the safe form of the above."""
        return tag_map(self.ib.accountSummary(), account)

    def positions(self, account: str = ""):
        return [p for p in self.ib.positions() if belongs_to(p, account)]
