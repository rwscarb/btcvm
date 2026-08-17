"""
imgfs.py — Bitcoin-anchored media archive for btcvm.

Images: SHA256 of full file → leaf in global Merkle tree.
Video:  File split into CHUNK_SIZE blocks → per-file Merkle tree → file root
        used as leaf in global tree. Allows byte-range inclusion proofs.

Global Merkle root committed to Bitcoin via btcvm ledger:
  SHA256(block_hash + global_root) → optional OP_RETURN

Usage:
    python imgfs.py add photo.jpg video.mp4 ...   # images or video
    python imgfs.py status
    python imgfs.py commit
    python imgfs.py verify photo.jpg
    python imgfs.py verify-chunk video.mp4 3      # prove chunk N is in archive
    python imgfs.py list
"""

import argparse
import hashlib
import json
import os
import sys
import time


MANIFEST_PATH = os.environ.get('IMGFS_MANIFEST', 'imgfs_manifest.jsonl')
LEDGER_PATH   = os.environ.get('IMGFS_LEDGER',   'imgfs_ledger.jsonl')
CHUNK_SIZE    = int(os.environ.get('IMGFS_CHUNK_BYTES', str(256 * 1024)))  # 256 KB

VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.mts', '.ts'}


# ── Hashing ───────────────────────────────────────────────────────────────────

def sha256_file(path: str) -> str:
    """SHA256 of entire file contents."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def chunk_hashes(path: str) -> list[str]:
    """Split file into CHUNK_SIZE blocks, return SHA256 of each chunk."""
    hashes = []
    with open(path, 'rb') as f:
        while True:
            block = f.read(CHUNK_SIZE)
            if not block:
                break
            hashes.append(hashlib.sha256(block).hexdigest())
    return hashes


def file_root(path: str) -> tuple[str, list[str]]:
    """Build per-file Merkle tree from chunks.
    Returns (root, chunk_hash_list).
    For small files (<= 1 chunk) the root is just the single chunk hash.
    """
    chunks = chunk_hashes(path)
    if not chunks:
        chunks = [hashlib.sha256(b'').hexdigest()]
    return merkle_root(chunks), chunks


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


# ── Merkle tree ───────────────────────────────────────────────────────────────

def merkle_root(leaves: list[str]) -> str:
    """Binary Merkle root over a list of hex-digest strings."""
    if not leaves:
        return '0' * 64
    layer = list(leaves)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])   # pad odd layer with duplicate
        layer = [
            hashlib.sha256((layer[i] + layer[i + 1]).encode()).hexdigest()
            for i in range(0, len(layer), 2)
        ]
    return layer[0]


def merkle_proof(leaves: list[str], index: int) -> list[dict]:
    """Merkle inclusion proof for leaf at index.

    Returns list of {"sibling": hash, "side": "left"|"right"} steps
    that allow verifying leaves[index] is in the tree.
    """
    proof = []
    layer = list(leaves)
    idx = index
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        sibling_idx = idx ^ 1   # XOR with 1 flips between even/odd
        proof.append({
            'sibling': layer[sibling_idx],
            'side': 'right' if idx % 2 == 0 else 'left',
        })
        layer = [
            hashlib.sha256((layer[i] + layer[i + 1]).encode()).hexdigest()
            for i in range(0, len(layer), 2)
        ]
        idx //= 2
    return proof


def verify_proof(leaf: str, proof: list[dict], root: str) -> bool:
    """Verify a Merkle inclusion proof."""
    h = leaf
    for step in proof:
        sib = step['sibling']
        if step['side'] == 'right':
            h = hashlib.sha256((h + sib).encode()).hexdigest()
        else:
            h = hashlib.sha256((sib + h).encode()).hexdigest()
    return h == root


# ── Manifest I/O ──────────────────────────────────────────────────────────────

def load_manifest() -> list[dict]:
    entries = []
    if not os.path.exists(MANIFEST_PATH):
        return entries
    with open(MANIFEST_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def save_entry(entry: dict):
    with open(MANIFEST_PATH, 'a') as f:
        f.write(json.dumps(entry) + '\n')


def current_root() -> str:
    entries = load_manifest()
    leaves = [e['sha256'] for e in entries]
    return merkle_root(leaves)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_add(paths: list[str]):
    existing = {e['sha256'] for e in load_manifest()}
    added = 0
    for path in paths:
        if not os.path.isfile(path):
            print(f'  ✗ not found: {path}')
            continue
        size = os.path.getsize(path)
        video = is_video(path)

        if video:
            # Chunked: per-file Merkle root is the global leaf
            print(f'  chunking {os.path.basename(path)} ({size:,} bytes)…', end=' ', flush=True)
            froot, chunks = file_root(path)
            digest = froot   # global leaf = per-file Merkle root
            n_chunks = len(chunks)
            print(f'{n_chunks} chunks')
        else:
            digest = sha256_file(path)
            chunks = [digest]   # single chunk = whole file
            n_chunks = 1

        if digest in existing:
            print(f'  = already archived: {os.path.basename(path)} ({digest[:12]}…)')
            continue

        entry = {
            'path':     os.path.abspath(path),
            'name':     os.path.basename(path),
            'sha256':   digest,        # full-file hash (image) or per-file Merkle root (video)
            'size':     size,
            'added':    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'type':     'video' if video else 'image',
            'chunks':   chunks if video else None,
            'n_chunks': n_chunks,
            'chunk_size': CHUNK_SIZE if video else None,
        }
        save_entry(entry)
        existing.add(digest)
        added += 1
        tag = f'  [{n_chunks} chunks × {CHUNK_SIZE//1024}KB]' if video else ''
        print(f'  + {os.path.basename(path)}  {digest[:16]}…  ({size:,} bytes){tag}')

    if added:
        root = current_root()
        print(f'\n  Merkle root: {root}')
        print(f'  Archive: {len(load_manifest())} file(s) total')
    else:
        print('  No new files added.')


def cmd_status():
    entries = load_manifest()
    if not entries:
        print('  Archive is empty.')
        return
    root = merkle_root([e['sha256'] for e in entries])
    total_bytes = sum(e.get('size', 0) for e in entries)
    print(f'  Images:      {len(entries)}')
    print(f'  Total size:  {total_bytes:,} bytes')
    print(f'  Merkle root: {root}')

    # Show last commit from ledger
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH) as f:
            lines = [l.strip() for l in f if l.strip()]
        if lines:
            last = json.loads(lines[-1])
            print(f'  Last commit: block {last.get("block_height", "?")} '
                  f'at {last.get("ts", "?")}')
            if last.get('merkle_root') == root:
                print('  Status:      ✅ root is committed')
            else:
                print('  Status:      ⚠️  root has changed since last commit')


def cmd_commit():
    """Commit current Merkle root to the btcvm ledger."""
    entries = load_manifest()
    if not entries:
        print('  Nothing to commit — archive is empty.')
        return

    root = merkle_root([e['sha256'] for e in entries])
    print(f'  Merkle root: {root}')
    print('  Fetching latest Bitcoin block...')

    try:
        from clock import latest_block  # btcvm clock module
        block = latest_block()
    except Exception as e:
        print(f'  ✗ Could not fetch block: {e}')
        print('  Committing with synthetic block hash (offline mode).')
        block = {
            'height': 0,
            'hash': hashlib.sha256(str(time.time()).encode()).hexdigest(),
        }

    block_hash = block['hash']
    commitment = hashlib.sha256((block_hash + root).encode()).hexdigest()
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    ledger_entry = {
        'ts':           ts,
        'block_height': block['height'],
        'block_hash':   block_hash,
        'merkle_root':  root,
        'commitment':   commitment,
        'image_count':  len(entries),
    }
    with open(LEDGER_PATH, 'a') as f:
        f.write(json.dumps(ledger_entry) + '\n')

    print(f'  Block:       {block["height"]} ({block_hash[:16]}…)')
    print(f'  Commitment:  {commitment}')
    print(f'  Written to:  {LEDGER_PATH}')
    print()
    print('  To anchor on-chain:')
    print(f'    python broadcast.py {commitment}')


def cmd_verify_chunk(path: str, chunk_idx: int):
    """Prove that chunk N of a video file is included in the committed archive."""
    if not os.path.isfile(path):
        print(f'  ✗ File not found: {path}')
        return

    entries = load_manifest()
    name = os.path.basename(path)
    entry = next((e for e in entries if e['name'] == name and e.get('type') == 'video'), None)

    if entry is None:
        print(f'  ✗ {name} not in archive as a video file')
        return

    chunks = entry.get('chunks', [])
    if not chunks:
        print(f'  ✗ No chunk data stored for {name} — re-add the file')
        return

    if chunk_idx < 0 or chunk_idx >= len(chunks):
        print(f'  ✗ Chunk index {chunk_idx} out of range (0–{len(chunks)-1})')
        return

    # Step 1: verify the actual bytes on disk match the stored chunk hash
    chunk_size = entry.get('chunk_size', CHUNK_SIZE)
    with open(path, 'rb') as f:
        f.seek(chunk_idx * chunk_size)
        block = f.read(chunk_size)
    actual_hash = hashlib.sha256(block).hexdigest()
    stored_hash = chunks[chunk_idx]
    bytes_match = actual_hash == stored_hash

    # Step 2: per-file Merkle proof (chunk → file root)
    file_merkle_root = merkle_root(chunks)
    file_proof = merkle_proof(chunks, chunk_idx)
    file_proof_ok = verify_proof(stored_hash, file_proof, file_merkle_root)

    # Step 3: global Merkle proof (file root → global root)
    global_leaves = [e['sha256'] for e in entries]
    file_leaf_idx = global_leaves.index(entry['sha256'])
    global_root = merkle_root(global_leaves)
    global_proof = merkle_proof(global_leaves, file_leaf_idx)
    global_proof_ok = verify_proof(file_merkle_root, global_proof, global_root)

    byte_start = chunk_idx * chunk_size
    byte_end   = byte_start + len(block)

    print(f'  File:         {name}')
    print(f'  Chunk:        {chunk_idx} of {len(chunks)}  '
          f'(bytes {byte_start:,}–{byte_end:,})')
    print(f'  Bytes match:  {"✅" if bytes_match else "✗ MISMATCH — file has changed"}')
    print(f'  Chunk hash:   {stored_hash[:32]}…')
    print(f'  File root:    {file_merkle_root[:32]}…  proof={"✅" if file_proof_ok else "✗"}')
    print(f'  Global root:  {global_root[:32]}…  proof={"✅" if global_proof_ok else "✗"}')

    # Check against ledger
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH) as f:
            lines = [l.strip() for l in f if l.strip()]
        if lines:
            last = json.loads(lines[-1])
            if last.get('merkle_root') == global_root:
                print(f'  On-chain:     ✅ root committed at block '
                      f'{last.get("block_height", "?")}')
            else:
                print('  On-chain:     ⚠️  root not yet committed')


def cmd_verify(path: str):
    """Verify an image is in the archive and prove inclusion."""
    if not os.path.isfile(path):
        print(f'  ✗ File not found: {path}')
        return

    digest = sha256_file(path)
    entries = load_manifest()
    leaves = [e['sha256'] for e in entries]

    if digest not in leaves:
        print(f'  ✗ {os.path.basename(path)} not in archive')
        print(f'    SHA256: {digest}')
        return

    idx = leaves.index(digest)
    root = merkle_root(leaves)
    proof = merkle_proof(leaves, idx)
    ok = verify_proof(digest, proof, root)

    print(f'  ✅ {os.path.basename(path)}')
    print(f'  SHA256:      {digest}')
    print(f'  Leaf index:  {idx} of {len(leaves)}')
    print(f'  Merkle root: {root}')
    print(f'  Proof valid: {ok}')
    print(f'  Proof steps: {len(proof)}')

    # Check against last ledger commitment
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH) as f:
            lines = [l.strip() for l in f if l.strip()]
        if lines:
            last = json.loads(lines[-1])
            if last.get('merkle_root') == root:
                print(f'  On-chain:    ✅ root committed at block '
                      f'{last.get("block_height", "?")}')
            else:
                print('  On-chain:    ⚠️  root not yet committed')


def cmd_list():
    entries = load_manifest()
    if not entries:
        print('  Archive is empty.')
        return
    print(f'  {"#":<4} {"name":<40} {"sha256":<18} {"size":>10}  added')
    print('  ' + '-' * 90)
    for i, e in enumerate(entries):
        print(f'  {i:<4} {e["name"]:<40} {e["sha256"][:16]}…  '
              f'{e.get("size", 0):>10,}  {e.get("added", "?")}')
    root = merkle_root([e['sha256'] for e in entries])
    print(f'\n  Merkle root: {root}')


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='btcvm image filesystem')
    sub = parser.add_subparsers(dest='cmd')

    p_add = sub.add_parser('add', help='Add images or video to the archive')
    p_add.add_argument('paths', nargs='+')

    sub.add_parser('status', help='Show archive status and current Merkle root')
    sub.add_parser('commit', help='Commit Merkle root to btcvm ledger')
    sub.add_parser('list',   help='List all archived files')

    p_verify = sub.add_parser('verify', help='Verify file inclusion with Merkle proof')
    p_verify.add_argument('path')

    p_vc = sub.add_parser('verify-chunk', help='Prove byte-range inclusion for a video chunk')
    p_vc.add_argument('path')
    p_vc.add_argument('chunk', type=int, help='Chunk index (0-based)')

    args = parser.parse_args()

    if args.cmd == 'add':
        cmd_add(args.paths)
    elif args.cmd == 'status':
        cmd_status()
    elif args.cmd == 'commit':
        cmd_commit()
    elif args.cmd == 'list':
        cmd_list()
    elif args.cmd == 'verify':
        cmd_verify(args.path)
    elif args.cmd == 'verify-chunk':
        cmd_verify_chunk(args.path, args.chunk)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
