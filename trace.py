"""
Execution trace for btcvm v2.

Records every VM step as a hash-chained log entry.  A binary Merkle tree over
the step hashes produces a single root that replaces the simple state_hash in
the ledger commitment — anyone can verify any individual step without replaying
the entire execution.

This is the trace format a ZK prover (RISC Zero, SP1, Cairo) would consume to
generate a succinct proof of correct execution.  The trace itself is not a ZK
proof, but it is verifiable by replay and structured for future ZK integration.
"""

import hashlib
import json


class VMTrace:
    def __init__(self):
        self._steps: list[dict] = []
        self._prev_hash = '0' * 64  # genesis sentinel

    def record(self, pc: int, op: str, regs_before: list[int], regs_after: list[int]):
        """Append one VM step to the trace."""
        payload = json.dumps({
            'prev': self._prev_hash,
            'pc': pc,
            'op': op,
            'before': regs_before,
            'after': regs_after,
        }, sort_keys=True).encode()
        step_hash = hashlib.sha256(payload).hexdigest()
        self._steps.append({
            'step': len(self._steps),
            'pc': pc,
            'op': op,
            'regs_before': regs_before,
            'regs_after': regs_after,
            'step_hash': step_hash,
        })
        self._prev_hash = step_hash

    def tip_hash(self) -> str:
        """Hash of the last recorded step (chain tip)."""
        return self._prev_hash

    def merkle_root(self) -> str:
        """Binary Merkle root over all step hashes."""
        if not self._steps:
            return '0' * 64
        leaves = [s['step_hash'] for s in self._steps]
        while len(leaves) > 1:
            if len(leaves) % 2:
                leaves.append(leaves[-1])  # pad odd layer with duplicate
            leaves = [
                hashlib.sha256((leaves[i] + leaves[i + 1]).encode()).hexdigest()
                for i in range(0, len(leaves), 2)
            ]
        return leaves[0]

    def step_count(self) -> int:
        return len(self._steps)

    def export(self, path: str):
        """Write the trace to a .jsonl file."""
        with open(path, 'w') as f:
            for step in self._steps:
                f.write(json.dumps(step) + '\n')

    @staticmethod
    def verify_file(path: str) -> tuple[bool, str]:
        """
        Verify a trace .jsonl by replaying every step hash.

        Returns (ok, merkle_root).  Raises FileNotFoundError if path missing.
        """
        steps = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    steps.append(json.loads(line))

        prev_hash = '0' * 64
        for step in steps:
            payload = json.dumps({
                'prev': prev_hash,
                'pc': step['pc'],
                'op': step['op'],
                'before': step['regs_before'],
                'after': step['regs_after'],
            }, sort_keys=True).encode()
            expected = hashlib.sha256(payload).hexdigest()
            if expected != step['step_hash']:
                return False, ''
            prev_hash = step['step_hash']

        # Recompute Merkle root
        leaves = [s['step_hash'] for s in steps]
        if not leaves:
            return True, '0' * 64
        while len(leaves) > 1:
            if len(leaves) % 2:
                leaves.append(leaves[-1])
            leaves = [
                hashlib.sha256((leaves[i] + leaves[i + 1]).encode()).hexdigest()
                for i in range(0, len(leaves), 2)
            ]
        return True, leaves[0]
