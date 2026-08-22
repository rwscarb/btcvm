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
| `ott.py` | Bitcoin-anchored media archive — images, video, and git repos; also an interactive shell |
| `test_vm.py` | Unit tests (VM, VDF, trace, fleet) |
| `test_ott_completion.py` | Unit tests (ott shell tab completion, path handling) |
| `Makefile` | Common tasks: install, test, lint, run, ott |
| `completions/` | Bash and Zsh tab completions for `btcvm` and `ott` |

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

### ott — Bitcoin-anchored media archive

`ott` stores images and video in a content-addressed Merkle tree and commits
the root to Bitcoin via the btcvm ledger. Images are hashed whole; video files
are split into 256 KB chunks with a per-file Merkle tree, enabling byte-range
inclusion proofs.

```
image.jpg  → SHA256 ──────────────────────────────────┐
                                                       ├─→ global root → Bitcoin
video.mp4  → [chunk₀, chunk₁, …, chunkₙ] → file root ┘
```

```bash
# Stage files (images or video — detected by extension). Hashed now — that's
# what identifies the file and catches duplicates — but not yet copied into
# object storage or written to the manifest.
python3 ott.py add photo.jpg family.jpg video.mp4

# Show what's staged vs. already archived, and the current Merkle root
python3 ott.py status

# Changed your mind about a staged file? Drop it before it's archived.
# (Only works pre-commit — once a file's archived, rm can't touch it.)
python3 ott.py rm family.jpg

# Archive everything staged, then commit the Merkle root to the btcvm
# ledger (sync is a plain alias for commit — same thing, either name)
python3 ott.py commit

# List all archived files
python3 ott.py list

# Generate a wallet key, then anchor the commitment on-chain (optional)
python3 ott.py keygen --network testnet
python3 ott.py broadcast --wif <WIF_KEY>

# Verify a file is in the archive (Merkle inclusion proof)
python3 ott.py verify photo.jpg

# Prove a specific 256 KB chunk of a video is in the archive
python3 ott.py verify-chunk video.mp4 3

# Verify every ledger commit against real Bitcoin, not just internal state
python3 ott.py verify-chain
```

**Interactive shell:**

```bash
python3 ott.py            # or: python3 ott.py shell
```

The shell wraps the same archive with a stateful `cd`-able hierarchy, tab
completion on every command (archive names, tags, local paths), and a few
things the flat CLI doesn't have:

| Command | Description |
|---|---|
| `add [-r] <file>...` | Stage files (git-index style) — hashed now, archived on `commit`/`sync` |
| `rm <name_or_hash>` | Unstage a pending `add`; refuses once a file's actually committed |
| `commit` / `sync` | Archive everything staged, then commit the Merkle root to the ledger (same action, either name) |
| `l [-a] [-t tag] [-b] [dir]` | One-level `ls`-style view of the archive hierarchy (from `orig_path`) |
| `tree [-a] [-t tag] [-dN] [-b] [dir]` | Recursive tree view; `-d0` for unlimited depth |
| `cd`, `pwd` | Navigate the *archive* hierarchy (real filesystem nav is `lcd`/`lpwd`) — `cd`'ing to a repo's name instead jumps the real filesystem cwd to that repo's checkout, since repos have no virtual file tree of their own |
| `list` / `ls [pattern]` | Full flat dump of every path in one table, optionally filtered by regex (bare or `/slash-delimited/`) |
| `open <name_or_hash>` (alias `o`) | Open an archived file with the OS default handler (prefers the live copy, falls back to the archived one) |
| `mv <name> <new_path>` | Update an entry's tracked path; moves into an existing dir like real `mv` |
| `reindex [root]` | One indexed filesystem scan — relocates every stale entry and re-anchors `orig_path` to the archive root |
| `tag <add\|rm\|list> <pattern> <tagname>` | Bulk-tag entries by regex match against their archive path |
| `repo <add\|list\|verify\|update\|outdated\|update-all\|tag\|verify-tag\|qr>` | Track a git repo's HEAD and (optionally) a GPG-signed release tag in the archive; `outdated` (alias `o`) checks every tracked repo against its remote, `update-all` (alias `ua`) pulls all of them |
| `backfill [--workers N]` | Store archive copies for entries on disk but not yet uploaded to the active backend — also the migration path when switching backends (e.g. local → S3). N concurrent (default 8, or `OTT_BACKFILL_WORKERS`); live progress bar on a TTY, one line per file when piped |
| `verify-objects [--workers N]` | Audit every archived entry against the active backend (existence + size) — for S3 this always queries the bucket directly, bypassing the local cache, so it's the real answer to "did this actually upload" |
| `bump-fee --wif <WIF> [--fee SAT_PER_VBYTE] [--network testnet\|mainnet]` | CPFP fee bump for a stuck `broadcast` tx — spends the pending change output at a higher fee so miners confirm both together (not RBF). Default 10 sat/vbyte |
| `verify-chain [-c]` | Verify every ledger commit against real Bitcoin (refetches each block's actual hash) — `-c` also checks the OP_RETURN landed |
| `keygen [--network]` | Generate a wallet key for `broadcast` (prints WIF + a QR of the address only, never the key) |
| `broadcast [--wif]` | Broadcast a commitment as a Bitcoin OP_RETURN tx |
| `qr [hash\|file]` | QR code for a hash, a file's SHA256, or the current Merkle root |
| `!<cmd>` | Run a real shell command without leaving the shell |

Entries missing at their last known path are hidden by default in `l`/`tree`
(`-a` shows them); `ott reindex` is the real fix.

`add` only stages — nothing's copied into object storage or written to the
manifest until `commit`/`sync`. Names/hashes resolve relative to the current
archive directory first (`cd`-aware), only falling back to a global search
if nothing matches locally.

**Via Makefile:**

```bash
make add FILE=photo.jpg
make verify-file FILE=photo.jpg
make ott-status
make ott-list
make ott-commit
make ott-clean      # remove manifest + ledger
```

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `OTT_MANIFEST` | `ott_manifest.jsonl` | Manifest path |
| `OTT_LEDGER` | `ott_ledger.jsonl` | Ledger path |
| `OTT_CHUNK_BYTES` | `262144` (256 KB) | Video chunk size |
| `OTT_HOME` | `~/.ott` | Archive root when not using a local `.ott/` dir |
| `OTT_BACKEND` | `local` | Storage backend — `local` or `s3` (needs `pip install btcvm[s3]`) |
| `OTT_S3_BUCKET` | — | Required when `OTT_BACKEND=s3` |
| `OTT_S3_PREFIX` | `''` | Optional key prefix within the bucket |
| `OTT_S3_CACHE_DIR` | `.ott/cache` | Local cache dir for S3-backed objects |
| `OTT_BACKFILL_WORKERS` | `8` | Default concurrency for `backfill`/`verify-objects` |

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

**ott ledger entry:**
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

Installs Bash and Zsh completions for both `btcvm` and `ott`:
- `btcvm` — flags and values (`--vdf-ticks`, `--vms`, etc.)
- `ott add` — any file
- `ott verify` — filenames from the manifest
- `ott verify-chunk` — video names, then chunk indices from the manifest

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
make ott-status   show archive status
make ott-list     list archived files
make ott-commit   commit Merkle root
make ott-clean    remove manifest + ledger
make ott-repo-add                 update archived repo record to HEAD
make ott-tag [OTT_NEXT_TAG=v1.x]  sign a git tag + record fingerprint in ott
make ott-push [OTT_NEXT_TAG=v1.x] push commits + tag, then commit ott root
make ott-snapshot                 repo-add + commit root (no tag, no push)
make ott-release [OTT_NEXT_TAG=v1.x] full: tag → push → commit root
make add FILE=…     add file to ott
make verify-file FILE=…  verify inclusion
make completion     install shell completions
```

## Roadmap

- ✅ **v1** — Bitcoin-clocked register machine, local ledger, optional OP_RETURN
- ✅ **v1.1** — Ledger verification against block hashes and OP_RETURN
- ✅ **v1.2** — VDF sub-clock (`--vdf-ticks N`)
- ✅ **v2** — Trace commitment / Merkle root (`--trace`)
- ✅ **v3** — Fleet Merkle: N parallel VMs, single OP_RETURN (`--vms N`)
- ✅ **ott** — Bitcoin-anchored media archive with chunked video and inclusion proofs

## Tests

```bash
make test
# or
python3 -m pytest -v
```

## License

MIT
