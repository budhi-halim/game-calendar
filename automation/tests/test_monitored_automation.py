"""Regression coverage for guarded deployment, evidence handling, and retained history."""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common
import daily
import hunt
from progress import Progress
from review_history import summarize
from run_history import archive_run, code_fingerprint, compact_calendar, compact_hunt, compact_pipeline, record_public_run, safe_tree
from sources import publication_candidates, publication_metadata, walk_news

FIXTURE = json.loads(Path(__file__).with_name('fixture-data.json').read_text())
FEED = FIXTURE['config']['automation']['sources']['feeds']['hsr']


class MetadataTests(unittest.TestCase):
    """Distinguish metadata wrappers from nested unrelated news and event prose."""

    def test_nested_publication_field(self):
        value = {'ext': {'publish_time': '2026-10-04T01:00:00+08:00'}}
        self.assertEqual(publication_metadata(value), ('2026-10-04', 'publisher JSON: ext.publishtime'))

    def test_json_encoded_metadata(self):
        value = {'metadata': json.dumps({'datePublished': '2026-10-04'})}
        self.assertEqual(publication_metadata(value)[0], '2026-10-04')

    def test_cms_key_value_pairs(self):
        value = {'ext': [{'key': 'publish_date', 'value': '2026-10-04'}]}
        self.assertEqual(publication_metadata(value)[0], '2026-10-04')

    def test_publication_over_creation(self):
        value = {'publishedAt': '2026-10-04', 'createdAt': '2026-09-29'}
        self.assertEqual(publication_metadata(value)[0], '2026-10-04')

    def test_conflicting_publications_not_selected(self):
        self.assertEqual(publication_metadata({'publishedAt': '2026-10-04', 'ext': {'datePublished': '2026-10-05'}}), (None, None))

    def test_related_article_cannot_supply_publication(self):
        self.assertEqual(publication_metadata({'related': [{'publishedAt': '2026-10-04'}]}), (None, None))

    def test_update_timestamp_not_publication(self):
        self.assertEqual(publication_metadata({'updatedAt': '2026-10-04'}), (None, None))

    def test_field_diagnostics_omit_arbitrary_raw_values(self):
        result = publication_candidates({'publish_time': 'client_secret=do-not-copy', 'metadata': {'refresh_token': 'SECRET'}})
        self.assertEqual(result, [{'field': 'publishtime', 'key': 'publishtime', 'day': None}])

    def test_date_survives_record_identity_extraction(self):
        rows = walk_news({'data': {'id': 456, 'title': 'Version 4.7 Update Details', 'ext': {'datePublished': '2026-10-04'}}}, FEED)
        self.assertEqual(rows[0]['publishedOn'], '2026-10-04')
        self.assertEqual(rows[0]['publicationFields'][0]['field'], 'ext.datepublished')

    def test_existing_source_publication_not_erased(self):
        data = copy.deepcopy(FIXTURE)
        item = {'gameId': 'hsr', 'label': '4.6', 'date': '2026-09-28', 'url': FEED['url'] + '/166468',
                'sourceTitle': 'Version 4.6 Update Details', 'publishedOn': '2026-09-27', 'releaseAnnouncement': True}
        settings = data['config']['automation']['sources']
        hunt.merge_observation(data, item, settings)
        key = 'official-' + common.digest(item['url'])[:16]
        item['publishedOn'] = None
        hunt.merge_observation(data, item, settings)
        self.assertEqual(data['sources']['sources'][key]['publishedOn'], '2026-09-27')

    def test_older_same_date_notice_does_not_roll_back_announced_on(self):
        data = copy.deepcopy(FIXTURE)
        item = {'gameId': 'hsr', 'label': '4.6', 'date': '2026-09-28', 'url': FEED['url'] + '/166468',
                'sourceTitle': 'Version 4.6 Update Details', 'publishedOn': '2026-09-27', 'releaseAnnouncement': True}
        settings = data['config']['automation']['sources']
        hunt.merge_observation(data, item, settings)
        item['publishedOn'] = '2026-09-26'
        hunt.merge_observation(data, item, settings)
        row = next(row for row in data['overrides']['games']['hsr'] if row['sequence'] == 30)
        self.assertEqual(row['announcedOn'], '2026-09-27')


class TransactionTests(unittest.TestCase):
    """Public data cannot be left mixed after ordinary write failures or externally overwritten."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        for name, value in FIXTURE.items():
            common.write_json(self.root / 'data' / (name + '.json'), value)
        self.data = copy.deepcopy(FIXTURE)
        for value in self.data.values():
            value['datasetId'] = 'test-updated'

    def tearDown(self):
        self.folder.cleanup()

    def test_successful_transaction_removes_journal(self):
        self.assertEqual(len(common.save_data(self.data, self.root, expected=FIXTURE)), 4)
        self.assertEqual(common.load_data(self.root), self.data)
        self.assertFalse((self.root / '.patch-calendar/data-transaction.json').exists())

    def test_failed_second_file_restores_all_four(self):
        real = common.write_json
        original = {name: (self.root / 'data' / (name + '.json')).read_bytes() for name in common.DATA_NAMES}
        def fail(path, value, private=False):
            if Path(path).name == 'overrides.json':
                raise OSError('simulated disk failure')
            return real(path, value, private)
        with patch.object(common, 'write_json', side_effect=fail), self.assertRaises(OSError):
            common.save_data(self.data, self.root)
        self.assertEqual(common.load_data(self.root), FIXTURE)
        self.assertEqual({name: (self.root / 'data' / (name + '.json')).read_bytes() for name in common.DATA_NAMES}, original)

    def test_external_edits_prevent_update(self):
        changed = copy.deepcopy(FIXTURE)
        changed['config']['title'] = 'My edit'
        common.write_json(self.root / 'data/config.json', changed['config'])
        with self.assertRaises(ValueError):
            common.save_data(self.data, self.root, expected=FIXTURE)
        self.assertEqual(common.load_data(self.root), changed)

    def test_pending_journal_blocks_normal_load(self):
        common.write_json(self.root / '.patch-calendar/data-transaction.json', {})
        with self.assertRaisesRegex(ValueError, '--recover'):
            common.load_data(self.root)

    def test_no_change_creates_no_journal(self):
        self.assertEqual(common.save_data(FIXTURE, self.root), [])
        self.assertFalse((self.root / '.patch-calendar').exists())

    def test_no_pending_recovery_is_noop(self):
        self.assertFalse(common.recover_data(self.root))


class HistoryTests(unittest.TestCase):
    """Never treat successful execution logs as independent ground truth about releases."""

    def test_secret_fields_and_values_are_removed(self):
        with contextlib.redirect_stdout(io.StringIO()):
            reporter = Progress('test')
            try:
                reporter.protect('actual-secret')
                value = safe_tree({'client_secret': 'secret', 'nested': {'note': 'actual-secret', 'calendar_id': 'private'}, 'errors': ['Bearer private-token']}, reporter.clean)
                self.assertNotIn('actual-secret', json.dumps(value))
                self.assertNotIn('client_secret', value)
                self.assertNotIn('calendar_id', value['nested'])
            finally:
                reporter.close()

    def test_archive_is_unique_and_does_not_touch_credentials(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            directory = Path(folder)
            (directory / 'google-calendar.json').write_text('PRIVATE')
            reporter = Progress('test')
            reporter.attach(directory / 'hunt-progress.log')
            try:
                record = {'startedAt': '2026-10-04T12:00:00+07:00', 'status': 'passed', 'client_secret': 'SECRET'}
                archive = archive_run(directory, 'hunt', record, reporter)
                self.assertEqual((directory / 'google-calendar.json').read_text(), 'PRIVATE')
                self.assertNotIn('SECRET', (archive / 'report.json').read_text())
                with self.assertRaises(ValueError):
                    archive_run(directory, 'hunt', record, reporter)
            finally:
                reporter.close()

    def test_compact_calendar_contains_no_destination_or_error_text(self):
        result = compact_calendar({'status': 'failed', 'calendar_id': 'email@example.org', 'error': 'private details', 'counts': {'created': 1}})
        self.assertNotIn('email@example.org', json.dumps(result))
        self.assertNotIn('private details', json.dumps(result))
        self.assertEqual(result['counts']['created'], 1)

    def test_compact_hunt_keeps_changes_without_raw_page_content(self):
        report = {'status': 'passed', 'failureDetails': {'bodyExcerpt': 'SECRET'}, 'notices': ['private string'],
                  'proposedChanges': [{'releaseId': 'hsr-31', 'before': {'name': {}}, 'after': {'name': {'label': '4.7'}}}]}
        result = compact_hunt(report)
        self.assertEqual(result['warningCount'], 1)
        self.assertEqual(result['changes'][0]['kind'], 'schedule-or-name')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('private string', json.dumps(result))

    def test_public_pipeline_omits_local_paths_and_free_text(self):
        row = {'runId': 'x', 'status': 'failed', 'lastProgress': 'SECRET local path',
               'error': 'SECRET exception', 'calendar_id': 'private@example.org',
               'hunt': compact_hunt({}), 'calendar': compact_calendar({})}
        value = json.dumps(compact_pipeline(row))
        self.assertNotIn('SECRET', value)
        self.assertNotIn('private@example.org', value)

    def test_code_fingerprint_ignores_credentials_and_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'automation').mkdir()
            (root / 'automation/hunt.py').write_text('one')
            before = code_fingerprint(root)
            (root / 'automation/google-calendar.json').write_text('PRIVATE')
            self.assertEqual(code_fingerprint(root), before)
            (root / 'automation/hunt.py').write_text('two')
            self.assertNotEqual(code_fingerprint(root), before)

    def test_public_history_appends_without_repeating_same_run(self):
        with tempfile.TemporaryDirectory() as folder:
            value = {'runId': 'unique', 'startedAt': '2026-10-04T12:00:00+07:00', 'component': 'pipeline'}
            path = record_public_run(folder, value)
            record_public_run(folder, value)
            self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_reviews_do_not_invent_accuracy_rate(self):
        result = summarize([{'runId': 'one', 'component': 'pipeline', 'mode': 'apply', 'startedAt': '2026-10-04T02:00:00+07:00', 'trigger': 'schedule', 'status': 'passed'}], '2026-10-01', '2026-10-07')
        self.assertEqual(result['appliedPipelineRuns'], 1)
        self.assertEqual(result['daysWithoutRecordedScheduledReceipt'], ['2026-10-05', '2026-10-06'])
        self.assertEqual(result['announcementCompleteness'], 'Not independently measured by execution logs.')

    def test_checks_are_not_counted_as_applied_runs(self):
        value = {'runId': 'x', 'component': 'pipeline', 'mode': 'check', 'startedAt': '2026-10-04T02:00:00+07:00', 'status': 'passed'}
        result = summarize([value, value], '2026-10-01', '2026-10-07')
        self.assertEqual(result['pipelineChecks'], 1)
        self.assertEqual(result['appliedPipelineRuns'], 0)

    def test_cache_state_strips_unknown_fields(self):
        value = daily.clean_state({'fingerprint': 'a'*64, 'targetHash': 'b'*64, 'lastSyncedOn': '2026-10-04', 'client_secret': 'PRIVATE'})
        self.assertEqual(set(value), {'fingerprint', 'targetHash', 'lastSyncedOn'})

    def test_bad_cache_fingerprint_is_rejected(self):
        with self.assertRaises(ValueError):
            daily.clean_state({'fingerprint': 'bad', 'targetHash': 'b'*64, 'lastSyncedOn': '2026-10-04'})

    def test_stale_report_is_not_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.json'
            common.write_json(path, {'runId': 'old', 'status': 'passed'})
            self.assertIsNone(daily.fresh_report(path, 'old'))


class PipelineTests(unittest.TestCase):
    """Simulated child executions prove order and fail-closed write controls."""

    def run_pipeline(self, args, fail_hunt=False, fail_calendar=False, stale=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / '.patch-calendar'
            directory.mkdir()
            def execute(command, progress, timeout, google=False):
                if command[0].endswith('hunt.py'):
                    if not stale:
                        common.write_json(directory / 'hunt-report.json', {'runId': 'hunt-new', 'ok': not fail_hunt,
                            'status': 'failed' if fail_hunt else 'passed', 'checkOnly': '--check' in command, 'games': {}, 'notices': [], 'errors': []})
                    return 1 if fail_hunt else 0
                common.write_json(directory / 'calendar-sync-report.json', {'runId': 'cal-new', 'status': 'failed' if fail_calendar else 'preview' if len(command) == 1 else 'synced', 'mode': 'preview' if len(command) == 1 else 'apply', 'counts': {'updated': 7}})
                return 1 if fail_calendar else 0
            with contextlib.redirect_stdout(io.StringIO()), patch.object(sys, 'argv', ['daily.py', *args]), \
                    patch.object(daily, 'ROOT', root), patch.object(daily, 'private_directory', return_value=directory), \
                    patch.object(daily, 'load_data', return_value=copy.deepcopy(FIXTURE)), patch.object(daily, 'stage', side_effect=execute) as child, \
                    patch.dict(os.environ, {'GITHUB_ACTIONS': '', 'GITHUB_OUTPUT': '', 'GITHUB_STEP_SUMMARY': ''}):
                code = daily.main()
            return code, common.read_json(directory / 'pipeline-report.json'), child.call_args_list, list((root / 'automation/history').glob('*.jsonl'))

    def test_locked_pipeline_preserves_current_report(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            report_path = directory / 'pipeline-report.json'
            report_path.write_text('KEEP')
            with contextlib.redirect_stdout(io.StringIO()), patch.object(sys, 'argv', ['daily.py']), \
                    patch.object(daily, 'private_directory', return_value=directory), \
                    patch.object(daily, 'exclusive_lock', side_effect=RuntimeError('already running')):
                code = daily.main()
            self.assertEqual(code, 1)
            self.assertEqual(report_path.read_text(), 'KEEP')

    def test_default_is_read_only(self):
        code, result, calls, history = self.run_pipeline([])
        self.assertEqual(code, 0)
        self.assertIn('--check', calls[0].args[0])
        self.assertEqual(calls[1].args[0], ['automation/calendar_sync.py'])
        self.assertFalse(calls[1].kwargs['google'])
        self.assertFalse(history)

    def test_worker_override_applies_only_to_hunter(self):
        """Changing scrape concurrency must not enable or parallelize Google writes."""
        code, result, calls, history = self.run_pipeline(['--check', '--workers', '2'])
        self.assertEqual(code, 0)
        self.assertEqual(calls[0].args[0], ['automation/hunt.py', '--check', '--workers', '2'])
        self.assertEqual(calls[1].args[0], ['automation/calendar_sync.py'])
        self.assertFalse(calls[1].kwargs['google'])
        self.assertFalse(history)

    def test_failed_hunt_never_calls_calendar(self):
        code, result, calls, _ = self.run_pipeline(['--apply', '--sync-calendar'], fail_hunt=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['status'], 'hunt-failed')

    def test_stale_hunt_report_never_calls_calendar(self):
        code, result, calls, _ = self.run_pipeline(['--apply', '--sync-calendar'], stale=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)

    def test_explicit_local_sync_obeys_order(self):
        code, result, calls, _ = self.run_pipeline(['--apply', '--sync-calendar'])
        self.assertEqual(code, 0)
        self.assertEqual(calls[0].args[0], ['automation/hunt.py'])
        self.assertEqual(calls[1].args[0], ['automation/calendar_sync.py', '--apply'])
        self.assertTrue(result['huntApplied'])
        self.assertEqual(result['calendar']['counts']['updated'], 7)

    def test_apply_alone_respects_disabled_calendar(self):
        code, result, calls, _ = self.run_pipeline(['--apply'])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['status'], 'passed-calendar-disabled')

    def test_calendar_failure_retains_hunt_result_and_fails(self):
        code, result, calls, _ = self.run_pipeline(['--apply', '--sync-calendar'], fail_calendar=True)
        self.assertEqual(code, 1)
        self.assertTrue(result['huntApplied'])
        self.assertEqual(result['status'], 'calendar-failed')

    def test_failure_can_write_compact_public_history_explicitly(self):
        code, result, calls, history = self.run_pipeline(['--apply', '--publish-history'], fail_hunt=True)
        self.assertEqual(code, 1)
        self.assertTrue(result['publicHistoryWritten'])
        self.assertEqual(len(history), 1)


if __name__ == '__main__':
    unittest.main()
