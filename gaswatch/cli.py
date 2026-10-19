"""Command-line interface for gaswatch."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from .fee_math import (
    FeeHistory,
    FeeHistoryError,
    base_fee_spike,
    base_fee_trend,
    parse_fee_history,
    recommend,
    wei_to_gwei,
)
from .rpc import RpcError, fetch_fee_history

DEFAULT_RPC = "https://ethereum.publicnode.com"
DEFAULT_BLOCKS = 20
DEFAULT_PERCENTILES = "10,50,90"
DEFAULT_TIMEOUT = 15.0
MAX_BLOCKS = 100


class GaswatchError(Exception):
    """User-visible failure (message is printed, no traceback)."""


def parse_percentiles(text: str) -> list:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 2:
        raise GaswatchError("need at least 2 reward percentiles, e.g. --percentiles 10,50,90")
    if len(parts) > 10:
        raise GaswatchError("too many percentiles (max 10)")
    try:
        values = [int(p) for p in parts]
    except ValueError:
        raise GaswatchError(f"percentiles must be integers 0-100: {text!r}")
    for v in values:
        if not 0 <= v <= 100:
            raise GaswatchError(f"percentile out of range 0-100: {v}")
    if len(set(values)) != len(values):
        raise GaswatchError(f"duplicate percentiles: {text!r}")
    return sorted(values)


def fetch_and_compute(rpc: str, blocks: int, percentiles: list, timeout: float) -> dict:
    """One estimation pass; returns a plain-dict snapshot."""
    try:
        raw = fetch_fee_history(rpc, blocks, percentiles, timeout)
    except RpcError as exc:
        raise GaswatchError(str(exc))
    try:
        fh = parse_fee_history(raw, percentiles)
    except FeeHistoryError as exc:
        raise GaswatchError(f"invalid fee history from endpoint: {exc}")

    trend, change_pct = base_fee_trend(fh)
    recs = recommend(fh)
    spiked, ratio = base_fee_spike(fh)

    newest_block = fh.oldest_block + len(fh.base_fees) - 1
    snapshot = {
        "rpc": rpc,
        "block_range": [fh.oldest_block, newest_block],
        "blocks_analyzed": len(fh.base_fees),
        "base_fee_gwei": round(wei_to_gwei(fh.base_fees[-1]), 2),
        "base_fee_trend": trend,
        "base_fee_change_pct": round(change_pct, 1),
        "spike": spiked,
        "spike_ratio": round(ratio, 2),
        "recommendations": {},
    }
    for tier in ("slow", "standard", "fast"):
        rec = recs[tier]
        snapshot["recommendations"][tier] = {
            "max_priority_fee_gwei": round(wei_to_gwei(rec["max_priority_fee_wei"]), 2),
            "max_fee_per_gas_gwei": round(wei_to_gwei(rec["max_fee_per_gas_wei"]), 2),
        }
    return snapshot


def fmt_gwei(value: float) -> str:
    return f"{value:>10.2f}"


def print_table(snap: dict) -> None:
    start, end = snap["block_range"]
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    trend = snap["base_fee_trend"]
    arrow = {"rising": "\u2191", "falling": "\u2193", "stable": "\u2192"}[trend]
    print(f"gaswatch  {ts}")
    print(f"blocks   #{start} .. #{end}  ({snap['blocks_analyzed']} blocks)")
    print(
        f"base fee {snap['base_fee_gwei']:.2f} gwei  "
        f"trend: {trend} {arrow} {snap['base_fee_change_pct']:+.1f}%"
    )
    if snap["spike"]:
        print(
            f"!! BASE-FEE SPIKE: current base fee is {snap['spike_ratio']:.1f}x "
            "the window median - expect fast inclusion to cost more"
        )
    print()
    print(f"{'tier':<10}{'max priority fee':>17}{'max fee per gas':>17}")
    print("-" * 44)
    for tier in ("slow", "standard", "fast"):
        rec = snap["recommendations"][tier]
        print(
            f"{tier:<10}"
            f"{fmt_gwei(rec['max_priority_fee_gwei'])} gwei"
            f"{fmt_gwei(rec['max_fee_per_gas_gwei'])} gwei"
        )
    print()
    print("Heuristic estimate from public fee history - not a guarantee of inclusion.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gaswatch",
        description=(
            "Read-only EIP-1559 gas estimator. Fetches eth_feeHistory from a "
            "public RPC and recommends slow/standard/fast max fees. Never signs "
            "or broadcasts transactions."
        ),
    )
    p.add_argument("--rpc", default=DEFAULT_RPC, help=f"JSON-RPC endpoint (default: {DEFAULT_RPC})")
    p.add_argument(
        "--blocks",
        type=int,
        default=DEFAULT_BLOCKS,
        help=f"fee-history window in blocks, 1-{MAX_BLOCKS} (default: {DEFAULT_BLOCKS})",
    )
    p.add_argument(
        "--percentiles",
        default=DEFAULT_PERCENTILES,
        help=f"reward percentiles, comma separated (default: {DEFAULT_PERCENTILES})",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"RPC timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
    )
    p.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a table")
    p.add_argument(
        "--watch",
        type=float,
        default=None,
        metavar="N",
        help="refresh every N seconds (Ctrl+C to stop)",
    )
    p.add_argument(
        "--count",
        type=int,
        default=None,
        metavar="K",
        help="with --watch: stop after K snapshots (default: run forever)",
    )
    return p


def run(args) -> int:
    if not 1 <= args.blocks <= MAX_BLOCKS:
        raise GaswatchError(f"--blocks must be between 1 and {MAX_BLOCKS}, got {args.blocks}")
    if args.timeout <= 0:
        raise GaswatchError(f"--timeout must be positive, got {args.timeout}")
    if args.watch is not None and args.watch <= 0:
        raise GaswatchError(f"--watch interval must be positive, got {args.watch}")
    if args.count is not None and args.count < 1:
        raise GaswatchError(f"--count must be >= 1, got {args.count}")

    percentiles = parse_percentiles(args.percentiles)

    def one_pass() -> int:
        try:
            snap = fetch_and_compute(args.rpc, args.blocks, percentiles, args.timeout)
        except GaswatchError as exc:
            print(f"gaswatch: error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(snap, indent=2))
        else:
            print_table(snap)
        return 0

    if args.watch is None:
        return one_pass()

    done = 0
    try:
        while True:
            rc = one_pass()
            done += 1
            if rc != 0:
                return rc
            if args.count is not None and done >= args.count:
                return 0
            if not args.json:
                print(f"--- next refresh in {args.watch:g}s (Ctrl+C to stop) ---")
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\ngaswatch: interrupted, exiting", file=sys.stderr)
        return 130


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except GaswatchError as exc:
        print(f"gaswatch: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
