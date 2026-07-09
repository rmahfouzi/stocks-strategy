# stocks-strategy

Systematic trading strategy for Nasdaq Stockholm stocks, executed manually
through Avanza. Full strategy design: **[STRATEGY.md](STRATEGY.md)**.

## Data layer

Local SQLite store (`data/nrfm.sqlite`) fed by two sources:

| Source | Used for | Client |
|---|---|---|
| Nasdaq Nordic API (`api.nasdaq.com/api/nordic`) | universe membership (STO Large+Mid Cap), unadjusted OHLCV, OMXSGI index, validation | `src/nrfm/sources/nasdaq.py` |
| Yahoo Finance (`yfinance`) | dividend/split-adjusted daily prices — the price of record for signals | `src/nrfm/sources/yahoo.py` |

## Setup

```bash
uv venv .venv --python 3.14
uv pip install -p .venv/bin/python -e .
```

## Commands

```bash
.venv/bin/nrfm universe    # refresh instrument list (305 names)
.venv/bin/nrfm backfill    # full history; resumable, run once (~20 min)
.venv/bin/nrfm update      # nightly incremental fetch + validation
.venv/bin/nrfm validate    # run data-quality kill-switch checks only
.venv/bin/nrfm status      # store statistics
.venv/bin/nrfm signals     # today's regime + target portfolio (read-only)
.venv/bin/nrfm daily       # nightly decision run; emails trade list or heartbeat
.venv/bin/nrfm equity [SEK]  # show/set portfolio size (order amounts in emails)
.venv/bin/nrfm backtest    # historical simulation [--start] [--end]
.venv/bin/nrfm hold add|rm|list [TICKER]   # record actual Avanza holdings
.venv/bin/nrfm observe [DATE]  # show/set observation-period start date
.venv/bin/nrfm report      # modeled track record since observation start
```

## Daily operation (manual execution loop)

1. Cron runs `update` then `daily` every trading evening (18:30 UTC).
2. An email arrives every trading evening: a trade list if action is
   needed (regime flip, catastrophe stop, monthly rebalance), otherwise
   a heartbeat with regime, holdings, and the paper track record. No
   email on a weekday means the pipeline is broken. Data problems
   always email.
3. Execute the listed orders at next morning's opening auction in
   Avanza, then mirror them: `nrfm hold add/rm TICKER`.
4. Keep `nrfm equity` roughly current so emails show SEK amounts.

### Updating holdings by email

Instead of the CLI, send an email to the strategy's Gmail account
(from the configured alert address) with **NRFM** in the subject and
commands in the body, one per line:

```
add SOBI
rm ERIC-B
equity 120000
list
```

Commands are applied at the start of the nightly run (or immediately
via `nrfm inbox`) and answered with a confirmation email showing the
resulting register. Tickers are validated against the universe; the
channel can only change the local register, never place orders.

During the observation (paper) period, still mirror every emailed order
in `nrfm hold` (or by reply email as above) — the rank buffer and stops
are path-dependent and only produce realistic signals against the
recorded portfolio. `nrfm report`
(also appended to each monthly rebalance email) replays the strategy
from the `observe` date through the shared engine and compares it to
OMXSGI: that replay is the paper track record.

## Engine

`src/nrfm/engine/` implements STRATEGY.md §6–9: `signals.py` (momentum,
volatility, regime state machine), `select.py` (`select_portfolio()` —
the single selection code path shared by live signals and every backtest
rebalance), `backtest.py` (close-to-next-open simulator, 0.25%/side
costs). `nrfm signals` refuses to run if the last data validation failed
or is stale. Backtest artifacts land in `data/backtest/`; the reported
results are survivorship-biased (today's universe members only) — see
STRATEGY.md §13.2.

`nrfm update` is designed to run every trading day after ~19:30 CET.
Exit codes: 0 = OK, 1 = validation failed, 2 = crashed (both failure
modes email an alert). The strategy layer must not generate orders from
the store in a failed state (STRATEGY.md §11).

## Scheduling

`scripts/nightly_update.sh` runs the update, logs to `data/logs/`
(pruned after 90 days), and sends a fallback alert email if the process
died without Python being able to report it. Installed in cron as:

```cron
30 18 * * 1-5 /home/user/repos/stocks-strategy/scripts/nightly_update.sh
```

(18:30 UTC — after the Stockholm close year-round.)

## Email notifications

Signals and validation alerts are emailed via SMTP. Credentials live in
`~/.config/nrfm/email.env` (chmod 600, never in the repo) — see
`src/nrfm/notify.py` for the keys. For Gmail, create an app password at
<https://myaccount.google.com/apppasswords> (requires 2-step verification).
Verify with:

```bash
.venv/bin/nrfm email-test
```
