"""Project-local runtime, release changes, and Git-exclusion regressions."""

import contextlib
import copy
import io
import json
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
        fixture = json.loads(Path(__file__).with_name('fixture-data.json').read_text(encoding='utf-8'))
        with patch.object(calendar_sync, 'load_data', return_value=fixture), \
                patch.object(sys, 'argv', ['calendar_sync.py', '--date', '2026-10-04']), \
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
    """Exercise release reporting against fixed inputs, never changing production data."""

    def setUp(self):
        """Keep unit tests independent of the bot's latest confirmed release."""
        self.data = json.loads(Path(__file__).with_name('fixture-data.json').read_text(encoding='utf-8'))
        common.validate(self.data)
        guard = patch.object(common, 'load_data', side_effect=AssertionError(
            'Release-report unit tests must use fixture-data.json, not the live dataset.'))
        guard.start()
        self.addCleanup(guard.stop)

    @staticmethod
    def anchor(data, sequence):
        """Select the intended permanent release ID, not whichever row is last."""
        return next(row for row in data['overrides']['games']['hsr'] if row['sequence'] == sequence)

    def with_newer_releases(self, count=1):
        """Model automatic data growth, including a major-version name transition."""
        data = copy.deepcopy(self.data)
        for sequence in range(31, 31 + count):
            date = common.release(data, 'hsr', sequence)['date']
            data['overrides']['games']['hsr'].append({
                'sequence': sequence, 'date': date, 'status': 'confirmed',
                'verification': 'official', 'sources': ['hsr-4-6']})
            data['versions']['games']['hsr'][str(sequence)] = {
                'label': '4.7' if sequence == 31 else f'5.{sequence - 32}',
                'sources': ['hsr-4-6']}
        common.validate(data)
        return data

    def test_before_after_dates_and_names(self):
        """Show changed dates and names for the same permanent release ID."""
        before = self.data
        after = copy.deepcopy(before)
        self.anchor(after, 30)['date'] = '2026-10-01'
        after['versions']['games']['hsr']['30']['title'] = 'A test title'
        common.validate(after)
        changes = release_changes(before, after)
        self.assertEqual([item['releaseId'] for item in changes], ['hsr-30'])
        change = changes[0]
        self.assertEqual(change['before']['override']['date'], '2026-09-28')
        self.assertEqual(change['after']['override']['date'], '2026-10-01')
        self.assertEqual(change['after']['name']['title'], 'A test title')
        self.assertEqual(change['previousCalculatedDate'], '2026-09-28')
        self.assertEqual(change['currentCalculatedDate'], '2026-10-01')

    def test_metadata_refresh_not_shown_as_release_change(self):
        """A checkedOn/dataset-ID refresh does not pretend to move a release."""
        before = self.data
        after = copy.deepcopy(before)
        after['sources']['checkedOn'] = '2026-10-05'
        for value in after.values():
            value['datasetId'] = 'test-metadata-refresh'
        common.validate(after)
        self.assertEqual(release_changes(before, after), [])

    def test_date_only_correction_preserves_name(self):
        """The date record and its calculated date change without a name edit."""
        after = copy.deepcopy(self.data)
        self.anchor(after, 30)['date'] = '2026-10-01'
        changes = release_changes(self.data, after)
        self.assertEqual([item['releaseId'] for item in changes], ['hsr-30'])
        self.assertEqual(changes[0]['before']['name'], changes[0]['after']['name'])
        self.assertEqual(changes[0]['currentCalculatedDate'], '2026-10-01')

    def test_name_only_change_preserves_date(self):
        """A naming change must not be misreported as rescheduling."""
        after = copy.deepcopy(self.data)
        after['versions']['games']['hsr']['30']['title'] = 'A test title'
        changes = release_changes(self.data, after)
        self.assertEqual([item['releaseId'] for item in changes], ['hsr-30'])
        self.assertEqual(changes[0]['before']['override'], changes[0]['after']['override'])
        self.assertEqual(changes[0]['previousCalculatedDate'], changes[0]['currentCalculatedDate'])

    def test_historical_change_survives_newer_releases(self):
        """Appending releases cannot redirect the historical correction to the last row."""
        for count in (1, 5, 50):
            with self.subTest(newer_releases=count):
                before = self.with_newer_releases(count)
                after = copy.deepcopy(before)
                self.anchor(after, 30)['date'] = '2026-10-01'
                after['versions']['games']['hsr']['30']['title'] = 'Historical correction'
                common.validate(after)
                changes = release_changes(before, after)
                self.assertEqual([item['releaseId'] for item in changes], ['hsr-30'])
                self.assertEqual(changes[0]['before']['override']['date'], '2026-09-28')
                self.assertEqual(changes[0]['after']['override']['date'], '2026-10-01')
                self.assertEqual(after['overrides']['games']['hsr'][31:],
                                 before['overrides']['games']['hsr'][31:])

    def test_different_release_edits_are_not_conflated(self):
        """Reproduce Nov 17: editing 4.6's name and 4.7's date creates two changes."""
        before = self.with_newer_releases()
        after = copy.deepcopy(before)
        after['versions']['games']['hsr']['30']['title'] = 'Historical correction'
        self.anchor(after, 31)['date'] = '2026-11-12'
        common.validate(after)
        changes = {item['releaseId']: item for item in release_changes(before, after)}
        self.assertEqual(set(changes), {'hsr-30', 'hsr-31'})
        self.assertEqual(changes['hsr-30']['after']['override']['date'], '2026-09-28')
        self.assertEqual(changes['hsr-31']['after']['override']['date'], '2026-11-12')
        self.assertEqual(changes['hsr-31']['previousCalculatedDate'], '2026-11-11')
        self.assertEqual(changes['hsr-31']['currentCalculatedDate'], '2026-11-12')

    def test_new_confirmation_reports_previous_projection(self):
        """A new confirmed anchor is recorded even when the prediction was correct."""
        after = self.with_newer_releases()
        changes = release_changes(self.data, after)
        self.assertEqual([item['releaseId'] for item in changes], ['hsr-31'])
        self.assertIsNone(changes[0]['before']['override'])
        self.assertEqual(changes[0]['after']['override']['status'], 'confirmed')
        self.assertEqual(changes[0]['previousCalculatedDate'], '2026-11-11')
        self.assertEqual(changes[0]['currentCalculatedDate'], '2026-11-11')

    def test_reporting_does_not_mutate_inputs(self):
        """The diagnostic diff must leave both caller-owned datasets unchanged."""
        before = self.with_newer_releases(5)
        after = copy.deepcopy(before)
        self.anchor(after, 30)['date'] = '2026-10-01'
        saved_before, saved_after = copy.deepcopy(before), copy.deepcopy(after)
        release_changes(before, after)
        self.assertEqual(before, saved_before)
        self.assertEqual(after, saved_after)


if __name__ == '__main__':
    unittest.main()
