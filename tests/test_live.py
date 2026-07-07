import numpy as np
import pandas as pd

from nrfm import config
from nrfm.engine.live import (DailyReport, check_stops, format_report,
                              STATE_LAST_REBALANCE, build_daily_report)
from nrfm.engine.panels import Panels
from nrfm.store import Store


def _panels(prices: dict[str, list[float]], start="2026-01-01") -> Panels:
    idx = pd.date_range(start, periods=len(next(iter(prices.values()))),
                        freq="B")
    adj = pd.DataFrame(prices, index=idx)
    vol = pd.DataFrame(1_000_000.0, index=idx, columns=list(prices))
    return Panels(adj=adj, close=adj.copy(), open=adj.copy(), volume=vol,
                  sectors=dict.fromkeys(prices, "X"))


def test_check_stops_triggers_below_trailing_high():
    up_then_crash = list(np.linspace(100, 140, 50)) + [104.0] * 10  # -26%
    steady = list(np.linspace(100, 110, 60))
    panels = _panels({"CRASH.ST": up_then_crash, "OK.ST": steady})
    since = panels.calendar[0].date().isoformat()
    hits = check_stops(panels, {"CRASH.ST": since, "OK.ST": since})
    assert hits == ["CRASH.ST"]


def test_check_stops_ignores_pre_entry_high():
    # peak happened BEFORE entry; since entry it only rose -> no stop
    crash_then_up = [140.0] * 10 + list(np.linspace(100, 110, 50))
    panels = _panels({"T.ST": crash_then_up})
    since = panels.calendar[20].date().isoformat()
    assert check_stops(panels, {"T.ST": since}) == []


def test_format_report_paper_trading_note_and_amounts():
    report = DailyReport(as_of="2026-07-07", regime="RISK_ON",
                         actionable=True, reason="monthly rebalance",
                         buys={"A.ST": 0.10}, targets={"A.ST": 0.10})
    body = format_report(report, equity=100.0)
    assert "PAPER TRADING" in body
    body_big = format_report(report, equity=100_000.0)
    assert "PAPER TRADING" not in body_big
    assert "10,000 SEK" in body_big


def test_daily_report_risk_off_with_holdings_sells_all(tmp_path,
                                                       monkeypatch):
    # integration-ish: store with synthetic data, index below SMA
    store = Store(tmp_path / "t.sqlite")
    days = 320
    idx_dates = pd.date_range("2025-01-01", periods=days, freq="B")
    falling = np.linspace(120, 80, days)  # ends far below its SMA
    store.insert_index_bars(config.INDEX_ORDERBOOK_ID, [
        {"date": d.date().isoformat(), "close": float(v)}
        for d, v in zip(idx_dates, falling)])
    rising = np.linspace(90, 130, days)
    store.upsert_instruments([{
        "orderbook_id": "TX1", "symbol": "AAA", "isin": "SE1",
        "name": "AAA", "currency": "SEK", "segment": "LARGE_CAP",
        "sector": "X", "yahoo_ticker": "AAA.ST",
    }], seen_date="2025-01-01")
    store.insert_yahoo_bars("AAA.ST", [
        {"date": d.date().isoformat(), "open": float(v), "high": float(v),
         "low": float(v), "close": float(v), "adj_close": float(v),
         "volume": 1_000_000}
        for d, v in zip(idx_dates, rising)])
    store.hold_add("AAA.ST", since="2025-06-01")

    report = build_daily_report(store)
    assert report.regime == "RISK_OFF"
    assert report.actionable
    assert report.sells == ["AAA.ST"]
    store.close()
