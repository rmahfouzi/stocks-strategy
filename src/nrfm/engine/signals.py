"""Feature computation and regime state machine (STRATEGY.md section 6).

Pure functions of the panels -- no store access, no side effects, so the
backtest and the live run cannot diverge.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from nrfm import config
from nrfm.engine.panels import Panels

RISK_ON = "RISK_ON"
RISK_OFF = "RISK_OFF"


def features_asof(panels: Panels, i: int) -> pd.DataFrame:
    """Cross-sectional features at calendar position `i` (a close).

    Columns: mom, vol, adv, price, missing, history, eligible.
    Uses only data at or before position i.
    """
    if i < config.LOOKBACK_12M:
        raise ValueError(f"position {i} is inside the warmup window")

    adj = panels.adj_ff
    p_now = adj.iloc[i - config.SKIP_DAYS]
    r6 = p_now / adj.iloc[i - config.LOOKBACK_6M] - 1
    r12 = p_now / adj.iloc[i - config.LOOKBACK_12M] - 1
    mom = 0.5 * r6 + 0.5 * r12

    window = slice(i - config.VOL_WINDOW + 1, i + 1)
    rets = np.log(adj.iloc[window] / adj.iloc[window].shift())
    vol = rets.std() * np.sqrt(252)

    turnover = (panels.close * panels.volume).iloc[window]
    adv = turnover.median()

    feat = pd.DataFrame({
        "mom": mom,
        "vol": vol,
        "adv": adv,
        "price": panels.close_ff.iloc[i],
        "missing": panels.adj.iloc[window].isna().sum(),
        "history": panels.history.iloc[i],
    })
    feat["eligible"] = (
        feat["mom"].notna()
        & feat["vol"].notna()
        & (feat["history"] >= config.MIN_HISTORY_DAYS)
        & (feat["adv"] >= config.ADV_MIN_SEK)
        & (feat["price"] >= config.PRICE_MIN_SEK)
        & (feat["missing"] <= config.MAX_MISSING_DAYS)
        & (feat["vol"] <= config.VOL_MAX)
        & (feat["vol"] > 0)
    )
    return feat


def regime_states(index_close: pd.Series,
                  sma_window: int = config.SMA_WINDOW,
                  hysteresis: float = config.REGIME_HYSTERESIS) -> pd.Series:
    """Hysteresis state machine over an index series.

    RISK_OFF when close < (1-h)*SMA, RISK_ON when close > (1+h)*SMA,
    previous state kept inside the band. NaN during SMA warmup.
    """
    sma = index_close.rolling(sma_window).mean()
    states = pd.Series(index=index_close.index, dtype=object)
    state = None
    for ts, close, avg in zip(index_close.index, index_close.values, sma.values):
        if np.isnan(avg) or np.isnan(close):
            states[ts] = state
            continue
        if state is None:
            state = RISK_ON if close > avg else RISK_OFF
        elif close < (1 - hysteresis) * avg:
            state = RISK_OFF
        elif close > (1 + hysteresis) * avg:
            state = RISK_ON
        states[ts] = state
    return states
