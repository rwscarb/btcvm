"""
test_ott_completion.py — Tests for OttShell tab completion path handling.

Covers:
- ~ expansion in _files()
- spaces in filenames (escaping + shlex round-trip)
- parentheses in filenames
- mixed special chars
- directory trailing slash
- complete_add delegation
"""

import os
import shlex
import tempfile
import pytest

import ott


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_shell():
    return ott.OttShell()


SPECIALS = set(" ()[]&!'\";") | {','}


def esc(s):
    """Mirror the quoting logic in _files(); used to compute expected values."""
    if any(c in s for c in SPECIALS):
        return '"' + s.replace('"', '\\"') + '"'
    return s


# ── _files() unit tests ───────────────────────────────────────────────────────

class TestFilesCompleter:
    def setup_method(self):
        self.shell = make_shell()
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make(self, name, is_dir=False):
        path = os.path.join(self.tmpdir, name)
        if is_dir:
            os.makedirs(path, exist_ok=True)
        else:
            open(path, 'w').close()
        return path

    def test_plain_file(self):
        self._make('photo.jpg')
        prefix = os.path.join(self.tmpdir, 'photo')
        results = self.shell._files(prefix)
        assert any('photo.jpg' in r for r in results)

    def test_directory_gets_trailing_slash(self):
        self._make('subdir', is_dir=True)
        prefix = os.path.join(self.tmpdir, 'sub')
        results = self.shell._files(prefix)
        assert any(r.endswith('subdir/') for r in results)

    def test_space_in_filename_quoted(self):
        self._make('VTS_01_1 (1).VOB')
        prefix = os.path.join(self.tmpdir, 'VTS_01_1')
        results = self.shell._files(prefix)
        assert len(results) == 1
        assert results[0].startswith('"')
        assert results[0].endswith('"')

    def test_paren_in_filename_quoted(self):
        self._make('file (copy).jpg')
        prefix = os.path.join(self.tmpdir, 'file')
        results = self.shell._files(prefix)
        assert any(r.startswith('"') for r in results)

    def test_tilde_expansion(self):
        home = os.path.expanduser('~')
        results = self.shell._files('~/')
        # Should return paths starting with ~/
        assert all(r.startswith('~/') for r in results)
        # Should not expose raw expanded home path
        assert not any(r.startswith(home + '/') for r in results)

    def test_tilde_prefix_preserved(self):
        results = self.shell._files('~/D')
        # All results should still start with ~/
        for r in results:
            assert r.startswith('~/')

    def test_multiple_specials(self):
        self._make("Party & Fun [2024].mp4")
        prefix = os.path.join(self.tmpdir, 'Party')
        results = self.shell._files(prefix)
        assert len(results) == 1
        assert results[0].startswith('"')
        assert '&' in results[0]
        assert '[' in results[0]

    def test_no_special_chars_unchanged(self):
        self._make('simple.jpg')
        prefix = os.path.join(self.tmpdir, 'simple')
        results = self.shell._files(prefix)
        assert results == [os.path.join(self.tmpdir, 'simple.jpg')]

    def test_nonexistent_prefix_returns_empty(self):
        prefix = os.path.join(self.tmpdir, 'zzz_nonexistent')
        results = self.shell._files(prefix)
        assert results == []


# ── shlex round-trip ──────────────────────────────────────────────────────────

class TestShlexRoundTrip:
    """Escaped completions must survive shlex.split() back to original paths."""

    @pytest.mark.parametrize("original", [
        '/home/ford/Desktop/VTS_01_1 (1).VOB',
        '~/Desktop/VTS_01_2 (3).VOB',
        '/tmp/my file.jpg',
        '/tmp/file (copy) [v2].mp4',
        '/tmp/Party & Fun.mp4',
        '/tmp/plain.jpg',
        '/tmp/under_score.jpg',
        "/tmp/file's.jpg",
    ])
    def test_roundtrip(self, original):
        escaped = esc(original)
        roundtrip = shlex.split(escaped)[0]
        assert roundtrip == original, (
            f'Round-trip failed:\n  original: {original!r}\n'
            f'  escaped:  {escaped!r}\n  got back: {roundtrip!r}'
        )

    def test_multiple_files_in_one_line(self):
        """Two escaped filenames should split into exactly two paths."""
        f1 = '/tmp/file one (1).jpg'
        f2 = '/tmp/file two (2).jpg'
        line = f'{esc(f1)} {esc(f2)}'
        parts = shlex.split(line)
        assert parts == [f1, f2]


# ── complete_add integration ──────────────────────────────────────────────────

class TestCompleteAdd:
    def setup_method(self):
        self.shell = make_shell()
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make(self, name):
        path = os.path.join(self.tmpdir, name)
        open(path, 'w').close()
        return path

    def test_complete_add_returns_quoted(self):
        self._make('VTS_01_1 (1).VOB')
        prefix = os.path.join(self.tmpdir, 'VTS_01_1')
        results = self.shell.complete_add(prefix, f'a {prefix}', 2, 2 + len(prefix))
        assert len(results) == 1
        assert results[0].startswith('"')

    def test_complete_add_multiple_matches(self):
        self._make('photo1.jpg')
        self._make('photo2.jpg')
        prefix = os.path.join(self.tmpdir, 'photo')
        results = self.shell.complete_add(prefix, f'a {prefix}', 2, 2 + len(prefix))
        assert len(results) == 2

    def test_complete_add_dir_has_slash(self):
        subdir = os.path.join(self.tmpdir, 'subdir')
        os.makedirs(subdir)
        prefix = os.path.join(self.tmpdir, 'sub')
        results = self.shell.complete_add(prefix, f'a {prefix}', 2, 2 + len(prefix))
        assert any(r.endswith('/') for r in results)


# ── preloop readline config ───────────────────────────────────────────────────

class TestPreloop:
    def test_preloop_sets_whitespace_delimiters(self):
        try:
            import readline
        except ImportError:
            pytest.skip('readline not available')

        shell = make_shell()
        shell.preloop()
        delims = readline.get_completer_delims()
        assert '~' not in delims
        assert '/' not in delims
        assert '_' not in delims
        assert '(' not in delims
        assert ' ' in delims   # spaces still split tokens
        assert '\t' in delims
