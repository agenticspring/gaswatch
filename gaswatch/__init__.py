"""gaswatch — read-only EIP-1559 gas estimator from fee history.

This package never signs or broadcasts transactions and never handles
private keys. It only reads ``eth_feeHistory`` via JSON-RPC.
"""

__version__ = "0.1.0"
