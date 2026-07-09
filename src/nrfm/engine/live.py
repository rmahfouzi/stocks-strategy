"""Daily live decision engine: turns tonight's data into a trade list.

Mirrors the backtest event logic (decide at close, execute at tomorrow's
opening auction) using the same select_portfolio() code path. Emits an
email-ready report: a trade list when there is something to do, otherwise
a short heartbeat confirming the run -- silence always means failure.

Events, in priority order (STRATEGY.md section 11):
  1. regime flip to RISK_OFF  -> sell everything
  2. regime flip to RISK_ON   -> build full portfolio now
  3. catastrophe stops        -> sell blown-up holdings
  4. first report of a new calendar month while RISK_ON -> rebalance
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from nrfm import config
from nrfm.engine.panels import Panels, load_index_series, load_panels
from nrfm.engine.select import select_portfolio
from nrfm.engine.signals import RISK_ON, features_asof, regime_states
from nrfm.store import Store

STATE_LAST_REBALANCE = "last_rebalance_month"
STATE_EQUITY = "portfolio_equity_sek"
STATE_OBSERVATION_START = "observation_start"

# below this, MIN_ORDER_SEK makes the 10-stock design unexecutable
VIABLE_EQUITY_SEK = 20_000


@dataclass
class DailyReport:
    as_of: str
    regime: str
    actionable: bool = False
    sells: list[str] = field(default_factory=list)
    buys: dict[str, float] = field(default_factory=dict)      # ticker -> weight
    targets: dict[str, float] = field(default_factory=dict)   # full portfolio
    stop_hits: list[str] = field(default_factory=list)
    reason: str = "no action"
    notes: list[str] = field(default_factory=list)


def check_stops(panels: Panels, holdings_since: dict[str, str]) -> list[str]:
    """Holdings whose last close is CATASTROPHE_STOP below the highest
    close since entry."""
    hits = []
    for ticker, since in holdings_since.items():
        if ticker not in panels.adj_ff.columns:
            continue
        series = panels.adj_ff[ticker].loc[since:].dropna()
        if len(series) < 2:
            continue
        if series.iloc[-1] < (1 - config.CATASTROPHE_STOP) * series.max():
            hits.append(ticker)
    return hits


def build_daily_report(store: Store) -> DailyReport:
    lookback_start = (
        pd.Timestamp.today() - pd.Timedelta(days=550)).date().isoformat()
    panels = load_panels(store, start=lookback_start)
    index = load_index_series(store, config.INDEX_ORDERBOOK_ID)
    regime = regime_states(index).dropna()
    state, prev = regime.iloc[-1], regime.iloc[-2]
    as_of = str(panels.calendar[-1].date())

    holdings_since = {
        r["ticker"]: r["since"] for r in store.conn.execute(
            "SELECT ticker, since FROM holdings")
    }
    held = set(holdings_since)
    report = DailyReport(as_of=as_of, regime=state)

    if state != RISK_ON:
        if prev == RISK_ON or held:
            report.actionable = bool(held)
            report.sells = sorted(held)
            report.reason = "regime flip to RISK_OFF -- exit to cash" \
                if prev == RISK_ON else "RISK_OFF and holdings remain -- exit to cash"
        return report

    stop_hits = check_stops(panels, holdings_since)
    survivors = held - set(stop_hits)

    month = as_of[:7]
    reentry = prev != RISK_ON
    month_due = store.state_get(STATE_LAST_REBALANCE) != month

    if reentry or month_due:
        feat = features_asof(panels, len(panels.calendar) - 1)
        sel = select_portfolio(feat, panels.sectors, holdings=survivors,
                               companies=panels.companies)
        report.sells = sorted(set(stop_hits) | set(sel.dropped))
        report.buys = {t: sel.weights[t] for t in sel.added}
        report.targets = sel.weights
        report.stop_hits = stop_hits
        report.reason = ("regime flip to RISK_ON -- rebuild portfolio"
                         if reentry else f"monthly rebalance ({month})")
        report.actionable = bool(report.sells or report.buys)
        store.state_set(STATE_LAST_REBALANCE, month)
    elif stop_hits:
        report.sells = sorted(stop_hits)
        report.stop_hits = stop_hits
        report.reason = "catastrophe stop (25% below post-entry high)"
        report.actionable = True

    return report


def performance_summary(store: Store) -> str:
    """Replay the strategy from the observation start with the shared
    engine: this IS the paper track record (same selection code, same
    execution assumptions as the emails)."""
    from nrfm.engine.backtest import run_backtest

    obs_start = store.state_get(STATE_OBSERVATION_START)
    if not obs_start:
        return "(no observation start date set -- run `nrfm observe`)"
    lookback = (pd.Timestamp(obs_start)
                - pd.Timedelta(days=600)).date().isoformat()
    panels = load_panels(store, start=lookback)
    index = load_index_series(store, config.INDEX_ORDERBOOK_ID)
    equity_raw = store.state_get(STATE_EQUITY)
    capital = float(equity_raw) if equity_raw else config.INITIAL_CAPITAL_SEK

    result = run_backtest(panels, index, start=obs_start,
                          initial_capital=capital,
                          benchmarks={"OMXSGI": index})
    eq = result.equity
    if len(eq) < 3:
        return f"(observation started {obs_start}; too early to report)"
    strat_ret = eq.iloc[-1] / eq.iloc[0] - 1
    bench = index.reindex(eq.index).ffill()
    bench_ret = bench.iloc[-1] / bench.iloc[0] - 1
    dd = (eq / eq.cummax() - 1).min()
    lines = [
        f"Observation track record since {obs_start} "
        f"(modeled, {capital:,.0f} SEK start):",
        f"  strategy {strat_ret:+.2%}   OMXSGI {bench_ret:+.2%}   "
        f"active {strat_ret - bench_ret:+.2%}",
        f"  current value {eq.iloc[-1]:,.0f} SEK   max drawdown {dd:.1%}   "
        f"trades {len(result.trades)}",
    ]
    return "\n".join(lines)


def format_heartbeat(report: DailyReport, holdings: list[str],
                     equity: float | None) -> str:
    """Body for the no-action nights: proof the pipeline ran, so that
    silence always means something is broken."""
    lines = [
        f"NRFM heartbeat -- nightly run OK, data as of close {report.as_of}",
        f"regime: {report.regime}",
        f"event: {report.reason} -- nothing to execute",
        "",
    ]
    if holdings:
        lines.append(f"holdings ({len(holdings)}): " + ", ".join(holdings))
    else:
        lines.append("holdings: (none -- all cash)")
    if equity is not None:
        lines.append(f"portfolio equity: {equity:,.0f} SEK")
    lines += ["", "This email arrives every trading evening. If it stops "
              "on a weekday, the pipeline is broken -- check data/logs/."]
    return "\n".join(lines)


def format_report(report: DailyReport, equity: float | None) -> str:
    lines = [
        f"NRFM daily signal -- data as of close {report.as_of}",
        f"regime: {report.regime}",
        f"event: {report.reason}",
        "",
        "Orders for tomorrow's opening auction:",
    ]

    def sek(w: float) -> str:
        return f" (~{round(w * equity / 10) * 10:,.0f} SEK)" if equity else ""

    for t in report.sells:
        tag = "  [STOP]" if t in report.stop_hits else ""
        lines.append(f"  SELL ALL  {t}{tag}")
    for t, w in sorted(report.buys.items(), key=lambda kv: -kv[1]):
        lines.append(f"  BUY       {t:14} {w:6.1%}{sek(w)}")
    if not (report.sells or report.buys):
        lines.append("  (none)")

    if report.targets:
        lines += ["", "Full target portfolio:"]
        for t, w in sorted(report.targets.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {t:14} {w:6.1%}{sek(w)}")

    if equity is not None and equity < VIABLE_EQUITY_SEK:
        lines += ["", (
            f"NOTE: configured portfolio ({equity:,.0f} SEK) is below the "
            f"~{VIABLE_EQUITY_SEK:,} SEK needed to execute this strategy "
            f"(min order {config.MIN_ORDER_SEK:,} SEK x {config.N_HOLDINGS} "
            "positions). Treat these signals as PAPER TRADING."
        )]
    lines += ["", "After executing, update the register: "
              "nrfm hold add/rm TICKER"]
    return "\n".join(lines)
