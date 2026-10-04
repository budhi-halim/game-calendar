"""Regression checks for truthful, immediate CLI progress without live service calls."""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import calendar_sync
import google_api
import google_auth
import hunt
from common import digest, exclusive_lock, parse_day, read_json, write_json
from progress import Progress
from test_automation import FakeAPI


def fixture():
    """Keep tests independent of future daily changes to the public data."""
    return json.loads(Path(__file__).with_name('fixture-data.json').read_text(encoding='utf-8'))


class ProgressTests(unittest.TestCase):
    """Exercise the reporter itself, including blocked operations and sensitive values."""

    def setUp(self):
        """Capture both console and a disposable private run log."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.log = Progress('test', heartbeat=0.04)
        self.addCleanup(self.log.close)
        self.path = Path(self.directory.name) / 'progress.log'

    def test_immediate_output_and_file_flush(self):
        """Status is readable before close and without Python's -u flag."""
        self.log.info('Starting before file checks.')
        self.log.attach(self.path)
        self.log.info('Reading release records.')
        self.assertIn('Reading release records.', self.output.getvalue())
        self.assertIn('Starting before file checks.', self.path.read_text())
        self.assertIn('Reading release records.', self.path.read_text())

    def test_heartbeat_during_silent_blocking_call(self):
        """A blocked operation produces a WAIT record without claiming completion."""
        self.log.attach(self.path)
        with self.log.waiting('Google GET managed events', announce=True):
            time.sleep(0.13)
        value = self.output.getvalue()
        self.assertIn('WAIT', value)
        self.assertIn('Still waiting: Google GET managed events', value)
        self.assertIn('no completion yet', value)
        self.assertNotIn('100%', value)

    def test_regular_progress_suppresses_idle_spam(self):
        """An active stream need not print an extra heartbeat after every update."""
        self.log.heartbeat = 1
        for number in range(5):
            self.log.info(f'Article {number}/5')
        self.assertNotIn('WAIT', self.output.getvalue())

    def test_nested_wait_restores_parent(self):
        """The operation name is restored after a temporary token-refresh phase."""
        self.log.info('Outer phase')
        with self.log.waiting('Inner phase'):
            self.assertEqual(self.log.activity, 'Inner phase')
        self.assertEqual(self.log.activity, 'Outer phase')

    def test_close_joins_heartbeat_and_is_repeatable(self):
        """No stale thread writes into the next command's console capture."""
        self.log.close()
        self.log.close()
        self.assertFalse(self.log.thread.is_alive())
        size = len(self.output.getvalue())
        time.sleep(0.07)
        self.assertEqual(len(self.output.getvalue()), size)

    def test_previous_log_is_retained_once(self):
        """A new run retains only its predecessor, without touching other runtime files."""
        self.path.write_text('previous run')
        previous = self.path.with_name('progress.previous.log')
        previous.write_text('older run')
        sentinel = self.path.with_name('calendar-state.json')
        sentinel.write_text('keep this state')
        self.log.attach(self.path)
        self.log.success('Current run')
        self.assertEqual(previous.read_text(), 'previous run')
        self.assertEqual(sentinel.read_text(), 'keep this state')
        self.assertIn('Current run', self.path.read_text())

    @unittest.skipIf(os.name == 'nt', 'Link creation can require special Windows permissions.')
    def test_link_destination_refused(self):
        """A log path cannot be redirected to a credential file or another directory."""
        target = self.path.with_name('secret.json')
        target.write_text('unchanged')
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.log.attach(self.path)
        self.assertEqual(target.read_text(), 'unchanged')

    def test_known_secrets_and_encoded_variants_redacted(self):
        """Credentials never survive into either the console or the retained file."""
        secret = 'test/private+secret123'
        self.log.protect(secret)
        self.log.attach(self.path)
        self.log.error(secret + ' ' + quote(secret, safe=''))
        for output in (self.output.getvalue(), self.path.read_text()):
            self.assertNotIn(secret, output)
            self.assertNotIn(quote(secret, safe=''), output)
            self.assertIn('[redacted]', output)

    def test_generic_token_and_callback_redaction(self):
        """Fallback pattern filtering also catches token formats and callback queries."""
        value = 'Bearer abc123 https://local/?code=code123&state=nonce123&client_id=client123 access_token=token123 person@example.com ya29.secret_token'
        self.log.attach(self.path)
        self.log.error(value)
        output = self.path.read_text()
        for sensitive in ('abc123', 'code123', 'nonce123', 'client123', 'token123', 'person@example.com', 'ya29.secret_token'):
            self.assertNotIn(sensitive, output)

    def test_console_only_data_not_logged(self):
        """Explicit calendar listing can show IDs locally without writing them to a log."""
        self.log.attach(self.path)
        self.log.console_data('{"calendar_id":"personal@example.com"}', 'Selected calendar')
        self.assertIn('personal@example.com', self.output.getvalue())
        self.assertNotIn('personal@example.com', self.path.read_text())

    def test_external_text_cannot_emit_ansi_or_multiline(self):
        """A news headline is data, not a terminal-control sequence."""
        self.log.info('Title\x1b[2J\r\nInjected')
        value = self.output.getvalue()
        self.assertNotIn('\x1b', value)
        self.assertNotIn('\r', value)
        self.assertEqual(len(value.splitlines()), 1)

    def test_ascii_console_accepts_non_ascii_headlines(self):
        """Windows-compatible escaping works without changing the real UTF-8 log."""
        class AsciiOutput(io.StringIO):
            """Reject Unicode as an older console would."""
            encoding = 'ascii'
            def write(self, value):
                """Simulate strict console encoding."""
                value.encode('ascii')
                return super().write(value)
        self.log.output = AsciiOutput()
        self.log.attach(self.path)
        self.log.info('雪 · café')
        self.assertIn('雪', self.path.read_text(encoding='utf-8'))
        self.assertIn('\\u96ea', self.log.output.getvalue())

    def test_logging_failure_does_not_retry_remote_work(self):
        """A disk-full diagnostic stream cannot turn a successful API write into failure."""
        class FullDisk:
            """Reject log writes but allow cleanup."""
            def write(self, value):
                """Simulate ENOSPC."""
                raise OSError('disk full')
            def close(self):
                """No resources to free."""
                pass
        self.log.path = self.path
        self.log.stream = FullDisk()
        self.log.success('Remote write finished')
        self.assertIsNotNone(self.log.log_error)
        self.assertIn('console reporting continues', self.output.getvalue())


class CalendarProgressTests(unittest.TestCase):
    """Check progress without changing stable IDs, saved fingerprints, or real calendars."""

    def setUp(self):
        """Use a private mock destination and a fixed release snapshot."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.data = fixture()
        self.values = {'client_id': 'fake-client-id', 'client_secret': 'fake-client-secret',
                       'refresh_token': 'fake-refresh-token', 'calendar_id': 'primary', 'calendar_ids': {}}
        self.targets = {game['id']: 'primary' for game in self.data['config']['games']}

    def run_command(self, arguments, api=None):
        """Run the real CLI with all Google traffic replaced by a deliberate fake."""
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(sys, 'argv', ['calendar_sync.py'] + arguments), \
             patch.object(calendar_sync, 'private_directory', return_value=self.root), \
             patch.object(calendar_sync, 'load_data', return_value=copy.deepcopy(self.data)), \
             patch.object(calendar_sync, 'credentials', return_value=self.values), \
             patch.object(calendar_sync, 'today', return_value='2026-10-04'), \
             patch.object(calendar_sync, 'GoogleAPI', return_value=api) as constructor:
            result = calendar_sync.main()
        return result, output.getvalue(), constructor

    def test_preview_has_progress_but_no_google_calls(self):
        """Preview remains offline and writes only its normal runtime output."""
        result, output, api = self.run_command([])
        self.assertEqual(result, 0)
        api.assert_not_called()
        self.assertIn('Step 3/3', output)
        self.assertIn('57 historical + 9 future', output)
        self.assertTrue((self.root / 'calendar-preview.json').exists())
        self.assertFalse((self.root / 'calendar-state.json').exists())

    def test_existing_fingerprint_is_compatible_and_preserved(self):
        """Upgrading logging must not cause the user's successful sync to reset."""
        desired = calendar_sync.desired_events(self.data, '2026-10-04')
        state = {'fingerprint': calendar_sync.plan_fingerprint(self.data, desired, self.targets),
                 'targetHash': digest(self.targets), 'lastSyncedOn': '2026-10-04'}
        path = self.root / 'calendar-state.json'
        write_json(path, state, private=True)
        before = path.read_bytes()
        result, output, api = self.run_command(['--apply'])
        self.assertEqual(result, 0)
        api.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertIn('Unchanged data and future window; Google API skipped.', output)
        self.assertIn('no remote audit', output)

    def test_first_mock_sync_reports_all_66_events(self):
        """Each event gets an explicit start/completion record and final counts."""
        api = FakeAPI()
        api.resolve_targets = lambda requested, private: {key: 'games' for key in requested}
        result, output, constructor = self.run_command(['--apply'], api)
        self.assertEqual(result, 0)
        self.assertEqual(len(api.events), 66)
        self.assertIn('Events 1/66 | Create', output)
        self.assertIn('Events 66/66 completed', output)
        self.assertIn('Step 6/6', output)
        self.assertIn('created=66', output)
        log = (self.root / 'calendar-sync.log').read_text()
        self.assertIn('Events 66/66 completed', log)
        self.assertNotIn('fake-client-secret', log)
        self.assertNotIn('fake-refresh-token', log)

    def test_force_audit_does_not_create_duplicates(self):
        """A deliberate remote audit can report unchanged rows without recreating them."""
        api = FakeAPI()
        api.resolve_targets = lambda requested, private: {key: 'games' for key in requested}
        self.assertEqual(self.run_command(['--apply'], api)[0], 0)
        result, output, _ = self.run_command(['--apply', '--force'], api)
        self.assertEqual(result, 0)
        self.assertEqual(len(api.events), 66)
        self.assertIn('created=0', output)
        self.assertIn('unchanged=66', output)
        self.assertIn('Forced remote recheck requested', output)

    def test_partial_failure_preserves_old_state_and_warns(self):
        """Progress must not call partially completed Google changes rolled back."""
        api = FakeAPI()
        api.resolve_targets = lambda requested, private: {key: 'games' for key in requested}
        original = api.request
        def fail(method, *args, **kwargs):
            """Fail after two completed inserts."""
            if method == 'POST' and len(api.events) == 2:
                raise RuntimeError('Simulated connection failure')
            return original(method, *args, **kwargs)
        api.request = fail
        state_path = self.root / 'calendar-state.json'
        state_path.write_text('{"fingerprint":"old"}')
        before = state_path.read_bytes()
        result, output, _ = self.run_command(['--apply'], api)
        self.assertEqual(result, 1)
        self.assertEqual(len(api.events), 2)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertIn('Some Google writes may already have completed', output)
        self.assertNotIn('Calendar sync complete:', output)

    def test_control_c_is_reported_without_false_success(self):
        """The command returns interruption status and never saves a success fingerprint."""
        api = FakeAPI()
        api.resolve_targets = lambda requested, private: {key: 'games' for key in requested}
        api.request = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        result, output, _ = self.run_command(['--apply'], api)
        self.assertEqual(result, 130)
        self.assertIn('interrupted', output)
        self.assertFalse((self.root / 'calendar-state.json').exists())

    def test_disabled_workflow_does_not_contact_google(self):
        """Progress additions never enable scheduled synchronization."""
        self.data['config']['automation']['googleCalendar']['enabled'] = False
        result, output, api = self.run_command(['--workflow'])
        self.assertEqual(result, 0)
        api.assert_not_called()
        self.assertIn('sync is disabled', output)

    def test_error_before_setup_is_visible(self):
        """Even an invalid runtime folder yields an immediate start and failure record."""
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(sys, 'argv', ['calendar_sync.py']), \
             patch.object(calendar_sync, 'private_directory', side_effect=ValueError('Runtime guard rejected the path')):
            self.assertEqual(calendar_sync.main(), 1)
        self.assertIn('Calendar started', output.getvalue())
        self.assertIn('CALENDAR SYNC FAILED', output.getvalue())

    def test_preexisting_live_log_survives_concurrent_preview(self):
        """A second local process cannot rotate the active sync's progress log."""
        log = self.root / 'calendar-sync.log'
        log.write_text('active synchronization')
        with exclusive_lock(self.root / 'calendar-sync.lock'):
            result, output, _ = self.run_command([])
        self.assertEqual(result, 1)
        self.assertEqual(log.read_text(), 'active synchronization')
        self.assertIn('Another local Calendar sync', output)

    def test_duplicate_create_recovery_reports_unchanged(self):
        """A retried create that finds its existing ID is not falsely counted as new."""
        api = FakeAPI()
        settings = calendar_sync.settings_for(self.data)
        plan = calendar_sync.desired_events(self.data, '2026-10-04')
        body = next(iter(plan.values()))['event']
        calendar_sync.insert_event(api, 'games', body, settings['namespace'])
        outcome = {}
        calendar_sync.insert_event(api, 'games', body, settings['namespace'], outcome=outcome)
        self.assertEqual(outcome, {'action': 'unchanged'})
        self.assertEqual(len(api.events), 1)


class GoogleNetworkProgressTests(unittest.TestCase):
    """Mock the HTTP layer to exercise wait, retry, pagination, and token reporting."""

    def setUp(self):
        """Use memory-only credentials and a captured reporter."""
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.progress = Progress('google', heartbeat=0.03)
        self.addCleanup(self.progress.close)
        self.api = google_api.GoogleAPI({'client_id': 'fake-client', 'client_secret': 'fake-secret', 'refresh_token': 'fake-refresh'}, self.progress)
        self.api.access_token = 'fake-access'
        self.api.expires = time.monotonic() + 1000

    def response(self, value):
        """Use a context-managed byte stream as a synthetic JSON response."""
        return io.BytesIO(json.dumps(value).encode())

    def test_retry_announces_backoff_and_attempt(self):
        """Throttling is visible without dumping the sensitive request URL."""
        error = HTTPError('https://secret.invalid/', 429, 'limited', {}, io.BytesIO(b''))
        with patch.object(google_api, 'urlopen', side_effect=[error, self.response({'ok': True})]), \
             patch.object(google_api.time, 'sleep'), patch.object(google_api.random, 'random', return_value=0):
            self.assertEqual(self.api.request('GET', '/calendars/personal@example.com/events'), {'ok': True})
        self.assertIn('HTTP 429', self.output.getvalue())
        self.assertIn('next attempt 2/5', self.output.getvalue())
        self.assertNotIn('personal@example.com', self.output.getvalue())

    def test_connection_failure_reports_retry_exhaustion(self):
        """Five failed attempts are bounded and do not pretend to finish successfully."""
        with patch.object(google_api, 'urlopen', side_effect=URLError('private endpoint')), patch.object(google_api.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'after retries'):
                self.api.request('GET', '/users/me/calendarList')
        self.assertIn('next attempt 5/5', self.output.getvalue())
        self.assertNotIn('private endpoint', self.output.getvalue())

    def test_wait_heartbeat_is_visible_during_slow_response(self):
        """A long network operation identifies its real method and resource."""
        def response(*args, **kwargs):
            """Emulate a slow remote server."""
            time.sleep(0.1)
            return self.response({'items': []})
        with patch.object(google_api, 'urlopen', side_effect=response):
            self.api.request('GET', '/calendars/hidden-id/events')
        self.assertIn('Still waiting: Google GET managed events', self.output.getvalue())
        self.assertNotIn('hidden-id', self.output.getvalue())

    def test_paginated_read_reports_page_counts(self):
        """Unknown totals use discovered counts rather than fictional percentages."""
        with patch.object(self.api, 'request', side_effect=[{'items': [1], 'nextPageToken': 'never-log-token'}, {'items': [2, 3]}]):
            self.assertEqual(self.api.list_all('/users/me/calendarList'), [1, 2, 3])
        self.assertIn('page 2: 2 records; 3 total', self.output.getvalue())
        self.assertNotIn('never-log-token', self.output.getvalue())
        self.assertNotIn('%', self.output.getvalue())

    def test_token_refresh_is_visible_without_secret_output(self):
        """Only the start/result of credential exchange is logged."""
        with patch.object(google_api, 'token_request', return_value={'access_token': 'secret-access-123', 'expires_in': 3600}):
            self.api.refresh()
        self.assertIn('Refreshing Google access authorization', self.output.getvalue())
        self.assertIn('authorization refreshed', self.output.getvalue())
        self.assertNotIn('secret-access-123', self.output.getvalue())

    def test_permission_failure_is_not_retried_as_transient(self):
        """Permanent permission errors fail instead of silently sleeping repeatedly."""
        error = HTTPError('https://private.invalid/', 403, 'forbidden', {}, io.BytesIO(b'{}'))
        with patch.object(google_api, 'urlopen', side_effect=error) as request:
            with self.assertRaises(google_api.ApiError):
                self.api.request('GET', '/users/me/calendarList')
        self.assertEqual(request.call_count, 1)
        self.assertNotIn('Retrying', self.output.getvalue())

    def test_token_endpoint_error_does_not_expose_body(self):
        """A diagnostic error cannot disclose an echoed client secret."""
        error = HTTPError('https://oauth2.googleapis.com/token', 400, 'bad', {}, io.BytesIO(b'{"secret":"do-not-print"}'))
        with patch.object(google_api, 'urlopen', side_effect=error):
            with self.assertRaises(google_api.ApiError) as caught:
                google_api.token_request({'client_secret': 'do-not-print'}, self.progress)
        self.assertNotIn('do-not-print', str(caught.exception))


class AuthProgressTests(unittest.TestCase):
    """Exercise local browser authorization without a real Google account or network."""

    def setUp(self):
        """Prepare only temporary development settings."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'google-calendar.json'
        self.values = {'client_id': 'private-client', 'client_secret': 'private-client-secret', 'refresh_token': '', 'calendar_id': 'primary', 'calendar_ids': {}}
        write_json(self.path, self.values)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.progress = Progress('google-auth', heartbeat=0.03)
        self.addCleanup(self.progress.close)
        self.log_path = self.root / 'google-auth.log'
        self.progress.attach(self.log_path)
        self.browser_url = ''
        self.deny = False
        self.interrupt = False
        self.fallback_seen = False

    def server_class(self):
        """Build a loopback-server substitute that delivers the current test's callback."""
        test = self
        class Server:
            """Use the actual callback handler without opening a socket."""
            def __init__(self, address, handler):
                """Capture the script's callback implementation."""
                self.handler, self.server_port = handler, 43210
            def __enter__(self):
                """Expose the fake server."""
                return self
            def __exit__(self, *args):
                """No actual socket was opened."""
                pass
            def handle_request(self):
                """Deliver a valid success/denial callback or simulate Ctrl+C."""
                if test.interrupt:
                    raise KeyboardInterrupt()
                time.sleep(0.06)
                nonce = parse_qs(urlsplit(test.browser_url).query)['state'][0]
                handler = object.__new__(self.handler)
                handler.path = '/?state=' + nonce + ('&error=access_denied' if test.deny else '&code=private-test-auth-code')
                handler.send_response = lambda *args: None
                handler.send_header = lambda *args: None
                handler.end_headers = lambda: None
                handler.wfile = io.BytesIO()
                handler.do_GET()
        return Server

    def open_browser(self, url):
        """Capture the authorization URL only inside the test fixture."""
        self.browser_url = url
        paths = list(self.root.glob('authorization-*.html'))
        self.fallback_seen = bool(paths) and 'Continue with Google' in paths[0].read_text()
        return True

    def authorize(self, token=None):
        """Drive the real authorization logic using browser and HTTP mocks."""
        token = token or {'refresh_token': 'private-new-refresh', 'access_token': 'private-new-access', 'scope': ' '.join(google_api.SCOPES)}
        with patch.object(google_auth, 'HTTPServer', self.server_class()), \
             patch.object(google_auth.webbrowser, 'open', side_effect=self.open_browser), \
             patch.object(google_auth, 'token_request', return_value=token):
            google_auth.authorize(self.path, self.progress)

    def test_success_reports_wait_and_saves_token_without_logging_it(self):
        """Consent waits produce heartbeat records but no URL/code/token leakage."""
        self.authorize()
        log = self.log_path.read_text()
        self.assertIn('Authorization 4/4', log)
        self.assertIn('Still waiting: Browser consent', log)
        self.assertTrue(self.fallback_seen)
        self.assertEqual(read_json(self.path)['refresh_token'], 'private-new-refresh')
        self.assertEqual(list(self.root.glob('authorization-*.html')), [])
        for secret in ('private-client-secret', 'private-new-refresh', 'private-new-access', 'private-test-auth-code', self.browser_url):
            self.assertNotIn(secret, log)
            self.assertNotIn(secret, self.output.getvalue())

    def test_denial_keeps_settings_and_removes_temporary_link(self):
        """A denied attempt does not overwrite existing private settings."""
        before = self.path.read_bytes()
        self.deny = True
        with self.assertRaisesRegex(ValueError, 'denied or timed out'):
            self.authorize()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob('authorization-*.html')), [])

    def test_interruption_removes_temporary_link(self):
        """Ctrl+C cleans up transient login material without resetting credentials."""
        before = self.path.read_bytes()
        self.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.authorize()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob('authorization-*.html')), [])

    def test_incomplete_scope_grant_does_not_replace_credentials(self):
        """Only the complete requested permission set can be saved."""
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'All three'):
            self.authorize({'refresh_token': 'private-new-refresh', 'scope': google_api.SCOPES[0]})
        self.assertEqual(self.path.read_bytes(), before)

    def test_list_calendar_values_not_written_to_log(self):
        """Only the console displays the user's explicitly requested destination IDs."""
        api = type('API', (), {'list_all': lambda *args: [{'id': 'personal@example.com', 'summary': 'My private calendar name', 'primary': True, 'accessRole': 'owner'}]})()
        # Close the fixture reporter before the CLI uses the same log path.
        self.progress.close()
        with patch.object(sys, 'argv', ['google_auth.py', '--list-calendars']), \
             patch.object(google_auth, 'private_directory', return_value=self.root), \
             patch.object(google_auth, 'credentials', return_value=self.values), \
             patch.object(google_auth, 'GoogleAPI', return_value=api):
            self.assertEqual(google_auth.main(), 0)
        self.assertIn('personal@example.com', self.output.getvalue())
        self.assertNotIn('personal@example.com', self.log_path.read_text())
        self.assertNotIn('My private calendar name', self.log_path.read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
