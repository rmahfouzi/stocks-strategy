"""SQLite-backed local data store.

Single file, append-mostly. All dates are ISO strings (YYYY-MM-DD) in
exchange-local terms (Europe/Stockholm trading days).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from nrfm import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    orderbook_id TEXT PRIMARY KEY,
    symbol       TEXT NOT NULL,
    isin         TEXT,
    name         TEXT,
    currency     TEXT,
    segment      TEXT,
    sector       TEXT,
    yahoo_ticker TEXT,
    first_seen   TEXT,
    last_seen    TEXT,
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS prices_nasdaq (
    orderbook_id TEXT NOT NULL,
    date         TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume       INTEGER,
    PRIMARY KEY (orderbook_id, date)
);

CREATE TABLE IF NOT EXISTS prices_yahoo (
    ticker    TEXT NOT NULL,
    date      TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    adj_close REAL,
    volume    INTEGER,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS index_prices (
    orderbook_id TEXT NOT NULL,
    date         TEXT NOT NULL,
    close        REAL,
    PRIMARY KEY (orderbook_id, date)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    ts      TEXT NOT NULL,
    source  TEXT NOT NULL,
    key     TEXT NOT NULL,
    status  TEXT NOT NULL,
    rows    INTEGER,
    message TEXT
);

CREATE TABLE IF NOT EXISTS validation_log (
    ts      TEXT NOT NULL,
    date    TEXT NOT NULL,
    ok      INTEGER NOT NULL,
    details TEXT
);

-- what the user actually holds at Avanza (manually maintained via
-- `nrfm hold`); drives the rank-buffer logic in live signals
CREATE TABLE IF NOT EXISTS holdings (
    ticker  TEXT PRIMARY KEY,
    since   TEXT
);

-- small key-value state (last rebalance month, portfolio equity, ...)
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, db_path: Path | str = config.DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- instruments ---------------------------------------------------

    def upsert_instruments(self, rows: Iterable[dict], seen_date: str) -> int:
        """Upsert screener rows; mark instruments absent from `rows` inactive."""
        cur = self.conn.cursor()
        seen_ids = []
        for r in rows:
            seen_ids.append(r["orderbook_id"])
            cur.execute(
                """
                INSERT INTO instruments
                    (orderbook_id, symbol, isin, name, currency, segment,
                     sector, yahoo_ticker, first_seen, last_seen, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(orderbook_id) DO UPDATE SET
                    symbol=excluded.symbol, isin=excluded.isin,
                    name=excluded.name, currency=excluded.currency,
                    segment=excluded.segment, sector=excluded.sector,
                    yahoo_ticker=excluded.yahoo_ticker,
                    last_seen=excluded.last_seen, active=1
                """,
                (
                    r["orderbook_id"], r["symbol"], r.get("isin"),
                    r.get("name"), r.get("currency"), r.get("segment"),
                    r.get("sector"), r.get("yahoo_ticker"),
                    seen_date, seen_date,
                ),
            )
        if seen_ids:
            placeholders = ",".join("?" * len(seen_ids))
            cur.execute(
                f"UPDATE instruments SET active=0 "
                f"WHERE orderbook_id NOT IN ({placeholders})",
                seen_ids,
            )
        self.conn.commit()
        return len(seen_ids)

    def active_instruments(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM instruments WHERE active=1 ORDER BY symbol"
        ).fetchall()

    # --- prices ---------------------------------------------------------

    def insert_nasdaq_bars(self, orderbook_id: str, bars: Iterable[dict]) -> int:
        cur = self.conn.executemany(
            """
            INSERT OR REPLACE INTO prices_nasdaq
                (orderbook_id, date, open, high, low, close, volume)
            VALUES (:orderbook_id, :date, :open, :high, :low, :close, :volume)
            """,
            [{**b, "orderbook_id": orderbook_id} for b in bars],
        )
        self.conn.commit()
        return cur.rowcount

    def insert_yahoo_bars(self, ticker: str, bars: Iterable[dict]) -> int:
        cur = self.conn.executemany(
            """
            INSERT OR REPLACE INTO prices_yahoo
                (ticker, date, open, high, low, close, adj_close, volume)
            VALUES (:ticker, :date, :open, :high, :low, :close,
                    :adj_close, :volume)
            """,
            [{**b, "ticker": ticker} for b in bars],
        )
        self.conn.commit()
        return cur.rowcount

    def insert_index_bars(self, orderbook_id: str, bars: Iterable[dict]) -> int:
        cur = self.conn.executemany(
            """
            INSERT OR REPLACE INTO index_prices (orderbook_id, date, close)
            VALUES (:orderbook_id, :date, :close)
            """,
            [
                {"orderbook_id": orderbook_id, "date": b["date"],
                 "close": b["close"]}
                for b in bars
            ],
        )
        self.conn.commit()
        return cur.rowcount

    def last_date(self, table: str, key_col: str, key: str) -> str | None:
        row = self.conn.execute(
            f"SELECT MAX(date) AS d FROM {table} WHERE {key_col}=?", (key,)
        ).fetchone()
        return row["d"]

    def latest_index_date(self) -> str | None:
        return self.last_date("index_prices", "orderbook_id",
                              config.INDEX_ORDERBOOK_ID)

    # --- holdings ---------------------------------------------------------

    def holdings(self) -> list[str]:
        return [r["ticker"] for r in self.conn.execute(
            "SELECT ticker FROM holdings ORDER BY ticker")]

    def hold_add(self, ticker: str, since: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO holdings (ticker, since) VALUES (?, ?)",
            (ticker, since))
        self.conn.commit()

    def hold_remove(self, ticker: str) -> None:
        self.conn.execute("DELETE FROM holdings WHERE ticker=?", (ticker,))
        self.conn.commit()

    # --- state ------------------------------------------------------------

    def state_get(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def state_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
            (key, value))
        self.conn.commit()

    # --- logs -----------------------------------------------------------

    def log_fetch(self, source: str, key: str, status: str,
                  rows: int = 0, message: str = "") -> None:
        self.conn.execute(
            "INSERT INTO fetch_log (ts, source, key, status, rows, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_utcnow(), source, key, status, rows, message),
        )
        self.conn.commit()

    def log_validation(self, date: str, ok: bool, details: str) -> None:
        self.conn.execute(
            "INSERT INTO validation_log (ts, date, ok, details) "
            "VALUES (?, ?, ?, ?)",
            (_utcnow(), date, int(ok), details),
        )
        self.conn.commit()

    # --- stats ------------------------------------------------------------

    def stats(self) -> dict:
        q = self.conn.execute
        out = {}
        out["instruments_active"] = q(
            "SELECT COUNT(*) c FROM instruments WHERE active=1"
        ).fetchone()["c"]
        out["instruments_total"] = q(
            "SELECT COUNT(*) c FROM instruments").fetchone()["c"]
        for table, label in [("prices_nasdaq", "nasdaq"),
                             ("prices_yahoo", "yahoo"),
                             ("index_prices", "index")]:
            row = q(f"SELECT COUNT(*) c, MIN(date) lo, MAX(date) hi "
                    f"FROM {table}").fetchone()
            out[f"{label}_rows"] = row["c"]
            out[f"{label}_range"] = (row["lo"], row["hi"])
        row = q("SELECT ok, date FROM validation_log "
                "ORDER BY ts DESC LIMIT 1").fetchone()
        out["last_validation"] = (
            {"date": row["date"], "ok": bool(row["ok"])} if row else None
        )
        return out
