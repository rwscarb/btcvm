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
VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.mts', '.ts'}


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
        """Load entries; last write per sha256 wins (append = update)."""
        if not os.path.exists(self.manifest_path):
            return []
        seen: dict[str, dict] = {}
        with open(self.manifest_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        e = json.loads(line)
                        seen[e['sha256']] = e
                    except (json.JSONDecodeError, KeyError):
                        pass
        return list(seen.values())

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
        t = 'V' if e.get('type') == 'video' else 'I'
        ok = '✅' if os.path.isfile(e.get('last_path', '')) else '⚠️ '
        print(f'  {i:<4} {t:<2} {e["name"]:<36} {e["sha256"][:16]}…  '
              f'{e.get("size", 0):>10,}  {ok}')
    print(f'\n  Merkle root: {store.current_root()}')
    print('  T: I=image V=video  ✅=at last_path  ⚠️ =path missing (run ott find)')


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
        print(f'  ⚠️  File not at last known path — proof uses stored hash')
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
    for dirpath, _dirs, files in os.walk(root):
        if name in files:
            candidate = os.path.join(dirpath, name)
            try:
                if entry.get('type') == 'video':
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
    """Call a cmd_* function, printing OttError neatly."""
    try:
        fn(*args, **kwargs)
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
        return glob.glob(text + '*')

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
        paths = shlex.split(arg)
        if not paths:
            print('  Usage: add <file> [file ...]')
            return
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

    def do_quit(self, _arg):
        """quit  — Exit ott shell."""
        return True

    def do_EOF(self, _arg):
        print()
        return True

    def emptyline(self):
        pass

    # ── aliases ───────────────────────────────────────────────────────────────

    def do_ls(self, a):    """ls    — list""";           self.do_list(a)
    def do_st(self, a):    """st    — status""";         self.do_status(a)
    def do_a(self, a):     """a     — add""";             self.do_add(a)
    def do_v(self, a):     """v     — verify""";          self.do_verify(a)
    def do_vc(self, a):    """vc    — verify_chunk""";    self.do_verify_chunk(a)
    def do_ci(self, a):    """ci    — commit""";          self.do_commit(a)
    def do_q(self, a):     """q     — quit""";             return self.do_quit(a)

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

    args = parser.parse_args()

    try:
        if args.cmd == 'init':
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
        elif args.cmd == 'shell' or args.cmd is None:
            OttShell().cmdloop()
    except OttNotFoundError as e:
        print(f'  ✗ {e}', file=sys.stderr)
        sys.exit(1)
    except OttError as e:
        print(f'  ✗ {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
