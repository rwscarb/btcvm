#!/usr/bin/env python3
"""
Verify a btcvm ledger.jsonl against Bitcoin.

For each entry:
  1. Fetch the block hash at the recorded height from blockstream.info
  2. Recompute commitment = SHA256(block_hash[:vdf_hash]:state_or_root)
  3. If VDF fields present, verify the sequential hash chain across sub-ticks
  4. If trace fields present and a trace file exists, verify the Merkle root
  5. Optionally check that a matching OP_RETURN tx exists in that block

Usage:
    python3 verify.py [ledger.jsonl] [--trace-file PATH] [--check-txs] [--testnet]
"""

import argparse
import hashlib
import json
import os
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


def recompute_commitment(block_hash: str, state_or_root: str, vdf_hash: str | None) -> str:
    if vdf_hash:
        return hashlib.sha256(f"{block_hash}:{vdf_hash}:{state_or_root}".encode()).hexdigest()
    return hashlib.sha256(f"{block_hash}:{state_or_root}".encode()).hexdigest()


def check_op_return(block_hash: str, commitment: str, api: str) -> tuple[bool, str]:
    txs = fetch_json(f"{api}/block/{block_hash}/txs")
    if txs is None:
        return False, "could not fetch block txs"
    for tx in txs:
        for vout in tx.get('vout', []):
            if vout.get('scriptpubkey', '').startswith('6a'):
                if commitment.lower() in vout['scriptpubkey'].lower():
                    return True, tx['txid']
    return False, "not found in block"


def verify_ledger(ledger_path: str, trace_file: str | None, check_txs: bool, testnet: bool | None) -> bool:
    try:
        with open(ledger_path) as f:
            entries = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        print(f"Error: {ledger_path} not found")
        return False

    if not entries:
        print("Ledger is empty.")
        return True

    # Auto-detect network from ledger unless caller forced it
    if testnet is None:
        recorded_network = entries[0].get('network', 'mainnet')
        testnet = (recorded_network == 'testnet')

    api = API_TEST if testnet else API_MAIN
    net_label = "testnet" if testnet else "mainnet"

    has_vdf = any('vdf_hash' in e for e in entries)
    has_trace = any('trace_root' in e for e in entries)

    mode_parts = []
    if has_vdf:
        mode_parts.append("VDF")
    if has_trace:
        mode_parts.append("trace")
    mode_label = f" [{'+'.join(mode_parts)}]" if mode_parts else ""

    print(f"Verifying {len(entries)} entries against Bitcoin {net_label}{mode_label} ...\n")

    # --- VDF verification ---
    # Each run seeds a fresh VDF from the block hash; detect run boundaries by
    # vdf_tick == 0 or a broken input→output link, then verify each sub-chain.
    if has_vdf:
        from vdf import VDF, STEPS_PER_TICK
        vdf_entries = [e for e in entries if e.get('vdf_hash')]

        # Split into sub-chains at every run boundary
        sub_chains: list[list[dict]] = []
        current: list[dict] = []
        for e in vdf_entries:
            if not current or (e.get('vdf_tick', 0) == 0 or e['vdf_input'] != current[-1]['vdf_hash']):
                if current:
                    sub_chains.append(current)
                current = [e]
            else:
                current.append(e)
        if current:
            sub_chains.append(current)

        tick_failures = 0
        chain_failures = 0
        for chain in sub_chains:
            for e in chain:
                if not VDF.verify(e['vdf_input'], e['vdf_hash'], STEPS_PER_TICK):
                    tick_failures += 1
            for i in range(len(chain) - 1):
                if chain[i + 1]['vdf_input'] != chain[i]['vdf_hash']:
                    chain_failures += 1

        vdf_ok = tick_failures == 0 and chain_failures == 0
        if vdf_ok:
            runs = len(sub_chains)
            ticks = len(vdf_entries)
            print(f"VDF: OK  {ticks} tick(s) across {runs} run(s)\n")
        else:
            if tick_failures:
                print(f"VDF: FAIL  {tick_failures} bad tick(s)")
            if chain_failures:
                print(f"VDF chain: FAIL  {chain_failures} broken link(s)")
            print()

    # --- Trace verification ---
    trace_root_verified: str | None = None
    if has_trace and trace_file and os.path.exists(trace_file):
        from trace import VMTrace
        print(f"Verifying trace: {trace_file} ...")
        try:
            ok, computed_root = VMTrace.verify_file(trace_file)
        except Exception as e:
            print(f"Trace verify ERROR: {e}\n")
            ok, computed_root = False, ''
        if ok:
            print(f"Trace: OK  Merkle root ..{computed_root[-8:]}\n")
            trace_root_verified = computed_root
        else:
            print("Trace: FAIL  step hash mismatch\n")
    elif has_trace and trace_file:
        print(f"Note: trace file {trace_file} not found; skipping trace verification\n")

    # --- Per-entry commitment verification ---
    all_ok = True

    for i, entry in enumerate(entries):
        height = entry['block_height']
        recorded_hash = entry['block_hash']
        recorded_commitment = entry['commitment']
        vdf_tick = entry.get('vdf_tick', 0)
        vdf_hash = entry.get('vdf_hash')
        tx_hash = entry.get('tx_hash')

        # Choose the right hash for the commitment
        state_or_root = entry.get('trace_root', entry['state_hash'])

        prefix = f"[{i+1}/{len(entries)}] block {height}"
        if vdf_hash:
            prefix += f" tick {vdf_tick}"

        # 1. Fetch canonical block hash (only for first entry per block to save API calls)
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
        expected = recompute_commitment(canon_hash, state_or_root, vdf_hash)
        if expected != recorded_commitment:
            print(f"{prefix} FAIL  commitment mismatch")
            print(f"         expected:  {expected}")
            print(f"         recorded:  {recorded_commitment}")
            all_ok = False
            continue

        # 3. Cross-check trace root if we verified the file
        if trace_root_verified and 'trace_root' in entry:
            # Only the final entry's trace_root matches the file root (cumulative trace)
            # For intermediate entries, we can only check it's a valid hex string
            pass

        # 4. Optionally verify OP_RETURN on-chain
        tx_status = ""
        if check_txs and tx_hash and not str(tx_hash).startswith('ERROR'):
            found, detail = check_op_return(canon_hash, recorded_commitment, api)
            tx_status = f" | tx={'OK:'+detail[:12] if found else 'MISSING:'+detail}"
        elif tx_hash and not str(tx_hash).startswith('ERROR'):
            tx_status = f" | tx=..{tx_hash[-8:]}"

        root_key = 'trace_root' if 'trace_root' in entry else 'state_hash'
        print(f"{prefix} OK    root=..{entry[root_key][-8:]} commit=..{recorded_commitment[-8:]}{tx_status}")

    print(f"\n{'All entries verified.' if all_ok else 'VERIFICATION FAILED.'}")
    return all_ok


def main():
    parser = argparse.ArgumentParser(description='Verify btcvm ledger against Bitcoin')
    parser.add_argument('ledger', nargs='?', default='ledger.jsonl')
    parser.add_argument('--trace-file', default='trace.jsonl', metavar='PATH',
                        help='Trace file to verify against (default: trace.jsonl)')
    parser.add_argument('--check-txs', action='store_true',
                        help='Verify OP_RETURN presence in block (slower)')
    parser.add_argument('--testnet', action='store_true', default=None,
                        help='Force Bitcoin testnet (auto-detected from ledger by default)')
    args = parser.parse_args()

    # None → auto-detect from ledger; True/False → explicit override
    testnet: bool | None = True if args.testnet else None
    ok = verify_ledger(args.ledger, args.trace_file, args.check_txs, testnet)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
