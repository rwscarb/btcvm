# btcvm

A minimal register machine that executes in lockstep with Bitcoin blocks.

Each Bitcoin block triggers a fixed number of VM cycles. After each block, the VM's state is hashed and cryptographically bound to the block hash — producing a verifiable ledger of computation anchored to Bitcoin's clock.

## Why

Bitcoin is a clock. Every ~10 minutes a new block is mined, and its hash is unpredictable until the moment it's found. This makes it a natural source of consensus timing for off-chain computation.

`btcvm` demonstrates the minimal architecture for a Bitcoin-clocked state machine:

```
Bitcoin block hash
      ↓
  VM executes N cycles
      ↓
  commitment = SHA256(block_hash + state_hash)
      ↓
  OP_RETURN → committed to Bitcoin (optional)
```

The resulting ledger is independently verifiable: anyone with the block hashes (publicly available from any Bitcoin node or API) can recompute every commitment and confirm the VM ran exactly as recorded.

## Files

| File | Description |
|------|-------------|
| `vm.py` | Minimal register machine (8 registers, 7 opcodes) |
| `clock.py` | Bitcoin block clock via blockstream.info API |
| `programs.py` | Sample programs (fibonacci, countdown) |
| `vdf.py` | VDF sub-clock — sequential SHA256 chain with tick/verify (v1.2) |
| `trace.py` | Step-by-step execution trace with Merkle root commitment (v2) |
| `fleet.py` | N parallel VMs with fleet Merkle root — one OP_RETURN anchors all (v3) |
| `broadcast.py` | Optional OP_RETURN broadcast via the `bit` library |
| `main.py` | Orchestrator — runs clock loop, executes VM(s), writes ledger |
| `verify.py` | Verifies ledger, VDF chain, trace, and fleet Merkle against Bitcoin |
| `test_vm.py` | Unit tests (VM, VDF, trace, fleet) |

## VM

The VM has 8 registers (`R0`–`R7`) and 7 opcodes:

| Opcode | Description |
|--------|-------------|
| `LOAD r, val` | Load immediate value into register |
| `ADD dst, a, b` | `Rdst = Ra + Rb` |
| `SUB dst, a, b` | `Rdst = Ra - Rb` |
| `MUL dst, a, b` | `Rdst = Ra * Rb` |
| `JMP addr` | Unconditional jump |
| `JZ r, addr` | Jump if register is zero |
| `HALT` | Stop execution |

Programs are tuples loaded into memory. See `programs.py` for examples.

## Usage

**Requirements:** Python 3.8+, no dependencies for core operation.

```bash
# v1 — one entry per Bitcoin block (state hash commitment)
python3 main.py fibonacci

# v1.2 — VDF sub-clock: 5 sequential-hash ticks per block, one ledger entry each
python3 main.py fibonacci --vdf-ticks 5

# v2 — trace mode: Merkle root over all step hashes replaces state_hash
python3 main.py fibonacci --trace

# v3 — fleet mode: 4 parallel VMs, single fleet Merkle root per OP_RETURN
python3 main.py fibonacci --vms 4

# Everything combined
python3 main.py fibonacci --vms 4 --vdf-ticks 3 --trace

# Optional OP_RETURN broadcast to Bitcoin
pip install bit
python3 main.py fibonacci --broadcast --wif <your-WIF-key> --network mainnet
```

**Getting a wallet for broadcast:**

```python
from bit import PrivateKey
k = PrivateKey()
print(k.to_wif(), k.address)
```

Fund the address with a small amount (~5000 sats covers ~8 commitments at low fee rates). The `--broadcast` flag is optional — the ledger and verification work without it.

**Verifying a ledger:**

```bash
# Verify block hashes, commitments, VDF chain, and trace (fast)
python3 verify.py

# With a trace file
python3 verify.py --trace-file trace.jsonl

# Also check OP_RETURN presence in blocks (fetches block tx lists)
python3 verify.py --check-txs
```

**v1 ledger entry:**

```json
{
  "block_height": 961224,
  "block_hash": "000000000000...",
  "vdf_tick": 0,
  "vm_ticks": 10,
  "cycles_this_tick": 10,
  "halted": false,
  "registers": [1, 1, 1, 20, 1, 0, 0, 0],
  "state_hash": "a3f9c2b1...",
  "commitment": "88d6f091...",
  "tx_hash": "3969fe2c..."
}
```

**v1.2 entry** (VDF active) adds:

```json
{
  "vdf_tick": 2,
  "vdf_input": "prev_tick_output_hex...",
  "vdf_hash": "this_tick_output_hex...",
  "commitment": "SHA256(block_hash:vdf_hash:state_hash)"
}
```

**v2 entry** (trace active) adds:

```json
{
  "trace_root": "merkle_root_over_all_steps_hex...",
  "trace_steps": 42,
  "commitment": "SHA256(block_hash:trace_root)"
}
```

**v3 entry** (fleet active) replaces per-VM fields with:

```json
{
  "fleet_size": 4,
  "fleet_root": "merkle_root_over_all_vm_commitments...",
  "commitment": "SHA256(block_hash:fleet_root)",
  "vms": [
    {"vm_id": 0, "program": "fibonacci", "vm_ticks": 10, "cycles": 10,
     "halted": false, "registers": [...], "state_hash": "..."},
    {"vm_id": 1, ...},
    {"vm_id": 2, ...},
    {"vm_id": 3, ...}
  ]
}
```

## Architecture

### VDF sub-clock (v1.2)

Between Bitcoin blocks the VM no longer fires once and waits — instead it fires once per VDF tick. Each tick is `STEPS_PER_TICK` sequential SHA256 evaluations seeded from the previous tick's output (and the first tick is seeded from the Bitcoin block hash). Because SHA256 can't be parallelised in this chained form, the ticks represent a minimum sequential cost that any verifier must replay.

```
Bitcoin block hash B_h
  VDF tick 0:  SHA256^N(B_h) → vdf_0  →  VM cycles  →  commit_0
  VDF tick 1:  SHA256^N(vdf_0) → vdf_1  →  VM cycles  →  commit_1
  ...
  Next Bitcoin block B_{h+1} reseeds the VDF
```

### Trace commitment (v2)

Every VM step is recorded as a hash-chained entry: `step_hash = SHA256(prev_hash || pc || op || regs_before || regs_after)`. A binary Merkle tree over all step hashes produces a single root that replaces `state_hash` in the ledger commitment.

This means:
- Anyone can verify any individual step without replaying the entire execution
- The trace file is the witness a ZK prover (RISC Zero, SP1, Cairo) would consume to generate a succinct proof

### Fleet Merkle root (v3)

N VMs run in parallel each tick. Each contributes a commitment (state_hash or trace_root). A binary Merkle tree over all N commitments produces a single `fleet_root` — one value, one OP_RETURN, regardless of fleet size.

```
tick N:
  VM_0 → commitment_0 ─┐
  VM_1 → commitment_1 ─┤ Merkle → fleet_root → SHA256(block_hash:fleet_root) → OP_RETURN
  VM_2 → commitment_2 ─┤
  VM_3 → commitment_3 ─┘
```

Verification recomputes the fleet Merkle from per-VM hashes stored in the ledger entry and checks it against the recorded `fleet_root`, without re-executing any VM.

## Roadmap

- ✅ **v1** — Bitcoin-clocked register machine, local ledger, optional OP_RETURN
- ✅ **v1.1** — Ledger verification against block hashes and OP_RETURN (`--check-txs`)
- ✅ **v1.2** — VDF sub-clock (`--vdf-ticks N`)
- ✅ **v2** — Trace commitment / Merkle root (`--trace`)
- ✅ **v3** — Fleet Merkle tree: N parallel VMs, single OP_RETURN (`--vms N`)

## Tests

```bash
pip install pytest
python3 -m pytest test_vm.py -v
```

## License

MIT
