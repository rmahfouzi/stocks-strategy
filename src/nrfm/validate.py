"""Data validation kill-switch (STRATEGY.md section 11, step 2).

The strategy layer must refuse to generate orders unless the latest
validation passed. Checks:

1. Coverage: >=95% of active instruments have a Yahoo bar for the
   latest index trading day.
2. Cross-source: on a random sample, Yahoo unadjusted close matches
   Nasdaq close within 2%.
3. Glitch detector: no adjusted-close day jump >40% without a matching
   raw-close jump (bad dividend/split adjustments).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from nrfm import config
from nrfm.store import Store


@dataclass
class ValidationResult:
    date: str
    ok: bool = True
    issues: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.issues.append(msg)

    def summary(self) -> str:
        status = "OK" if self.ok else "FAIL"
        lines = [f"validation {status} for {self.date}"]
        lines += [f"  - {i}" for i in self.issues]
        return "\n".join(lines)


def validate(store: Store, sample_seed: int | None = None) -> ValidationResult:
    latest = store.latest_index_date()
    if latest is None:
        result = ValidationResult(date="?", ok=False)
        result.issues.append("no index data in store")
        store.log_validation("?", False, "; ".join(result.issues))
        return result

    result = ValidationResult(date=latest)
    instruments = store.active_instruments()

    _check_coverage(store, instruments, latest, result)
    _check_cross_source(store, instruments, latest, result, sample_seed)
    _check_adjustment_glitches(store, instruments, result)

    store.log_validation(latest, result.ok, "; ".join(result.issues) or "all checks passed")
    return result


def _check_coverage(store, instruments, latest: str, result: ValidationResult) -> None:
    total = len(instruments)
    if total == 0:
        result.fail("no active instruments")
        return
    covered = store.conn.execute(
        """
        SELECT COUNT(*) c FROM instruments i
        JOIN prices_yahoo p ON p.ticker = i.yahoo_ticker AND p.date = ?
        WHERE i.active = 1
        """,
        (latest,),
    ).fetchone()["c"]
    coverage = covered / total
    if coverage < config.VALIDATION_MIN_COVERAGE:
        result.fail(
            f"yahoo coverage {covered}/{total} = {coverage:.1%} "
            f"< {config.VALIDATION_MIN_COVERAGE:.0%} for {latest}"
        )


def _check_cross_source(store, instruments, latest: str,
                        result: ValidationResult, seed: int | None) -> None:
    rng = random.Random(seed)
    sample = rng.sample(list(instruments),
                        min(config.VALIDATION_SAMPLE_SIZE, len(instruments)))
    compared = 0
    for inst in sample:
        row = store.conn.execute(
            """
            SELECT y.close AS yc, n.close AS nc
            FROM prices_yahoo y
            JOIN prices_nasdaq n ON n.date = y.date AND n.orderbook_id = ?
            WHERE y.ticker = ? AND y.date = ?
            """,
            (inst["orderbook_id"], inst["yahoo_ticker"], latest),
        ).fetchone()
        if row is None or not row["nc"]:
            continue
        compared += 1
        diff = abs(row["yc"] - row["nc"]) / row["nc"]
        if diff > config.VALIDATION_MAX_CLOSE_DIFF:
            result.fail(
                f"close mismatch {inst['symbol']}: yahoo {row['yc']:.2f} "
                f"vs nasdaq {row['nc']:.2f} ({diff:.1%})"
            )
    if compared < len(sample) // 2:
        result.fail(
            f"cross-source check compared only {compared}/{len(sample)} "
            f"sampled names (missing overlapping data)"
        )


def _check_adjustment_glitches(store, instruments, result: ValidationResult) -> None:
    """Adjusted close jumping without the raw close jumping means a broken
    adjustment factor; window kept short so old, already-accepted history
    is not re-litigated daily."""
    for inst in instruments:
        rows = store.conn.execute(
            """
            SELECT date, close, adj_close FROM prices_yahoo
            WHERE ticker = ? ORDER BY date DESC LIMIT 10
            """,
            (inst["yahoo_ticker"],),
        ).fetchall()
        rows = list(reversed(rows))
        for prev, cur in zip(rows, rows[1:]):
            if not (prev["adj_close"] and cur["adj_close"]
                    and prev["close"] and cur["close"]):
                continue
            adj_jump = abs(cur["adj_close"] / prev["adj_close"] - 1)
            raw_jump = abs(cur["close"] / prev["close"] - 1)
            if adj_jump > config.VALIDATION_GLITCH_JUMP and raw_jump < config.VALIDATION_GLITCH_JUMP:
                result.fail(
                    f"adjustment glitch {inst['symbol']} on {cur['date']}: "
                    f"adj_close jumped {adj_jump:.0%}, close only {raw_jump:.0%}"
                )
