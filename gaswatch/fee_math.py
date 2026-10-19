"""Pure EIP-1559 fee math for gaswatch.

Everything in this module is a pure function over already-fetched data, so it
is fully testable offline with captured ``eth_feeHistory`` fixtures.

READ-ONLY SAFETY: this module never touches a private key, never signs
anything, and never broadcasts transactions. It only does arithmetic on
public fee-history numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

GWEI = 10 ** 9

#: EIP-1559 per-block base-fee adjustment limit (12.5%).
MAX_BASE_FEE_JUMP = 0.125

#: Headroom multipliers applied to the current base fee per tier, covering
#: one (standard) or two (fast) full-block base-fee increases while a tx is
#: pending. These are heuristics, not guarantees.
TIER_HEADROOM = {"slow": 1.0, "standard": 1.125, "fast": 1.25}


class FeeHistoryError(ValueError):
    """Raised when an ``eth_feeHistory`` result is malformed or empty."""


def hex_to_int(value) -> int:
    """Convert a JSON-RPC hex string (or plain int/float) to int."""
    if isinstance(value, bool):
        raise FeeHistoryError(f"boolean is not a valid quantity: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise FeeHistoryError(f"non-integer quantity: {value!r}")
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 16) if text.lower().startswith("0x") else int(text)
        except ValueError:
            raise FeeHistoryError(f"invalid quantity string: {value!r}")
    raise FeeHistoryError(f"unsupported quantity type {type(value).__name__}: {value!r}")


@dataclass
class FeeHistory:
    """Parsed ``eth_feeHistory`` result."""

    oldest_block: int
    #: base fee per gas for each block, newest LAST (len = block_count + 1).
    base_fees: List[int]
    gas_used_ratios: List[float]
    #: reward[block][i] = priority fee at percentiles[i], in wei.
    reward: List[List[int]]
    percentiles: List[int]


def parse_fee_history(result: dict, percentiles: Sequence[int]) -> FeeHistory:
    """Parse a raw ``eth_feeHistory`` result dict into :class:`FeeHistory`.

    Raises :class:`FeeHistoryError` on anything malformed or empty.
    """
    if not isinstance(result, dict):
        raise FeeHistoryError(f"result must be an object, got {type(result).__name__}")
    try:
        oldest = result["oldestBlock"]
        raw_base = result["baseFeePerGas"]
        raw_ratio = result["gasUsedRatio"]
        raw_reward = result["reward"]
    except KeyError as exc:
        raise FeeHistoryError(f"missing required field: {exc.args[0]}")

    base_fees = [hex_to_int(v) for v in raw_base]
    if len(base_fees) < 2:
        raise FeeHistoryError("empty fee history: baseFeePerGas has fewer than 2 entries")

    gas_used_ratios = []
    for v in raw_ratio:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise FeeHistoryError(f"invalid gasUsedRatio entry: {v!r}")
        gas_used_ratios.append(float(v))

    if len(raw_reward) == 0:
        raise FeeHistoryError("empty fee history: reward list is empty")
    pct = list(percentiles)
    reward = []
    for row in raw_reward:
        if not isinstance(row, list) or len(row) != len(pct):
            raise FeeHistoryError(
                f"reward row length {len(row) if isinstance(row, list) else 'n/a'} "
                f"does not match requested percentiles {pct}"
            )
        reward.append([hex_to_int(v) for v in row])

    return FeeHistory(
        oldest_block=hex_to_int(oldest),
        base_fees=base_fees,
        gas_used_ratios=gas_used_ratios,
        reward=reward,
        percentiles=pct,
    )


def _percentile(sorted_vals: List[float], pct: float) -> float:
    """Percentile with linear interpolation (pct in 0..100)."""
    if not sorted_vals:
        raise FeeHistoryError("cannot compute percentile of empty data")
    if not 0 <= pct <= 100:
        raise ValueError(f"percentile out of range: {pct}")
    vals = sorted(sorted_vals)
    if len(vals) == 1:
        return vals[0]
    rank = pct / 100 * (len(vals) - 1)
    low = int(rank)
    frac = rank - low
    return vals[low] + frac * (vals[min(low + 1, len(vals) - 1)] - vals[low])


def _median(vals: Sequence[float]) -> float:
    return _percentile(list(vals), 50.0)


def base_fee_trend(fh: FeeHistory) -> Tuple[str, float]:
    """Classify the base-fee trend as rising / falling / stable.

    Compares the median of the most recent quarter of base fees against the
    median of the oldest quarter. Threshold: +/-5%.
    Returns (label, change_percent).
    """
    n = len(fh.base_fees)
    quarter = max(1, n // 4)
    old = _median(fh.base_fees[:quarter])
    new = _median(fh.base_fees[-quarter:])
    if old == 0:
        return ("stable", 0.0)
    change_pct = (new - old) / old * 100.0
    if change_pct > 5.0:
        return ("rising", change_pct)
    if change_pct < -5.0:
        return ("falling", change_pct)
    return ("stable", change_pct)


def priority_fee_stats(fh: FeeHistory, pct: float = 50.0) -> Dict[int, int]:
    """Per-requested-percentile priority-fee summary, in wei.

    For each requested reward percentile (e.g. 10 / 50 / 90), collect the
    values across all blocks and take the ``pct``-th percentile of them.
    Default ``pct`` = 50 -> the cross-block median.
    """
    stats: Dict[int, int] = {}
    for i, level in enumerate(fh.percentiles):
        col = [float(row[i]) for row in fh.reward]
        stats[level] = int(round(_percentile(col, pct)))
    return stats


def recommend(
    fh: FeeHistory,
    slow_pct: float = 10.0,
    standard_pct: float = 50.0,
    fast_pct: float = 90.0,
) -> Dict[str, Dict[str, int]]:
    """Recommend maxFeePerGas (and maxPriorityFeePerGas) per tier, in wei.

    - slow: current base fee + 10th percentile of the reward p10 values
    - standard: current base fee x 1.125 + median of the reward p50 values
    - fast: current base fee x 1.25 + 90th percentile of the reward p90 values

    The 1.125 / 1.25 headroom covers full-block EIP-1559 base-fee jumps
    while the transaction waits in the mempool. Heuristic, not a guarantee.
    """
    base = fh.base_fees[-1]
    stats_slow = priority_fee_stats(fh, slow_pct)
    stats_std = priority_fee_stats(fh, standard_pct)
    stats_fast = priority_fee_stats(fh, fast_pct)

    low_level = min(fh.percentiles)
    mid_level = fh.percentiles[len(fh.percentiles) // 2]
    high_level = max(fh.percentiles)

    out: Dict[str, Dict[str, int]] = {}
    tiers = (
        ("slow", stats_slow[low_level], TIER_HEADROOM["slow"]),
        ("standard", stats_std[mid_level], TIER_HEADROOM["standard"]),
        ("fast", stats_fast[high_level], TIER_HEADROOM["fast"]),
    )
    for tier, priority, headroom in tiers:
        max_fee = int(round(base * headroom)) + priority
        out[tier] = {"max_priority_fee_wei": priority, "max_fee_per_gas_wei": max_fee}
    return out


def base_fee_spike(fh: FeeHistory, factor: float = 2.0) -> Tuple[bool, float]:
    """Detect a base-fee spike: newest base fee vs median of the window.

    Returns (is_spike, ratio). A spike is newest > factor * window_median.
    """
    median = _median(fh.base_fees[:-1])
    if median == 0:
        return (False, 0.0)
    ratio = fh.base_fees[-1] / median
    return (ratio > factor, ratio)


def wei_to_gwei(wei: int) -> float:
    return wei / GWEI
