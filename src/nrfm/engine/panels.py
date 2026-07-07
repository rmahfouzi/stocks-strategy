"""Load price panels (dates x tickers) from the store for the engine.

The engine works on Yahoo data exclusively (adjusted closes for returns,
raw close*volume for liquidity) so that the backtest and the live signal
run consume byte-identical inputs. Nasdaq data stays in its lane:
universe membership, sectors, index, validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from nrfm import config
from nrfm.store import Store


@dataclass
class Panels:
    adj: pd.DataFrame        # adjusted close (raw, NaN where no bar)
    close: pd.DataFrame      # unadjusted close
    open: pd.DataFrame       # unadjusted open
    volume: pd.DataFrame
    sectors: dict[str, str]  # ticker -> sector name
    companies: dict[str, str] | None = None  # ticker -> issuer key (class dedup)
    calendar: pd.DatetimeIndex = field(init=False)
    adj_ff: pd.DataFrame = field(init=False)    # ffilled (limit 5) adj close
    close_ff: pd.DataFrame = field(init=False)
    adj_open: pd.DataFrame = field(init=False)  # open scaled to adj terms
    history: pd.DataFrame = field(init=False)   # cumulative bar count

    def __post_init__(self):
        self.calendar = self.adj.index
        self.adj_ff = self.adj.ffill(limit=config.MAX_MISSING_DAYS)
        self.close_ff = self.close.ffill(limit=config.MAX_MISSING_DAYS)
        # adjusted open = open * (adj_close / close); falls back to
        # adjusted close where the open is missing
        factor = self.adj / self.close
        self.adj_open = (self.open * factor).where(
            self.open.notna(), self.adj)
        self.history = self.adj.notna().cumsum()


def company_key(symbol: str) -> str:
    """Issuer key for share-class dedup: 'SSAB A'/'SSAB B' -> 'SSAB'."""
    parts = symbol.rsplit(" ", 1)
    if len(parts) == 2 and parts[1] in {"A", "B", "C", "D", "SDB"}:
        return parts[0]
    return symbol


def load_panels(store: Store, start: str | None = None) -> Panels:
    instruments = store.active_instruments()
    tickers = [r["yahoo_ticker"] for r in instruments]
    sectors = {r["yahoo_ticker"]: r["sector"] or "Unknown"
               for r in instruments}
    companies = {r["yahoo_ticker"]: company_key(r["symbol"])
                 for r in instruments}

    query = (
        "SELECT ticker, date, open, close, adj_close, volume "
        "FROM prices_yahoo WHERE ticker IN ({}) {}".format(
            ",".join("?" * len(tickers)),
            "AND date >= ?" if start else "",
        )
    )
    params = tickers + ([start] if start else [])
    df = pd.read_sql_query(query, store.conn, params=params,
                           parse_dates=["date"])

    def pivot(col: str) -> pd.DataFrame:
        return df.pivot(index="date", columns="ticker", values=col).sort_index()

    return Panels(
        adj=pivot("adj_close"),
        close=pivot("close"),
        open=pivot("open"),
        volume=pivot("volume"),
        sectors=sectors,
        companies=companies,
    )


def load_index_series(store: Store, orderbook_id: str,
                      start: str | None = None) -> pd.Series:
    query = ("SELECT date, close FROM index_prices WHERE orderbook_id=? "
             + ("AND date >= ? " if start else "") + "ORDER BY date")
    params = [orderbook_id] + ([start] if start else [])
    df = pd.read_sql_query(query, store.conn, params=params,
                           parse_dates=["date"])
    return df.set_index("date")["close"].astype(np.float64)
