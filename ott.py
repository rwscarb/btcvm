"""
ott.py — Bitcoin-anchored media archive for btcvm.

Metadata lives in .ott/ (like .git) — archive travels with your files,
works from any subdirectory, and survives file moves gracefully.

    .ott/
      config          # JSON: chunk_size, created, version
      manifest.jsonl  # per-file records; last-write-wins on sha256
      ledger.jsonl    # Bitcoin commitments
      chunks/         # <file_root_hash>.json — chunk lists (video only)

Commands:
    ott init                         create .ott/ in current directory
    ott init --migrate               init + import old ott_manifest.jsonl
    ott add photo.jpg video.mp4      add files (images or video)
    ott status                       current state + Merkle root
    ott list                         list archived files
    ott commit                       commit Merkle root to Bitcoin ledger
    ott verify photo.jpg             Merkle inclusion proof
    ott verify-chunk video.mp4 3     byte-range inclusion proof (video)
    ott find photo.jpg               locate file if it moved; update record
    ott mv photo.jpg /new/path.jpg   update path record
    ott qr                           QR code of current Merkle root
    ott                              interactive shell (all commands + aliases)
"""

import argparse
import cmd
import hashlib
import json
import os
import shlex
import sys
import time

try:
    import qrcode  # pip install qrcode
    _HAS_QR = True
except ImportError:
    _HAS_QR = False

CHUNK_SIZE_DEFAULT = 256 * 1024  # 256 KB
VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.mts', '.ts', '.vob', '.mpg', '.mpeg', '.m2ts', '.wmv', '.flv', '.ogv'}


# ── Exceptions ────────────────────────────────────────────────────────────────

class OttError(Exception):
    pass

class OttNotFoundError(OttError):
    """No .ott/ directory found in this tree."""
    pass


# ── Hashing ───────────────────────────────────────────────────────────────────

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def chunk_hashes(path: str, chunk_size: int) -> list[str]:
    hashes = []
    with open(path, 'rb') as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            hashes.append(hashlib.sha256(block).hexdigest())
    return hashes


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


# ── Merkle tree ───────────────────────────────────────────────────────────────

def merkle_root(leaves: list[str]) -> str:
    if not leaves:
        return '0' * 64
    layer = list(leaves)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [
            hashlib.sha256((layer[i] + layer[i + 1]).encode()).hexdigest()
            for i in range(0, len(layer), 2)
        ]
    return layer[0]


def merkle_proof(leaves: list[str], index: int) -> list[dict]:
    proof = []
    layer = list(leaves)
    idx = index
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        sibling_idx = idx ^ 1
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
    h = leaf
    for step in proof:
        sib = step['sibling']
        if step['side'] == 'right':
            h = hashlib.sha256((h + sib).encode()).hexdigest()
        else:
            h = hashlib.sha256((sib + h).encode()).hexdigest()
    return h == root


# ── .ott/ store ───────────────────────────────────────────────────────────────

def find_ott_dir(start: str | None = None) -> str | None:
    """Walk up from start (default: cwd) looking for .ott/."""
    cur = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(cur, '.ott')
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(cur)
        if parent == cur:  # filesystem root
            return None
        cur = parent


class OttStore:
    def __init__(self, ott_dir: str):
        self.dir           = ott_dir
        self.root_dir      = os.path.dirname(ott_dir)
        self.manifest_path = os.path.join(ott_dir, 'manifest.jsonl')
        self.ledger_path   = os.path.join(ott_dir, 'ledger.jsonl')
        self.config_path   = os.path.join(ott_dir, 'config')
        self.chunks_dir    = os.path.join(ott_dir, 'chunks')

    @classmethod
    def init(cls, path: str = '.') -> 'OttStore':
        ott_dir = os.path.join(os.path.abspath(path), '.ott')
        if os.path.exists(ott_dir):
            raise OttError(f'.ott/ already exists at {ott_dir}')
        os.makedirs(os.path.join(ott_dir, 'chunks'))
        cfg = {
            'version':    1,
            'created':    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'chunk_size': CHUNK_SIZE_DEFAULT,
        }
        with open(os.path.join(ott_dir, 'config'), 'w') as f:
            json.dump(cfg, f, indent=2)
        return cls(ott_dir)

    def config(self) -> dict:
        if not os.path.exists(self.config_path):
            return {'chunk_size': CHUNK_SIZE_DEFAULT, 'version': 1}
        with open(self.config_path) as f:
            return json.load(f)

    @property
    def chunk_size(self) -> int:
        return int(os.environ.get('OTT_CHUNK_BYTES',
                                  self.config().get('chunk_size', CHUNK_SIZE_DEFAULT)))

    def load_manifest(self) -> list[dict]:
        """Load entries; last write per sha256 wins (append = update).
        Repos additionally deduplicate by name — a repo's identity is its
        name, not its commit hash, so re-adding at a new commit replaces.
        """
        if not os.path.exists(self.manifest_path):
            return []
        by_hash: dict[str, dict] = {}
        by_repo_name: dict[str, str] = {}  # repo name → sha256 of latest entry
        with open(self.manifest_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        e = json.loads(line)
                        sha = e['sha256']
                        by_hash[sha] = e
                        if e.get('type') == 'repo':
                            # evict old sha256 for this repo name
                            old_sha = by_repo_name.get(e['name'])
                            if old_sha and old_sha != sha:
                                by_hash.pop(old_sha, None)
                            by_repo_name[e['name']] = sha
                    except (json.JSONDecodeError, KeyError):
                        pass
        return list(by_hash.values())

    def save_entry(self, entry: dict):
        with open(self.manifest_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    def update_entry(self, sha256: str, updates: dict):
        """Append an updated version of an entry (last-write-wins on load)."""
        entries = self.load_manifest()
        entry = next((e for e in entries if e['sha256'] == sha256), None)
        if entry is None:
            raise OttError(f'Entry not found: {sha256[:16]}…')
        entry.update(updates)
        self.save_entry(entry)

    def load_chunks(self, file_hash: str) -> list[str]:
        path = os.path.join(self.chunks_dir, f'{file_hash}.json')
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return json.load(f)

    def save_chunks(self, file_hash: str, chunks: list[str]):
        os.makedirs(self.chunks_dir, exist_ok=True)
        with open(os.path.join(self.chunks_dir, f'{file_hash}.json'), 'w') as f:
            json.dump(chunks, f)

    def load_ledger(self) -> list[dict]:
        if not os.path.exists(self.ledger_path):
            return []
        out = []
        with open(self.ledger_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return out

    def append_ledger(self, entry: dict):
        with open(self.ledger_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    def current_root(self) -> str:
        return merkle_root([e['sha256'] for e in self.load_manifest()])


# Module-level store cache
_store: OttStore | None = None


def get_store() -> OttStore:
    global _store
    if _store is not None:
        return _store
    ott_dir = find_ott_dir()
    if ott_dir is None:
        raise OttNotFoundError(
            'No .ott/ archive found in this directory tree.\n'
            '  Run: ott init'
        )
    _store = OttStore(ott_dir)
    return _store


def _reset_store():
    global _store
    _store = None


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_init(path: str = '.', migrate: bool = False):
    store = OttStore.init(path)
    _reset_store()
    print(f'  ✅ Initialized .ott/ at {store.dir}')
    if migrate:
        _do_migrate(store, os.path.abspath(path))
    else:
        # Auto-detect old flat files and offer migration hint
        for old in ('ott_manifest.jsonl', 'imgfs_manifest.jsonl'):
            if os.path.exists(os.path.join(os.path.abspath(path), old)):
                print(f'  ℹ️  Found {old} — run `ott init --migrate` to import it')
                break


def _do_migrate(store: OttStore, path: str):
    """Import old flat manifest/ledger into the new .ott/ store."""
    migrated = 0
    for old_name in ('ott_manifest.jsonl', 'imgfs_manifest.jsonl'):
        old_path = os.path.join(path, old_name)
        if not os.path.exists(old_path):
            continue
        print(f'  Migrating {old_name}…')
        with open(old_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    # Extract chunks into separate file
                    chunks = e.pop('chunks', None)
                    if chunks:
                        store.save_chunks(e['sha256'], chunks)
                    # Rename 'path' → 'last_path'
                    if 'path' in e and 'last_path' not in e:
                        e['last_path'] = e.pop('path')
                    store.save_entry(e)
                    migrated += 1
                except (json.JSONDecodeError, KeyError):
                    pass
        print(f'    ✅ {migrated} entries imported')
        break

    for old_name in ('ott_ledger.jsonl', 'imgfs_ledger.jsonl'):
        old_path = os.path.join(path, old_name)
        if not os.path.exists(old_path):
            continue
        print(f'  Migrating {old_name}…')
        count = 0
        with open(old_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        store.append_ledger(json.loads(line))
                        count += 1
                    except json.JSONDecodeError:
                        pass
        print(f'    ✅ {count} ledger entries imported')
        break


def cmd_add(paths: list[str]):
    store = get_store()
    existing = {e['sha256'] for e in store.load_manifest()}
    chunk_size = store.chunk_size
    added = 0

    for path in paths:
        if not os.path.isfile(path):
            print(f'  ✗ not found: {path}')
            continue
        size = os.path.getsize(path)
        video = is_video(path)
        abs_path = os.path.abspath(path)

        if video:
            print(f'  chunking {os.path.basename(path)} ({size:,} bytes)…', end=' ', flush=True)
            chunks = chunk_hashes(path, chunk_size)
            if not chunks:
                chunks = [hashlib.sha256(b'').hexdigest()]
            digest = merkle_root(chunks)
            n_chunks = len(chunks)
            print(f'{n_chunks} chunks')
        else:
            digest = sha256_file(path)
            chunks = None
            n_chunks = 1

        if digest in existing:
            print(f'  = already archived: {os.path.basename(path)} ({digest[:12]}…)')
            continue

        entry = {
            'sha256':     digest,
            'name':       os.path.basename(path),
            'last_path':  abs_path,
            'size':       size,
            'added':      time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'type':       'video' if video else 'image',
            'n_chunks':   n_chunks,
            'chunk_size': chunk_size if video else None,
        }
        store.save_entry(entry)
        if video and chunks:
            store.save_chunks(digest, chunks)
        existing.add(digest)
        added += 1
        tag = f'  [{n_chunks} chunks × {chunk_size // 1024}KB]' if video else ''
        print(f'  + {os.path.basename(path)}  {digest[:16]}…  ({size:,} bytes){tag}')

    if added:
        root = store.current_root()
        print(f'\n  Merkle root: {root}')
        print(f'  Archive: {len(store.load_manifest())} file(s) total  (.ott/ at {store.dir})')
    else:
        print('  No new files added.')


def cmd_status():
    store = get_store()
    entries = store.load_manifest()
    print(f'  Archive:     {store.dir}')
    if not entries:
        print('  Empty — no files archived yet.')
        return
    root = store.current_root()
    total_bytes = sum(e.get('size', 0) for e in entries)
    n_images = sum(1 for e in entries if e.get('type') != 'video')
    n_videos = sum(1 for e in entries if e.get('type') == 'video')
    n_missing = sum(1 for e in entries if not os.path.isfile(e.get('last_path', '')))
    print(f'  Files:       {len(entries)}  ({n_images} images, {n_videos} videos)')
    if n_missing:
        print(f'  Missing:     {n_missing} file(s) not found at last_path  (run ott find)')
    print(f'  Total size:  {total_bytes:,} bytes')
    print(f'  Merkle root: {root}')
    ledger = store.load_ledger()
    if ledger:
        last = ledger[-1]
        print(f'  Last commit: block {last.get("block_height", "?")} at {last.get("ts", "?")}')
        if last.get('merkle_root') == root:
            print('  Status:      ✅ root is committed')
        else:
            print('  Status:      ⚠️  uncommitted changes since last commit')
    else:
        print('  Status:      not yet committed to Bitcoin')


def cmd_list():
    store = get_store()
    entries = store.load_manifest()
    if not entries:
        print('  Archive is empty.')
        return
    print(f'  {"#":<4} {"T":<2} {"name":<36} {"sha256":<18} {"size":>10}  {"  "}')
    print('  ' + '-' * 88)
    for i, e in enumerate(entries):
        etype = e.get('type', 'image')
        t = {'video': 'V', 'repo': 'R'}.get(etype, 'I')
        is_repo = etype == 'repo'
        path_ok = (os.path.isdir if is_repo else os.path.isfile)(e.get('last_path', ''))
        ok = '✅' if path_ok else '⚠️ '
        print(f'  {i:<4} {t:<2} {e["name"]:<36} {e["sha256"][:16]}…  '
              f'{e.get("size", 0):>10,}  {ok}')
    print(f'\n  Merkle root: {store.current_root()}')
    print('  T: I=image V=video R=repo  ✅=at last_path  ⚠️ =path missing (run ott find)')


def cmd_commit():
    store = get_store()
    entries = store.load_manifest()
    if not entries:
        print('  Nothing to commit — archive is empty.')
        return

    root = store.current_root()
    print(f'  Merkle root: {root}')
    print('  Fetching latest Bitcoin block…')

    try:
        from clock import get_tip
        height, block_hash = get_tip()
        if height is None:
            raise RuntimeError('get_tip returned None — network unreachable?')
        block = {'height': height, 'hash': block_hash}
    except Exception as e:
        print(f'  ✗ Could not fetch block: {e}')
        print('  Committing with synthetic block hash (offline mode).')
        block = {'height': 0, 'hash': hashlib.sha256(str(time.time()).encode()).hexdigest()}

    block_hash = block['hash']
    commitment = hashlib.sha256((block_hash + root).encode()).hexdigest()
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    store.append_ledger({
        'ts':           ts,
        'block_height': block['height'],
        'block_hash':   block_hash,
        'merkle_root':  root,
        'commitment':   commitment,
        'file_count':   len(entries),
    })

    print(f'  Block:       {block["height"]} ({block_hash[:16]}…)')
    print(f'  Commitment:  {commitment}')
    print(f'  Ledger:      {store.ledger_path}')
    print()
    print('  To anchor on-chain:')
    print(f'    python broadcast.py {commitment}')
    print()
    cmd_qr(commitment, label='commitment QR')


def cmd_verify(path_or_name: str):
    store = get_store()
    entries = store.load_manifest()
    leaves = [e['sha256'] for e in entries]
    abs_path = os.path.abspath(path_or_name)
    name = os.path.basename(path_or_name)

    if os.path.isfile(abs_path):
        digest = sha256_file(abs_path)
        source = 'live file'
    else:
        entry = next((e for e in entries if e['name'] == name), None)
        if entry is None:
            print(f'  ✗ {name} not in archive and not found on disk')
            return
        digest = entry['sha256']
        source = f'manifest only (file not found at {entry.get("last_path", "?")})'
        print('  ⚠️  File not at last known path — proof uses stored hash')
        print(f'     Run `ott find {name}` to locate it and update the record')

    if digest not in leaves:
        print(f'  ✗ {name} not in archive  (SHA256: {digest})')
        return

    # Update last_path if file was found
    if os.path.isfile(abs_path):
        entry = next(e for e in entries if e['sha256'] == digest)
        if entry.get('last_path') != abs_path:
            store.update_entry(digest, {'last_path': abs_path})

    idx = leaves.index(digest)
    root = merkle_root(leaves)
    proof = merkle_proof(leaves, idx)
    ok = verify_proof(digest, proof, root)

    print(f'  ✅ {name}  ({source})')
    print(f'  SHA256:      {digest}')
    print(f'  Leaf index:  {idx} of {len(leaves)}')
    print(f'  Merkle root: {root}')
    print(f'  Proof:       {"✅ valid" if ok else "✗ invalid"}  ({len(proof)} steps)')

    ledger = store.load_ledger()
    if ledger:
        last = ledger[-1]
        if last.get('merkle_root') == root:
            print(f'  On-chain:    ✅ root committed at block {last.get("block_height", "?")}')
        else:
            print('  On-chain:    ⚠️  root not yet committed')


def cmd_verify_chunk(path_or_name: str, chunk_idx: int):
    store = get_store()
    entries = store.load_manifest()
    name = os.path.basename(path_or_name)
    entry = next((e for e in entries if e['name'] == name and e.get('type') == 'video'), None)

    if entry is None:
        print(f'  ✗ {name} not in archive as a video file')
        return

    chunks = store.load_chunks(entry['sha256'])
    if not chunks:
        print(f'  ✗ No chunk data for {name} — re-add the file')
        return

    if chunk_idx < 0 or chunk_idx >= len(chunks):
        print(f'  ✗ Chunk {chunk_idx} out of range (0–{len(chunks) - 1})')
        return

    stored_hash = chunks[chunk_idx]
    chunk_size = entry.get('chunk_size', store.chunk_size)
    byte_start = chunk_idx * chunk_size
    bytes_match = None

    disk_path = entry.get('last_path', path_or_name)
    if os.path.isfile(disk_path):
        with open(disk_path, 'rb') as f:
            f.seek(byte_start)
            block = f.read(chunk_size)
        bytes_match = hashlib.sha256(block).hexdigest() == stored_hash
        byte_end = byte_start + len(block)
    else:
        print(f'  ⚠️  File not found at {disk_path} — verifying stored proof only')
        byte_end = byte_start + chunk_size

    file_root_val = merkle_root(chunks)
    file_proof = merkle_proof(chunks, chunk_idx)
    file_ok = verify_proof(stored_hash, file_proof, file_root_val)

    global_leaves = [e['sha256'] for e in entries]
    file_leaf_idx = global_leaves.index(entry['sha256'])
    global_root_val = merkle_root(global_leaves)
    global_proof = merkle_proof(global_leaves, file_leaf_idx)
    global_ok = verify_proof(file_root_val, global_proof, global_root_val)

    print(f'  File:         {name}')
    print(f'  Chunk:        {chunk_idx} of {len(chunks)}  (bytes {byte_start:,}–{byte_end:,})')
    if bytes_match is not None:
        print(f'  Bytes match:  {"✅" if bytes_match else "✗ MISMATCH — file has changed"}')
    print(f'  Chunk hash:   {stored_hash[:32]}…')
    print(f'  File root:    {file_root_val[:32]}…  proof={"✅" if file_ok else "✗"}')
    print(f'  Global root:  {global_root_val[:32]}…  proof={"✅" if global_ok else "✗"}')

    ledger = store.load_ledger()
    if ledger:
        last = ledger[-1]
        if last.get('merkle_root') == global_root_val:
            print(f'  On-chain:     ✅ root committed at block {last.get("block_height", "?")}')
        else:
            print('  On-chain:     ⚠️  root not yet committed')


def cmd_find(name_or_hash: str, search_root: str | None = None):
    """Search filesystem for a file by name or hash prefix; update last_path."""
    store = get_store()
    entries = store.load_manifest()

    entry = next(
        (e for e in entries
         if e['name'] == name_or_hash or e['sha256'].startswith(name_or_hash)),
        None,
    )
    if entry is None:
        print(f'  ✗ {name_or_hash} not in archive')
        return

    root = search_root or store.root_dir
    name = entry['name']
    print(f'  Searching for {name} under {root}…')

    found = []
    is_repo = entry.get('type') == 'repo'
    for dirpath, dirs, files in os.walk(root):
        candidates = []
        if is_repo:
            # repos are directories — check subdirs named `name`
            if name in dirs:
                candidates.append(os.path.join(dirpath, name))
        else:
            if name in files:
                candidates.append(os.path.join(dirpath, name))
        for candidate in candidates:
            try:
                if is_repo:
                    match = os.path.isdir(os.path.join(candidate, '.git'))
                elif entry.get('type') == 'video':
                    chunks = chunk_hashes(candidate, entry.get('chunk_size', store.chunk_size))
                    match = merkle_root(chunks) == entry['sha256']
                else:
                    match = sha256_file(candidate) == entry['sha256']
                if match:
                    found.append(candidate)
            except OSError:
                pass

    if not found:
        print(f'  ✗ Not found under {root}  (name matches but hash differs, or absent)')
        return

    best = found[0]
    store.update_entry(entry['sha256'], {'last_path': best})
    print(f'  ✅ Found: {best}')
    if len(found) > 1:
        print(f'     Also at: {", ".join(found[1:])}')
    print('  Updated last_path in manifest')


def cmd_migrate(path: str | None = None):
    """Import old flat ott_manifest/imgfs_manifest into existing .ott/ store."""
    store = get_store()
    search = os.path.abspath(path or store.root_dir)
    _do_migrate(store, search)


def cmd_mv(name_or_hash: str, new_path: str):
    """Update last_path (and name if basename changed) for a manifest entry."""
    store = get_store()
    entries = store.load_manifest()

    entry = next(
        (e for e in entries
         if e['name'] == name_or_hash or e['sha256'].startswith(name_or_hash)),
        None,
    )
    if entry is None:
        print(f'  ✗ {name_or_hash} not in archive')
        return

    abs_new = os.path.abspath(new_path)
    updates: dict = {'last_path': abs_new}
    new_name = os.path.basename(abs_new)
    if new_name != entry['name']:
        updates['name'] = new_name
        print(f'  Renaming {entry["name"]} → {new_name}')

    store.update_entry(entry['sha256'], updates)
    print(f'  ✅ {entry["name"]} → {abs_new}')


# ── Git / repo ───────────────────────────────────────────────────────────────

def _git(repo_path: str, *args) -> str:
    """Run a git command in repo_path; return stdout stripped. Raises OttError on failure."""
    import subprocess
    result = subprocess.run(
        ['git', '-C', repo_path, *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise OttError(f'git error: {result.stderr.strip()}')
    return result.stdout.strip()


def _git_available() -> bool:
    import shutil
    return shutil.which('git') is not None


def _gpg_available() -> bool:
    import shutil
    return shutil.which('gpg') is not None


def _gpg(repo_path: str, *args) -> str:
    """Run gpg; return stdout. Raises OttError on failure."""
    import subprocess
    result = subprocess.run(
        ['gpg', *args],
        capture_output=True, text=True, cwd=repo_path,
    )
    if result.returncode != 0:
        raise OttError(f'gpg error: {result.stderr.strip()}')
    return result.stdout.strip()


def _gpg_key_fingerprint(key_id: str | None = None) -> tuple[str, str]:
    """Return (fingerprint, uid) for key_id (or default signing key)."""
    import subprocess
    args = ['gpg', '--with-colons', '--fingerprint']
    if key_id:
        args.append(key_id)
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise OttError(f'gpg key lookup failed: {result.stderr.strip()}')
    fingerprint = uid = None
    for line in result.stdout.splitlines():
        parts = line.split(':')
        if parts[0] == 'fpr' and not fingerprint:
            fingerprint = parts[9]
        if parts[0] == 'uid' and not uid:
            uid = parts[9]
    if not fingerprint:
        raise OttError('No GPG key found' + (f' for {key_id}' if key_id else ''))
    return fingerprint, uid or ''


def _git_signing_key(repo_path: str) -> str | None:
    """Return user.signingkey from git config, or None."""
    import subprocess
    result = subprocess.run(
        ['git', '-C', repo_path, 'config', '--get', 'user.signingkey'],
        capture_output=True, text=True,
    )
    return result.stdout.strip() or None


def _git_verify_tag(repo_path: str, tag: str) -> tuple[bool, str]:
    """Verify a signed git tag. Returns (ok, gpg_output)."""
    import subprocess
    result = subprocess.run(
        ['git', '-C', repo_path, 'verify-tag', '--raw', tag],
        capture_output=True, text=True,
    )
    output = result.stderr + result.stdout
    return result.returncode == 0, output


def repo_leaf(git_hash: str) -> str:
    """Global Merkle leaf for a repo = SHA256(git_commit_hash)."""
    return hashlib.sha256(git_hash.encode()).hexdigest()


def cmd_repo_add(repo_path: str, commit: str | None = None):
    """Add or update a git repo in the archive."""
    if not _git_available():
        raise OttError('git not found in PATH')
    store = get_store()
    abs_path = os.path.abspath(repo_path)

    if not os.path.isdir(os.path.join(abs_path, '.git')):
        raise OttError(f'{abs_path} is not a git repository')

    git_hash = _git(abs_path, 'rev-parse', commit or 'HEAD')
    name     = os.path.basename(abs_path)

    try:
        remote = _git(abs_path, 'remote', 'get-url', 'origin')
    except OttError:
        remote = None
    try:
        branch = _git(abs_path, 'branch', '--show-current')
    except OttError:
        branch = None
    try:
        subject = _git(abs_path, 'log', '-1', '--pretty=%s', git_hash)
    except OttError:
        subject = None

    leaf = repo_leaf(git_hash)
    entries = store.load_manifest()
    existing_names = {e['name']: e for e in entries if e.get('type') == 'repo'}

    if name in existing_names and existing_names[name]['sha256'] == leaf:
        print(f'  = already archived: {name} @ {git_hash[:12]}…')
        return

    if name in existing_names:
        # Update: remove old leaf by appending a tombstone-replace
        old = existing_names[name]
        print(f'  ~ updating {name}: {old["git_hash"][:12]}… → {git_hash[:12]}…')

    entry = {
        'sha256':    leaf,
        'name':      name,
        'last_path': abs_path,
        'type':      'repo',
        'git_hash':  git_hash,
        'remote':    remote,
        'branch':    branch,
        'subject':   subject,
        'added':     time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'size':      0,
        'n_chunks':  1,
        'chunk_size': None,
    }
    store.save_entry(entry)
    root = store.current_root()
    print(f'  + {name}  {git_hash[:16]}…')
    if subject:
        print(f'    "{subject}"')
    if remote:
        print(f'    remote: {remote}')
    print(f'\n  Merkle root: {root}')


def cmd_repo_list():
    store = get_store()
    entries = [e for e in store.load_manifest() if e.get('type') == 'repo']
    if not entries:
        print('  No repos archived.')
        return
    print(f'  {"#":<4} {"name":<28} {"commit":<16} {"branch":<16}  remote')
    print('  ' + '-' * 88)
    for i, e in enumerate(entries):
        at_path = os.path.isdir(e.get('last_path', ''))
        ok = '✅' if at_path else '⚠️ '
        branch = e.get('branch') or '—'
        remote = e.get('remote') or '—'
        print(f'  {i:<4} {e["name"]:<28} {e["git_hash"][:14]}…  '
              f'{branch:<16}  {remote}  {ok}')
    print(f'\n  Merkle root: {store.current_root()}')


def cmd_repo_verify(repo_path_or_name: str):
    store = get_store()
    entries = store.load_manifest()
    all_leaves = [e['sha256'] for e in entries]

    # Resolve by name or path
    abs_path = os.path.abspath(repo_path_or_name)
    name = os.path.basename(abs_path)
    entry = next(
        (e for e in entries
         if e.get('type') == 'repo' and (e['name'] == name or e.get('last_path') == abs_path)),
        None,
    )
    if entry is None:
        raise OttError(f'{name} not in archive as a repo')

    stored_git_hash = entry['git_hash']
    stored_leaf     = entry['sha256']

    # Live HEAD check
    live_git_hash = None
    head_matches  = None
    if _git_available() and os.path.isdir(os.path.join(abs_path, '.git')):
        try:
            live_git_hash = _git(abs_path, 'rev-parse', 'HEAD')
            head_matches  = live_git_hash == stored_git_hash
        except OttError:
            pass

    idx   = all_leaves.index(stored_leaf)
    root  = merkle_root(all_leaves)
    proof = merkle_proof(all_leaves, idx)
    ok    = verify_proof(stored_leaf, proof, root)

    print(f'  Repo:        {entry["name"]}')
    print(f'  Archived:    {stored_git_hash}')
    if entry.get('subject'):
        print(f'  Commit msg:  "{entry["subject"]}"')
    if live_git_hash is not None:
        icon = '✅' if head_matches else '⚠️ '
        print(f'  HEAD now:    {live_git_hash}  {icon}')
        if not head_matches:
            print('               (HEAD has moved since archiving — run `ott repo add` to update)')
    print(f'  Leaf:        {stored_leaf[:32]}…  (SHA256 of commit hash)')
    print(f'  Leaf index:  {idx} of {len(all_leaves)}')
    print(f'  Merkle root: {root}')
    print(f'  Proof:       {"✅ valid" if ok else "✗ invalid"}  ({len(proof)} steps)')

    ledger = store.load_ledger()
    if ledger:
        last = ledger[-1]
        if last.get('merkle_root') == root:
            print(f'  On-chain:    ✅ root committed at block {last.get("block_height", "?")}')
        else:
            print('  On-chain:    ⚠️  root not yet committed')


def cmd_repo_tag(repo_path_or_name: str, tag: str, key_id: str | None = None, message: str | None = None):
    """Create a signed git tag and record the GPG fingerprint in the manifest."""
    if not _git_available():
        raise OttError('git not found in PATH')
    if not _gpg_available():
        raise OttError('gpg not found in PATH')

    store = get_store()
    entries = store.load_manifest()
    abs_path = os.path.abspath(repo_path_or_name)
    name = os.path.basename(abs_path)
    entry = next(
        (e for e in entries
         if e.get('type') == 'repo' and (e['name'] == name or e.get('last_path') == abs_path)),
        None,
    )
    if entry is None:
        raise OttError(f'{name} not in archive — run `ott repo add` first')

    if not os.path.isdir(os.path.join(abs_path, '.git')):
        raise OttError(f'{abs_path} is not a git repository')

    # Resolve signing key: explicit arg > git config > gpg default
    signing_key = key_id or _git_signing_key(abs_path)
    fingerprint, uid = _gpg_key_fingerprint(signing_key)

    # Create the signed tag
    tag_msg = message or f'ott: {name} @ {entry["git_hash"][:12]}'
    sign_args = ['tag', '-s', tag, '-m', tag_msg]
    if signing_key:
        sign_args += ['-u', signing_key]
    _git(abs_path, *sign_args)
    print(f'  ✅ Signed tag: {tag}')
    print(f'     Key:   {fingerprint}')
    print(f'     UID:   {uid}')

    # Verify immediately
    ok, gpg_out = _git_verify_tag(abs_path, tag)
    if not ok:
        raise OttError(f'Tag verification failed after signing:\n{gpg_out}')
    print(f'     Verify: ✅')

    # Record in manifest
    updates = {
        'git_tag':         tag,
        'gpg_fingerprint': fingerprint,
        'gpg_uid':         uid,
        'gpg_signed_at':   time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    store.update_entry(entry['sha256'], updates)
    print(f'     Recorded in manifest')
    print(f'\n  To push the tag:')
    print(f'     git push origin {tag}')


def cmd_repo_verify_tag(repo_path_or_name: str, tag: str | None = None):
    """Verify a signed tag (stored or specified) and show full chain."""
    store = get_store()
    entries = store.load_manifest()
    abs_path = os.path.abspath(repo_path_or_name)
    name = os.path.basename(abs_path)
    entry = next(
        (e for e in entries
         if e.get('type') == 'repo' and (e['name'] == name or e.get('last_path') == abs_path)),
        None,
    )
    if entry is None:
        raise OttError(f'{name} not in archive')

    tag_name = tag or entry.get('git_tag')
    if not tag_name:
        raise OttError(f'No tag stored for {name} — run `ott repo tag {name} <tagname>` first')

    if not _git_available():
        raise OttError('git not found in PATH')
    if not os.path.isdir(os.path.join(abs_path, '.git')):
        print(f'  ⚠️  Repo not at {abs_path} — verifying stored fingerprint only')
        tag_ok, gpg_out = False, ''
    else:
        tag_ok, gpg_out = _git_verify_tag(abs_path, tag_name)

    # Parse fingerprint from gpg --status output
    live_fpr = None
    for line in gpg_out.splitlines():
        if '[GNUPG:] VALIDSIG' in line:
            parts = line.split()
            if len(parts) >= 3:
                live_fpr = parts[2]

    stored_fpr = entry.get('gpg_fingerprint')

    # Merkle proof
    all_leaves = [e['sha256'] for e in entries]
    idx = all_leaves.index(entry['sha256'])
    root = merkle_root(all_leaves)
    proof = merkle_proof(all_leaves, idx)
    merkle_ok = verify_proof(entry['sha256'], proof, root)

    print(f'  Repo:        {name}')
    print(f'  Tag:         {tag_name}')
    print(f'  Commit:      {entry["git_hash"]}')
    if tag_ok:
        print(f'  GPG sig:     ✅ valid')
    else:
        print(f'  GPG sig:     ✗ invalid or repo not available')
    if stored_fpr:
        match = live_fpr == stored_fpr if live_fpr else None
        match_icon = '✅' if match else ('⚠️ unverified' if match is None else '✗ MISMATCH')
        print(f'  Fingerprint: {stored_fpr}  {match_icon}')
    if entry.get('gpg_uid'):
        print(f'  Signed by:   {entry["gpg_uid"]}')
    if entry.get('gpg_signed_at'):
        print(f'  Signed at:   {entry["gpg_signed_at"]}')
    print(f'  Merkle root: {root}')
    print(f'  Proof:       {"✅ valid" if merkle_ok else "✗ invalid"}  ({len(proof)} steps)')

    ledger = store.load_ledger()
    if ledger:
        last = ledger[-1]
        if last.get('merkle_root') == root:
            print(f'  On-chain:    ✅ root committed at block {last.get("block_height", "?")}')
        else:
            print('  On-chain:    ⚠️  root not yet committed')

    print()
    print('  Full chain:')
    print(f'    GPG key ({stored_fpr[:16] if stored_fpr else "?"}…)')
    print(f'      ↓ signs')
    print(f'    git tag {tag_name!r} → commit {entry["git_hash"][:16]}…')
    print(f'      ↓ SHA256')
    print(f'    Merkle leaf {entry["sha256"][:16]}…')
    print(f'      ↓ Merkle tree')
    print(f'    Root {root[:16]}…')
    print(f'      ↓ Bitcoin block')
    ledger = store.load_ledger()
    if ledger:
        last = ledger[-1]
        print(f'    Block {last.get("block_height", "?")} commitment {last.get("commitment", "?")[:16]}…')


def cmd_repo_update(repo_path_or_name: str):
    """Re-add repo at current HEAD."""
    store = get_store()
    entries = store.load_manifest()
    abs_path = os.path.abspath(repo_path_or_name)
    name = os.path.basename(abs_path)
    entry = next(
        (e for e in entries
         if e.get('type') == 'repo' and (e['name'] == name or e.get('last_path') == abs_path)),
        None,
    )
    if entry is None:
        raise OttError(f'{name} not in archive — use `ott repo add` first')
    cmd_repo_add(entry.get('last_path', abs_path))


def cmd_repo(subcmd: str, args: list[str]):
    """Dispatch ott repo <subcmd> <args>."""
    if subcmd in ('add', 'a'):
        if not args:
            raise OttError('Usage: repo add <path> [commit]')
        cmd_repo_add(args[0], args[1] if len(args) > 1 else None)
    elif subcmd in ('list', 'ls', 'l'):
        cmd_repo_list()
    elif subcmd in ('verify', 'v'):
        if not args:
            raise OttError('Usage: repo verify <path_or_name>')
        cmd_repo_verify(args[0])
    elif subcmd in ('update', 'up'):
        if not args:
            raise OttError('Usage: repo update <path_or_name>')
        cmd_repo_update(args[0])
    elif subcmd in ('tag', 't'):
        if len(args) < 2:
            raise OttError('Usage: repo tag <path_or_name> <tagname> [key_id] [message]')
        cmd_repo_tag(
            args[0], args[1],
            key_id=args[2] if len(args) > 2 else None,
            message=args[3] if len(args) > 3 else None,
        )
    elif subcmd in ('verify-tag', 'vt'):
        if not args:
            raise OttError('Usage: repo verify-tag <path_or_name> [tagname]')
        cmd_repo_verify_tag(args[0], args[1] if len(args) > 1 else None)
    elif subcmd in ('qr',):
        target = args[0] if args else None
        store = get_store()
        entries = [e for e in store.load_manifest() if e.get('type') == 'repo']
        if target:
            entry = next((e for e in entries if e['name'] == target or
                          e.get('last_path') == os.path.abspath(target)), None)
            if entry is None:
                raise OttError(f'{target} not in archive')
            cmd_qr(entry['git_hash'], label=f'{entry["name"]} commit')
        else:
            cmd_qr(store.current_root(), label='current Merkle root')
    else:
        print('  repo subcommands: add, list, verify, update, qr')


def cmd_qr(data: str, label: str = ''):
    if not _HAS_QR:
        print('  ⚠️  qrcode not installed — pip install qrcode')
        print(f'  Data: {data}')
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(data)
    qr.make(fit=True)
    if label:
        print(f'  ── {label} ──')
    qr.print_ascii(invert=True)
    print(f'  {data}')


# ── Interactive shell ─────────────────────────────────────────────────────────

def _run(fn, *args, **kwargs):
    """Call a cmd_* function, printing errors neatly."""
    try:
        fn(*args, **kwargs)
    except KeyboardInterrupt:
        print('\n  interrupted')
    except OttNotFoundError as e:
        print(f'  ✗ {e}')
    except OttError as e:
        print(f'  ✗ {e}')


class OttShell(cmd.Cmd):
    intro = (
        '\n  ott — Bitcoin-anchored media archive\n'
        '  Type help or ? for commands. Tab completes. Ctrl-D or q to exit.\n'
    )
    prompt = 'ott> '

    def preloop(self):
        try:
            import readline
            # Use only whitespace as delimiters so full paths
            # (~/foo, dir/file, names_with_underscores) reach the completer intact
            readline.set_completer_delims(' \t\n')
        except ImportError:
            pass

    # ── completion helpers ────────────────────────────────────────────────────

    def _manifest_names(self, text: str) -> list[str]:
        try:
            return [e['name'] for e in get_store().load_manifest()
                    if e['name'].startswith(text)]
        except OttNotFoundError:
            return []

    def _video_names(self, text: str) -> list[str]:
        try:
            return [e['name'] for e in get_store().load_manifest()
                    if e.get('type') == 'video' and e['name'].startswith(text)]
        except OttNotFoundError:
            return []

    def _files(self, text: str) -> list[str]:
        import glob
        expanded = os.path.expanduser(text)
        matches = glob.glob(expanded + '*')
        # If ~ was expanded, put it back so readline inserts the right thing
        if text.startswith('~') and not expanded.startswith('~'):
            home = os.path.expanduser('~')
            matches = [('~' + m[len(home):] if m.startswith(home) else m) for m in matches]
        # Wrap in double-quotes if the path contains any shell-special chars.
        # Quoting is more reliable than backslash-escaping across readline builds.
        SPECIALS = set(' ()[]&!\'";,')

        def _quote(s):
            if any(c in s for c in SPECIALS):
                # Escape any embedded double-quotes, then wrap
                return '"' + s.replace('"', '\\"') + '"'
            return s

        out = []
        for m in matches:
            is_dir = os.path.isdir(os.path.expanduser(m)) and not m.endswith('/')
            out.append(_quote(m + '/' if is_dir else m))
        return out

    def complete_add(self, text, line, begidx, endidx):
        return self._files(text)

    def complete_verify(self, text, line, begidx, endidx):
        return self._manifest_names(text) or self._files(text)

    def complete_verify_chunk(self, text, line, begidx, endidx):
        parts = shlex.split(line[:begidx])
        if len(parts) == 1:
            return self._video_names(text) or self._files(text)
        if len(parts) == 2:
            try:
                entry = next((e for e in get_store().load_manifest()
                              if e['name'] == parts[1]), None)
                if entry:
                    return [str(i) for i in range(entry.get('n_chunks', 1))
                            if str(i).startswith(text)]
            except OttNotFoundError:
                pass
        return []

    def complete_find(self, text, line, begidx, endidx):
        return self._manifest_names(text)

    def complete_mv(self, text, line, begidx, endidx):
        parts = shlex.split(line[:begidx])
        if len(parts) == 1:
            return self._manifest_names(text)
        return self._files(text)

    def complete_qr(self, text, line, begidx, endidx):
        return self._manifest_names(text) or self._files(text)

    # ── commands ──────────────────────────────────────────────────────────────

    def do_init(self, arg):
        """init [path] [--migrate]  — Create .ott/ archive; --migrate imports old flat files."""
        parts = shlex.split(arg)
        path = next((p for p in parts if not p.startswith('--')), '.')
        migrate = '--migrate' in parts
        _run(cmd_init, path, migrate)

    def do_add(self, arg):
        """add <file> [file ...]  — Add images or video to the archive."""
        import glob
        tokens = shlex.split(arg)
        if not tokens:
            print('  Usage: add <file> [file ...]')
            return
        # Expand globs and ~ for each token
        paths = []
        for token in tokens:
            expanded = os.path.expanduser(token)
            matches = glob.glob(expanded)
            if matches:
                paths.extend(sorted(matches))
            else:
                paths.append(expanded)  # let cmd_add report the missing file
        _run(cmd_add, paths)

    def do_status(self, _arg):
        """status  — Show archive status and current Merkle root."""
        _run(cmd_status)

    def do_list(self, _arg):
        """list  — List all archived files."""
        _run(cmd_list)

    def do_commit(self, _arg):
        """commit  — Commit Merkle root to btcvm ledger."""
        _run(cmd_commit)

    def do_verify(self, arg):
        """verify <file>  — Merkle inclusion proof for a file."""
        parts = shlex.split(arg)
        if not parts:
            print('  Usage: verify <file>')
            return
        _run(cmd_verify, parts[0])

    def do_verify_chunk(self, arg):
        """verify_chunk <file> <chunk>  — Byte-range inclusion proof for a video chunk."""
        parts = shlex.split(arg)
        if len(parts) < 2:
            print('  Usage: verify_chunk <file> <chunk_index>')
            return
        try:
            _run(cmd_verify_chunk, parts[0], int(parts[1]))
        except ValueError:
            print('  Chunk index must be an integer.')

    def do_find(self, arg):
        """find <name> [search_root]  — Locate a moved file; update last_path record."""
        parts = shlex.split(arg)
        if not parts:
            print('  Usage: find <name> [search_root]')
            return
        _run(cmd_find, parts[0], parts[1] if len(parts) > 1 else None)

    def do_repo(self, arg):
        """repo <add|list|verify|update|tag|verify-tag|qr> [args]  — Archive git repos."""
        parts = shlex.split(arg)
        if not parts:
            print('  repo subcommands: add, list (ls), verify (v), update (up), tag (t), verify-tag (vt), qr')
            return
        _run(cmd_repo, parts[0], parts[1:])

    def complete_repo(self, text, line, begidx, endidx):
        parts = shlex.split(line[:begidx])
        if len(parts) == 1:  # completing subcommand
            subcmds = ['add', 'list', 'verify', 'update', 'tag', 'verify-tag', 'qr']
            return [s for s in subcmds if s.startswith(text)]
        if len(parts) == 2:  # completing path/name
            if parts[1] in ('verify', 'v', 'update', 'up', 'qr'):
                try:
                    names = [e['name'] for e in get_store().load_manifest()
                             if e.get('type') == 'repo']
                    return [n for n in names if n.startswith(text)] or self._files(text)
                except OttNotFoundError:
                    pass
            return self._files(text)
        return []

    def do_migrate(self, arg):
        """migrate [path]  — Import old ott_manifest.jsonl / imgfs_manifest.jsonl into .ott/"""
        parts = shlex.split(arg)
        _run(cmd_migrate, parts[0] if parts else None)

    def do_mv(self, arg):
        """mv <name> <new_path>  — Update last_path (and name) for an entry."""
        parts = shlex.split(arg)
        if len(parts) < 2:
            print('  Usage: mv <name_or_hash> <new_path>')
            return
        _run(cmd_mv, parts[0], parts[1])

    def do_qr(self, arg):
        """qr [hash|file]  — QR code for a hash, file SHA256, or current Merkle root."""
        parts = shlex.split(arg)
        if not parts:
            try:
                cmd_qr(get_store().current_root(), label='current Merkle root')
            except OttNotFoundError as e:
                print(f'  ✗ {e}')
        elif len(parts[0]) == 64 and all(c in '0123456789abcdef' for c in parts[0]):
            cmd_qr(parts[0], label='hash')
        else:
            path = parts[0]
            if not os.path.isfile(path):
                print(f'  ✗ not a file or valid hex hash: {path}')
                return
            cmd_qr(sha256_file(path), label=os.path.basename(path))

    def do_pwd(self, _arg):
        """pwd  — Print current working directory."""
        print(f'  {os.getcwd()}')

    def do_cd(self, arg):
        """cd <path>  — Change working directory."""
        path = os.path.expanduser(shlex.split(arg)[0]) if arg.strip() else os.path.expanduser('~')
        try:
            os.chdir(path)
            print(f'  {os.getcwd()}')
        except OSError as e:
            print(f'  ✗ {e}')

    def do_ls_dir(self, arg):
        """lsd [path]  — List directory contents."""
        path = os.path.expanduser(shlex.split(arg)[0]) if arg.strip() else '.'
        try:
            entries = sorted(os.listdir(path))
            for name in entries:
                full = os.path.join(path, name)
                suffix = '/' if os.path.isdir(full) else ''
                print(f'  {name}{suffix}')
        except OSError as e:
            print(f'  ✗ {e}')

    def default(self, line):
        """Pass through !cmd to shell."""
        if line.startswith('!'):
            import subprocess
            cmd_str = line[1:].strip()
            if cmd_str:
                result = subprocess.run(cmd_str, shell=True, text=True,
                                        capture_output=False)
            else:
                print('  Usage: !<shell command>')
        else:
            print(f'  *** Unknown syntax: {line}')

    def do_quit(self, _arg):
        """quit  — Exit ott shell."""
        return True

    def do_EOF(self, _arg):
        print()
        return True

    def emptyline(self):
        pass

    # ── aliases ───────────────────────────────────────────────────────────────

    def do_h(self, a):
        """h   — help"""
        self.do_help(a)

    def do_l(self, a):
        """l   — list"""
        self.do_list(a)

    def do_ls(self, a):
        """ls  — list"""
        self.do_list(a)

    def do_st(self, a):
        """st  — status"""
        self.do_status(a)

    def do_a(self, a):
        """a   — add"""
        self.do_add(a)

    def do_v(self, a):
        """v   — verify"""
        self.do_verify(a)

    def do_vc(self, a):
        """vc  — verify_chunk"""
        self.do_verify_chunk(a)

    def do_ci(self, a):
        """ci  — commit"""
        self.do_commit(a)

    def do_q(self, a):
        """q   — quit"""
        return self.do_quit(a)

    def complete_a(self, *a):    return self.complete_add(*a)
    def complete_v(self, *a):    return self.complete_verify(*a)
    def complete_vc(self, *a):   return self.complete_verify_chunk(*a)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='ott — Bitcoin-anchored media archive',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest='cmd')
    sub.add_parser('help', help='Show this help message')

    p_migrate = sub.add_parser('migrate', help='Import old flat manifest/ledger into .ott/')
    p_migrate.add_argument('path', nargs='?', default=None)

    p_init = sub.add_parser('init', help='Create .ott/ archive in current directory')
    p_init.add_argument('path', nargs='?', default='.')
    p_init.add_argument('--migrate', action='store_true',
                        help='Import old ott_manifest.jsonl / imgfs_manifest.jsonl')

    p_add = sub.add_parser('add', help='Add images or video to the archive')
    p_add.add_argument('paths', nargs='+')

    sub.add_parser('status', help='Show archive status and Merkle root')
    sub.add_parser('list',   help='List all archived files')
    sub.add_parser('commit', help='Commit Merkle root to btcvm ledger')
    sub.add_parser('shell',  help='Start interactive shell')

    p_verify = sub.add_parser('verify', help='Merkle inclusion proof for a file')
    p_verify.add_argument('path')

    p_vc = sub.add_parser('verify-chunk', help='Byte-range inclusion proof for a video chunk')
    p_vc.add_argument('path')
    p_vc.add_argument('chunk', type=int)

    p_find = sub.add_parser('find', help='Locate a moved file; update last_path')
    p_find.add_argument('name')
    p_find.add_argument('search_root', nargs='?', default=None)

    p_mv = sub.add_parser('mv', help='Update path record for a file')
    p_mv.add_argument('name')
    p_mv.add_argument('new_path')

    p_qr = sub.add_parser('qr', help='QR code for a hash, file, or current root')
    p_qr.add_argument('target', nargs='?')

    p_repo = sub.add_parser('repo', help='Archive git repos')
    p_repo.add_argument('subcmd',
                        choices=['add', 'list', 'ls', 'verify', 'update', 'up',
                                 'tag', 't', 'verify-tag', 'vt', 'qr'],
                        metavar='add|list|verify|update|tag|verify-tag|qr')
    p_repo.add_argument('args', nargs='*')

    args = parser.parse_args()

    try:
        if args.cmd == 'help' or args.cmd is None and len(sys.argv) > 1:
            parser.print_help()
        elif args.cmd == 'migrate':
            cmd_migrate(args.path)
        elif args.cmd == 'init':
            cmd_init(args.path, args.migrate)
        elif args.cmd == 'add':
            cmd_add(args.paths)
        elif args.cmd == 'status':
            cmd_status()
        elif args.cmd == 'list':
            cmd_list()
        elif args.cmd == 'commit':
            cmd_commit()
        elif args.cmd == 'verify':
            cmd_verify(args.path)
        elif args.cmd == 'verify-chunk':
            cmd_verify_chunk(args.path, args.chunk)
        elif args.cmd == 'find':
            cmd_find(args.name, args.search_root)
        elif args.cmd == 'mv':
            cmd_mv(args.name, args.new_path)
        elif args.cmd == 'qr':
            t = args.target
            if not t:
                cmd_qr(get_store().current_root(), label='current Merkle root')
            elif len(t) == 64 and all(c in '0123456789abcdef' for c in t):
                cmd_qr(t, label='hash')
            else:
                cmd_qr(sha256_file(t), label=os.path.basename(t))
        elif args.cmd == 'repo':
            cmd_repo(args.subcmd, args.args)
        elif args.cmd == 'shell' or args.cmd is None:
            try:
                OttShell().cmdloop()
            except KeyboardInterrupt:
                print()
    except OttNotFoundError as e:
        print(f'  ✗ {e}', file=sys.stderr)
        sys.exit(1)
    except OttError as e:
        print(f'  ✗ {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(0)
