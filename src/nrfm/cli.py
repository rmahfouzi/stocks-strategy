"""Command-line entry point.

    nrfm universe    refresh instrument list from the Nasdaq screener
    nrfm backfill    full history: Nasdaq charts (~10y), Yahoo (2005-),
                     index; safe to re-run, resumes where it stopped
    nrfm update      nightly incremental: universe + new bars + validation
    nrfm validate    run data validation only
    nrfm status      print store statistics
    nrfm email-test  send a test email using the configured SMTP settings
    nrfm signals     compute today's regime + target portfolio (read-only)
    nrfm daily       nightly decision run: emails a trade list when action
                     is needed (regime flip, stop, monthly rebalance)
    nrfm equity      show or set portfolio size in SEK (for order amounts)
    nrfm backtest    run the historical simulation and print the report
    nrfm hold        record actual holdings: hold add T | rm T | list
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, timedelta

from nrfm import config
from nrfm.notify import load_config, send_email, try_send_email
from nrfm.sources.nasdaq import NasdaqApiError, NasdaqNordicClient
from nrfm.sources.yahoo import YahooClient, YahooFetchError
from nrfm.store import Store
from nrfm.validate import validate


def _log(msg: str) -> None:
    print(msg, flush=True)


def refresh_universe(store: Store, nasdaq: NasdaqNordicClient) -> None:
    today = date.today().isoformat()
    rows = nasdaq.universe()
    n = store.upsert_instruments(rows, seen_date=today)
    store.log_fetch("nasdaq", "universe", "ok", rows=n)
    _log(f"universe: {n} active instruments "
         f"({config.UNIVERSE_MARKET} {'+'.join(config.UNIVERSE_SEGMENTS)})")


def fetch_nasdaq_prices(store: Store, nasdaq: NasdaqNordicClient,
                        full: bool) -> int:
    """Per-instrument daily bars; incremental unless `full`."""
    default_start = (
        date.today() - timedelta(days=365 * config.BACKFILL_YEARS_NASDAQ)
    ).isoformat()
    failures = 0
    instruments = store.active_instruments()
    for i, inst in enumerate(instruments, 1):
        obid = inst["orderbook_id"]
        last = None if full else store.last_date(
            "prices_nasdaq", "orderbook_id", obid)
        start = last or default_start
        try:
            # re-fetch from the last stored date (inclusive): a bar stored
            # intraday must be replaced by the final end-of-day bar
            bars = nasdaq.daily_bars(obid, from_date=start)
            new = [b for b in bars if last is None or b["date"] >= last]
            store.insert_nasdaq_bars(obid, new)
            store.log_fetch("nasdaq", obid, "ok", rows=len(new))
        except NasdaqApiError as e:
            failures += 1
            store.log_fetch("nasdaq", obid, "error", message=str(e))
            _log(f"  nasdaq FAIL {inst['symbol']}: {e}")
        if i % 50 == 0:
            _log(f"  nasdaq prices: {i}/{len(instruments)}")
    _log(f"nasdaq prices done ({len(instruments)} instruments, "
         f"{failures} failures)")
    return failures


def fetch_yahoo_prices(store: Store, yahoo: YahooClient, full: bool) -> int:
    failures = 0
    instruments = store.active_instruments()
    for i, inst in enumerate(instruments, 1):
        ticker = inst["yahoo_ticker"]
        last = None if full else store.last_date("prices_yahoo", "ticker", ticker)
        # start at the last stored date (inclusive), not the day after: a bar
        # stored intraday must be replaced by the final end-of-day bar
        start = config.BACKFILL_START_YAHOO if last is None else last
        if start > date.today().isoformat():
            continue
        try:
            bars = yahoo.daily_bars(ticker, start=start)
            store.insert_yahoo_bars(ticker, bars)
            store.log_fetch("yahoo", ticker, "ok", rows=len(bars))
        except YahooFetchError as e:
            failures += 1
            store.log_fetch("yahoo", ticker, "error", message=str(e))
            _log(f"  yahoo FAIL {ticker}: {e}")
        if i % 50 == 0:
            _log(f"  yahoo prices: {i}/{len(instruments)}")
    _log(f"yahoo prices done ({len(instruments)} tickers, {failures} failures)")
    return failures


def fetch_index(store: Store, nasdaq: NasdaqNordicClient, full: bool) -> None:
    last = None if full else store.latest_index_date()
    start = last or (
        date.today() - timedelta(days=365 * config.BACKFILL_YEARS_NASDAQ)
    ).isoformat()
    bars = nasdaq.index_bars(from_date=start)
    new = [b for b in bars if last is None or b["date"] > last]
    store.insert_index_bars(config.INDEX_ORDERBOOK_ID, new)
    store.log_fetch("nasdaq", config.INDEX_SYMBOL, "ok", rows=len(new))
    _log(f"index {config.INDEX_SYMBOL}: +{len(new)} bars")


def fetch_regime_index_yahoo(store: Store, yahoo: YahooClient) -> None:
    """OMXS30 via Yahoo: long-history regime/benchmark index for backtests."""
    key = config.REGIME_INDEX_YAHOO
    last = store.last_date("index_prices", "orderbook_id", key)
    start = last or config.BACKFILL_START_YAHOO
    bars = yahoo.daily_bars(key, start=start)
    store.insert_index_bars(key, bars)
    store.log_fetch("yahoo", key, "ok", rows=len(bars))
    _log(f"index {key}: +{len(bars)} bars")


def cmd_universe(store: Store) -> int:
    refresh_universe(store, NasdaqNordicClient())
    return 0


def cmd_backfill(store: Store) -> int:
    nasdaq = NasdaqNordicClient()
    yahoo = YahooClient()
    refresh_universe(store, nasdaq)
    fetch_index(store, nasdaq, full=False)
    fetch_regime_index_yahoo(store, yahoo)
    nasdaq_failures = fetch_nasdaq_prices(store, nasdaq, full=False)
    yahoo_failures = fetch_yahoo_prices(store, yahoo, full=False)
    result = validate(store)
    _log(result.summary())
    return 0 if (result.ok and nasdaq_failures + yahoo_failures == 0) else 1


def cmd_update(store: Store) -> int:
    nasdaq = NasdaqNordicClient()
    yahoo = YahooClient()
    refresh_universe(store, nasdaq)
    fetch_index(store, nasdaq, full=False)
    fetch_regime_index_yahoo(store, yahoo)
    fetch_nasdaq_prices(store, nasdaq, full=False)
    fetch_yahoo_prices(store, yahoo, full=False)
    result = validate(store)
    _log(result.summary())
    if not result.ok:
        err = try_send_email(
            f"[NRFM] DATA VALIDATION FAILED ({result.date})",
            "The nightly update finished but data validation failed.\n"
            "No trading decisions should be made today.\n\n"
            + result.summary(),
        )
        if err:
            _log(f"alert email failed: {err}")
    return 0 if result.ok else 1


def cmd_validate(store: Store) -> int:
    result = validate(store)
    _log(result.summary())
    return 0 if result.ok else 1


def cmd_status(store: Store) -> int:
    for k, v in store.stats().items():
        _log(f"{k:22} {v}")
    return 0


def _kill_switch_check(store: Store) -> str | None:
    """Return an error message if the store is not fit for decisions."""
    latest = store.latest_index_date()
    row = store.conn.execute(
        "SELECT ok, date FROM validation_log ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "no validation has ever run"
    if not row["ok"]:
        return f"last validation FAILED (for {row['date']})"
    if row["date"] != latest:
        return (f"last validation is stale (validated {row['date']}, "
                f"latest data {latest})")
    return None


def cmd_signals(store: Store) -> int:
    from nrfm.engine.panels import load_index_series, load_panels
    from nrfm.engine.select import select_portfolio
    from nrfm.engine.signals import features_asof, regime_states

    err = _kill_switch_check(store)
    if err:
        _log(f"KILL SWITCH: {err} -- no signals generated")
        return 1

    lookback_start = (date.today() - timedelta(days=550)).isoformat()
    panels = load_panels(store, start=lookback_start)
    index = load_index_series(store, config.INDEX_ORDERBOOK_ID)
    regime = regime_states(index)
    state = regime.dropna().iloc[-1]
    holdings = set(store.holdings())

    _log(f"as of close {panels.calendar[-1].date()}   "
         f"regime[{config.INDEX_SYMBOL}]: {state}")
    if state != "RISK_ON":
        _log("RISK_OFF: target portfolio is 100% cash")
        if holdings:
            _log("sell all holdings: " + ", ".join(sorted(holdings)))
        return 0

    feat = features_asof(panels, len(panels.calendar) - 1)
    sel = select_portfolio(feat, panels.sectors, holdings=holdings,
                           companies=panels.companies)
    _log(f"eligible universe: {int(feat['eligible'].sum())} names")
    if holdings:
        _log(f"kept (rank<=({config.HOLD_BUFFER_RANK})): {', '.join(sel.kept) or '-'}")
        _log(f"sell: {', '.join(sel.dropped) or '-'}")
        _log(f"buy:  {', '.join(sel.added) or '-'}")
    _log("\ntarget portfolio:")
    ranked = sel.ranked
    for t, w in sorted(sel.weights.items(), key=lambda kv: -kv[1]):
        row = ranked.loc[t]
        _log(f"  {t:14} {w:6.1%}  mom={row['mom']:+7.1%} "
             f"vol={row['vol']:5.1%} rank={int(row['rank'])}")
    return 0


def cmd_inbox(store: Store) -> int:
    from nrfm.inbox import process_inbox
    handled = process_inbox(store)
    _log(f"email commands processed: {handled} message(s)")
    return 0


def cmd_daily(store: Store) -> int:
    from nrfm.engine.live import (STATE_EQUITY, build_daily_report,
                                  format_report)
    from nrfm.inbox import process_inbox

    # apply any emailed holdings updates BEFORE computing signals
    try:
        handled = process_inbox(store)
        if handled:
            _log(f"applied email commands from {handled} message(s)")
    except Exception as e:
        _log(f"inbox processing failed (non-fatal): {e}")

    err = _kill_switch_check(store)
    if err:
        _log(f"KILL SWITCH: {err} -- no signals generated")
        send_err = try_send_email(
            "[NRFM] NO SIGNALS -- data not trustworthy",
            f"The daily decision run was blocked by the kill switch:\n"
            f"  {err}\n\nDo not trade on today's data.",
        )
        if send_err:
            _log(f"alert email failed: {send_err}")
        return 1

    report = build_daily_report(store)
    equity_raw = store.state_get(STATE_EQUITY)
    equity = float(equity_raw) if equity_raw else None
    body = format_report(report, equity)
    if report.actionable and "rebalance" in report.reason:
        from nrfm.engine.live import performance_summary
        body += "\n\n" + performance_summary(store)
    _log(body)

    if report.actionable:
        n = len(report.sells) + len(report.buys)
        send_err = try_send_email(
            f"[NRFM] TRADE SIGNALS ({report.as_of}): {n} orders -- "
            f"{report.reason}",
            body,
        )
        if send_err:
            _log(f"signal email failed: {send_err}")
            return 1
        _log("signal email sent")
    else:
        _log("no action required -- no email sent")
    return 0


def cmd_report(store: Store) -> int:
    from nrfm.engine.live import performance_summary
    _log(performance_summary(store))
    return 0


def cmd_observe(store: Store, start: str | None) -> int:
    from nrfm.engine.live import STATE_OBSERVATION_START
    if start:
        date.fromisoformat(start)  # validate format
        store.state_set(STATE_OBSERVATION_START, start)
    current = store.state_get(STATE_OBSERVATION_START)
    _log(f"observation start: {current or '(not set)'}")
    return 0


def cmd_equity(store: Store, amount: str | None) -> int:
    from nrfm.engine.live import STATE_EQUITY
    if amount is None:
        current = store.state_get(STATE_EQUITY)
        _log(f"portfolio equity: {current or '(not set)'} SEK")
        return 0
    value = float(amount)
    store.state_set(STATE_EQUITY, str(value))
    _log(f"portfolio equity set to {value:,.0f} SEK")
    return 0


def cmd_backtest(store: Store, start: str | None, end: str | None) -> int:
    import json

    from nrfm.engine.backtest import run_backtest
    from nrfm.engine.panels import load_index_series, load_panels

    _log("loading panels...")
    panels = load_panels(store)
    index = load_index_series(store, config.REGIME_INDEX_YAHOO)
    if index.empty:
        _log(f"no {config.REGIME_INDEX_YAHOO} index data -- "
             f"run `nrfm update` first")
        return 1
    omxsgi = load_index_series(store, config.INDEX_ORDERBOOK_ID)
    _log(f"panels: {panels.adj.shape[0]} days x {panels.adj.shape[1]} "
         f"tickers; index {config.REGIME_INDEX_YAHOO} from "
         f"{index.index[0].date()}")
    result = run_backtest(panels, index, start=start, end=end,
                          benchmarks={"OMXS30(price)": index,
                                      "OMXSGI(total return)": omxsgi})

    out_dir = config.DATA_DIR / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    result.equity.to_csv(out_dir / "equity.csv")
    result.trades.to_csv(out_dir / "trades.csv", index=False)
    (out_dir / "metrics.json").write_text(
        json.dumps(result.metrics, indent=2))

    m = result.metrics
    _log("\n=== BACKTEST (survivorship-biased universe -- see STRATEGY.md 13.2) ===")
    _log(f"{m['start']} -> {m['end']} ({m['years']} yrs)   "
         f"final equity {m['final_equity']:,} SEK")
    _log(f"CAGR {m['cagr']:.2%}   vol {m['vol']:.2%}   "
         f"Sharpe {m['sharpe']:.2f}")
    _log(f"maxDD {m['max_drawdown']:.1%}   "
         f"longest underwater {m['longest_underwater_days']}d")
    _log(f"turnover {m['turnover_oneway_peryear']:.1f}x/yr one-way   "
         f"cost drag {m['cost_drag_peryear']:.2%}/yr   "
         f"trades {m['n_trades']}")
    for name, b in m["benchmarks"].items():
        _log(f"benchmark {name} (from {b['from']}): CAGR {b['cagr']:.2%}   "
             f"maxDD {b['max_drawdown']:.1%}   "
             f"active {m['cagr'] - b['cagr']:+.2%}")
    bench_names = list(m["yearly_bench"])
    _log("\nyear   strategy   " + "   ".join(bench_names))
    for y in sorted(m["yearly_returns"]):
        row = f"{y}   {m['yearly_returns'][y]:+8.1%}"
        for name in bench_names:
            b = m["yearly_bench"][name].get(y)
            row += f"   {b:+8.1%}" if b is not None else "        n/a"
        _log(row)
    _log(f"\nartifacts: {out_dir}/(equity.csv, trades.csv, metrics.json)")
    return 0


def cmd_hold(store: Store, action: str, ticker: str | None) -> int:
    if action == "list":
        held = store.holdings()
        _log("\n".join(held) if held else "(no holdings recorded)")
        return 0
    if not ticker:
        _log("usage: nrfm hold add|rm TICKER")
        return 1
    ticker = ticker.upper()
    if not ticker.endswith(".ST"):
        ticker += ".ST"
    if action == "add":
        store.hold_add(ticker, since=date.today().isoformat())
        _log(f"holding recorded: {ticker}")
    else:
        store.hold_remove(ticker)
        _log(f"holding removed: {ticker}")
    return 0


def cmd_email_test(store: Store) -> int:
    cfg = load_config()
    send_email(
        "[NRFM] test email",
        "Email notifications are configured correctly.\n\n"
        "You will receive:\n"
        "- trade signal lists (once the strategy engine is live)\n"
        "- data validation failure alerts from the nightly update\n",
        cfg,
    )
    _log(f"test email sent to {cfg.to} via {cfg.host}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nrfm", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("universe", "backfill", "update", "validate", "status",
                 "email-test", "signals", "daily", "report", "inbox"):
        sub.add_parser(name)
    p_obs = sub.add_parser("observe")
    p_obs.add_argument("start", nargs="?")
    p_bt = sub.add_parser("backtest")
    p_bt.add_argument("--start")
    p_bt.add_argument("--end")
    p_hold = sub.add_parser("hold")
    p_hold.add_argument("action", choices=["add", "rm", "list"])
    p_hold.add_argument("ticker", nargs="?")
    p_eq = sub.add_parser("equity")
    p_eq.add_argument("amount", nargs="?")
    args = parser.parse_args(argv)

    commands = {
        "universe": cmd_universe,
        "backfill": cmd_backfill,
        "update": cmd_update,
        "validate": cmd_validate,
        "status": cmd_status,
        "email-test": cmd_email_test,
        "signals": cmd_signals,
        "daily": cmd_daily,
        "report": cmd_report,
        "inbox": cmd_inbox,
    }
    try:
        with Store() as store:
            if args.command == "backtest":
                return cmd_backtest(store, args.start, args.end)
            if args.command == "hold":
                return cmd_hold(store, args.action, args.ticker)
            if args.command == "equity":
                return cmd_equity(store, args.amount)
            if args.command == "observe":
                return cmd_observe(store, args.start)
            return commands[args.command](store)
    except Exception:
        if args.command not in ("update", "daily"):
            raise
        # unattended nightly run: any crash anywhere (store setup, fetch,
        # validation) must page the operator; exit 2 tells the cron
        # wrapper the alert was already attempted
        tb = traceback.format_exc()
        _log(tb)
        err = try_send_email(
            f"[NRFM] {args.command.upper()} CRASHED",
            f"The nightly `nrfm {args.command}` run crashed before "
            "completing.\nNo trading decisions should be made until a "
            "later run succeeds.\n\n" + tb,
        )
        if err:
            _log(f"alert email failed: {err}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
