"""Minimal JSON-RPC client for gaswatch (stdlib only: urllib + json).

READ-ONLY: this module issues read-only JSON-RPC calls
(``eth_feeHistory``). It has no wallet, no key handling, no signing, and no
broadcasting capability of any kind.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List


class RpcError(Exception):
    """Any failure talking to the RPC endpoint."""


def _post(rpc_url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """POST a JSON-RPC payload and return the decoded response object."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        rpc_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "gaswatch/0.1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raise RpcError(f"RPC endpoint returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        detail = str(reason) if reason is not None else "unknown network error"
        raise RpcError(f"cannot reach RPC endpoint: {detail}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise RpcError(f"RPC request timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise RpcError(f"network error: {exc}") from exc

    if status != 200:
        raise RpcError(f"RPC endpoint returned HTTP {status}")

    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RpcError(f"RPC endpoint returned malformed JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise RpcError("RPC endpoint returned a non-object response")
    return decoded


def fetch_fee_history(
    rpc_url: str,
    block_count: int,
    percentiles: List[int],
    timeout: float,
) -> Dict[str, Any]:
    """Fetch ``eth_feeHistory`` and return the raw ``result`` object.

    Raises :class:`RpcError` on network/HTTP/JSON errors, JSON-RPC error
    responses, missing results, or empty histories.
    """
    if not rpc_url.startswith(("http://", "https://")):
        raise RpcError(f"RPC URL must start with http:// or https://: {rpc_url!r}")
    if timeout <= 0:
        raise RpcError(f"timeout must be positive, got {timeout!r}")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_feeHistory",
        "params": [hex(block_count), "latest", percentiles],
    }
    decoded = _post(rpc_url, payload, timeout)

    if "error" in decoded:
        err = decoded["error"]
        if isinstance(err, dict):
            msg = err.get("message", str(err))
        else:
            msg = str(err)
        raise RpcError(f"RPC error: {msg}")
    result = decoded.get("result")
    if result is None:
        raise RpcError("RPC endpoint returned no result")
    if not isinstance(result, dict):
        raise RpcError("RPC endpoint returned a non-object result")
    return result
