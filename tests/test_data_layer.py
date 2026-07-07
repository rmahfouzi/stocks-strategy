from datetime import date

import pytest

from nrfm import config
from nrfm.sources.nasdaq import _to_float, _to_int, yahoo_ticker_for
from nrfm.store import Store
from nrfm.validate import validate


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.sqlite") as s:
        yield s


def test_number_parsing_handles_thousand_separators():
    assert _to_float("3,404,518") == 3404518.0
    assert _to_int("3,404,518") == 3404518
    assert _to_float("") is None
    assert _to_float(None) is None


def test_yahoo_ticker_mapping():
    assert yahoo_ticker_for("VOLV B") == "VOLV-B.ST"
    assert yahoo_ticker_for("INVE A") == "INVE-A.ST"
    assert yahoo_ticker_for("SAAB B") == "SAAB-B.ST"


def _instrument(obid="TX100", symbol="VOLV B"):
    return {
        "orderbook_id": obid, "symbol": symbol, "isin": "SE0000115446",
        "name": "Volvo B", "currency": "SEK", "segment": "LARGE_CAP",
        "sector": "Industrials", "yahoo_ticker": yahoo_ticker_for(symbol),
    }


def test_universe_upsert_marks_dropped_inactive(store):
    store.upsert_instruments([_instrument(), _instrument("TX99", "VOLV A")],
                             seen_date="2026-07-01")
    store.upsert_instruments([_instrument()], seen_date="2026-07-07")
    rows = {r["orderbook_id"]: r["active"]
            for r in store.conn.execute("SELECT * FROM instruments")}
    assert rows == {"TX100": 1, "TX99": 0}


def test_price_roundtrip_and_incremental_watermark(store):
    bars = [
        {"date": "2026-07-03", "open": 1, "high": 2, "low": 0.5,
         "close": 1.5, "volume": 100},
        {"date": "2026-07-06", "open": 1, "high": 2, "low": 0.5,
         "close": 1.6, "volume": 200},
    ]
    store.insert_nasdaq_bars("TX100", bars)
    assert store.last_date("prices_nasdaq", "orderbook_id", "TX100") == "2026-07-06"
    # re-insert is idempotent
    store.insert_nasdaq_bars("TX100", bars)
    n = store.conn.execute("SELECT COUNT(*) c FROM prices_nasdaq").fetchone()["c"]
    assert n == 2


def _seed_consistent_day(store, d="2026-07-06"):
    store.upsert_instruments([_instrument()], seen_date=d)
    store.insert_index_bars(config.INDEX_ORDERBOOK_ID,
                            [{"date": d, "close": 583.46}])
    store.insert_nasdaq_bars("TX100", [
        {"date": d, "open": 340, "high": 342, "low": 338,
         "close": 340.7, "volume": 1000}])
    store.insert_yahoo_bars("VOLV-B.ST", [
        {"date": d, "open": 340, "high": 342, "low": 338,
         "close": 340.7, "adj_close": 335.0, "volume": 1000}])


def test_validation_passes_on_consistent_data(store):
    _seed_consistent_day(store)
    assert validate(store, sample_seed=1).ok


def test_validation_fails_on_close_mismatch(store):
    _seed_consistent_day(store)
    store.insert_yahoo_bars("VOLV-B.ST", [
        {"date": "2026-07-06", "open": 340, "high": 342, "low": 338,
         "close": 300.0, "adj_close": 295.0, "volume": 1000}])
    result = validate(store, sample_seed=1)
    assert not result.ok
    assert any("mismatch" in i for i in result.issues)


def test_validation_fails_on_missing_coverage(store):
    _seed_consistent_day(store)
    # index has a newer day than any stock price -> 0% coverage
    store.insert_index_bars(config.INDEX_ORDERBOOK_ID,
                            [{"date": "2026-07-07", "close": 584.0}])
    result = validate(store, sample_seed=1)
    assert not result.ok
    assert any("coverage" in i for i in result.issues)


def test_validation_fails_on_adjustment_glitch(store):
    _seed_consistent_day(store)
    store.insert_yahoo_bars("VOLV-B.ST", [
        {"date": "2026-07-05", "open": 340, "high": 342, "low": 338,
         "close": 340.0, "adj_close": 200.0, "volume": 900}])
    result = validate(store, sample_seed=1)
    assert not result.ok
    assert any("glitch" in i for i in result.issues)
