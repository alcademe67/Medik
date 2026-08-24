"""Choosing WHICH account to read or trade, on a login that has more than one.

Every ib_async read -- accountValues(), portfolio(), positions(), openTrades()
-- spans all accounts under the login and tags each row with the account it
came from. Code written when the login had exactly one account tends to drop
that tag: reducing account values to {tag: value} collapses two accounts into
whichever row arrived last, and iterating positions() acts on holdings that
belong to a different account.

That stayed invisible here until a second account activated, at which point a
funded account read as $0.00 because an empty one happened to be listed last.

The rule this module encodes: when the login has one account, use it; when it
has several, the account must be NAMED. Defaulting to the first is not a
convenience, it is a way to place real orders in the wrong account.
"""
from __future__ import annotations

import os

DEFAULT_ENV_VARS = ("IBKR_ACCOUNT",)


def resolve_account(
    managed,
    env_vars: tuple = DEFAULT_ENV_VARS,
    explicit: str = "",
) -> tuple[str, str]:
    """Pick the single account to act on. ("", reason) means refuse.

    Precedence: an explicit argument (a --account flag), then each environment
    variable in order, then a sole managed account. Anything else refuses.
    """
    managed = [a for a in managed if a]
    if not managed:
        return "", "no managed accounts reported by TWS"

    wanted, source = (explicit or "").strip(), "--account"
    if not wanted:
        for var in env_vars:
            value = os.environ.get(var, "").strip()
            if value:
                wanted, source = value, var
                break

    if wanted:
        if wanted not in managed:
            return "", (f"{source}={wanted!r} is not one of the managed "
                        f"accounts {managed}")
        return wanted, f"{wanted} (selected by {source})"

    if len(managed) == 1:
        return managed[0], f"{managed[0]} (the only managed account)"

    names = "/".join(env_vars)
    return "", (f"{len(managed)} managed accounts {managed} but {names} "
                "is not set — refusing to guess which one to trade")


def belongs_to(obj, account: str) -> bool:
    """True when an ib_async row is for `account` (or no account is selected).

    Reads the `.account` field the object already carries, rather than passing
    the account down to ib_async, so the filter keeps working regardless of
    which calls the installed version accepts an account argument for.
    """
    return not account or getattr(obj, "account", "") == account


def order_belongs_to(trade, account: str) -> bool:
    """Same, for a Trade, but a BLANK account counts as ours.

    Blank is ambiguous, and the two errors are not symmetric: treating a
    stranger's order as ours only makes us cautious, while treating ours as a
    stranger's can leave a live order unseen — uncancelled during a shutdown,
    or uncounted when deciding there is nothing outstanding.
    """
    return not account or getattr(trade.order, "account", "") in (account, "")


def tag_map(rows, account: str = "") -> dict:
    """{tag: value} for ONE account, from accountValues() or accountSummary().

    Both return rows keyed by (account, tag, currency), so building the dict
    without filtering keeps whichever account's row arrived last. Non-USD rows
    are dropped for the same reason: a tag reported per-currency would
    otherwise overwrite the base-currency figure.
    """
    return {r.tag: r.value for r in rows
            if belongs_to(r, account) and r.currency in ("USD", "BASE", "")}


def positions_for(ib, account: str = "") -> list:
    """Non-zero positions in ONE account. ib.positions() spans the login."""
    return [p for p in ib.positions() if p.position and belongs_to(p, account)]


def require_account(ib, env_vars: tuple = DEFAULT_ENV_VARS, explicit: str = ""):
    """(account, managed, reason). Convenience for scripts: resolve or explain.

    Returns the empty string for `account` when the caller must stop; every
    caller here prints `reason` and exits rather than proceeding on a guess.
    """
    managed = list(ib.managedAccounts())
    account, reason = resolve_account(managed, env_vars=env_vars, explicit=explicit)
    return account, managed, reason


class AccountAmbiguous(RuntimeError):
    """The login manages several accounts and none was named.

    Raised rather than returned so a script cannot proceed by ignoring it. The
    failure it prevents is quiet: sizing a trade against a balance that belongs
    to some other account reads as a working run right up until the order.
    """


def account_context(ib, explicit: str = "", env_vars: tuple = DEFAULT_ENV_VARS):
    """(account, {tag: value}) for the one account this script should use.

    Resolves the account, then reads accountSummary() scoped to it. Callers
    that used to walk accountSummary() row by row were taking the LAST match
    across every account, which with two accounts is whichever TWS sent last.
    """
    account, reason = resolve_account(list(ib.managedAccounts()),
                                      env_vars=env_vars, explicit=explicit)
    if not account:
        raise AccountAmbiguous(reason)
    return account, tag_map(ib.accountSummary(), account)
