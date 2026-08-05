#!/usr/bin/env python3
"""
BTC-Clocked VM — MVP
Executes a register machine in lockstep with Bitcoin blocks.
Each block triggers N VM cycles; state is committed to a local ledger.
Optionally broadcasts each commitment as an OP_RETURN to Bitcoin testnet.

Usage:
    python3 main.py [program] [--broadcast] [--wif WIF_KEY] [--network mainnet|testnet]

Examples:
    python3 main.py fibonacci
    python3 main.py countdown --broadcast --wif cNfsPbDJg...
"""

import argparse
import hashlib
import json
import time
import sys
import os

from vm import VM
from clock import BitcoinClock
from programs import PROGRAMS

LEDGER_FILE = "ledger.jsonl"
CYCLES_PER_BLOCK = 10
POLL_INTERVAL = 30  # seconds between block polls


def commitment(block_hash: str, state_hash: str) -> str:
    return hashlib.sha256(f"{block_hash}:{state_hash}".encode()).hexdigest()


def write_entry(entry: dict):
    with open(LEDGER_FILE, 'a') as f:
        f.write(json.dumps(entry) + '\n')


def on_block(block: dict, vm: VM, broadcast: bool, wif: str, network: str) -> dict:
    cycles = vm.run(max_steps=CYCLES_PER_BLOCK)
    state_hash = vm.state_hash()
    commit_hash = commitment(block['hash'], state_hash)

    entry = {
        'block_height': block['height'],
        'block_hash': block['hash'],
        'vm_ticks': vm.ticks,
        'cycles_this_block': cycles,
        'halted': vm.halted,
        'registers': vm.registers[:],
        'state_hash': state_hash,
        'commitment': commit_hash,
        'tx_hash': None,
    }

    if broadcast:
        try:
            from broadcast import broadcast_commitment
            tx_hash = broadcast_commitment(commit_hash, wif, network)
            entry['tx_hash'] = tx_hash
        except Exception as e:
            import traceback
            traceback.print_exc()
            entry['tx_hash'] = f'ERROR: {e}'

    write_entry(entry)
    return entry


def print_entry(entry: dict):
    tx = ''
    if entry['tx_hash']:
        if entry['tx_hash'].startswith('ERROR'):
            tx = f" | tx=FAILED({entry['tx_hash'][7:30]}...)"
        else:
            tx = f" | tx=..{entry['tx_hash'][-8:]}"
    print(
        f"  block {entry['block_height']} | "
        f"cycles +{entry['cycles_this_block']} (total {entry['vm_ticks']}) | "
        f"R0={entry['registers'][0]} R1={entry['registers'][1]} | "
        f"state ..{entry['state_hash'][-8:]} | "
        f"commit ..{entry['commitment'][-8:]}"
        + tx
        + (" [HALTED]" if entry['halted'] else "")
    )


def main():
    parser = argparse.ArgumentParser(description='BTC-Clocked VM')
    parser.add_argument('program', nargs='?', default='fibonacci',
                        choices=list(PROGRAMS), help='Program to run')
    parser.add_argument('--broadcast', action='store_true',
                        help='Broadcast commitment as OP_RETURN to Bitcoin')
    parser.add_argument('--wif', default=None,
                        help='WIF private key for broadcasting (testnet key starts with c)')
    parser.add_argument('--network', default='testnet', choices=['testnet', 'mainnet'],
                        help='Bitcoin network (default: testnet)')
    args = parser.parse_args()

    if args.broadcast and not args.wif:
        print("Error: --broadcast requires --wif <WIF_KEY>")
        sys.exit(1)

    vm = VM()
    vm.load_program(PROGRAMS[args.program])
    clock = BitcoinClock()

    net_label = f" [{args.network} broadcast]" if args.broadcast else ""
    print(f"BTC-Clocked VM | program={args.program} | cycles_per_block={CYCLES_PER_BLOCK}{net_label}")
    print(f"Ledger: {os.path.abspath(LEDGER_FILE)}")
    print(f"Polling blockstream.info every {POLL_INTERVAL}s ...\n")

    if args.broadcast:
        try:
            from broadcast import get_balance
            bal = get_balance(args.wif, args.network)
            print(f"Wallet balance: {bal} sats\n")
        except Exception as e:
            print(f"Warning: could not check balance: {e}\n")

    while True:
        block = clock.poll()
        if block:
            entry = on_block(block, vm, args.broadcast, args.wif, args.network)
            print_entry(entry)
            if vm.halted:
                print(f"\nDone. {vm.ticks} total VM ticks.")
                print(f"Final state hash: {vm.state_hash()}")
                if entry.get('tx_hash') and not str(entry['tx_hash']).startswith('ERROR'):
                    net = args.network
                    explorer = (
                        f"https://blockstream.info/testnet/tx/{entry['tx_hash']}"
                        if net == 'testnet'
                        else f"https://blockstream.info/tx/{entry['tx_hash']}"
                    )
                    print(f"Final tx: {explorer}")
                break
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
