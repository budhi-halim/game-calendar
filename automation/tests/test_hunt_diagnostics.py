"""Offline checks for progress reporting, read-only checks, and interrupted hunts."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hunt


class HuntDiagnosticsTests(unittest.TestCase):
    """Exercise the command entry point without opening a browser or Google session."""

    def run_hunt(self, arguments, collect, parse=None):
        """Run with private temporary diagnostics and reject every data-write attempt."""
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.object(sys, 'argv', ['hunt.py'] + arguments), \
                    patch.object(hunt, 'private_directory', return_value=directory), \
                    patch.object(hunt, 'today', return_value='2026-10-04'), \
                    patch.object(hunt, 'collect_feed', side_effect=collect) as collector, \
                    patch.object(hunt, 'parse_article', side_effect=parse or (lambda *args: [])), \
                    patch.object(hunt, 'merge_observation', return_value=False), \
                    patch.object(hunt, 'save_data') as save:
                result = hunt.main()
            report_path = directory / 'hunt-report.json'
            report = json.loads(report_path.read_text()) if report_path.exists() else None
            log_path = directory / 'hunt-progress.log'
            log = log_path.read_text(encoding='utf-8') if log_path.exists() else ''
            return result, output.getvalue(), report, log, collector, save

    def test_first_feed_failure_stops_before_other_feeds(self):
        """A failed read must not trigger hundreds of further page reads or save data."""
        result, output, report, log, collect, save = self.run_hunt(['--check', '--workers', '1'], RuntimeError('test navigation timeout'))
        self.assertEqual(result, 1)
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['ok'])
        self.assertIn('test navigation timeout', log)
        self.assertIn('Hunt started', output)
        self.assertIn('Progress log:', output)
        save.assert_not_called()

    def test_article_failure_details_saved_in_report(self):
        """The next run must explain the page mismatch instead of only naming a timeout."""
        from sources import FeedReadError
        details = {'expectedTitle': 'Version 4.7', 'headings': ['Loading...'], 'networkErrors': []}
        result, output, report, log, collect, save = self.run_hunt(['--check'], FeedReadError('Article not ready', details))
        self.assertEqual(result, 1)
        self.assertEqual(report['games']['hsr']['failureDetails'], details)
        save.assert_not_called()

    def test_control_c_saves_interrupted_report(self):
        """Cancellation preserves the progress log and cannot be mistaken for success."""
        result, output, report, log, collect, save = self.run_hunt(['--check'], KeyboardInterrupt())
        self.assertEqual(result, 130)
        self.assertEqual(report['status'], 'interrupted')
        self.assertFalse(report['ok'])
        self.assertIn('Hunt interrupted', log)
        self.assertIn('finishedAt', report)
        save.assert_not_called()

    def test_game_scope_is_check_only(self):
        """Partial source checks are blocked in write mode."""
        with patch.object(sys, 'argv', ['hunt.py', '--game', 'hsr']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                hunt.main()
        self.assertEqual(error.exception.code, 2)

    def test_unknown_game_does_not_collect(self):
        """An invalid scope yields a clear failure without browser traffic."""
        result, output, report, log, collect, save = self.run_hunt(['--check', '--game', 'unknown'], [])
        self.assertEqual(result, 1)
        self.assertIn('Unknown game', log)
        collect.assert_not_called()
        save.assert_not_called()

    def test_game_scope_and_visible_browser_are_forwarded(self):
        """Diagnostic browser visibility must not enable writes or other feeds."""
        result, output, report, log, collect, save = self.run_hunt(['--check', '--game', 'zzz', '--headed'], RuntimeError('test'))
        self.assertEqual(report['scope'], ['zzz'])
        self.assertEqual(collect.call_args.args[0], 'zzz')
        self.assertTrue(collect.call_args.kwargs['headed'])
        save.assert_not_called()

    def test_check_success_never_saves_public_data(self):
        """A complete successful check still writes only private diagnostics."""
        def collect(game_id, *args, progress=None, **kwargs):
            """Emit a browser-like progress message and return a harmless record."""
            progress('Test page read completed.')
            return [{'gameId': game_id}]

        def parse(article, style):
            """Return an already-known label without a dated override."""
            return [{'gameId': article['gameId'], 'url': 'https://example.test/news/1',
                     'label': '1.0', 'date': None, 'warnings': []}]

        result, output, report, log, collector, save = self.run_hunt(['--check'], collect, parse)
        self.assertEqual(result, 0)
        self.assertEqual(collector.call_count, 3)
        self.assertTrue(report['ok'])
        self.assertIn('Live check passed', output)
        self.assertIn('Test page read completed', log)
        save.assert_not_called()

    def test_validate_does_not_create_live_report_or_collect(self):
        """Offline validation must remain independent of browser installation."""
        result, output, report, log, collect, save = self.run_hunt(['--validate'], [])
        self.assertEqual(result, 0)
        self.assertIsNone(report)
        self.assertEqual(log, '')
        collect.assert_not_called()
        save.assert_not_called()

    def test_log_is_flushed_before_close(self):
        """The last progress line remains available even if the process later hangs."""
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            report = {}
            log = hunt.HuntLog(Path(folder), report)
            try:
                log('Reading a version announcement.')
                self.assertIn('Reading a version announcement.', log.path.read_text(encoding='utf-8'))
            finally:
                log.close()


if __name__ == '__main__':
    unittest.main()
