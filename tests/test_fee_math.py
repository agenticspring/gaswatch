"""Offline unit tests for gaswatch.fee_math (no network)."""

import json
import os
import unittest

from gaswatch.fee_math import (
    FeeHistory,
    FeeHistoryError,
    base_fee_spike,
    base_fee_trend,
    hex_to_int,
    parse_fee_history,
    priority_fee_stats,
    recommend,
    wei_to_gwei,
)

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "fee_history_20blocks.json")

GWEI = 10 ** 9


def load_fixture():
    with open(FIXTURE_PATH) as f:
        return json.load(f)


class TestHexToInt(unittest.TestCase):
    def test_hex_string(self):
        self.assertEqual(hex_to_int("0x10"), 16)
        self.assertEqual(hex_to_int("0x0"), 0)

    def test_uppercase_hex(self):
        self.assertEqual(hex_to_int("0xFF"), 255)

    def test_int_passthrough(self):
        self.assertEqual(hex_to_int(42), 42)

    def test_decimal_string(self):
        self.assertEqual(hex_to_int("100"), 100)

    def test_invalid_string(self):
        with self.assertRaises(FeeHistoryError):
            hex_to_int("not-a-number")

    def test_bool_rejected(self):
        with self.assertRaises(FeeHistoryError):
            hex_to_int(True)

    def test_none_rejected(self):
        with self.assertRaises(FeeHistoryError):
            hex_to_int(None)


class TestParseFeeHistory(unittest.TestCase):
    def test_parse_fixture(self):
        fh = parse_fee_history(load_fixture(), [10, 50, 90])
        self.assertIsInstance(fh, FeeHistory)
        self.assertEqual(fh.oldest_block, 23950001)
        self.assertEqual(len(fh.base_fees), 21)  # 20 blocks + 1
        self.assertEqual(len(fh.reward), 20)
        self.assertEqual(fh.percentiles, [10, 50, 90])

    def test_missing_field(self):
        with self.assertRaises(FeeHistoryError):
            parse_fee_history({"oldestBlock": "0x1"}, [10, 50, 90])

    def test_empty_base_fees(self):
        raw = dict(load_fixture())
        raw["baseFeePerGas"] = ["0x1"]
        with self.assertRaises(FeeHistoryError):
            parse_fee_history(raw, [10, 50, 90])

    def test_empty_reward(self):
        raw = dict(load_fixture())
        raw["reward"] = []
        with self.assertRaises(FeeHistoryError):
            parse_fee_history(raw, [10, 50, 90])

    def test_reward_row_length_mismatch(self):
        raw = dict(load_fixture())
        raw["reward"] = [["0x1", "0x2"]]  # 2 cols but 3 percentiles requested
        with self.assertRaises(FeeHistoryError):
            parse_fee_history(raw, [10, 50, 90])

    def test_non_dict_result(self):
        with self.assertRaises(FeeHistoryError):
            parse_fee_history([1, 2, 3], [10, 50, 90])


class TestBaseFeeTrend(unittest.TestCase):
    def _fh(self, fees_gwei):
        base = [int(g * GWEI) for g in fees_gwei]
        return FeeHistory(
            oldest_block=1,
            base_fees=base,
            gas_used_ratios=[0.5] * (len(base) - 1),
            reward=[[1, 2, 3]] * (len(base) - 1),
            percentiles=[10, 50, 90],
        )

    def test_rising(self):
        fh = self._fh([10, 10, 10, 10, 12, 13, 14, 15])
        label, pct = base_fee_trend(fh)
        self.assertEqual(label, "rising")
        self.assertGreater(pct, 5.0)

    def test_falling(self):
        fh = self._fh([15, 14, 13, 12, 10, 10, 10, 10])
        label, pct = base_fee_trend(fh)
        self.assertEqual(label, "falling")
        self.assertLess(pct, -5.0)

    def test_stable(self):
        fh = self._fh([10, 10.2, 9.9, 10.1, 10, 10.1, 9.9, 10])
        label, pct = base_fee_trend(fh)
        self.assertEqual(label, "stable")
        self.assertTrue(-5.0 <= pct <= 5.0)


class TestPriorityFeeStats(unittest.TestCase):
    def test_fixture_median(self):
        fh = parse_fee_history(load_fixture(), [10, 50, 90])
        stats = priority_fee_stats(fh, 50.0)
        # generator used: p10 in 0.2-0.6, p50 in 0.8-1.8, p90 in 2.5-6.0 gwei
        self.assertTrue(0.2 * GWEI <= stats[10] <= 0.6 * GWEI)
        self.assertTrue(0.8 * GWEI <= stats[50] <= 1.8 * GWEI)
        self.assertTrue(2.5 * GWEI <= stats[90] <= 6.0 * GWEI)
        self.assertLess(stats[10], stats[50])
        self.assertLess(stats[50], stats[90])


class TestRecommend(unittest.TestCase):
    def test_fixture_recommendations(self):
        fh = parse_fee_history(load_fixture(), [10, 50, 90])
        recs = recommend(fh)
        self.assertEqual(set(recs), {"slow", "standard", "fast"})
        base = fh.base_fees[-1]
        # tier math: slow = base*1.0 + p10, standard = base*1.125 + p50, fast = base*1.25 + p90
        self.assertEqual(recs["slow"]["max_fee_per_gas_wei"], base + recs["slow"]["max_priority_fee_wei"])
        self.assertEqual(
            recs["standard"]["max_fee_per_gas_wei"],
            int(round(base * 1.125)) + recs["standard"]["max_priority_fee_wei"],
        )
        self.assertEqual(
            recs["fast"]["max_fee_per_gas_wei"],
            int(round(base * 1.25)) + recs["fast"]["max_priority_fee_wei"],
        )
        # ordering sanity: slow < standard < fast max fee
        self.assertLess(recs["slow"]["max_fee_per_gas_wei"], recs["standard"]["max_fee_per_gas_wei"])
        self.assertLess(recs["standard"]["max_fee_per_gas_wei"], recs["fast"]["max_fee_per_gas_wei"])

    def test_synthetic_exact(self):
        # 2 blocks, constant base 10 gwei, known rewards -> hand-verified math
        fh = FeeHistory(
            oldest_block=1,
            base_fees=[10 * GWEI, 10 * GWEI, 10 * GWEI],
            gas_used_ratios=[0.5, 0.5],
            reward=[[int(0.5 * GWEI), int(1.0 * GWEI), int(3.0 * GWEI)]] * 2,
            percentiles=[10, 50, 90],
        )
        recs = recommend(fh)
        self.assertEqual(recs["slow"]["max_priority_fee_wei"], int(0.5 * GWEI))
        self.assertEqual(recs["standard"]["max_priority_fee_wei"], int(1.0 * GWEI))
        self.assertEqual(recs["fast"]["max_priority_fee_wei"], int(3.0 * GWEI))
        self.assertEqual(recs["slow"]["max_fee_per_gas_wei"], int(10.5 * GWEI))
        self.assertEqual(recs["standard"]["max_fee_per_gas_wei"], int(11.25 * GWEI) + int(1.0 * GWEI))
        self.assertEqual(recs["fast"]["max_fee_per_gas_wei"], int(12.5 * GWEI) + int(3.0 * GWEI))


class TestSpike(unittest.TestCase):
    def test_spike_detected(self):
        fh = parse_fee_history(load_fixture(), [10, 50, 90])
        spiked, ratio = base_fee_spike(fh, factor=2.0)
        self.assertTrue(spiked)  # fixture ends with a 2.4x spike block
        self.assertGreater(ratio, 2.0)

    def test_no_spike(self):
        fh = FeeHistory(
            oldest_block=1,
            base_fees=[10 * GWEI] * 21,
            gas_used_ratios=[0.5] * 20,
            reward=[[1, 2, 3]] * 20,
            percentiles=[10, 50, 90],
        )
        spiked, _ = base_fee_spike(fh)
        self.assertFalse(spiked)


class TestUnits(unittest.TestCase):
    def test_wei_to_gwei(self):
        self.assertAlmostEqual(wei_to_gwei(GWEI), 1.0)
        self.assertAlmostEqual(wei_to_gwei(int(1.5 * GWEI)), 1.5)


if __name__ == "__main__":
    unittest.main()
