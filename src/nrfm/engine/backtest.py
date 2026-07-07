"""Backtest simulator (STRATEGY.md sections 12-13).

Event loop mirrors the live daily workflow exactly:
- decisions on close of day t, fills at the adjusted open of day t+1
- monthly rebalance (first trading day of month) via select_portfolio()
- regime switches and catastrophe stops checked daily
- costs charged per side on traded value

Known bias, by design and disclosed: the universe is TODAY's Large/Mid
Cap members (survivorship). Results, especially pre-2020, are optimistic;
see STRATEGY.md section 13.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from nrfm import config
from nrfm.engine.panels import Panels
from nrfm.engine.select import select_portfolio
from nrfm.engine.signals import RISK_ON, features_asof, regime_states


@dataclass
class Position:
    shares: float
    high_water: float  # highest adj close since entry


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: pd.DataFrame
    regime: pd.Series
    metrics: dict = field(default_factory=dict)


def run_backtest(panels: Panels, index_close: pd.Series,
                 start: str | None = None, end: str | None = None,
                 initial_capital: float = config.INITIAL_CAPITAL_SEK,
                 cost_per_side: float = config.COST_PER_SIDE,
                 benchmarks: dict[str, pd.Series] | None = None) -> BacktestResult:
    cal = panels.calendar
    regime = regime_states(index_close).reindex(cal).ffill()

    # warmup: need 252 bars of panel history and a defined regime state
    first = config.LOOKBACK_12M + 1
    while first < len(cal) and pd.isna(regime.iloc[first]):
        first += 1
    if start:
        first = max(first, cal.searchsorted(pd.Timestamp(start)))
    last = cal.searchsorted(pd.Timestamp(end)) if end else len(cal) - 1
    last = min(last, len(cal) - 1)

    cash = initial_capital
    positions: dict[str, Position] = {}
    equity_curve: dict[pd.Timestamp, float] = {}
    trade_rows: list[dict] = []

    def mark(i: int) -> float:
        px = panels.adj_ff.iloc[i]
        return cash + sum(p.shares * px[t] for t, p in positions.items()
                          if not np.isnan(px[t]))

    def exec_price(i: int, t: str) -> float:
        p = panels.adj_open.iloc[i][t]
        return p if not np.isnan(p) else panels.adj_ff.iloc[i][t]

    def execute(i: int, orders: dict[str, float], reason: str) -> None:
        """orders: ticker -> target SEK value (0 = close position)."""
        nonlocal cash
        date = cal[i]
        # sells first to free cash
        for t, target in sorted(orders.items(),
                                key=lambda kv: kv[1]):
            pos = positions.get(t)
            held_val = 0.0
            price = exec_price(i, t)
            if np.isnan(price):
                continue  # no market today; retried at next event
            if pos:
                held_val = pos.shares * price
            delta = target - held_val
            if abs(delta) < config.MIN_ORDER_SEK and target != 0:
                continue
            if delta < 0:  # sell
                sell_val = min(-delta, held_val)
                shares = sell_val / price
                cash += sell_val * (1 - cost_per_side)
                pos.shares -= shares
                if target == 0 or pos.shares * price < 1.0:
                    positions.pop(t, None)
                trade_rows.append(dict(date=date, ticker=t, side="SELL",
                                       value=sell_val, reason=reason))
            elif delta > 0:  # buy
                buy_val = min(delta, cash / (1 + cost_per_side))
                if buy_val < config.MIN_ORDER_SEK:
                    continue
                shares = buy_val / price
                cash -= buy_val * (1 + cost_per_side)
                if pos:
                    pos.shares += shares
                else:
                    positions[t] = Position(shares=shares, high_water=price)
                trade_rows.append(dict(date=date, ticker=t, side="BUY",
                                       value=buy_val, reason=reason))

    pending: dict[str, float] | None = None
    pending_reason = ""

    for i in range(first, last + 1):
        # 1) execute orders decided yesterday at today's open
        if pending is not None:
            execute(i, pending, pending_reason)
            pending, pending_reason = None, ""

        px = panels.adj_ff.iloc[i]
        state, prev_state = regime.iloc[i], regime.iloc[i - 1]

        # 2) forced exits: delisted/suspended beyond ffill limit
        for t in [t for t, p in positions.items() if np.isnan(px[t])]:
            pos = positions.pop(t)
            last_px = panels.adj_ff.iloc[:i][t].dropna()
            if len(last_px):
                value = pos.shares * last_px.iloc[-1]
                cash += value * (1 - config.COST_PER_SIDE)
                trade_rows.append(dict(date=cal[i], ticker=t, side="SELL",
                                       value=value, reason="delisted"))

        # 3) update high-water marks, mark equity
        for t, pos in positions.items():
            if not np.isnan(px[t]):
                pos.high_water = max(pos.high_water, px[t])
        equity = mark(i)
        equity_curve[cal[i]] = equity

        if i == last:
            break

        # 4) decide orders for tomorrow's open
        if state != RISK_ON and prev_state == RISK_ON:
            pending = {t: 0.0 for t in positions}
            pending_reason = "regime_exit"
        elif state == RISK_ON:
            orders: dict[str, float] = {}
            # catastrophe stops
            for t, pos in positions.items():
                if not np.isnan(px[t]) and \
                        px[t] < (1 - config.CATASTROPHE_STOP) * pos.high_water:
                    orders[t] = 0.0
            reason = "stop"
            month_start = cal[i + 1].month != cal[i].month
            # initial build on day one mirrors the live engine, which
            # emits the first portfolio immediately, not at month-end
            reenter = prev_state != RISK_ON or (i == first and not positions)
            if reenter or month_start:
                held = set(positions) - {t for t, v in orders.items() if v == 0.0}
                feat = features_asof(panels, i)
                sel = select_portfolio(feat, panels.sectors, holdings=held,
                                       companies=panels.companies)
                for t in sel.dropped:
                    orders[t] = 0.0
                for t, w in sel.weights.items():
                    target = w * equity
                    if t in positions and t in held:
                        cur = positions[t].shares * px[t]
                        if abs(cur - target) <= config.TRADE_BAND * target:
                            continue
                    orders[t] = target
                reason = "reentry" if reenter else "rebalance"
            if orders:
                pending = orders
                pending_reason = reason

    equity = pd.Series(equity_curve).sort_index()
    trades = pd.DataFrame(trade_rows)
    result = BacktestResult(equity=equity, trades=trades,
                            regime=regime.reindex(equity.index))
    if len(equity) >= 2:
        result.metrics = compute_metrics(equity, trades,
                                         benchmarks or {"index": index_close})
    return result


def compute_metrics(equity: pd.Series, trades: pd.DataFrame,
                    benchmarks: dict[str, pd.Series]) -> dict:
    rets = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
    vol = rets.std() * np.sqrt(252)
    dd = equity / equity.cummax() - 1
    underwater = (dd < 0).astype(int)
    # longest underwater stretch in trading days
    longest, run = 0, 0
    for v in underwater.values:
        run = run + 1 if v else 0
        longest = max(longest, run)

    bench_stats = {}
    yearly_bench_all = {}
    for name, series in benchmarks.items():
        bench = series.reindex(equity.index).ffill().dropna()
        if len(bench) < 252:
            continue
        by = (bench.index[-1] - bench.index[0]).days / 365.25
        bench_stats[name] = {
            "from": str(bench.index[0].date()),
            "cagr": (bench.iloc[-1] / bench.iloc[0]) ** (1 / by) - 1,
            "max_drawdown": (bench / bench.cummax() - 1).min(),
        }
        yearly_bench_all[name] = {
            str(k.year): round(v, 4) for k, v in
            bench.resample("YE").last().pct_change().dropna().items()
        }

    avg_equity = equity.mean()
    traded = trades["value"].sum() if len(trades) else 0.0
    turnover = traded / 2 / avg_equity / years  # one-way per year
    costs = traded * config.COST_PER_SIDE

    yearly = equity.resample("YE").last().pct_change().dropna()

    return {
        "start": str(equity.index[0].date()),
        "end": str(equity.index[-1].date()),
        "years": round(years, 1),
        "final_equity": round(equity.iloc[-1]),
        "cagr": cagr,
        "vol": vol,
        "sharpe": cagr / vol if vol > 0 else float("nan"),
        "max_drawdown": dd.min(),
        "longest_underwater_days": longest,
        "turnover_oneway_peryear": turnover,
        "total_costs_sek": round(costs),
        "cost_drag_peryear": costs / avg_equity / years,
        "n_trades": len(trades),
        "benchmarks": bench_stats,
        "yearly_returns": {str(k.year): round(v, 4)
                           for k, v in yearly.items()},
        "yearly_bench": yearly_bench_all,
    }
