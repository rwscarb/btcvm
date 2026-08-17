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
| `imgfs.py` | Bitcoin-anchored media archive — images and chunked video |
| `test_vm.py` | Unit tests (VM, VDF, trace, fleet) |
| `Makefile` | Common tasks: install, test, lint, run, imgfs |
| `completions/` | Bash and Zsh tab completions for `btcvm` and `imgfs` |

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

**Requirements:** Python 3.11+, no runtime dependencies (stdlib only).

```bash
# Install
pip install -e .        # editable install
make install            # same via Makefile
make dev                # with dev deps (pytest, ruff)
```

### btcvm clock

```bash
# v1 — one entry per Bitcoin block
python3 main.py fibonacci

# v1.2 — VDF sub-clock: 5 ticks per block
python3 main.py fibonacci --vdf-ticks 5

# v2 — trace Merkle root replaces state_hash
python3 main.py fibonacci --trace

# v3 — fleet of 4 parallel VMs, single fleet root per OP_RETURN
python3 main.py fibonacci --vms 4

# Everything combined
python3 main.py fibonacci --vms 4 --vdf-ticks 3 --trace

# Optional: broadcast commitment as OP_RETURN
pip install bit
python3 main.py fibonacci --broadcast --wif <your-WIF-key> --network mainnet
```

**Getting a wallet for broadcast:**

```python
from bit import PrivateKey
k = PrivateKey()
print(k.to_wif(), k.address)
```

Fund the address with ~5000 sats (covers ~8 commitments at low fee rates).

**Verifying a ledger:**

```bash
python3 verify.py                          # block hashes + commitments
python3 verify.py --trace-file trace.jsonl # include trace verification
python3 verify.py --check-txs              # also fetch block tx lists
```

### imgfs — Bitcoin-anchored media archive

`imgfs` stores images and video in a content-addressed Merkle tree and commits
the root to Bitcoin via the btcvm ledger. Images are hashed whole; video files
are split into 256 KB chunks with a per-file Merkle tree, enabling byte-range
inclusion proofs.

```
image.jpg  → SHA256 ──────────────────────────────────┐
                                                       ├─→ global root → Bitcoin
video.mp4  → [chunk₀, chunk₁, …, chunkₙ] → file root ┘
```

```bash
# Add files (images or video — detected by extension)
python3 imgfs.py add photo.jpg family.jpg video.mp4

# Show current archive state and Merkle root
python3 imgfs.py status

# List all archived files
python3 imgfs.py list

# Commit current Merkle root to the btcvm ledger
python3 imgfs.py commit

# Then anchor on-chain (optional)
python3 broadcast.py <commitment>

# Verify a file is in the archive (Merkle inclusion proof)
python3 imgfs.py verify photo.jpg

# Prove a specific 256 KB chunk of a video is in the archive
python3 imgfs.py verify-chunk video.mp4 3
```

**Via Makefile:**

```bash
make add FILE=photo.jpg
make verify-file FILE=photo.jpg
make imgfs-status
make imgfs-list
make imgfs-commit
make imgfs-clean      # remove manifest + ledger
```

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `IMGFS_MANIFEST` | `imgfs_manifest.jsonl` | Manifest path |
| `IMGFS_LEDGER` | `imgfs_ledger.jsonl` | Ledger path |
| `IMGFS_CHUNK_BYTES` | `262144` (256 KB) | Video chunk size |

**Proof chain for a video chunk:**

```
chunk N bytes → SHA256 → chunk hash
                             ↓ Merkle proof (steps: log₂ chunks)
                         file root  (= global leaf)
                             ↓ Merkle proof (steps: log₂ files)
                         global root → committed to Bitcoin
```

`verify-chunk` outputs all three levels: bytes-on-disk match, per-file proof, global proof.

## Architecture

### VDF sub-clock (v1.2)

Between Bitcoin blocks the VM fires once per VDF tick. Each tick is `STEPS_PER_TICK` sequential SHA256 evaluations seeded from the previous tick's output. Because SHA256 can't be parallelised in this chained form, the ticks represent a minimum sequential cost any verifier must replay.

```
Bitcoin block hash B_h
  tick 0: SHA256ᴺ(B_h)    → vdf₀ → VM cycles → commit₀
  tick 1: SHA256ᴺ(vdf₀)   → vdf₁ → VM cycles → commit₁
  …
  Next block B_{h+1} reseeds the chain
```

### Trace commitment (v2)

Every VM step is recorded as a hash-chained entry:
`step_hash = SHA256(prev_hash ‖ pc ‖ op ‖ regs_before ‖ regs_after)`.
A binary Merkle tree over all step hashes produces a single root that
replaces `state_hash` in the ledger commitment. Any individual step can
be verified without replaying the full execution. The trace file is
the witness a ZK prover (RISC Zero, SP1, Cairo) would consume.

### Fleet Merkle root (v3)

N VMs run in parallel each tick. A binary Merkle tree over all N
commitments produces a single `fleet_root` — one OP_RETURN regardless
of fleet size.

```
tick N:
  VM₀ → commitment₀ ─┐
  VM₁ → commitment₁ ─┤ Merkle → fleet_root → SHA256(block_hash:fleet_root) → OP_RETURN
  VM₂ → commitment₂ ─┤
  VM₃ → commitment₃ ─┘
```

## Ledger formats

**v1 entry:**
```json
{
  "block_height": 961224,
  "block_hash": "000000000000...",
  "vdf_tick": 0,
  "vm_ticks": 10,
  "halted": false,
  "registers": [1, 1, 1, 20, 1, 0, 0, 0],
  "state_hash": "a3f9c2b1...",
  "commitment": "88d6f091..."
}
```

**v1.2 additions** (VDF active):
```json
{
  "vdf_tick": 2,
  "vdf_input": "prev_tick_output...",
  "vdf_hash": "this_tick_output...",
  "commitment": "SHA256(block_hash:vdf_hash:state_hash)"
}
```

**v2 additions** (trace active):
```json
{
  "trace_root": "merkle_root_over_all_steps...",
  "trace_steps": 42,
  "commitment": "SHA256(block_hash:trace_root)"
}
```

**v3** (fleet active) replaces per-VM fields with:
```json
{
  "fleet_size": 4,
  "fleet_root": "merkle_root_over_vm_commitments...",
  "commitment": "SHA256(block_hash:fleet_root)",
  "vms": [
    {"vm_id": 0, "program": "fibonacci", "halted": false, "registers": [...], "state_hash": "..."},
    ...
  ]
}
```

**imgfs ledger entry:**
```json
{
  "ts": "2026-08-17T20:37:00Z",
  "block_height": 961301,
  "block_hash": "000000000000...",
  "merkle_root": "b699603c...",
  "commitment": "9dc75674...",
  "image_count": 3
}
```

## Tab completions

```bash
make completion
```

Installs Bash and Zsh completions for both `btcvm` and `imgfs`:
- `btcvm` — flags and values (`--vdf-ticks`, `--vms`, etc.)
- `imgfs add` — any file
- `imgfs verify` — filenames from the manifest
- `imgfs verify-chunk` — video names, then chunk indices from the manifest

For Zsh, add to `~/.zshrc` if not already present:
```zsh
fpath=(~/.zsh/completions $fpath)
autoload -Uz compinit && compinit
```

## Makefile targets

```
make install        pip install -e .
make dev            install with dev deps
make test           run pytest
make lint           ruff check
make fix            ruff --fix
make run            btcvm one block
make run-vdf        btcvm with VDF sub-clock
make run-trace      btcvm with trace Merkle
make run-fleet      btcvm fleet of 4 VMs
make verify         verify ledger.jsonl
make imgfs-status   show archive status
make imgfs-list     list archived files
make imgfs-commit   commit Merkle root
make imgfs-clean    remove manifest + ledger
make add FILE=…     add file to imgfs
make verify-file FILE=…  verify inclusion
make completion     install shell completions
```

## Roadmap

- ✅ **v1** — Bitcoin-clocked register machine, local ledger, optional OP_RETURN
- ✅ **v1.1** — Ledger verification against block hashes and OP_RETURN
- ✅ **v1.2** — VDF sub-clock (`--vdf-ticks N`)
- ✅ **v2** — Trace commitment / Merkle root (`--trace`)
- ✅ **v3** — Fleet Merkle: N parallel VMs, single OP_RETURN (`--vms N`)
- ✅ **imgfs** — Bitcoin-anchored media archive with chunked video and inclusion proofs

## Tests

```bash
make test
# or
python3 -m pytest test_vm.py -v
```

## License

MIT
