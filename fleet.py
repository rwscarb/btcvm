"""
VMFleet — manages N parallel VM instances for btcvm v3.

Each block (or VDF tick) runs all VMs, collects a commitment from each, and
produces a single fleet Merkle root that replaces per-VM OP_RETURNs with one.

Ledger entry shape (fleet mode):
    {
      "fleet_size": 4,
      "fleet_root": "<merkle_root_over_vm_commitments>",
      "commitment": "SHA256(block_hash[:vdf_hash]:fleet_root)",
      "vms": [
        {"vm_id": 0, "program": "fibonacci", "vm_ticks": 10, "cycles": 10,
         "halted": false, "registers": [...], "state_hash": "..."},
        ...
      ]
    }
"""

import hashlib

from vm import VM
from programs import PROGRAMS


def _merkle(leaves: list[str]) -> str:
    if not leaves:
        return '0' * 64
    layer = leaves[:]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [
            hashlib.sha256((layer[i] + layer[i + 1]).encode()).hexdigest()
            for i in range(0, len(layer), 2)
        ]
    return layer[0]


class VMFleet:
    def __init__(self, programs: list[str], enable_trace: bool = False):
        """
        programs: list of program names (from PROGRAMS); length defines fleet size.
        enable_trace: attach a VMTrace to each VM for Merkle-root commitments.
        """
        self._vms: list[tuple[str, VM]] = []
        self._enable_trace = enable_trace

        for prog_name in programs:
            if prog_name not in PROGRAMS:
                raise ValueError(f"Unknown program: {prog_name!r}")
            vm = VM()
            vm.load_program(PROGRAMS[prog_name])
            if enable_trace:
                from trace import VMTrace
                vm.trace = VMTrace()
            self._vms.append((prog_name, vm))

    @property
    def size(self) -> int:
        return len(self._vms)

    def run_tick(self, max_steps: int) -> list[int]:
        """Run each VM for up to max_steps cycles. Returns cycles executed per VM."""
        return [vm.run(max_steps=max_steps) for _, vm in self._vms]

    def vm_commitments(self) -> list[str]:
        """Per-VM commitment: trace Merkle root if tracing, else state_hash."""
        result = []
        for _, vm in self._vms:
            if vm.trace is not None:
                result.append(vm.trace.merkle_root())
            else:
                result.append(vm.state_hash())
        return result

    def fleet_root(self) -> str:
        """Merkle root over all VM commitments."""
        return _merkle(self.vm_commitments())

    def all_halted(self) -> bool:
        return all(vm.halted for _, vm in self._vms)

    def any_halted(self) -> bool:
        return any(vm.halted for _, vm in self._vms)

    def snapshots(self, cycles_per_vm: list[int]) -> list[dict]:
        """Per-VM state dicts for embedding in a ledger entry."""
        result = []
        for i, ((prog_name, vm), cycles) in enumerate(zip(self._vms, cycles_per_vm)):
            snap: dict = {
                'vm_id': i,
                'program': prog_name,
                'vm_ticks': vm.ticks,
                'cycles': cycles,
                'halted': vm.halted,
                'registers': vm.registers[:],
                'state_hash': vm.state_hash(),
            }
            if vm.trace is not None:
                snap['trace_root'] = vm.trace.merkle_root()
                snap['trace_steps'] = vm.trace.step_count()
            result.append(snap)
        return result

    def export_traces(self, base_path: str):
        """Write trace_<vmid>.jsonl for each VM that has tracing enabled."""
        import os
        root, ext = os.path.splitext(base_path)
        for i, (_, vm) in enumerate(self._vms):
            if vm.trace is not None:
                vm.trace.export(f"{root}_{i}{ext}")

    @staticmethod
    def verify_fleet_root(entry: dict) -> bool:
        """Recompute fleet Merkle root from a ledger entry's per-VM data."""
        vms = entry.get('vms', [])
        leaves = []
        for snap in vms:
            leaves.append(snap.get('trace_root', snap['state_hash']))
        return _merkle(leaves) == entry['fleet_root']
