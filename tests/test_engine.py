import numpy as np
import pandas as pd
import pytest

from nrfm import config
from nrfm.engine.backtest import run_backtest
from nrfm.engine.panels import Panels
from nrfm.engine.select import inverse_vol_weights, select_portfolio
from nrfm.engine.signals import RISK_OFF, RISK_ON, features_asof, regime_states

# --- regime -----------------------------------------------------------------


def test_regime_hysteresis():
    # gently rising 250 days (strictly above SMA), then drop to 90
    # (< 0.98*SMA), then back inside the band (no flip), then far above
    vals = list(np.linspace(99.0, 101.0, 250)) + [90.0] * 5 + [99.0] * 5 + [112.0] * 5
    idx = pd.date_range("2020-01-01", periods=len(vals), freq="B")
    s = regime_states(pd.Series(vals, index=idx), sma_window=200,
                      hysteresis=0.02)
    assert s.iloc[249] == RISK_ON
    assert s.iloc[252] == RISK_OFF          # crossed lower band
    assert s.iloc[257] == RISK_OFF          # inside band: state sticks
    assert s.iloc[-1] == RISK_ON            # crossed upper band


def test_regime_warmup_is_none():
    idx = pd.date_range("2020-01-01", periods=100, freq="B")
    s = regime_states(pd.Series(100.0, index=idx), sma_window=200)
    assert s.isna().all()


# --- weights ----------------------------------------------------------------


def test_inverse_vol_weights_sum_and_cap():
    vol = pd.Series({"A": 0.10, "B": 0.20, "C": 0.40, "D": 0.40,
                     "E": 0.40, "F": 0.40, "G": 0.40, "H": 0.40})
    w = inverse_vol_weights(vol, cap=0.15)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert max(w.values()) <= 0.15 + 1e-9
    assert w["A"] == pytest.approx(0.15)    # lowest vol hits the cap
    assert w["C"] == pytest.approx(w["D"])


def test_inverse_vol_weights_few_names_leave_cash():
    w = inverse_vol_weights(pd.Series({"A": 0.2, "B": 0.3}), cap=0.15)
    assert w == {"A": 0.15, "B": 0.15}      # 70% stays in cash


# --- selection ---------------------------------------------------------------


def _feat(rows: dict[str, dict]) -> pd.DataFrame:
    df = pd.DataFrame.from_dict(rows, orient="index")
    df["eligible"] = True
    return df


def test_select_ranks_and_sector_cap():
    rows = {f"S{k}": {"mom": 1.0 - k * 0.01, "vol": 0.2} for k in range(15)}
    feat = _feat(rows)
    sectors = {t: ("Industrials" if int(t[1:]) < 5 else f"sec{int(t[1:]) % 4}")
               for t in feat.index}
    sel = select_portfolio(feat, sectors, holdings=set(), n=10)
    picked = set(sel.weights)
    # only 3 of the top-5 industrials allowed
    assert sum(1 for t in picked if sectors[t] == "Industrials") == 3
    assert len(picked) == 10
    # the two blocked industrials were skipped, not queued
    assert {"S0", "S1", "S2"}.issubset(picked)
    assert not {"S3", "S4"} & picked


def test_select_buffer_keeps_incumbent_outside_top10():
    rows = {f"S{k}": {"mom": 1.0 - k * 0.01, "vol": 0.2} for k in range(30)}
    feat = _feat(rows)
    sectors = dict.fromkeys(feat.index, f"X")
    sectors = {t: f"sec{int(t[1:]) % 5}" for t in feat.index}
    # S14 ranks 15th: kept because <= HOLD_BUFFER_RANK; S25 ranks 26th: dropped
    sel = select_portfolio(feat, sectors, holdings={"S14", "S25", "S0"}, n=10)
    assert "S14" in sel.kept
    assert "S25" in sel.dropped
    assert "S0" in sel.kept
    assert len(sel.weights) == 10


def test_select_dedups_share_classes():
    rows = {
        "SSAB-A.ST": {"mom": 0.50, "vol": 0.2, "adv": 5e7},
        "SSAB-B.ST": {"mom": 0.52, "vol": 0.2, "adv": 9e7},
        "OTHER.ST": {"mom": 0.30, "vol": 0.2, "adv": 9e7},
    }
    feat = _feat(rows)
    sectors = dict.fromkeys(feat.index, "Materials")
    companies = {"SSAB-A.ST": "SSAB", "SSAB-B.ST": "SSAB",
                 "OTHER.ST": "OTHER"}
    sel = select_portfolio(feat, sectors, holdings=set(), n=10,
                           companies=companies)
    assert "SSAB-B.ST" in sel.weights          # more liquid class wins
    assert "SSAB-A.ST" not in sel.weights
    # ...unless the A class is already held: then it is preferred
    sel2 = select_portfolio(feat, sectors, holdings={"SSAB-A.ST"}, n=10,
                            companies=companies)
    assert "SSAB-A.ST" in sel2.weights
    assert "SSAB-B.ST" not in sel2.weights


# --- synthetic backtest -------------------------------------------------------


def _make_panels(n_days=800, n_stocks=25, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n_days, freq="B")
    tickers = [f"T{k}.ST" for k in range(n_stocks)]
    drift = np.linspace(0.0008, -0.0004, n_stocks)  # T0 trends best
    rets = rng.normal(drift, 0.015, size=(n_days, n_stocks))
    price = 100 * np.exp(np.cumsum(rets, axis=0))
    adj = pd.DataFrame(price, index=idx, columns=tickers)
    volume = pd.DataFrame(1_000_000.0, index=idx, columns=tickers)
    sectors = {t: f"sec{k % 6}" for k, t in enumerate(tickers)}
    return Panels(adj=adj, close=adj.copy(), open=adj.copy(),
                  volume=volume, sectors=sectors)


def test_backtest_runs_and_is_sane():
    panels = _make_panels()
    # rising index: always RISK_ON after warmup
    index = pd.Series(np.linspace(100, 200, 800), index=panels.calendar)
    res = run_backtest(panels, index, initial_capital=500_000)
    assert len(res.equity) > 300
    assert res.equity.iloc[0] == pytest.approx(500_000, rel=0.02)
    assert (res.trades["value"] > 0).all()
    # equity stays positive and no cash leak: final = cash + positions only
    assert res.equity.min() > 0
    assert res.metrics["n_trades"] > 10


def test_backtest_regime_exit_goes_to_cash():
    panels = _make_panels()
    # index collapses halfway -> regime exit -> equity flat afterwards
    vals = np.concatenate([np.linspace(100, 150, 400),
                           np.linspace(150, 80, 400)])
    index = pd.Series(vals, index=panels.calendar)
    res = run_backtest(panels, index, initial_capital=500_000)
    regime = res.regime
    off_days = regime[regime == RISK_OFF]
    assert len(off_days) > 50
    # once risk-off is established, equity must be constant (all cash)
    tail = res.equity.loc[off_days.index[5]:]
    assert tail.std() / tail.mean() < 1e-6


def test_features_asof_uses_only_past_data():
    panels = _make_panels(n_days=400)
    i = 300
    feat_a = features_asof(panels, i)
    # mutate the future: must not change features at i
    panels2 = _make_panels(n_days=400)
    panels2.adj.iloc[i + 1:] *= 5.0
    panels2.__post_init__()
    feat_b = features_asof(panels2, i)
    pd.testing.assert_frame_equal(feat_a, feat_b)
