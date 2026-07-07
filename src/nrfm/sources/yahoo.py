"""Yahoo Finance client (adjusted daily prices, the price-of-record).

Fetches sequentially with throttling and backoff -- Yahoo's unofficial
endpoints rate-limit aggressively, and yfinance's threaded mode also has
a shared-cache locking bug, so one ticker at a time is both polite and
robust. A full-universe incremental pull (~300 tickers) takes a few
minutes, which is fine for a nightly job.
"""

from __future__ import annotations

import time

import pandas as pd
import yfinance as yf

from nrfm import config


class YahooFetchError(RuntimeError):
    pass


class YahooClient:
    def __init__(self, throttle: float = config.YAHOO_THROTTLE_SECONDS):
        self.throttle = throttle
        self._last_request = 0.0

    def daily_bars(self, ticker: str, start: str,
                   end: str | None = None) -> list[dict]:
        """Daily bars incl. adjusted close, oldest first.

        Returns [] when Yahoo has no data in the range (new listings,
        already-up-to-date tickers); raises YahooFetchError on repeated
        transport/rate-limit failures so callers can distinguish
        "no data" from "fetch broken".
        """
        wait = self.throttle - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

        last_err: Exception | None = None
        for attempt in range(config.MAX_RETRIES):
            try:
                self._last_request = time.monotonic()
                df = yf.download(
                    ticker,
                    start=start,
                    end=end,
                    interval="1d",
                    auto_adjust=False,
                    actions=False,
                    progress=False,
                    threads=False,
                )
                return self._normalize(df, ticker)
            except Exception as e:  # yfinance raises many exception types
                last_err = e
                time.sleep(config.BACKOFF_BASE_SECONDS * 2 ** attempt)
        raise YahooFetchError(f"{ticker}: {last_err}")

    @staticmethod
    def _normalize(df: pd.DataFrame, ticker: str) -> list[dict]:
        if df is None or df.empty:
            return []
        # yf.download returns column MultiIndex (field, ticker) even for
        # a single ticker; flatten to plain field names.
        if isinstance(df.columns, pd.MultiIndex):
            df = df.xs(ticker, axis=1, level="Ticker")
        bars = []
        for ts, row in df.iterrows():
            if pd.isna(row.get("Close")) or pd.isna(row.get("Adj Close")):
                continue  # partial rows appear around suspensions
            bars.append({
                "date": ts.date().isoformat(),
                "open": _f(row.get("Open")),
                "high": _f(row.get("High")),
                "low": _f(row.get("Low")),
                "close": _f(row.get("Close")),
                "adj_close": _f(row.get("Adj Close")),
                "volume": int(row["Volume"]) if pd.notna(row.get("Volume")) else None,
            })
        return bars


def _f(v) -> float | None:
    return None if v is None or pd.isna(v) else float(v)
