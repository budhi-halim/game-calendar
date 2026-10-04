"""Project-local runtime, release changes, and Git-exclusion regressions."""

import contextlib
import copy
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import calendar_sync
import common
import google_auth
from hunt import release_changes


class LocalRuntimeTests(unittest.TestCase):
    """Keep generated files inside the project without allowing accidental Git exposure."""

    def setUp(self):
        """Create a realistic folder with spaces and a separate fake home."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'OneDrive' / 'VS Code' / 'GitHub' / 'game-calendar'
        self.root.mkdir(parents=True)
        (self.root / '.gitignore').write_text('/.patch-calendar/\n')
        self.root_patch = patch.object(common, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        os.environ.pop('PATCH_CALENDAR_STATE_DIR', None)

    def git(self, *args):
        """Run Git in a disposable project without touching the user's repository."""
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, check=True)

    def test_default_inside_project_not_home(self):
        """The new default must not use Path.home or the process working directory."""
        with patch.object(Path, 'home', side_effect=AssertionError('Home must not be read')):
            path = common.private_directory()
        self.assertEqual(path, self.root / '.patch-calendar')
        self.assertTrue(path.is_dir())

    def test_relative_subfolder_resolves_against_project(self):
        """An optional local override cannot depend on the launch directory."""
        os.environ['PATCH_CALENDAR_STATE_DIR'] = '.patch-calendar/tests'
        self.assertEqual(common.private_directory(), self.root / '.patch-calendar' / 'tests')

    def test_external_override_fails_without_creating_it(self):
        """An old override must not silently recreate external clutter."""
        path = self.base / 'old-output'
        os.environ['PATCH_CALENDAR_STATE_DIR'] = str(path)
        with self.assertRaisesRegex(ValueError, 'inside this project'):
            common.private_directory()
        self.assertFalse(path.exists())

    def test_public_folder_override_rejected(self):
        """Even an in-repository destination cannot be placed in the public data directory."""
        os.environ['PATCH_CALENDAR_STATE_DIR'] = 'data'
        with self.assertRaises(ValueError):
            common.private_directory()

    def test_ignore_rule_required_before_creation(self):
        """No credential directory is created before the committed ignore rule exists."""
        (self.root / '.gitignore').unlink()
        with self.assertRaisesRegex(ValueError, 'gitignore'):
            common.private_directory()
        self.assertFalse((self.root / '.patch-calendar').exists())

    def test_git_add_does_not_track_runtime(self):
        """Prove normal staging leaves actual runtime files out of the index."""
        self.git('init', '-q')
        path = common.private_directory()
        common.write_json(path / 'google-calendar.json', {'refresh_token': 'test-only'}, private=True)
        self.git('add', '.')
        self.assertEqual(self.git('ls-files', '--', '.patch-calendar').stdout, b'')
        self.assertEqual(common.private_directory(), path)

    def test_previously_tracked_runtime_refused(self):
        """Force-added private files stop further automation; ignoring is not retroactive."""
        self.git('init', '-q')
        path = self.root / '.patch-calendar'
        path.mkdir()
        (path / 'google-calendar.json').write_text('{}')
        self.git('add', '-f', '.patch-calendar/google-calendar.json')
        with self.assertRaisesRegex(ValueError, 'Git already tracks'):
            common.private_directory()

    def test_unignore_exception_refused(self):
        """Use Git's own rule evaluation to detect a broken ignore policy."""
        self.git('init', '-q')
        (self.root / '.gitignore').write_text('/.patch-calendar/\n!/.patch-calendar/\n')
        with self.assertRaisesRegex(ValueError, 'not ignoring'):
            common.private_directory()

    @unittest.skipIf(os.name == 'nt', 'Windows link creation may require elevated privileges.')
    def test_runtime_symlink_escape_refused(self):
        """A linked runtime folder cannot redirect output to an external location."""
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / '.patch-calendar').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            common.private_directory()

    def test_calendar_preview_uses_project_runtime(self):
        """The default calendar command remains offline and writes locally."""
        with patch.object(sys, 'argv', ['calendar_sync.py', '--date', '2026-10-04']), \
                patch.object(calendar_sync, 'GoogleAPI', side_effect=AssertionError('No Google requests')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(calendar_sync.main(), 0)
        self.assertTrue((self.root / '.patch-calendar' / 'calendar-preview.json').exists())

    def test_google_init_uses_project_runtime(self):
        """Initialize in this project without changing an existing private JSON."""
        (self.root / 'automation').mkdir()
        common.write_json(self.root / 'automation' / 'google-calendar.example.json', {'calendar_id': 'primary'})
        with patch.object(google_auth, 'ROOT', self.root), patch.object(sys, 'argv', ['google_auth.py', '--init']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(google_auth.main(), 0)
            path = self.root / '.patch-calendar' / 'google-calendar.json'
            common.write_json(path, {'calendar_id': 'primary', 'client_id': 'keep-me'})
            self.assertEqual(google_auth.main(), 0)
        self.assertEqual(common.read_json(path)['client_id'], 'keep-me')


class ProposedChangeTests(unittest.TestCase):
    """Reports must expose the data that would change, not only filenames."""

    def test_before_after_dates_and_names(self):
        """Show changed dates beside permanent release IDs."""
        before = common.load_data()
        after = copy.deepcopy(before)
        after['overrides']['games']['hsr'][-1]['date'] = '2026-10-01'
        after['versions']['games']['hsr']['30']['title'] = 'A test title'
        changes = release_changes(before, after)
        self.assertEqual(changes[0]['releaseId'], 'hsr-30')
        self.assertNotEqual(changes[0]['before'], changes[0]['after'])
        self.assertEqual(changes[0]['after']['override']['date'], '2026-10-01')

    def test_metadata_refresh_not_shown_as_release_change(self):
        """A checkedOn/dataset-ID refresh does not pretend to move a release."""
        before = common.load_data()
        after = copy.deepcopy(before)
        after['sources']['checkedOn'] = '2026-10-05'
        after['config']['datasetId'] = 'test'
        self.assertEqual(release_changes(before, after), [])


if __name__ == '__main__':
    unittest.main()
