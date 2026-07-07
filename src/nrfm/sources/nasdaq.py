"""Client for the Nasdaq Nordic API (api.nasdaq.com/api/nordic).

This is the JSON API behind nasdaq.com's "European market activity" pages
(the successor of the legacy nasdaqomxnordic.com DataFeedProxy, which was
retired). Endpoints verified 2026-07-07:

- GET /screener/shares?category=MAIN_MARKET&tableonly=false&market=STO
      &segment=LARGE_CAP        -> universe listing incl. orderbookId,
                                   symbol, ISIN, sector, currency
- GET /instruments/{orderbookId}/chart?assetClass=SHARES
      &fromDate=YYYY-MM-DD&toDate=YYYY-MM-DD
                                -> daily OHLCV, ~10 years of depth
- GET /search?searchText=...    -> instrument lookup

Unofficial and undocumented: any schema change must surface as a loud
failure (raise), never as silently wrong data.
"""

from __future__ import annotations

import time
from datetime import date

import requests

from nrfm import config


class NasdaqApiError(RuntimeError):
    """The API answered, but not with the data we asked for."""


def _to_float(s: str | None) -> float | None:
    if s in (None, ""):
        return None
    return float(str(s).replace(",", ""))


def _to_int(s: str | None) -> int | None:
    f = _to_float(s)
    return None if f is None else int(f)


class NasdaqNordicClient:
    def __init__(self, throttle: float = config.NASDAQ_THROTTLE_SECONDS):
        self.throttle = throttle
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json",
        })

    def _get(self, path: str, params: dict) -> dict:
        wait = self.throttle - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        url = f"{config.NASDAQ_API_BASE}/{path}"
        last_err: Exception | None = None
        for attempt in range(config.MAX_RETRIES):
            try:
                self._last_request = time.monotonic()
                resp = self.session.get(
                    url, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise NasdaqApiError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                payload = resp.json()
                status = payload.get("status", {})
                if status.get("rCode") != 200:
                    raise NasdaqApiError(
                        f"rCode={status.get('rCode')} "
                        f"{status.get('bCodeMessage')}"
                    )
                return payload["data"]
            except (requests.RequestException, NasdaqApiError, ValueError) as e:
                last_err = e
                time.sleep(config.BACKOFF_BASE_SECONDS * 2 ** attempt)
        raise NasdaqApiError(f"GET {path} failed after retries: {last_err}")

    # --- universe -------------------------------------------------------

    def universe(self) -> list[dict]:
        """All shares in the configured market/segments, normalized."""
        rows: list[dict] = []
        for segment in config.UNIVERSE_SEGMENTS:
            data = self._get(
                "screener/shares",
                {
                    "category": "MAIN_MARKET",
                    "tableonly": "false",
                    "market": config.UNIVERSE_MARKET,
                    "segment": segment,
                },
            )
            listing = data["instrumentListing"]["rows"]
            pagination = data.get("pagination", {})
            if pagination.get("total", len(listing)) > len(listing):
                raise NasdaqApiError(
                    f"screener pagination unexpected: got {len(listing)} of "
                    f"{pagination.get('total')} rows for {segment}"
                )
            for r in listing:
                rows.append({
                    "orderbook_id": r["orderbookId"],
                    "symbol": r["symbol"],
                    "isin": r.get("isin"),
                    "name": r.get("fullName"),
                    "currency": r.get("currency"),
                    "segment": segment,
                    "sector": r.get("sector"),
                    "yahoo_ticker": yahoo_ticker_for(r["symbol"]),
                })
        return rows

    # --- prices ---------------------------------------------------------

    def daily_bars(
        self,
        orderbook_id: str,
        from_date: str,
        to_date: str | None = None,
        asset_class: str = "SHARES",
    ) -> list[dict]:
        """Daily OHLCV bars, oldest first. Index bars have volume=None."""
        data = self._get(
            f"instruments/{orderbook_id}/chart",
            {
                "assetClass": asset_class,
                "fromDate": from_date,
                "toDate": to_date or date.today().isoformat(),
            },
        )
        bars = []
        for point in data.get("CP") or []:
            z = point["z"]
            bars.append({
                "date": z["dateTime"],
                "open": _to_float(z.get("open")),
                "high": _to_float(z.get("high")),
                "low": _to_float(z.get("low")),
                "close": _to_float(z.get("close")),
                "volume": _to_int(z.get("volume")),
            })
        bars.sort(key=lambda b: b["date"])
        return bars

    def index_bars(self, from_date: str, to_date: str | None = None) -> list[dict]:
        return self.daily_bars(
            config.INDEX_ORDERBOOK_ID, from_date, to_date,
            asset_class="INDEXES",
        )

    def search(self, text: str) -> list[dict]:
        data = self._get("search", {"searchText": text})
        out = []
        for group in data or []:
            out.extend(group.get("instruments", []))
        return out


def yahoo_ticker_for(nasdaq_symbol: str) -> str:
    """Map a Nasdaq Nordic symbol ("VOLV B") to a Yahoo ticker ("VOLV-B.ST")."""
    if nasdaq_symbol in config.YAHOO_TICKER_OVERRIDES:
        return config.YAHOO_TICKER_OVERRIDES[nasdaq_symbol]
    return nasdaq_symbol.replace(" ", "-") + ".ST"
