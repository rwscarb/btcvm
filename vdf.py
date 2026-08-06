"""
VDF sub-clock for btcvm v1.2.

A Verifiable Delay Function built on sequential SHA256: each tick chains
STEPS_PER_TICK hash evaluations that cannot be parallelised. The output of
each tick seeds the next, and anyone can verify a tick by replaying it.

Between Bitcoin blocks the VM fires once per VDF tick instead of once per
block, giving sub-block precision with a provable sequential cost.
"""

import hashlib

STEPS_PER_TICK = 10_000  # sequential SHA256 iterations per tick; tune for ~1s on target hw


class VDF:
    def __init__(self, seed_hex: str, steps_per_tick: int = STEPS_PER_TICK):
        if len(seed_hex) != 64:
            raise ValueError("seed_hex must be a 64-char hex string (32 bytes)")
        self.steps_per_tick = steps_per_tick
        self._state = bytes.fromhex(seed_hex)
        self.tick_count = 0

    def tick(self) -> tuple[str, str]:
        """
        Run one VDF tick.

        Returns (input_hex, output_hex) so the caller can record both ends
        of the tick for later verification.
        """
        input_hex = self._state.hex()
        h = self._state
        for i in range(self.steps_per_tick):
            h = hashlib.sha256(h + i.to_bytes(4, 'big')).digest()
        self._state = h
        self.tick_count += 1
        return input_hex, h.hex()

    def current_hex(self) -> str:
        return self._state.hex()

    @staticmethod
    def verify(input_hex: str, output_hex: str, steps: int = STEPS_PER_TICK) -> bool:
        """Verify a single tick by replaying the sequential chain."""
        h = bytes.fromhex(input_hex)
        for i in range(steps):
            h = hashlib.sha256(h + i.to_bytes(4, 'big')).digest()
        return h.hex() == output_hex

    @staticmethod
    def verify_chain(ticks: list[dict], steps: int = STEPS_PER_TICK) -> bool:
        """
        Verify an ordered list of tick records.

        Each record must have 'vdf_input' and 'vdf_hash' fields.
        Consecutive ticks must chain: tick[i].vdf_hash == tick[i+1].vdf_input.
        """
        for i, t in enumerate(ticks):
            if not VDF.verify(t['vdf_input'], t['vdf_hash'], steps):
                return False
            if i + 1 < len(ticks):
                if ticks[i + 1]['vdf_input'] != t['vdf_hash']:
                    return False
        return True
