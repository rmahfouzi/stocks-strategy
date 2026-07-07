"""Portfolio selection -- THE shared code path (STRATEGY.md sections 7-9).

Both the live signal run and every backtest rebalance call
`select_portfolio()`. Do not fork this logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from nrfm import config


@dataclass
class Selection:
    weights: dict[str, float]          # ticker -> target weight (sums to <=0.98)
    ranked: pd.DataFrame               # eligible names with mom rank
    kept: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def select_portfolio(feat: pd.DataFrame, sectors: dict[str, str],
                     holdings: set[str] | None = None,
                     n: int = config.N_HOLDINGS,
                     companies: dict[str, str] | None = None) -> Selection:
    """Target portfolio from cross-sectional features.

    `holdings` enables the rank buffer: incumbents stay while ranked
    within HOLD_BUFFER_RANK; new entries come only from the top ranks,
    subject to the sector cap. `companies` dedups share classes: one
    ticker per issuer, preferring a held class, then the most liquid.
    """
    holdings = holdings or set()
    eligible = feat[feat["eligible"]].copy()
    if companies:
        eligible["_company"] = [companies.get(t, t) for t in eligible.index]
        eligible["_held"] = [t in holdings for t in eligible.index]
        eligible = (eligible
                    .sort_values(["_held", "adv"], ascending=False)
                    .drop_duplicates("_company")
                    .drop(columns=["_company", "_held"]))
    ranked = eligible.sort_values("mom", ascending=False)
    ranked["rank"] = range(1, len(ranked) + 1)
    rank_of = ranked["rank"].to_dict()

    kept = [h for h in holdings
            if rank_of.get(h, 10 ** 9) <= config.HOLD_BUFFER_RANK]
    dropped = sorted(holdings - set(kept))

    sector_count: dict[str, int] = {}
    for t in kept:
        s = sectors.get(t, "Unknown")
        sector_count[s] = sector_count.get(s, 0) + 1

    added: list[str] = []
    for t in ranked.index:
        if len(kept) + len(added) >= n:
            break
        if t in holdings:
            continue
        s = sectors.get(t, "Unknown")
        if sector_count.get(s, 0) >= config.SECTOR_CAP:
            continue
        added.append(t)
        sector_count[s] = sector_count.get(s, 0) + 1

    port = kept + added
    weights = inverse_vol_weights(feat.loc[port, "vol"]) if port else {}
    weights = {t: w * (1 - config.CASH_BUFFER) for t, w in weights.items()}
    return Selection(weights=weights, ranked=ranked, kept=sorted(kept),
                     added=added, dropped=dropped)


def inverse_vol_weights(vol: pd.Series,
                        cap: float = config.WEIGHT_CAP) -> dict[str, float]:
    """1/vol weights capped per name (waterfill).

    With fewer than 1/cap names the cap binds for everyone and the
    remainder deliberately stays in cash (concentration limit).
    """
    inv = 1.0 / vol
    if cap * len(inv) <= 1.0 + 1e-12:
        return {t: cap for t in inv.index}
    capped: set[str] = set()
    for _ in range(len(inv)):
        free = [t for t in inv.index if t not in capped]
        mass = 1.0 - cap * len(capped)
        w_free = inv[free] / inv[free].sum() * mass
        newly = w_free[w_free > cap + 1e-12].index
        if len(newly) == 0:
            return {t: cap for t in capped} | w_free.to_dict()
        capped.update(newly)
    return {t: cap for t in inv.index}
