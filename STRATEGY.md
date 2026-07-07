# Systematic Equity Strategy Specification

**Strategy name:** Nordic Regime-Filtered Momentum (NRFM)
**Version:** 1.0 — 2026-07-07
**Status:** Research specification, ready for implementation
**Target broker:** Avanza (execution layer only; all logic is broker-independent)

---

## 1. Strategy overview

A long-only, cross-sectional **momentum** strategy on **Nasdaq Stockholm Large + Mid Cap**
stocks, with an **absolute-momentum (trend) regime filter** on the broad Swedish index that
moves the whole portfolio to cash in sustained bear markets, and **inverse-volatility
position sizing**.

- Holds **10 stocks** (or 100% cash in risk-off regime).
- Rebalances **monthly** with a rank-buffer to keep turnover low (~200–350% one-way/year).
- Runs a **daily** job, but on most days the job only monitors (regime check + catastrophe
  stops); actual trading happens roughly once a month.
- Every rule is parameter-fixed **ex ante from the academic literature** — nothing is
  optimized on Swedish historical data, which is the main defense against curve fitting.

Why this has a realistic chance of beating the index after costs:

1. Cross-sectional momentum is the most robust and most replicated anomaly in equity
   markets (Jegadeesh & Titman 1993; Fama & French 2008 call it "the premier anomaly";
   documented in Sweden and across 40+ countries by Asness, Moskowitz & Pedersen 2013,
   *Value and Momentum Everywhere*).
2. Time-series trend filters (Faber 2007; Antonacci 2014 "dual momentum") do not add much
   raw return but historically cut maximum drawdown roughly in half, which is what makes
   the strategy survivable for a real human running real money.
3. Costs are controlled structurally (monthly cadence, rank buffers, liquid large/mid caps,
   SEK-only universe so no FX fees, ISK account so no per-trade capital gains tax in Sweden).

The realistic expectation, after costs, is **2–4% annualized outperformance over OMXSGI
with a materially lower maximum drawdown** — and multi-year periods of underperformance
along the way. Anyone promising more from free daily data is overfitting.

---

## 2. Market hypothesis

**Why should this make money? (economic rationale, not historical accident)**

- **Cross-sectional momentum:** investors underreact to news (anchoring, slow diffusion of
  information, disposition effect: winners are sold too early, losers held too long).
  Stocks that outperformed over the last 6–12 months continue to outperform over the next
  1–12 months. The effect *skips the most recent month* because at the ~1-month horizon
  the opposite (short-term reversal, driven by liquidity provision) dominates.
- **Time-series trend / absolute momentum:** bear markets are auto-correlated regimes, not
  single events — volatility clusters and drawdowns unfold over months. A slow trend
  signal (200-day moving average) exits after the first ~10–15% of a major decline and
  misses the worst of it. Its cost is whipsaw losses in choppy sideways markets.
- **Why the edge persists:** momentum requires tolerating tracking error, periodic sharp
  reversals ("momentum crashes"), and career risk that institutions cannot always bear.
  A small private account has none of those constraints and no capacity problem.
- **Why Sweden specifically:** the momentum premium is documented in Nordic markets, the
  Stockholm large/mid-cap segment is liquid enough for retail-size orders at tight
  spreads, it is Avanza's cheapest venue (no FX fee, lowest courtage), and the Swedish
  ISK account taxes a flat rate on capital instead of per-trade gains — which makes a
  moderate-turnover strategy dramatically more tax-efficient than in most countries.

---

## 3. Data sources

Only **daily OHLCV prices** (dividend/split adjusted), an **index level**, and a
**universe list with sector tags** are needed. No fundamentals, no sentiment, no macro
feeds. That is deliberate: fewer inputs = fewer failure modes and less overfitting surface.

### 3.1 Primary price source: Yahoo Finance via `yfinance` (`.ST` tickers)

- **Edge/why:** the only free source with long-history, **split- AND dividend-adjusted**
  daily closes for essentially all Stockholm-listed stocks. Total-return momentum needs
  adjusted prices; most free alternatives give raw prices only.
- **Extracted:** daily OHLCV + adjusted close, 15+ years of history, per ticker
  (e.g. `VOLV-B.ST`, `ATCO-A.ST`), plus `^OMX` (OMXS30) as fallback index.
- **Update frequency:** end-of-day (fetch after ~18:30 CET close; strategy only trades
  next-day open, so intraday delay is irrelevant).
- **Reliability/limitations:** unofficial endpoints; rate limits tightened since 2024 and
  can change without notice. Occasional bad adjusted closes around corporate actions.
  **Mitigations (mandatory):** cache everything locally in SQLite/Parquet, fetch
  incrementally (only missing days), throttle to ~1 request/2s, retry with exponential
  backoff, and run the cross-validation check in §3.2. ~200 tickers × 1 daily incremental
  fetch is far below any observed limit.
- **Alternatives:** EODHD (€/month for the ST exchange, best paid upgrade path),
  Stooq (free but Swedish coverage is thin and unadjusted), Alpha Vantage (25 req/day free
  — unusable for 200 tickers), Marketstack/Twelve Data (free tiers too small).

### 3.2 Validation + universe source: Nasdaq Nordic API (api.nasdaq.com/api/nordic)

*(Verified live 2026-07-07. The legacy `nasdaqomxnordic.com` DataFeedProxy described in
older wrappers is retired — it now redirects to nasdaq.com's "European market activity"
pages, which are backed by the unauthenticated JSON API below.)*

- **Edge/why:** the **exchange's own data** — authoritative for (a) the official list of
  Large/Mid Cap companies with sector tags, (b) unadjusted daily OHLCV used to cross-check
  Yahoo and compute turnover, (c) the **OMXSGI** index (broad Stockholm gross/total-return
  index, orderbook ID `IX6782`) used for the regime filter.
- **Endpoints (verified):**
  - `GET /screener/shares?category=MAIN_MARKET&tableonly=false&market=STO&segment=LARGE_CAP`
    (and `MID_CAP`) → 163+142 rows with `orderbookId`, `symbol`, `isin`, `sector`,
    `currency`, daily `turnover`.
  - `GET /instruments/{orderbookId}/chart?assetClass=SHARES&fromDate=…&toDate=…` →
    daily OHLCV, **~10 years of depth** (e.g. Volvo B = `TX100`).
  - `GET /search?searchText=…` → instrument lookup.
- **Update frequency:** end-of-day. **Historical availability:** ~10 years per instrument.
- **Reliability/limitations:** undocumented; the API host answers plain HTTP clients (the
  `qcapi.nasdaq.com` mirror is bot-blocked — use `api.nasdaq.com`). Treat any schema/parse
  failure as an alert, not a crash. Prices are unadjusted — use for volume/turnover,
  universe membership, index, and validation only, never for return computation.
- **Alternatives:** scraping Avanza's stock-list pages for universe membership; manual
  quarterly CSV of the segment lists as a last resort (membership changes slowly).

### 3.3 Broker-side source: Avanza unofficial API (`avanza-api` on PyPI, Qluxzz/avanza)

- **Edge/why:** this is where orders will eventually execute, so it is the ground truth
  for tradability, orderbook IDs, live quotes and spreads. Market-data endpoints
  (instrument search, orderbook, quote) are public; order placement needs TOTP login.
- **Extracted:** instrument/orderbook ID mapping (ISIN → Avanza ID), current bid/ask
  (spread check before sending orders), account positions.
- **Update frequency:** real-time. **Historical:** chart endpoints exist but Yahoo is
  better for history.
- **Reliability/limitations:** unofficial, no SLA, can change or break without warning
  (documented risk in the wrapper's README). Use only at execution time, never as the
  strategy's data dependency; strategy must be able to emit a human-readable trade list
  if the API is down.
- **Alternatives:** Nordnet's unofficial API (same caveats); manual execution from the
  generated trade list (the strategy trades ~1×/month, so manual fallback is genuinely viable).

### Rejected data sources

- **Fundamentals (Börsdata API):** best Nordic fundamentals but requires a paid Pro
  subscription → fails the "free" constraint. Noted as the #1 future upgrade (§15).
- **News/sentiment feeds, social media:** free versions are noisy, short-history,
  survivorship-riddled; enormous overfitting surface. No credible free edge here.
- **Macro data (FRED, Riksbank):** a price-based regime filter captures the same downside
  protection with one input instead of five. Macro timing rules rarely survive
  out-of-sample.

---

## 4. Research process: approaches considered and rejected

| Approach | Expected edge (retail, free data) | Robustness | Why rejected / accepted |
|---|---|---|---|
| **Cross-sectional momentum** | Strong, 40+ countries, 30 yrs OOS since publication | High | **ACCEPTED** — core signal. Price-only, low data needs, moderate turnover. |
| **Trend filter / absolute momentum** | Small return add, large drawdown cut | High | **ACCEPTED** — as regime overlay, not standalone. |
| **Volatility signals** | Real but secondary | High | **ACCEPTED** in reduced form: inverse-vol sizing + high-vol exclusion. Not a return source. |
| Mean reversion (daily/weekly) | Gross edge exists, net edge negative | Low for retail | High turnover (>2000%/yr) — Avanza courtage + spread destroys it. Needs near-zero costs. |
| Statistical arbitrage / pairs | — | Low | Needs shorting (expensive/limited at Avanza), intraday data, low costs. Not retail-feasible. |
| Fundamental factor investing (value/quality) | Moderate | High | Free point-in-time Nordic fundamentals don't exist; Yahoo fundamentals have look-ahead bias. Data constraint kills it, not the idea. |
| Risk parity | Diversification, not alpha | High | Asset-allocation strategy; needs bonds/commodities and cheap leverage. Doesn't answer "which stocks". |
| Macro regime models | Weak OOS | Low | Few independent macro cycles to learn from → overfitting machine. Price trend filter is the robust subset. |
| Sentiment | Unproven at retail | Very low | No reliable free Swedish-language sentiment source; extreme overfitting risk. |
| Trend following (futures-style, per-stock) | Moderate | Medium | Single-stock time-series trend is dominated by cross-sectional momentum + index filter; per-stock stops add turnover for little benefit. Folded into design as the regime filter. |

**Decision:** cross-sectional momentum + index trend filter + volatility sizing. Best joint
score on expected net return, robustness, simplicity, free-data availability, automation.

---

## 5. Universe definition

- **Country/currency:** Sweden, SEK only (no Avanza FX fee, no currency risk mixing).
- **Exchange:** Nasdaq Stockholm main market (XSTO), **Large Cap + Mid Cap** segments only
  (implicit market-cap floor ≈ €150M). No First North (spreads, manipulation risk), no
  Small Cap, no preference shares, no SDRs.
- **Share-class dedup:** one class per company — keep the class with highest 60-day median
  turnover (usually the B share).
- **Liquidity filter:** 60-day **median daily traded value ≥ SEK 10M** (retail orders of
  ~SEK 20–100k are then <1% of daily volume → negligible impact).
- **Price filter:** close ≥ SEK 5.
- **History filter:** ≥ 260 trading days of price history (needed for the 12-month signal;
  also excludes fresh IPOs, where momentum is unreliable).
- **Resulting universe:** ~200 listed → ~120–160 investable after filters. Refresh
  membership monthly from the Nasdaq Nordic list.

---

## 6. Feature calculations

All computed on **adjusted close** prices `P_t`. `t` = last completed trading day.

1. **Momentum score** (skip-month, two horizons averaged for robustness — no single
   lucky lookback):
   - `R6  = P[t-21] / P[t-126] - 1`   (6-month return, skipping last month)
   - `R12 = P[t-21] / P[t-252] - 1`   (12-month return, skipping last month)
   - `MOM = 0.5 * R6 + 0.5 * R12`
2. **Volatility:** `VOL = std(daily log returns, last 60 trading days) * sqrt(252)`
3. **Regime signal** (on OMXSGI; fallback OMXS30 `^OMX`):
   - `SMA200 = mean(index close, last 200 trading days)`
   - Hysteresis state machine (prevents whipsaw at the boundary):
     - switch to **RISK_OFF** when `index < 0.98 * SMA200`
     - switch to **RISK_ON** when `index > 1.02 * SMA200`
     - otherwise keep previous state. Initial state: RISK_ON iff `index > SMA200`.
4. **Liquidity:** `ADV = median(close * volume, last 60 trading days)` (unadjusted,
   from Nasdaq Nordic).

Missing data rule: a stock missing >5 of the last 60 closes is excluded this cycle.

---

## 7. Trading signals

On each **monthly rebalance date** (first trading day of the month), over the filtered
universe:

1. Exclude stocks with `VOL > 60%` annualized (momentum crashes concentrate in the
   highest-volatility names; this is a documented, principled screen, not a fitted one).
2. Rank remaining stocks by `MOM` descending → `rank(s)`.
3. **Buy list:** top 10 by rank, subject to sector cap (§9).
4. **Hold zone:** ranks 1–20. Existing holdings with `rank ≤ 20` are kept even if not in
   the top 10 (rank buffer — cuts turnover roughly in half for negligible signal loss).

## 8. Entry and exit rules

**Entries (monthly, only in RISK_ON):**
- Sell any holding with `rank > 20`, or that failed a universe filter.
- Fill open slots (10 − kept holdings) from the top of the rank list, skipping names
  blocked by the sector cap, at **next-day opening auction** (market-on-open or limit at
  open with a 0.5% limit band).

**Exits:**
1. **Rank exit** (monthly): rank > 20 → sell.
2. **Regime exit** (daily): state switches to RISK_OFF → sell **all** holdings next open;
   hold 100% cash (Avanza sparkonto / cash balance). No new entries until RISK_ON.
3. **Catastrophe stop** (daily): a holding closes **25% below its highest close since
   entry** → sell next open; slot stays in cash until the next monthly rebalance.
   Rationale: momentum strategies don't benefit from tight stops (they sell noise), but a
   25% trailing stop is wide enough to trigger only on genuine blowups (profit warnings,
   fraud) where the momentum thesis is dead anyway.
4. **Regime re-entry** (daily): state switches to RISK_ON → rebuild full 10-stock
   portfolio from current ranks at next open (do not wait for the monthly date).

## 9. Position sizing & portfolio construction

- Target weight for stock *i* among the N selected:
  `w_i = (1/VOL_i) / Σ_j (1/VOL_j)`, then **cap at 15%** and redistribute excess
  pro-rata; effective floor emerges naturally (~6–8%).
- **Sector cap:** max **3 holdings per ICB industry** (Stockholm is bank/industrial-heavy;
  uncapped momentum happily buys 6 industrials).
- **Cash buffer:** 2% held back for fees/slippage.
- **Trade-size band:** at monthly rebalance, only adjust an existing position if its
  actual weight deviates from target by **more than 20% relative** (e.g. target 10%,
  trade only outside 8–12%). Kills small churn trades that only pay minimum courtage.
- Minimum order size: skip any generated order below SEK 1,500 (minimum-courtage drag).

## 10. Risk management

| Layer | Mechanism | Protects against |
|---|---|---|
| Regime filter | 200d SMA + 2% hysteresis → 100% cash | Bear markets (2008, 2022 style) |
| Vol screen | Exclude VOL > 60% | Momentum crash exposure, junk rallies |
| Inverse-vol sizing + 15% cap | Weighting | Single-name concentration |
| Sector cap (3/industry) | Construction | Sector wipeout |
| Catastrophe stop (−25% trailing) | Daily check | Single-name blowups mid-month |
| Liquidity floor (SEK 10M ADV) | Universe | Unexitable positions, wide spreads |
| Ops kill-switch | If data validation fails (§11 step 2), **do nothing** and alert | Trading on corrupt data |

No leverage. No shorting. Max gross exposure 100%.

## 11. Daily execution workflow

Run every trading day at **~19:30 CET** (after close + data settling); orders queue for
next day's **09:00 opening auction**.

1. **Fetch:** incremental daily bars for all universe tickers (Yahoo) + index
   (Nasdaq Nordic) + universe membership if 1st of month. Persist to local store.
2. **Validate:** (a) today's date present for ≥95% of universe, (b) per-stock
   |Yahoo close − Nasdaq close| / Nasdaq close < 2% on a 20-name random sample,
   (c) no adjusted-close jump >40% without matching raw-close jump (corporate-action
   glitch detector). **Any failure → halt, alert, no orders today.**
3. **Regime:** update SMA200 state machine. On transition → generate full sell-all or
   full re-entry order list.
4. **Stops:** check trailing −25% on each holding → generate sell orders.
5. **If first trading day of month and RISK_ON:** run §7–9 → generate rebalance orders.
6. **Emit orders:** write trade list (ticker, side, quantity, order type) to file; send
   via Avanza API if enabled, else notify human for manual execution.
7. **Log:** positions, weights, signal values, regime state, all decisions → append-only
   log (this becomes the live track record and debugging trail).

## 12. Pseudocode

```
CONSTANTS (fixed ex ante, never re-optimized):
  N=10, HOLD_BUFFER=20, SKIP=21, L6=126, L12=252, VOL_WIN=60,
  VOL_MAX=0.60, SMA_WIN=200, HYST=0.02, W_CAP=0.15, SECTOR_CAP=3,
  ADV_MIN=10_000_000 SEK, PRICE_MIN=5, TRADE_BAND=0.20, STOP=0.25,
  CASH_BUFFER=0.02, MIN_ORDER=1500 SEK

daily_job(today):
  prices, index, members = fetch_and_cache()
  if not validate(prices, index): alert("DATA FAIL"); return   # kill-switch

  state = update_regime(index)          # hysteresis state machine, §6.3
  orders = []

  if state changed to RISK_OFF:
      orders += sell_all(holdings)
  elif state changed to RISK_ON:
      orders += build_portfolio(select(today), equity)          # immediate re-entry
  elif state == RISK_ON:
      for h in holdings:                                        # catastrophe stops
          if h.close < (1-STOP) * h.high_since_entry: orders += sell(h)
      if is_first_trading_day_of_month(today):
          orders += monthly_rebalance()

  emit(orders); log_state()

select(today):
  u = members(LargeCap|MidCap), dedup share classes by ADV
  u = filter(u: ADV>=ADV_MIN, price>=PRICE_MIN, history>=260d, missing<=5/60d)
  for s in u:
      s.MOM = 0.5*(P[t-SKIP]/P[t-L6]-1) + 0.5*(P[t-SKIP]/P[t-L12]-1)
      s.VOL = std(logret, VOL_WIN)*sqrt(252)
  u = filter(u: VOL <= VOL_MAX)
  return sort_desc(u, MOM)                                      # rank 1 = best

monthly_rebalance():
  ranked = select(today)
  keep = [h for h in holdings if rank(h) <= HOLD_BUFFER and h passes filters]
  adds = top of ranked, skipping held names and names whose ICB industry
         already has SECTOR_CAP picks, until len(keep)+len(adds) == N
  port = keep + adds
  w = inverse_vol_weights(port, cap=W_CAP, renormalize)
  w *= (1 - CASH_BUFFER)
  orders = sells(holdings - port)
  for s in port:
      if s new: orders += buy(s, w[s]*equity)
      elif |actual_w(s)-w[s]| > TRADE_BAND*w[s]: orders += adjust(s, w[s]*equity)
  drop orders with |value| < MIN_ORDER
  return orders
```

All orders execute at the **next day's opening auction** (limit at previous close ±0.5%
band; unfilled remainder re-sent as limit at mid after open).

## 13. Backtesting methodology

The point of the backtest is **falsification, not decoration** — parameters are already
fixed; the backtest answers "does this survive realistic frictions and sub-periods?".

1. **Data:** 2005 → present, adjusted daily closes; OMXSGI as benchmark (total return —
   comparing against a price index is the classic self-flattering error).
2. **Survivorship bias — the #1 threat:** today's Large/Mid Cap list excludes delistings
   (Fingerprint-style collapses, buyouts). Mitigations, in order of preference:
   (a) reconstruct point-in-time membership from archived Nasdaq segment lists (Wayback
   Machine has the listed-companies pages), (b) include all currently delisted `.ST`
   tickers retrievable from Yahoo, (c) if neither is complete, **discount reported alpha
   by ~1.5–2%/yr** and say so in the report. Momentum backtests on survivor universes
   overstate returns.
3. **Execution realism:** signals from close of day *t*, fills at **open of day t+1**;
   costs = 0.15% commission + 0.10% half-spread = **0.25% per side** (Avanza Small
   courtage class + observed large-cap spreads); run a 2× cost stress (0.50%/side).
4. **No optimization loop.** One run with the ex-ante parameters is the headline result.
   Then a **sensitivity grid** (N ∈ {8,10,15}; lookbacks 6m-only/12m-only/both; buffer
   {15,20,25}; SMA {150,200,250}; rebalance on 1st/8th/15th of month): the strategy passes
   only if the headline is *not* an outlier in its neighborhood — the grid is a robustness
   check, and it is forbidden to pick the best cell.
5. **Sub-period analysis:** 2005–09 (crash), 2010–15, 2016–19, 2020–21 (COVID whip),
   2022 (bear), 2023–26. Must beat or roughly match benchmark risk-adjusted in most, and
   must show the drawdown cut in 2008 and 2022 (that's the regime filter's whole job).
6. **Metrics:** CAGR, vol, Sharpe, max drawdown, longest underwater period, one-way
   turnover, total cost drag, active return vs OMXSGI, hit rate of regime switches,
   number of whipsaws. Report **net of costs** only.
7. **Statistical honesty:** ~250 monthly observations → a 2% annual alpha will *not* be
   statistically significant at 95%. Report the t-stat anyway; the confidence comes from
   the prior literature + cost realism + sub-period consistency, not from p-values.
8. **Paper-trade** ≥3 months live (daily job running, orders logged not sent) before real
   money; compare paper fills vs backtest assumptions.

## 14. Expected weaknesses (known and accepted)

1. **Momentum crashes:** sharp bear-market rebounds (2009-Q2 style) hurt momentum right
   when the regime filter is also late to re-enter. Vol screen and hysteresis mitigate,
   don't eliminate.
2. **Whipsaw:** sideways choppy markets (e.g. 2011, 2015-16) make the regime filter pay
   ~1–2% per false round trip.
3. **Tracking-error pain:** 10 stocks vs an index — expect years like "index +18%, strategy
   +9%". Behavioral risk of abandoning the system is the biggest real-world failure mode.
4. **Data fragility:** both Yahoo and Nasdaq Nordic endpoints are unofficial. The
   kill-switch (never trade on unvalidated data) turns outages into missed days, not losses.
5. **Small-sample concentration:** one fraud/blowup ≈ −2.5% portfolio hit even with stops.
6. **Crowding/decay:** momentum is public since 1993; the premium is likely smaller than
   the historical print. Costs of the strategy are real; the premium is an estimate.
7. **Survivorship residue** in the backtest if point-in-time membership can't be fully
   reconstructed (§13.2).

## 15. Possible improvements (deferred, in priority order)

1. **Fundamentals overlay** (needs Börsdata paid API): screen out momentum names with
   collapsing earnings / extreme leverage — "quality momentum" typically improves crash
   behavior.
2. **Nordic expansion** (Helsinki/Copenhagen/Oslo): +~300 investable names improves
   diversification; costs +0.25% FX fee per side at Avanza — only worth it with evidence.
3. **Graded regime exposure** (e.g. 50% when between hysteresis bands) instead of binary.
4. **Breadth confirmation** (% of universe above own 200d SMA) as a second regime input.
5. **Volatility-targeted portfolio scaling** (target 15% annualized, scale cash) — smooths
   returns; adds complexity and a bond/cash leg decision.
6. **Execution upgrade:** VWAP-slicing instead of opening auction if account grows past
   ~SEK 2M (irrelevant below that).

---

## Implementation notes for the coding phase

- Python; local data store (SQLite or Parquet); `yfinance` behind a caching layer with
  throttle+backoff; Nasdaq Nordic via simple HTTP JSON/XML client (see
  `samlinz/nasdaqnordic_query` for endpoint reverse-engineering); `avanza-api` (Qluxzz)
  for execution, behind an interface so manual execution stays a first-class fallback.
- The backtest and the live engine **must share the exact same signal/selection code path**
  (one `select()` function, two drivers). This is the single most important architectural
  decision — separate implementations always diverge.
- Every constant in §12 lives in one config file and is treated as frozen; changing any of
  them requires re-running the full §13 methodology and writing down why.
