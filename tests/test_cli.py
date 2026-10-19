"""CLI tests for gaswatch against a local fake JSON-RPC server (no internet).

The fake server serves the captured fixture for eth_feeHistory, plus
scripted error modes, so every error path is exercised deterministically.
"""

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_PATH = os.path.join(ROOT, "tests", "fixtures", "fee_history_20blocks.json")

with open(FIXTURE_PATH) as f:
    FIXTURE = json.load(f)


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, *args):  # silence noisy server logs
        pass

    def do_POST(self):
        mode = _Handler.mode
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            payload = {}

        def reply(obj, status=200, raw=False):
            data = obj if raw else json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # client (timeout test) went away before we answered

        if mode == "garbage":
            reply(b"this is not json{{{", raw=True)
        elif mode == "http500":
            reply({"error": "boom"}, status=500)
        elif mode == "rpc_error":
            reply({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found"}})
        elif mode == "no_result":
            reply({"jsonrpc": "2.0", "id": 1})
        elif mode == "empty_history":
            reply({"jsonrpc": "2.0", "id": 1, "result": dict(FIXTURE, baseFeePerGas=["0x1"])})
        elif mode == "slow":
            import time

            time.sleep(3)  # longer than the tiny test timeout
            reply({"jsonrpc": "2.0", "id": 1, "result": FIXTURE})
        else:  # ok
            assert payload.get("method") == "eth_feeHistory"
            reply({"jsonrpc": "2.0", "id": 1, "result": FIXTURE})


def start_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run_cli(rpc_url, *extra_args):
    env = dict(os.environ)
    cmd = [sys.executable, "-m", "gaswatch.cli", "--rpc", rpc_url, *extra_args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=env, timeout=60)


class TestCliAgainstFakeRpc(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = start_server()
        port = cls.server.server_address[1]
        cls.rpc = f"http://127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _Handler.mode = "ok"

    def test_table_output(self):
        proc = run_cli(self.rpc, "--blocks", "20")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = proc.stdout
        for word in ("gaswatch", "base fee", "slow", "standard", "fast", "gwei"):
            self.assertIn(word, out)
        # spike warning expected from the fixture's final 2.4x block
        self.assertIn("SPIKE", out)

    def test_json_output(self):
        proc = run_cli(self.rpc, "--json", "--blocks", "20")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        snap = json.loads(proc.stdout)
        self.assertEqual(snap["block_range"][0], 23950001)
        self.assertEqual(snap["blocks_analyzed"], 21)
        self.assertTrue(snap["spike"])
        recs = snap["recommendations"]
        self.assertEqual(set(recs), {"slow", "standard", "fast"})
        for tier in recs.values():
            self.assertIn("max_priority_fee_gwei", tier)
            self.assertIn("max_fee_per_gas_gwei", tier)
        self.assertLess(
            recs["slow"]["max_fee_per_gas_gwei"], recs["standard"]["max_fee_per_gas_gwei"]
        )
        self.assertLess(
            recs["standard"]["max_fee_per_gas_gwei"], recs["fast"]["max_fee_per_gas_gwei"]
        )

    def test_json_values_match_fixture_math(self):
        proc = run_cli(self.rpc, "--json", "--blocks", "20")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        snap = json.loads(proc.stdout)
        base_gwei = int(FIXTURE["baseFeePerGas"][-1], 16) / 1e9
        self.assertAlmostEqual(snap["base_fee_gwei"], round(base_gwei, 2), places=1)

    def test_watch_count_two(self):
        proc = run_cli(self.rpc, "--watch", "0.1", "--count", "2", "--json")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # two pretty-printed JSON documents concatenated; stream-decode them
        decoder = json.JSONDecoder()
        text = proc.stdout.strip()
        docs, idx = [], 0
        while idx < len(text):
            obj, end = decoder.raw_decode(text, idx)
            docs.append(obj)
            idx = end
            while idx < len(text) and text[idx] in " \t\r\n":
                idx += 1
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0]["blocks_analyzed"], 21)
        self.assertEqual(docs[1]["blocks_analyzed"], 21)

    def test_bad_rpc_url_unreachable(self):
        proc = run_cli("http://127.0.0.1:1/", "--timeout", "2")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("gaswatch: error:", proc.stderr)

    def test_non_http_url_rejected(self):
        proc = run_cli("ftp://example.com/x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must start with http", proc.stderr)

    def test_garbage_response(self):
        _Handler.mode = "garbage"
        proc = run_cli(self.rpc)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("malformed JSON", proc.stderr)

    def test_http500(self):
        _Handler.mode = "http500"
        proc = run_cli(self.rpc)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("HTTP 500", proc.stderr)

    def test_jsonrpc_error(self):
        _Handler.mode = "rpc_error"
        proc = run_cli(self.rpc)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Method not found", proc.stderr)

    def test_no_result(self):
        _Handler.mode = "no_result"
        proc = run_cli(self.rpc)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no result", proc.stderr)

    def test_empty_history(self):
        _Handler.mode = "empty_history"
        proc = run_cli(self.rpc)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("empty fee history", proc.stderr)

    def test_timeout(self):
        _Handler.mode = "slow"
        proc = run_cli(self.rpc, "--timeout", "1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("timed out", proc.stderr)

    def test_bad_blocks_arg(self):
        proc = run_cli(self.rpc, "--blocks", "500")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--blocks", proc.stderr)

    def test_bad_percentiles_arg(self):
        proc = run_cli(self.rpc, "--percentiles", "abc")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("percentiles", proc.stderr)

    def test_duplicate_percentiles_rejected(self):
        proc = run_cli(self.rpc, "--percentiles", "10,50,10")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("duplicate", proc.stderr)


if __name__ == "__main__":
    unittest.main()
