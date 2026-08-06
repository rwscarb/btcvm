#!/usr/bin/env python3
"""
BTC-Clocked VM
Executes a register machine in lockstep with Bitcoin blocks.

v1   — one ledger entry per block (state hash commitment)
v1.2 — sub-block VDF clock: N sequential-hash ticks per block, one entry each
v2   — trace mode: Merkle root over all step hashes replaces state_hash

Usage:
    python3 main.py [program] [options]

Examples:
    python3 main.py fibonacci
    python3 main.py fibonacci --vdf-ticks 5
    python3 main.py fibonacci --trace
    python3 main.py fibonacci --vdf-ticks 5 --trace
    python3 main.py fibonacci --broadcast --wif cNfsPbDJg... --network testnet
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


# ---------------------------------------------------------------------------
# commitment formula
# ---------------------------------------------------------------------------

def make_commitment(block_hash: str, state_or_root: str, vdf_hash: str | None = None) -> str:
    """
    Compute a ledger entry commitment.

    v1:   SHA256(block_hash:state_hash)
    v1.2: SHA256(block_hash:vdf_hash:state_hash)
    v2:   same formula; caller passes merkle_root as state_or_root
    """
    if vdf_hash:
        return hashlib.sha256(f"{block_hash}:{vdf_hash}:{state_or_root}".encode()).hexdigest()
    return hashlib.sha256(f"{block_hash}:{state_or_root}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# ledger I/O
# ---------------------------------------------------------------------------

def write_entry(entry: dict, ledger_path: str = LEDGER_FILE):
    with open(ledger_path, 'a') as f:
        f.write(json.dumps(entry) + '\n')


def broadcast_entry(commit_hash: str, wif: str, network: str) -> str | None:
    try:
        from broadcast import broadcast_commitment
        return broadcast_commitment(commit_hash, wif, network)
    except Exception as e:
        return f'ERROR: {e}'


# ---------------------------------------------------------------------------
# per-tick handler (one ledger entry)
# ---------------------------------------------------------------------------

def on_tick(
    block: dict,
    vm: VM,
    vdf_tick: int,
    vdf_input: str | None,
    vdf_hash: str | None,
    trace_path: str | None,
    broadcast: bool,
    wif: str,
    network: str,
    ledger_path: str,
) -> dict:
    cycles = vm.run(max_steps=CYCLES_PER_BLOCK)

    if trace_path and vm.trace:
        state_or_root = vm.trace.merkle_root()
        trace_steps = vm.trace.step_count()
    else:
        state_or_root = vm.state_hash()
        trace_steps = None

    commit_hash = make_commitment(block['hash'], state_or_root, vdf_hash)

    entry: dict = {
        'block_height': block['height'],
        'block_hash': block['hash'],
        'vdf_tick': vdf_tick,
        'vm_ticks': vm.ticks,
        'cycles_this_tick': cycles,
        'halted': vm.halted,
        'registers': vm.registers[:],
        'state_hash': vm.state_hash(),
        'commitment': commit_hash,
        'tx_hash': None,
    }

    if vdf_hash is not None:
        entry['vdf_input'] = vdf_input
        entry['vdf_hash'] = vdf_hash

    if trace_steps is not None:
        entry['trace_root'] = state_or_root
        entry['trace_steps'] = trace_steps

    if broadcast:
        tx = broadcast_entry(commit_hash, wif, network)
        entry['tx_hash'] = tx

    write_entry(entry, ledger_path)
    return entry


# ---------------------------------------------------------------------------
# on_block: drives one or more ticks per Bitcoin block
# ---------------------------------------------------------------------------

def on_block(
    block: dict,
    vm: VM,
    vdf_ticks: int,
    trace_path: str | None,
    broadcast: bool,
    wif: str,
    network: str,
    ledger_path: str,
) -> list[dict]:
    entries = []

    if vdf_ticks > 0:
        from vdf import VDF
        vdf = VDF(block['hash'])
        for i in range(vdf_ticks):
            if vm.halted:
                break
            vdf_input, vdf_hash = vdf.tick()
            entry = on_tick(
                block=block,
                vm=vm,
                vdf_tick=i,
                vdf_input=vdf_input,
                vdf_hash=vdf_hash,
                trace_path=trace_path,
                broadcast=broadcast,
                wif=wif,
                network=network,
                ledger_path=ledger_path,
            )
            entries.append(entry)
    else:
        entry = on_tick(
            block=block,
            vm=vm,
            vdf_tick=0,
            vdf_input=None,
            vdf_hash=None,
            trace_path=trace_path,
            broadcast=broadcast,
            wif=wif,
            network=network,
            ledger_path=ledger_path,
        )
        entries.append(entry)

    if trace_path and vm.trace:
        vm.trace.export(trace_path)

    return entries


# ---------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------

def print_entry(entry: dict):
    vdf_label = f" vdf={entry['vdf_tick']}" if entry.get('vdf_hash') else ""
    trace_label = f" trace={entry['trace_steps']}steps" if 'trace_root' in entry else ""
    tx = ''
    if entry.get('tx_hash'):
        if str(entry['tx_hash']).startswith('ERROR'):
            tx = f" | tx=FAILED"
        else:
            tx = f" | tx=..{entry['tx_hash'][-8:]}"
    root_key = 'trace_root' if 'trace_root' in entry else 'state_hash'
    print(
        f"  block {entry['block_height']}{vdf_label} | "
        f"cycles +{entry['cycles_this_tick']} (total {entry['vm_ticks']}) | "
        f"R0={entry['registers'][0]} R1={entry['registers'][1]} | "
        f"root ..{entry[root_key][-8:]}{trace_label} | "
        f"commit ..{entry['commitment'][-8:]}"
        + tx
        + (" [HALTED]" if entry['halted'] else "")
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='BTC-Clocked VM')
    parser.add_argument('program', nargs='?', default='fibonacci',
                        choices=list(PROGRAMS), help='Program to run')
    parser.add_argument('--vdf-ticks', type=int, default=0, metavar='N',
                        help='VDF sub-clock ticks per Bitcoin block (v1.2); 0 = disabled')
    parser.add_argument('--trace', action='store_true',
                        help='Record full execution trace and commit to Merkle root (v2)')
    parser.add_argument('--trace-file', default='trace.jsonl', metavar='PATH',
                        help='Trace output file (default: trace.jsonl)')
    parser.add_argument('--ledger', default=LEDGER_FILE, metavar='PATH',
                        help='Ledger output file (default: ledger.jsonl)')
    parser.add_argument('--broadcast', action='store_true',
                        help='Broadcast commitment as OP_RETURN to Bitcoin')
    parser.add_argument('--wif', default=None,
                        help='WIF private key for broadcasting')
    parser.add_argument('--network', default='testnet', choices=['testnet', 'mainnet'],
                        help='Bitcoin network (default: testnet)')
    args = parser.parse_args()

    if args.broadcast and not args.wif:
        print("Error: --broadcast requires --wif <WIF_KEY>")
        sys.exit(1)

    vm = VM()
    vm.load_program(PROGRAMS[args.program])

    trace_path = None
    if args.trace:
        from trace import VMTrace
        vm.trace = VMTrace()
        trace_path = args.trace_file

    clock = BitcoinClock(testnet=(args.network == 'testnet'))

    mode_parts = []
    if args.vdf_ticks:
        mode_parts.append(f"vdf_ticks={args.vdf_ticks}")
    if args.trace:
        mode_parts.append(f"trace→{trace_path}")
    if args.broadcast:
        mode_parts.append(f"{args.network} broadcast")
    mode_label = f" [{', '.join(mode_parts)}]" if mode_parts else ""

    print(f"BTC-Clocked VM | program={args.program} | cycles_per_tick={CYCLES_PER_BLOCK}{mode_label}")
    print(f"Ledger: {os.path.abspath(args.ledger)}")
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
            entries = on_block(
                block=block,
                vm=vm,
                vdf_ticks=args.vdf_ticks,
                trace_path=trace_path,
                broadcast=args.broadcast,
                wif=args.wif,
                network=args.network,
                ledger_path=args.ledger,
            )
            for entry in entries:
                print_entry(entry)

            if vm.halted:
                print(f"\nDone. {vm.ticks} total VM ticks.")
                if args.trace:
                    from trace import VMTrace
                    ok, root = VMTrace.verify_file(trace_path)
                    print(f"Trace: {entry['trace_steps']} steps, Merkle root ..{root[-8:]} {'OK' if ok else 'VERIFY FAILED'}")
                last = entries[-1]
                print(f"Final commitment: {last['commitment']}")
                if last.get('tx_hash') and not str(last['tx_hash']).startswith('ERROR'):
                    net = args.network
                    explorer = (
                        f"https://blockstream.info/testnet/tx/{last['tx_hash']}"
                        if net == 'testnet'
                        else f"https://blockstream.info/tx/{last['tx_hash']}"
                    )
                    print(f"Final tx: {explorer}")
                break
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
