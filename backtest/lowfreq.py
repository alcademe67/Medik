"""Portfolio backtester for LOW-FREQUENCY strategies, with this account's
real commission schedule modeled inline.

Why this exists separately from backtest/engine.py: that engine is
signal-driven (one setup -> one trade -> stop or target). The 2026-08-05
verdict showed the binding constraint on this account isn't signal quality,
it's that a ~2%-per-round-trip cost destroys any strategy that trades
often. The strategies worth testing under that constraint are portfolio
strategies -- hold N things, rebalance rarely, hold for months -- which the
signal engine can't express.

Commission model, measured from 90 days of this account's actual fills:

    commission = clamp($0.005/share, min $1.00, max 1% of trade value)

Verified: JPM 0.09sh/$31.35 -> $0.3135 (1% cap binds), NFLX 1sh/$67.13 ->
$0.6713 (1% cap), F 9sh/$129.02 -> $1.0000 ($1 floor), MARA 16sh/$197.84
-> $1.0000, S 12sh/$232.67 -> $1.0000.

No-lookahead: weights are decided from the REBALANCE DATE's close and
filled at the NEXT session's open, same timing discipline as engine.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

PER_SHARE = 0.005
MIN_COMMISSION = 1.00
MAX_PCT_OF_VALUE = 0.01


def commission(shares: float, trade_value: float) -> float:
    """This account's real per-fill cost. Never negative; a zero-size fill
    costs nothing."""
    if shares <= 0 or trade_value <= 0:
        return 0.0
    return min(max(PER_SHARE * shares, MIN_COMMISSION), MAX_PCT_OF_VALUE * trade_value)


@dataclass
class LowFreqResult:
    name: str
    equity_curve: list = field(default_factory=list)   # [(date, equity)]
    rebalances: int = 0
    fills: int = 0
    total_commission: float = 0.0
    starting_capital: float = 0.0
    holdings_log: list = field(default_factory=list)   # [(date, {sym: weight})]

    def summary(self) -> dict:
        if not self.equity_curve:
            return {"name": self.name, "trades": 0}
        vals = [e for _, e in self.equity_curve]
        start, end = self.starting_capital, vals[-1]
        peak, max_dd = float("-inf"), 0.0
        for v in vals:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak)
        days = (self.equity_curve[-1][0] - self.equity_curve[0][0]).days or 1
        years = days / 365.25
        cagr = ((end / start) ** (1 / years) - 1) if start > 0 and years > 0 else 0.0

        rets = pd.Series(vals).pct_change().dropna()
        sharpe = (rets.mean() / rets.std() * (252 ** 0.5)) if len(rets) > 1 and rets.std() > 0 else 0.0

        return {
            "name": self.name,
            "start": start,
            "end": end,
            "total_return_pct": 100 * (end - start) / start if start else 0.0,
            "cagr_pct": 100 * cagr,
            "max_drawdown_pct": 100 * max_dd,
            "sharpe": sharpe,
            "rebalances": self.rebalances,
            "fills": self.fills,
            "commission": self.total_commission,
            "commission_pct_of_start": 100 * self.total_commission / start if start else 0.0,
            "years": years,
        }


def _rebalance_dates(index: pd.DatetimeIndex, freq: str) -> list:
    """Last trading day of each period. freq: 'M' monthly, 'Q' quarterly,
    'Y' yearly, 'never' for buy-and-hold (first date only)."""
    if freq == "never":
        return [index[0]]
    s = pd.Series(index, index=index)
    if freq == "M":
        key = [(d.year, d.month) for d in index]
    elif freq == "Q":
        key = [(d.year, (d.month - 1) // 3) for d in index]
    elif freq == "Y":
        key = [d.year for d in index]
    else:
        raise ValueError(f"bad freq {freq}")
    out, seen = [], {}
    for d, k in zip(index, key):
        seen[k] = d
    out = sorted(seen.values())
    return out


def run_lowfreq(
    name: str,
    price_data: dict,
    weight_fn,
    starting_capital: float,
    freq: str = "M",
    warmup_bars: int = 252,
    max_weight_per_position: float | None = None,
) -> LowFreqResult:
    """weight_fn(history: dict[str, DataFrame], date) -> dict[str, float]
    Target portfolio weights (0..1, summing to <=1; the remainder is cash).
    `history` contains ONLY bars up to and including `date`, so the function
    physically cannot see the future.

    max_weight_per_position caps any single name (e.g. 0.20 for the
    account's 20%-per-trade rule). Set None to let weight_fn decide.
    """
    closes = pd.DataFrame({s: df["close"] for s, df in price_data.items()}).sort_index()
    opens = pd.DataFrame({s: df["open"] for s, df in price_data.items()}).sort_index()
    index = closes.index

    if len(index) <= warmup_bars + 2:
        raise ValueError("not enough history for the warm-up window")

    if freq == "never":
        # Buy and hold: enter once, at the same point the timed strategies
        # become eligible, so every strategy is measured over an identical
        # window. (Filtering by the warm-up date generically would drop this
        # single entry entirely and leave the benchmark sitting in cash.)
        rebal_dates = [index[warmup_bars]]
    else:
        rebal_dates = [d for d in _rebalance_dates(index, freq) if d >= index[warmup_bars]]

    cash = starting_capital
    shares: dict[str, float] = {}
    result = LowFreqResult(name=name, starting_capital=starting_capital)
    pending_weights = None

    for i, date in enumerate(index):
        # 1) Execute any rebalance decided on the previous session's close.
        if pending_weights is not None:
            px = opens.loc[date]
            equity = cash + sum(q * closes[s].iloc[i - 1] for s, q in shares.items() if not pd.isna(closes[s].iloc[i - 1]))
            targets = {}
            for s, w in pending_weights.items():
                p = px.get(s)
                if p is None or pd.isna(p) or p <= 0:
                    continue
                targets[s] = (equity * w) / p

            for s in set(list(shares.keys()) + list(targets.keys())):
                cur = shares.get(s, 0.0)
                tgt = targets.get(s, 0.0)
                delta = tgt - cur
                p = px.get(s)
                if p is None or pd.isna(p) or p <= 0 or abs(delta) < 1e-9:
                    continue
                value = abs(delta) * p
                if value < 1.0:  # don't churn on dust
                    continue
                fee = commission(abs(delta), value)
                cash -= delta * p          # buying spends, selling adds
                cash -= fee
                result.total_commission += fee
                result.fills += 1
                if abs(tgt) < 1e-9:
                    shares.pop(s, None)
                else:
                    shares[s] = tgt
            result.rebalances += 1
            result.holdings_log.append((date, dict(pending_weights)))
            pending_weights = None

        # 2) Mark to market on today's close.
        equity = cash
        for s, q in shares.items():
            p = closes[s].iloc[i]
            if not pd.isna(p):
                equity += q * p
        result.equity_curve.append((date, equity))

        # 3) Decide next rebalance from TODAY's close, to fill at tomorrow's open.
        if date in rebal_dates and i + 1 < len(index):
            hist = {s: price_data[s].loc[:date] for s in price_data}
            w = weight_fn(hist, date) or {}
            if max_weight_per_position is not None:
                w = {s: min(v, max_weight_per_position) for s, v in w.items()}
            total = sum(w.values())
            if total > 1.0:
                w = {s: v / total for s, v in w.items()}
            pending_weights = w

    return result
