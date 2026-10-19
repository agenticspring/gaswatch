# gaswatch

Read-only EIP-1559 gas estimator. Fetches `eth_feeHistory` from a public Ethereum
JSON-RPC endpoint (stdlib `urllib` only — no `web3.py`, no dependencies) and
recommends **slow / standard / fast** `maxFeePerGas` values in gwei, with a human
table and a `--json` mode for scripts.

**Safety first:** gaswatch is strictly read-only. It never signs, never
broadcasts, and never asks for private keys, seed phrases, or wallet addresses.
It has no wallet code at all — only arithmetic over public fee-history numbers.

## Features

- `eth_feeHistory` fetch via public RPC (`--rpc` override, `--blocks` window, `--timeout`)
- Base-fee trend classification (rising / falling / stable)
- Priority-fee percentile stats + slow/standard/fast recommendations with documented
  headroom buffers (base × 1.0 / 1.125 / 1.25 covering full-block EIP-1559 jumps)
- `--watch N` live mode with base-fee **spike warnings** (>2x window median)
- Human table and `--json` output
- Zero dependencies, Python 3.9+ stdlib only

## Installation

Requires Python 3.9+.

```bash
git clone https://github.com/agenticspring/gaswatch.git
cd gaswatch
pip install -e .
# or run without installing:
python3 -m gaswatch.cli --help
```

## Usage

```bash
# One-shot estimate (default: https://ethereum.publicnode.com, 20 blocks)
gaswatch

# JSON output for scripts
gaswatch --json

# Custom RPC + wider window
gaswatch --rpc https://ethereum.publicnode.com --blocks 50

# Watch mode: refresh every 30s, with spike warnings
gaswatch --watch 30

# Watch mode, stop after 5 snapshots
gaswatch --watch 10 --count 5 --json
```

Example output:

```
gaswatch  2026-10-19 07:12:00 UTC
blocks   #23950001 .. #23950021  (21 blocks)
base fee 30.87 gwei  trend: rising ↑ +7.4%
!! BASE-FEE SPIKE: current base fee is 2.4x the window median - expect fast inclusion to cost more

tier        max priority fee   max fee per gas
--------------------------------------------
slow                  0.42 gwei         31.29 gwei
standard              1.25 gwei         35.98 gwei
fast                  4.10 gwei         42.69 gwei

Heuristic estimate from public fee history - not a guarantee of inclusion.
```

### Options

```
--rpc URL            JSON-RPC endpoint (default: https://ethereum.publicnode.com)
--blocks N           fee-history window, 1-100 (default: 20)
--percentiles A,B,C  reward percentiles, comma separated (default: 10,50,90)
--timeout S          RPC timeout in seconds (default: 15)
--json               machine-readable JSON output
--watch N            refresh every N seconds (Ctrl+C to stop)
--count K            with --watch: stop after K snapshots
```

### Method

1. Request `eth_feeHistory` for the last N blocks at percentiles [10, 50, 90].
2. Trend = median of the newest quarter of base fees vs the oldest quarter
   (rising > +5%, falling < -5%, else stable).
3. Recommendations (all in wei, displayed in gwei):
   - **slow** = current base fee + 10th percentile of reward-p10 values across blocks
   - **standard** = current base fee × 1.125 + median of reward-p50 values
   - **fast** = current base fee × 1.25 + 90th percentile of reward-p90 values

The 1.125× / 1.25× headroom covers one/two full-block EIP-1559 base-fee
increases while your transaction waits in the mempool.

## Limitations

- The estimate is a **heuristic, not a guarantee**: sudden mempool surges can
  still push fees above "fast" before your transaction lands.
- Public RPC endpoints may rate-limit; if you get HTTP 429/403, use `--rpc`
  to point at another public endpoint or your own node, and lower `--blocks`.
- A live smoke test from this repository's author environment could not reach
  the public RPC (proxied egress), so live correctness is covered by the
  offline suite; the recommendation math is verified against a realistic
  captured `eth_feeHistory` fixture — see `tests/`.
- Only Ethereum-compatible chains exposing `eth_feeHistory` are supported.
- This tool gives estimates only — it does not submit, sign, or manage
  transactions, and it never needs your wallet or keys.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The suite is fully offline: unit tests run against a captured
`eth_feeHistory` fixture (`tests/fixtures/fee_history_20blocks.json`), and the
CLI tests run against a local fake JSON-RPC server, covering every error path
(unreachable host, malformed JSON, HTTP 500, JSON-RPC error, missing result,
empty history, timeout, bad arguments).

## License

MIT — see [LICENSE](LICENSE).
