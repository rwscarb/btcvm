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
| `broadcast.py` | Optional OP_RETURN broadcast via the `bit` library |
| `main.py` | Orchestrator — runs clock loop, executes VM, writes ledger |
| `verify.py` | Verifies `ledger.jsonl` against Bitcoin block hashes |
| `test_vm.py` | VM unit tests |

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
# Run (ledger only, no broadcast)
python3 main.py fibonacci
python3 main.py countdown

# Run with OP_RETURN broadcast to Bitcoin mainnet
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
# Verify block hashes and commitments (fast)
python3 verify.py

# Also check OP_RETURN presence in blocks (fetches block tx lists)
python3 verify.py --check-txs
```

Each ledger entry looks like:

```json
{
  "block_height": 961224,
  "block_hash": "000000000000...",
  "vm_ticks": 10,
  "cycles_this_block": 10,
  "halted": false,
  "registers": [1, 1, 1, 20, 1, 0, 0, 0],
  "state_hash": "a3f9c2b1...",
  "commitment": "88d6f091...",
  "tx_hash": "3969fe2c..."
}
```

## Architecture

This is **v1** — a proof of concept demonstrating the core loop. The roadmap:

- **v1.1** — Ledger verification against OP_RETURN data on-chain (`--check-txs`)
- **v1.2** — VDF sub-clock for precision between Bitcoin blocks
- **v2** — ZK proofs of VM execution (RISC Zero / SP1) replacing state hashes
- **v3** — Multi-VM Merkle tree, single OP_RETURN anchors N parallel VMs

## Tests

```bash
pip install pytest
python3 -m pytest test_vm.py -v
```

## License

MIT
