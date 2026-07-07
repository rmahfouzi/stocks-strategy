"""Central configuration for the NRFM system.

Strategy constants are FROZEN ex ante (see STRATEGY.md section 12).
Changing any strategy constant requires re-running the full backtest
methodology of STRATEGY.md section 13 and documenting why.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Paths -----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("NRFM_DATA_DIR", REPO_ROOT / "data"))
DB_PATH = DATA_DIR / "nrfm.sqlite"

# --- Data sources ----------------------------------------------------------

NASDAQ_API_BASE = "https://api.nasdaq.com/api/nordic"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)
NASDAQ_THROTTLE_SECONDS = 0.5
YAHOO_THROTTLE_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0

# Universe: Nasdaq Stockholm main market segments (STRATEGY.md section 5)
UNIVERSE_MARKET = "STO"
UNIVERSE_SEGMENTS = ("LARGE_CAP", "MID_CAP")

# Regime index: OMX Stockholm Gross Index (total return, broad market)
INDEX_ORDERBOOK_ID = "IX6782"
INDEX_SYMBOL = "OMXSGI"
# Long-history regime/benchmark index for backtests (OMXSGI only reaches
# ~2016 on the Nasdaq API): OMXS30 price index via Yahoo, 2005-.
REGIME_INDEX_YAHOO = "^OMX"

# Nasdaq Nordic chart endpoint serves at most ~10 years of daily history.
BACKFILL_YEARS_NASDAQ = 10
# Yahoo serves deeper history; used for backtests.
BACKFILL_START_YAHOO = "2005-01-01"

# Yahoo ticker overrides for Nasdaq symbols whose mechanical mapping
# (replace spaces with "-", append ".ST") is wrong. Populated when
# validation flags mismatches.
YAHOO_TICKER_OVERRIDES: dict[str, str] = {}

# --- Validation kill-switch (STRATEGY.md section 11, step 2) ----------------

VALIDATION_MIN_COVERAGE = 0.95  # fraction of active universe with fresh bars
VALIDATION_SAMPLE_SIZE = 20  # names cross-checked Yahoo vs Nasdaq
VALIDATION_MAX_CLOSE_DIFF = 0.02  # relative close mismatch tolerance
VALIDATION_GLITCH_JUMP = 0.40  # adj-close jump without raw-close jump

# --- Strategy constants (FROZEN, STRATEGY.md section 12) ---------------------

N_HOLDINGS = 10
HOLD_BUFFER_RANK = 20
SKIP_DAYS = 21
LOOKBACK_6M = 126
LOOKBACK_12M = 252
VOL_WINDOW = 60
VOL_MAX = 0.60
SMA_WINDOW = 200
REGIME_HYSTERESIS = 0.02
WEIGHT_CAP = 0.15
SECTOR_CAP = 3
ADV_MIN_SEK = 10_000_000
ADV_WINDOW = 60
PRICE_MIN_SEK = 5.0
MIN_HISTORY_DAYS = 260
MAX_MISSING_DAYS = 5
TRADE_BAND = 0.20
CATASTROPHE_STOP = 0.25
CASH_BUFFER = 0.02
MIN_ORDER_SEK = 1_500

# --- Backtest assumptions (STRATEGY.md section 13) ---------------------------

INITIAL_CAPITAL_SEK = 500_000
COST_PER_SIDE = 0.0025  # 0.15% courtage + 0.10% half-spread
