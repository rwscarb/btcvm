"""
test_backends.py — Tests for the object storage backend abstraction:
LocalStorageBackend baseline, RaidStorageBackend mirroring/self-heal/
parallel-put/error-handling, the `ott backend` CLI (show/set/add/remove),
and backfill's is_fully_stored gating for RAID members.

No cloud credentials needed. GoogleStorageBackend/S3StorageBackend are
only exercised through the generic StorageBackend interface (construction
guards, missing-bucket errors) — everything that needs a real put/get
uses LocalStorageBackend instances as real, fully-functional stand-ins,
same as how this logic was verified by hand before this file existed.
"""
import hashlib
import os
import shutil
import tempfile
import time

import pytest

import ott


def _sha256_of(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


class SlowBackend(ott.StorageBackend):
    """Stand-in for a remote backend with a real, measurable upload delay —
    used to prove RaidStorageBackend.put() runs members in parallel."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.store = {}

    def exists(self, sha256):
        return sha256 in self.store

    def put(self, sha256, src_path):
        time.sleep(self.delay)
        self.store[sha256] = True

    def ensure_local(self, sha256):
        return None

    def describe(self, sha256):
        return f'slow:{sha256[:8]}'

    def size(self, sha256):
        return None


class FailingBackend(ott.StorageBackend):
    def exists(self, sha256):
        return False

    def put(self, sha256, src_path):
        raise RuntimeError('simulated failure')

    def ensure_local(self, sha256):
        return None

    def describe(self, sha256):
        return 'failing'

    def size(self, sha256):
        return None


@pytest.fixture
def src_file(tmp_path):
    p = tmp_path / 'photo.jpg'
    p.write_bytes(os.urandom(2000))
    return str(p)


# ── RaidStorageBackend ──────────────────────────────────────────────────────

class TestRaidStorageBackend:
    def test_requires_at_least_two_members(self, tmp_path):
        with pytest.raises(ott.OttError):
            ott.RaidStorageBackend([ott.LocalStorageBackend(str(tmp_path))])

    def test_put_mirrors_to_all_members(self, tmp_path, src_file):
        a = ott.LocalStorageBackend(str(tmp_path / 'a'))
        b = ott.LocalStorageBackend(str(tmp_path / 'b'))
        c = ott.LocalStorageBackend(str(tmp_path / 'c'))
        raid = ott.RaidStorageBackend([a, b, c])
        sha = _sha256_of(src_file)

        raid.put(sha, src_file)

        assert a.exists(sha) and b.exists(sha) and c.exists(sha)

    def test_exists_is_any_member_is_fully_stored_is_all_members(self, tmp_path, src_file):
        a = ott.LocalStorageBackend(str(tmp_path / 'a'))
        b = ott.LocalStorageBackend(str(tmp_path / 'b'))
        raid = ott.RaidStorageBackend([a, b])
        sha = _sha256_of(src_file)

        a.put(sha, src_file)  # only one member has it

        assert raid.exists(sha) is True
        assert raid.is_fully_stored(sha) is False

        b.put(sha, src_file)
        assert raid.is_fully_stored(sha) is True

    def test_put_runs_members_in_parallel_not_sequentially(self, src_file):
        sha = _sha256_of(src_file)
        members = [SlowBackend(delay=0.3), SlowBackend(delay=0.3), SlowBackend(delay=0.3)]
        raid = ott.RaidStorageBackend(members)

        t0 = time.time()
        raid.put(sha, src_file)
        elapsed = time.time() - t0

        # sequential would be ~0.9s; parallel should land near the single
        # slowest member (~0.3s) — generous bound for CI jitter
        assert elapsed < 0.6, f'members ran sequentially, not in parallel ({elapsed:.2f}s)'
        assert all(m.exists(sha) for m in members)

    def test_put_tolerates_one_failing_member(self, tmp_path, src_file):
        ok = ott.LocalStorageBackend(str(tmp_path / 'ok'))
        raid = ott.RaidStorageBackend([ok, FailingBackend()])
        sha = _sha256_of(src_file)

        raid.put(sha, src_file)  # must not raise — one member survived

        assert ok.exists(sha)

    def test_put_raises_when_every_member_fails(self, src_file):
        raid = ott.RaidStorageBackend([FailingBackend(), FailingBackend()])
        sha = _sha256_of(src_file)

        with pytest.raises(ott.OttError):
            raid.put(sha, src_file)

    def test_ensure_local_fast_path_does_not_touch_later_members(self, tmp_path, src_file):
        a = ott.LocalStorageBackend(str(tmp_path / 'a'))
        b = ott.LocalStorageBackend(str(tmp_path / 'b'))
        raid = ott.RaidStorageBackend([a, b])
        sha = _sha256_of(src_file)
        a.put(sha, src_file)  # only 'a' has it — primary/fast path

        path = raid.ensure_local(sha)

        assert path == a._path(sha)
        assert not b.exists(sha), 'a healthy primary read should not touch other members'

    def test_ensure_local_self_heals_a_missing_earlier_member(self, tmp_path, src_file):
        a = ott.LocalStorageBackend(str(tmp_path / 'a'))
        b = ott.LocalStorageBackend(str(tmp_path / 'b'))
        raid = ott.RaidStorageBackend([a, b])
        sha = _sha256_of(src_file)
        b.put(sha, src_file)  # 'a' (checked first) is missing it, 'b' has it

        path = raid.ensure_local(sha)

        assert path == b._path(sha)
        assert a.exists(sha), 'primary member should have been healed from the survivor'
        with open(a._path(sha), 'rb') as f:
            assert f.read() == open(src_file, 'rb').read()

    def test_ensure_local_returns_none_when_no_member_has_it(self, tmp_path):
        a = ott.LocalStorageBackend(str(tmp_path / 'a'))
        b = ott.LocalStorageBackend(str(tmp_path / 'b'))
        raid = ott.RaidStorageBackend([a, b])

        assert raid.ensure_local('0' * 64) is None

    def test_describe_lists_every_member_that_has_it(self, tmp_path, src_file):
        a = ott.LocalStorageBackend(str(tmp_path / 'a'))
        b = ott.LocalStorageBackend(str(tmp_path / 'b'))
        raid = ott.RaidStorageBackend([a, b])
        sha = _sha256_of(src_file)
        a.put(sha, src_file)

        desc = raid.describe(sha)
        assert a.describe(sha) in desc
        assert b.describe(sha) not in desc  # b never got a copy


# ── _build_backend / get_backend validation (no cloud creds needed) ─────────

class TestBuildBackendValidation:
    def test_s3_without_bucket_raises_clear_error(self, tmp_path):
        store = ott.OttStore(str(tmp_path / '.ott'))
        with pytest.raises(ott.OttError, match='needs a bucket'):
            ott._build_backend('s3', store)

    def test_gcs_without_bucket_raises_clear_error(self, tmp_path):
        store = ott.OttStore(str(tmp_path / '.ott'))
        with pytest.raises(ott.OttError, match='needs a bucket'):
            ott._build_backend('gcs', store)

    def test_unknown_kind_raises_clear_error(self, tmp_path):
        store = ott.OttStore(str(tmp_path / '.ott'))
        with pytest.raises(ott.OttError, match='Unknown object_backend'):
            ott._build_backend('nope', store)

    def test_google_backend_requires_package_when_not_installed(self, tmp_path):
        if ott._HAS_GCS:
            pytest.skip('google-cloud-storage is installed in this environment')
        with pytest.raises(ott.OttError, match='google-cloud-storage'):
            ott.GoogleStorageBackend('bucket', '', str(tmp_path))


# ── ott backend CLI + backfill-into-raid (real archive, temp .ott dir) ──────

class _RealArchiveTestBase:
    """Shared setup: a real .ott archive in a temp dir, cwd'd into so
    find_ott_dir() picks it up the same way the CLI would."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        ott._reset_store()
        ott.cmd_init('.')
        ott._reset_store()

    def teardown_method(self):
        os.chdir(self.orig_cwd)
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        ott._reset_store()


class TestBackendCLI(_RealArchiveTestBase):
    def test_default_backend_is_local(self):
        cfg = ott.get_store().config()
        assert cfg.get('object_backend', 'local') == 'local'

    def test_set_replaces_active_backend(self):
        ott.cmd_backend_set('s3', 'my-bucket', 'my-prefix')
        cfg = ott.get_store().config()
        assert cfg['object_backend'] == 's3'
        assert cfg['s3_bucket'] == 'my-bucket'
        assert cfg['s3_prefix'] == 'my-prefix'

    def test_add_promotes_current_backend_to_raid(self):
        ott.cmd_backend_add('s3', 'my-bucket', None)
        cfg = ott.get_store().config()
        assert cfg['object_backend'] == 'raid'
        assert cfg['raid_backends'] == ['local', 's3']

    def test_add_existing_member_is_a_noop(self):
        ott.cmd_backend_add('s3', 'my-bucket', None)
        before = ott.get_store().config()
        ott.cmd_backend_add('s3', None, None)
        after = ott.get_store().config()
        assert before == after

    def test_add_second_remote_builds_three_way_raid(self):
        ott.cmd_backend_add('s3', 'my-bucket', None)
        ott.cmd_backend_add('gcs', 'my-gcs-bucket', None)
        cfg = ott.get_store().config()
        assert cfg['raid_backends'] == ['local', 's3', 'gcs']

    def test_remove_collapses_to_single_backend_when_one_left(self):
        ott.cmd_backend_add('s3', 'my-bucket', None)
        ott.cmd_backend_remove('s3')
        cfg = ott.get_store().config()
        assert cfg['object_backend'] == 'local'
        assert 'raid_backends' not in cfg

    def test_remove_when_not_raid_raises(self):
        with pytest.raises(ott.OttError):
            ott.cmd_backend_remove('s3')

    def test_remove_nonmember_raises(self):
        ott.cmd_backend_add('s3', 'my-bucket', None)
        with pytest.raises(ott.OttError):
            ott.cmd_backend_remove('gcs')

    def test_save_config_invalidates_cached_backend(self):
        store = ott.get_store()
        store._backend = 'SENTINEL — pretend this is a stale cached backend'
        ott.cmd_backend_set('local', None, None)
        assert store._backend is None


class TestBackfillIntoRaid(_RealArchiveTestBase):
    """Regression test for the real bug found live: backfill used to gate
    on plain has_object() (any member), so a RAID member added *after* an
    archive already existed under a different backend never got topped up
    — is_fully_stored() (all members) is what backfill needs to gate on."""

    def test_backfill_tops_up_a_raid_member_missing_the_object(self):
        src = os.path.join(self.tmpdir, 'clip.mp4')
        with open(src, 'wb') as f:
            f.write(os.urandom(5000))
        ott.cmd_add([src])
        ott._absorb_staged(ott.get_store())  # manifest + local object, no network

        store = ott.get_store()
        sha = store.load_manifest()[0]['sha256']
        local = ott.LocalStorageBackend(store.objects_dir)
        secondary = ott.LocalStorageBackend(os.path.join(self.tmpdir, 'secondary'))
        assert local.exists(sha) and not secondary.exists(sha)  # sanity on the setup

        store._backend = ott.RaidStorageBackend([local, secondary])
        ott.cmd_backfill(workers=2)

        assert secondary.exists(sha), (
            'backfill must top up a raid member missing an object even when '
            'another member already has it'
        )
        with open(secondary._path(sha), 'rb') as f:
            assert f.read() == open(src, 'rb').read()

    def test_backfill_is_idempotent_once_fully_synced(self):
        src = os.path.join(self.tmpdir, 'clip.mp4')
        with open(src, 'wb') as f:
            f.write(os.urandom(5000))
        ott.cmd_add([src])
        ott._absorb_staged(ott.get_store())

        store = ott.get_store()
        sha = store.load_manifest()[0]['sha256']
        local = ott.LocalStorageBackend(store.objects_dir)
        secondary = ott.LocalStorageBackend(os.path.join(self.tmpdir, 'secondary'))
        store._backend = ott.RaidStorageBackend([local, secondary])

        ott.cmd_backfill(workers=2)
        mtime_after_first_run = os.path.getmtime(secondary._path(sha))

        time.sleep(0.05)
        ott.cmd_backfill(workers=2)  # should see is_fully_stored() and skip

        assert os.path.getmtime(secondary._path(sha)) == mtime_after_first_run, (
            'second backfill run re-copied an object that was already fully synced'
        )
