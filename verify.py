#!/usr/bin/env python3
"""
Verify a btcvm ledger.jsonl against Bitcoin.

For each entry:
  1. Fetch the block hash at the recorded height from blockstream.info
  2. Recompute commitment = SHA256(block_hash:state_hash)
  3. Optionally check that a matching OP_RETURN tx exists in that block

Usage:
    python3 verify.py [ledger.jsonl] [--check-txs] [--testnet]
"""

import argparse
import hashlib
import json
import sys
import urllib.request
import urllib.error

API_MAIN = "https://blockstream.info/api"
API_TEST = "https://blockstream.info/testnet/api"


def fetch(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read().decode().strip()
    except (urllib.error.URLError, OSError):
        return None


def fetch_json(url: str):
    raw = fetch(url)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def recompute_commitment(block_hash: str, state_hash: str) -> str:
    return hashlib.sha256(f"{block_hash}:{state_hash}".encode()).hexdigest()


def check_op_return(block_hash: str, commitment: str, api: str) -> tuple[bool, str]:
    """Check if any tx in the block has an OP_RETURN output containing the commitment."""
    txs = fetch_json(f"{api}/block/{block_hash}/txs")
    if txs is None:
        return False, "could not fetch block txs"
    commitment_bytes = commitment.encode()  # ASCII hex string as it appears in OP_RETURN
    for tx in txs:
        for vout in tx.get('vout', []):
            scriptpubkey = vout.get('scriptpubkey', '')
            # OP_RETURN scripts start with '6a'
            if scriptpubkey.startswith('6a'):
                # The data portion follows the length byte(s)
                if commitment.lower() in scriptpubkey.lower():
                    return True, tx['txid']
    return False, "not found in block"


def verify_ledger(ledger_path: str, check_txs: bool, testnet: bool) -> bool:
    api = API_TEST if testnet else API_MAIN
    net_label = "testnet" if testnet else "mainnet"

    try:
        with open(ledger_path) as f:
            entries = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        print(f"Error: {ledger_path} not found")
        return False

    if not entries:
        print("Ledger is empty.")
        return True

    print(f"Verifying {len(entries)} entries against Bitcoin {net_label} ...\n")
    all_ok = True

    for i, entry in enumerate(entries):
        height = entry['block_height']
        recorded_hash = entry['block_hash']
        state_hash = entry['state_hash']
        recorded_commitment = entry['commitment']
        tx_hash = entry.get('tx_hash')

        prefix = f"[{i+1}/{len(entries)}] block {height}"

        # 1. Fetch canonical block hash
        canon_hash = fetch(f"{api}/block-height/{height}")
        if canon_hash is None:
            print(f"{prefix} SKIP  (could not fetch block hash)")
            continue

        if canon_hash != recorded_hash:
            print(f"{prefix} FAIL  block hash mismatch")
            print(f"         recorded: {recorded_hash}")
            print(f"         canonical: {canon_hash}")
            all_ok = False
            continue

        # 2. Recompute commitment
        expected = recompute_commitment(canon_hash, state_hash)
        if expected != recorded_commitment:
            print(f"{prefix} FAIL  commitment mismatch")
            print(f"         expected:  {expected}")
            print(f"         recorded:  {recorded_commitment}")
            all_ok = False
            continue

        # 3. Optionally verify OP_RETURN on-chain
        tx_status = ""
        if check_txs and tx_hash and not tx_hash.startswith('ERROR'):
            found, detail = check_op_return(canon_hash, recorded_commitment, api)
            tx_status = f" | tx={'OK:'+detail[:12] if found else 'MISSING:'+detail}"
        elif tx_hash and not tx_hash.startswith('ERROR'):
            tx_status = f" | tx=..{tx_hash[-8:]}"

        print(f"{prefix} OK    commit=..{recorded_commitment[-8:]}{tx_status}")

    print(f"\n{'All entries verified.' if all_ok else 'VERIFICATION FAILED.'}")
    return all_ok


def main():
    parser = argparse.ArgumentParser(description='Verify btcvm ledger against Bitcoin')
    parser.add_argument('ledger', nargs='?', default='ledger.jsonl')
    parser.add_argument('--check-txs', action='store_true',
                        help='Verify OP_RETURN presence in block (slower)')
    parser.add_argument('--testnet', action='store_true',
                        help='Use Bitcoin testnet')
    args = parser.parse_args()

    ok = verify_ledger(args.ledger, args.check_txs, args.testnet)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
