#!/usr/bin/env python3
"""
BTC-Clocked VM
Executes a register machine in lockstep with Bitcoin blocks.

v1   — one ledger entry per block (state hash commitment)
v1.2 — sub-block VDF clock: N sequential-hash ticks per block, one entry each
v2   — trace mode: Merkle root over all step hashes replaces state_hash
v3   — fleet mode: N parallel VMs, single fleet Merkle root per OP_RETURN

Usage:
    python3 main.py [program] [options]

Examples:
    python3 main.py fibonacci
    python3 main.py fibonacci --vdf-ticks 5
    python3 main.py fibonacci --trace
    python3 main.py fibonacci --vms 4
    python3 main.py fibonacci --vms 4 --vdf-ticks 3 --trace
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
    v1:        SHA256(block_hash:state_hash)
    v1.2:      SHA256(block_hash:vdf_hash:state_hash)
    v2 / v3:   same formula; caller passes merkle_root or fleet_root as state_or_root
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
# single-VM tick handler
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
        'network': block.get('network', 'mainnet'),
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
# fleet tick handler (v3)
# ---------------------------------------------------------------------------

def on_tick_fleet(
    block: dict,
    fleet,
    vdf_tick: int,
    vdf_input: str | None,
    vdf_hash: str | None,
    trace_path: str | None,
    broadcast: bool,
    wif: str,
    network: str,
    ledger_path: str,
) -> dict:
    cycles_per_vm = fleet.run_tick(max_steps=CYCLES_PER_BLOCK)
    fleet_root = fleet.fleet_root()
    commit_hash = make_commitment(block['hash'], fleet_root, vdf_hash)

    entry: dict = {
        'block_height': block['height'],
        'block_hash': block['hash'],
        'network': block.get('network', 'mainnet'),
        'vdf_tick': vdf_tick,
        'fleet_size': fleet.size,
        'fleet_root': fleet_root,
        'halted': fleet.all_halted(),
        'commitment': commit_hash,
        'tx_hash': None,
        'vms': fleet.snapshots(cycles_per_vm),
    }

    if vdf_hash is not None:
        entry['vdf_input'] = vdf_input
        entry['vdf_hash'] = vdf_hash

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
    vm_or_fleet,
    vdf_ticks: int,
    trace_path: str | None,
    broadcast: bool,
    wif: str,
    network: str,
    ledger_path: str,
) -> list[dict]:
    from fleet import VMFleet
    is_fleet = isinstance(vm_or_fleet, VMFleet)
    tick_fn = on_tick_fleet if is_fleet else on_tick

    def _halted():
        return vm_or_fleet.all_halted() if is_fleet else vm_or_fleet.halted

    entries = []

    if vdf_ticks > 0:
        from vdf import VDF
        vdf = VDF(block['hash'])
        for i in range(vdf_ticks):
            if _halted():
                break
            vdf_input, vdf_hash = vdf.tick()
            entry = tick_fn(
                block=block,
                **({'fleet': vm_or_fleet} if is_fleet else {'vm': vm_or_fleet}),
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
        entry = tick_fn(
            block=block,
            **({'fleet': vm_or_fleet} if is_fleet else {'vm': vm_or_fleet}),
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

    # Export traces after each block
    if trace_path:
        if is_fleet:
            vm_or_fleet.export_traces(trace_path)
        elif vm_or_fleet.trace:
            vm_or_fleet.trace.export(trace_path)

    return entries


# ---------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------

def print_entry(entry: dict):
    vdf_label = f" vdf={entry['vdf_tick']}" if entry.get('vdf_hash') else ""
    tx = ''
    if entry.get('tx_hash'):
        tx = f" | tx=FAILED" if str(entry['tx_hash']).startswith('ERROR') else f" | tx=..{entry['tx_hash'][-8:]}"

    if 'fleet_root' in entry:
        n = entry['fleet_size']
        halted_vms = sum(1 for v in entry['vms'] if v['halted'])
        total_cycles = sum(v['cycles'] for v in entry['vms'])
        halt_label = f" [{halted_vms}/{n} HALTED]" if halted_vms else ""
        print(
            f"  block {entry['block_height']}{vdf_label} | "
            f"fleet={n} cycles +{total_cycles} | "
            f"fleet_root ..{entry['fleet_root'][-8:]} | "
            f"commit ..{entry['commitment'][-8:]}"
            + tx + halt_label
        )
    else:
        trace_label = f" trace={entry['trace_steps']}steps" if 'trace_root' in entry else ""
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
    parser.add_argument('--vms', type=int, default=1, metavar='N',
                        help='Fleet size: run N parallel VMs (v3); default 1')
    parser.add_argument('--vdf-ticks', type=int, default=0, metavar='N',
                        help='VDF sub-clock ticks per Bitcoin block (v1.2); 0 = disabled')
    parser.add_argument('--trace', action='store_true',
                        help='Record full execution trace and commit to Merkle root (v2)')
    parser.add_argument('--trace-file', default='trace.jsonl', metavar='PATH',
                        help='Trace output file; in fleet mode writes trace_<vmid>.jsonl')
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

    if args.vms < 1:
        print("Error: --vms must be >= 1")
        sys.exit(1)

    fleet_mode = args.vms > 1
    trace_path = args.trace_file if args.trace else None

    if fleet_mode:
        from fleet import VMFleet
        programs = [args.program] * args.vms
        vm_or_fleet = VMFleet(programs, enable_trace=args.trace)
    else:
        vm_or_fleet = VM()
        vm_or_fleet.load_program(PROGRAMS[args.program])
        if args.trace:
            from trace import VMTrace
            vm_or_fleet.trace = VMTrace()

    clock = BitcoinClock(testnet=(args.network == 'testnet'))

    mode_parts = []
    if fleet_mode:
        mode_parts.append(f"fleet={args.vms}×{args.program}")
    if args.vdf_ticks:
        mode_parts.append(f"vdf_ticks={args.vdf_ticks}")
    if args.trace:
        mode_parts.append(f"trace")
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

    network_label = args.network
    while True:
        block = clock.poll()
        if block:
            block['network'] = network_label
            entries = on_block(
                block=block,
                vm_or_fleet=vm_or_fleet,
                vdf_ticks=args.vdf_ticks,
                trace_path=trace_path,
                broadcast=args.broadcast,
                wif=args.wif,
                network=args.network,
                ledger_path=args.ledger,
            )
            for entry in entries:
                print_entry(entry)

            done = vm_or_fleet.all_halted() if fleet_mode else vm_or_fleet.halted
            if done:
                last = entries[-1]
                print(f"\nDone. Final fleet_root: ..{last['fleet_root'][-8:]}" if fleet_mode
                      else f"\nDone. {vm_or_fleet.ticks} total VM ticks.")
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
