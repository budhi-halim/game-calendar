"""Bounded worker scheduling, deterministic imports, and isolated real-browser fixtures."""

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
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hunt
import daily
from common import digest
from feed_pool import CollectionCancelled, check_cancelled, collect_parallel
from progress import Progress
from run_history import compact_hunt, code_fingerprint
from sources import CollectedArticles, FeedReadError, collect_feed, parse_article

FIXTURE = json.loads((Path(__file__).parent / 'fixture-data.json').read_text())


def make_jobs(count=3):
    """Create distinct publisher jobs without live URLs or secrets."""
    return [{'gameId': f'game{i}', 'feed': {'url': f'https://publisher{i}.example/news'},
             'settings': {'seedArticles': [{'title': 'kept'}]}} for i in range(count)]


class PoolTests(unittest.TestCase):
    """Check actual Python threads without browser or network dependencies."""

    def test_three_workers_actually_overlap(self):
        """A barrier cannot complete unless all three collector threads are active."""
        barrier, identities, metrics = threading.Barrier(3), set(), {}
        lock = threading.Lock()
        def collect(job, cancel):
            """Record each owner and rendezvous with the other workers."""
            with lock:
                identities.add(threading.get_ident())
            barrier.wait(timeout=5)
            return job['gameId']
        result = list(collect_parallel(make_jobs(), collect, 3, metrics))
        self.assertEqual(len(identities), 3)
        self.assertNotIn(threading.get_ident(), identities)
        self.assertEqual(metrics['peakActiveFeeds'], 3)
        self.assertEqual(metrics['completedFeeds'], 3)
        self.assertEqual({row['value'] for row in result}, {'game0', 'game1', 'game2'})

    def test_worker_limit_one_is_serial(self):
        """The fallback keeps one owner and one active collector."""
        identities, metrics = set(), {}
        def collect(job, cancel):
            """Record the owner thread."""
            identities.add(threading.get_ident())
            return job['gameId']
        rows = list(collect_parallel(make_jobs(), collect, 1, metrics))
        self.assertEqual(len(identities), 1)
        self.assertEqual(metrics['peakActiveFeeds'], 1)
        self.assertEqual([row['gameId'] for row in rows], ['game0', 'game1', 'game2'])

    def test_same_host_is_never_collected_twice_at_once(self):
        """A different hostname can proceed while a same-host job stays queued."""
        jobs = make_jobs()
        jobs[1]['feed']['url'] = jobs[0]['feed']['url']
        guard, active, peaks = threading.Lock(), {}, {}
        def collect(job, cancel):
            """Simulate bounded I/O and observe host-local concurrency."""
            host = job['feed']['url']
            with guard:
                active[host] = active.get(host, 0) + 1
                peaks[host] = max(peaks.get(host, 0), active[host])
            time.sleep(0.03)
            with guard:
                active[host] -= 1
            return True
        metrics = {}
        self.assertEqual(len(list(collect_parallel(jobs, collect, 3, metrics))), 3)
        self.assertEqual(max(peaks.values()), 1)
        self.assertEqual(metrics['peakActiveFeeds'], 2)

    def test_jobs_are_deep_copied_before_workers_mutate_them(self):
        """No worker receives the canonical configuration by reference."""
        jobs = make_jobs()
        before = copy.deepcopy(jobs)
        def collect(job, cancel):
            """Deliberately mutate only this worker's disposable snapshot."""
            job['settings']['seedArticles'][0]['title'] = 'changed'
            return True
        list(collect_parallel(jobs, collect, 3))
        self.assertEqual(jobs, before)

    def test_results_are_consumed_on_caller_thread(self):
        """Reporting and canonical saves remain on the main/caller side."""
        owner = threading.get_ident()
        for _ in collect_parallel(make_jobs(), lambda job, cancel: True, 3):
            self.assertEqual(threading.get_ident(), owner)

    def test_first_failure_prevents_starting_queued_work(self):
        """No later feed is opened after a serial worker fails."""
        opened, metrics = [], {}
        def collect(job, cancel):
            """Fail before any succeeding job starts."""
            opened.append(job['gameId'])
            raise RuntimeError('fixture failure')
        rows = list(collect_parallel(make_jobs(6), collect, 1, metrics))
        self.assertEqual(opened, ['game0'])
        self.assertEqual(metrics['failedFeeds'], 1)
        self.assertEqual(metrics['cancelledFeeds'], 5)
        self.assertEqual(len(rows), 6)

    def test_failure_stops_active_workers_cooperatively(self):
        """Concurrent requests already in flight stop at their next checkpoint."""
        barrier, metrics = threading.Barrier(3), {}
        def collect(job, cancel):
            """One worker fails; peers wait only until they see the signal."""
            barrier.wait(timeout=5)
            if job['gameId'] == 'game0':
                raise RuntimeError('fixture failure')
            for _ in range(500):
                cancel()
                time.sleep(0.005)
            raise AssertionError('Cancellation was never delivered')
        rows = list(collect_parallel(make_jobs(), collect, 3, metrics))
        self.assertEqual(sorted(row['status'] for row in rows), ['cancelled', 'cancelled', 'failed'])
        self.assertFalse(any(t.name.startswith('publisher-feed') for t in threading.enumerate()))

    def test_failure_preserves_structured_diagnostics(self):
        """Worker errors retain the original bounded article failure evidence."""
        def fail(job, cancel):
            """Return a publisher-reader failure rather than a bare exception string."""
            raise FeedReadError('not ready', {'headings': ['Loading']})
        result = list(collect_parallel(make_jobs(1), fail))[0]
        self.assertEqual(result['failureDetails'], {'headings': ['Loading']})

    def test_closing_iterator_joins_and_cancels_other_workers(self):
        """Caller cancellation cannot leave background collectors using a closed log."""
        barrier = threading.Barrier(3)
        def collect(job, cancel):
            """Finish one feed while the others remain in a cancellable operation."""
            barrier.wait(timeout=5)
            if job['gameId'] == 'game0':
                return True
            while True:
                cancel()
                time.sleep(0.005)
        with contextlib.closing(collect_parallel(make_jobs(), collect, 3)) as rows:
            self.assertEqual(next(rows)['gameId'], 'game0')
        self.assertFalse(any(t.name.startswith('publisher-feed') for t in threading.enumerate()))

    def test_keyboard_interrupt_is_not_swallowed(self):
        """Interrupts reach the CLI after worker shutdown."""
        def collect(job, cancel):
            """Simulate an interrupt from an owning collector."""
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            list(collect_parallel(make_jobs(), collect, 1))
        self.assertFalse(any(t.name.startswith('publisher-feed') for t in threading.enumerate()))

    def test_invalid_limits_fail_before_collection(self):
        """Avoid unbounded or accidentally truthy worker configurations."""
        for value in (0, 4, -1, True, 1.5, '3', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                list(collect_parallel(make_jobs(), lambda *args: self.fail('collector called'), value))

    def test_empty_queue_is_noop(self):
        """An empty test batch creates no worker threads."""
        metrics = {}
        self.assertEqual(list(collect_parallel([], lambda *args: self.fail('collector called'), metrics=metrics)), [])
        self.assertEqual(metrics['workerLimit'], 0)
        self.assertEqual(metrics['elapsedSeconds'], 0)

    def test_duplicate_game_jobs_rejected(self):
        """Never run or count a game twice."""
        jobs = make_jobs()
        jobs[1]['gameId'] = jobs[0]['gameId']
        with self.assertRaises(ValueError):
            list(collect_parallel(jobs, lambda *args: True))

    def test_unsafe_feed_urls_rejected(self):
        """Validate scheduler host keys before creating workers."""
        for url in ('http://example.org', 'https:///missing-host', 'https://secret@example.org/news'):
            jobs = make_jobs(1)
            jobs[0]['feed']['url'] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                list(collect_parallel(jobs, lambda *args: True))

    def test_cancellation_check(self):
        """The signal has no effect until explicitly set."""
        event = threading.Event()
        check_cancelled(event)
        event.set()
        with self.assertRaises(CollectionCancelled):
            check_cancelled(event)


class ParallelProgressTests(unittest.TestCase):
    """Parallel progress remains flushed, readable, and credential-redacted."""

    def test_heartbeat_names_all_active_tasks(self):
        """Do not imply only the most recently printed collector is still running."""
        output, barrier, stop = io.StringIO(), threading.Barrier(3), threading.Event()
        with contextlib.redirect_stdout(output):
            reporter = Progress('hunt', heartbeat=0.02)
            def worker(name):
                """Wait while exposing a per-thread operation to the heartbeat."""
                with reporter.task(name):
                    reporter.info(f'[{name}] Reading an announcement')
                    barrier.wait(timeout=5)
                    stop.wait(2)
            threads = [threading.Thread(target=worker, args=(name,)) for name in ('hsr', 'zzz')]
            try:
                for thread in threads:
                    thread.start()
                barrier.wait(timeout=5)
                time.sleep(0.1)
            finally:
                stop.set()
                for thread in threads:
                    thread.join(3)
                reporter.close()
        waits = [line for line in output.getvalue().splitlines() if 'WAIT' in line]
        self.assertTrue(any('hsr:' in line and 'zzz:' in line for line in waits))
        self.assertEqual(reporter.tasks, {})

    def test_concurrent_lines_are_complete_and_redacted(self):
        """Multiple writers cannot splice or buffer status records."""
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            reporter = Progress('hunt')
            reporter.attach(Path(folder) / 'hunt.log')
            reporter.protect('secret-value')
            def worker(name):
                """Write a series of individually flushed records."""
                with reporter.task(name):
                    for number in range(40):
                        reporter.info(f'[{name}] item {number} secret-value')
            threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(3)]
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                text = reporter.path.read_text()
                self.assertNotIn('secret-value', text)
                self.assertEqual(sum('item ' in line for line in text.splitlines()), 120)
                self.assertTrue(all(line.startswith('[') and '[hunt]' in line for line in text.splitlines()))
            finally:
                reporter.close()


class ParallelIntegrationTests(unittest.TestCase):
    """The CLI imports the same evidence regardless of worker completion order."""

    def run_fixture(self, workers, fault=False):
        """Run a complete check with fixture announcements and inspect only local output."""
        original = copy.deepcopy(FIXTURE)
        def collect(game_id, feed, settings, executable=None, progress=None, headed=False, cancel=None):
            """Complete feeds in deliberately different order with established releases."""
            time.sleep({'hsr': 0.035, 'zzz': 0.02, 'endfield': 0.005}[game_id])
            if fault and game_id == 'endfield':
                raise RuntimeError('controlled feed failure')
            cancel()
            data = CollectedArticles()
            data.discovery.update(coverage='current-anchor-overlap', checkpointReached=True, discoveredURLs=[])
            label = {'hsr': 'Version 4.6', 'zzz': 'Version 3.2', 'endfield': '[Dreamscape of Wind and Snow] Version'}[game_id]
            date = {'hsr': '2026/09/28', 'zzz': '2026/09/09', 'endfield': '2026/09/02'}[game_id]
            data.append({'gameId': game_id, 'url': feed['url'] + '/901', 'title': label + ' Update Details',
                         'text': 'Update Time\n' + date + ' 06:00 (UTC+8)', 'publishedOn': None})
            return data
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()), \
                patch.object(sys, 'argv', ['hunt.py', '--check', '--workers', str(workers)]), \
                patch.object(hunt, 'load_data', return_value=original), \
                patch.object(hunt, 'private_directory', return_value=Path(folder)), \
                patch.object(hunt, 'today', return_value='2026-10-04'), \
                patch.object(hunt, 'read_json', side_effect=lambda path: original[Path(path).stem]), \
                patch.object(hunt, 'collect_feed', side_effect=collect), patch.object(hunt, 'save_data') as save:
            code = hunt.main()
            report = json.loads((Path(folder) / 'hunt-report.json').read_text())
            save.assert_not_called()
            self.assertEqual(original, FIXTURE)
            return code, report

    def test_parallel_import_matches_serial_import(self):
        """Thread completion order cannot change IDs, observations, or release decisions."""
        code1, sequential = self.run_fixture(1)
        code3, parallel = self.run_fixture(3)
        self.assertEqual((code1, code3), (0, 0))
        for key in ('observations', 'proposedChanges', 'notices', 'deferredNames', 'changed'):
            self.assertEqual(sequential[key], parallel[key], key)
        self.assertEqual(parallel['collection']['peakActiveFeeds'], 3)
        self.assertEqual(list(parallel['games']), ['hsr', 'zzz', 'endfield'])

    def test_one_failed_parallel_feed_blocks_import(self):
        """Partial collector success is never a passed report or a public write."""
        code, report = self.run_fixture(3, True)
        self.assertEqual(code, 1)
        self.assertFalse(report['ok'])
        self.assertEqual(report['games']['endfield']['status'], 'failed')
        self.assertTrue(report['errors'])

    def test_compact_history_keeps_operational_metrics_only(self):
        """Retained performance counters do not add worker internals or private values."""
        report = {'collection': {'strategy': 'threaded-feeds', 'peakActiveFeeds': 3, 'client_secret': 'PRIVATE'},
                  'games': {'hsr': {'elapsedSeconds': 23.45}}}
        compact = compact_hunt(report)
        self.assertEqual(compact['collection']['peakActiveFeeds'], 3)
        self.assertEqual(compact['games']['hsr']['elapsedSeconds'], 23.45)
        self.assertNotIn('PRIVATE', json.dumps(compact))

    def test_worker_module_changes_code_fingerprint(self):
        """The monitoring record identifies changes to concurrency code too."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'automation').mkdir()
            path = root / 'automation/feed_pool.py'
            path.write_text('first')
            before = code_fingerprint(root)
            path.write_text('second')
            self.assertNotEqual(code_fingerprint(root), before)


class ParallelBrowserTests(unittest.TestCase):
    """Run production collectors on real Chromium instances with simulated page navigation."""

    def test_playwright_instances_remain_in_owner_threads(self):
        """Exercise three isolated Playwright drivers, browsers, pages, and teardown paths."""
        try:
            import playwright.sync_api as api
        except ImportError:
            self.skipTest('Playwright is not installed')
        actual_factory, owners, guard = api.sync_playwright, set(), threading.Lock()
        feed_settings = copy.deepcopy(FIXTURE['config']['automation']['sources'])
        feed_settings.update(timeoutSeconds=8, settleSeconds=0.02, maxListPages=1,
                             publicationWaitSeconds=0, maxArticlesPerGame=10)
        jobs = [{'gameId': key, 'feed': value, 'settings': feed_settings}
                for key, value in feed_settings['feeds'].items()]
        barrier = threading.Barrier(3)

        class Driver:
            """Proxy only fixture routing; every Playwright call stays in this driver owner."""
            def __enter__(self):
                """Create a real driver on this collector's thread."""
                self.owner = threading.get_ident()
                with guard:
                    owners.add(self.owner)
                barrier.wait(timeout=15)
                self.native = actual_factory()
                driver = self.native.__enter__()
                def launch(**kwargs):
                    """Wrap a real Chromium without sharing it with another worker."""
                    self.check()
                    browser = driver.chromium.launch(**kwargs)
                    def context(**options):
                        """Use real isolated pages while replacing external navigation only."""
                        self.check()
                        ctx = browser.new_context(**options)
                        owner_driver = self

                        class Page:
                            """Keep DOM evaluation real and the navigation URL fixture-local."""
                            def __init__(self, page):
                                """Bind the actual page to this worker's thread."""
                                self.page, self.url = page, 'about:blank'

                            def goto(self, url, **kwargs):
                                """Load fixture HTML without any external page requests."""
                                owner_driver.check()
                                self.url = url
                                prefix = url.split('/en-us/news')[0]
                                if url.endswith('/news'):
                                    content = '<main><a href="' + prefix + '/en-us/news/901"><h2>Version 4.6 Update Details</h2></a></main>'
                                else:
                                    content = '<main><article><h1>Version 4.6 Update Details</h1><time pubdate datetime="2026-10-04">2026-10-04</time><p>Update Time</p><p>2026/11/11 06:00 (UTC+8). Complete fixture announcement with independently matched article content.</p></article></main>'
                                self.page.set_content(content)
                                return SimpleNamespace(status=200)

                            def __getattr__(self, name):
                                """All other operations use the real DOM in this thread."""
                                owner_driver.check()
                                return getattr(self.page, name)

                        return SimpleNamespace(new_page=lambda: Page(ctx.new_page()),
                                               set_default_timeout=ctx.set_default_timeout)
                    def close():
                        """Close the browser on the thread that created it."""
                        self.check()
                        browser.close()
                    return SimpleNamespace(new_context=context, close=close)
                return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

            def check(self):
                """Reject cross-thread access in any browser lifecycle hook."""
                if self.owner != threading.get_ident():
                    raise AssertionError('Playwright object crossed thread boundary')

            def __exit__(self, *args):
                """Stop the driver in its owner thread."""
                self.check()
                return self.native.__exit__(*args)

        def collect(job, cancel):
            """Run the original parser and page verifier concurrently."""
            return collect_feed(job['gameId'], job['feed'], job['settings'],
                                executable=os.environ.get('PATCH_CALENDAR_TEST_BROWSER'), cancel=cancel)

        metrics = {}
        with patch.object(api, 'sync_playwright', side_effect=Driver), patch('sources.today', return_value='2026-10-04'):
            results = list(collect_parallel(jobs, collect, 3, metrics))
        self.assertEqual(len(owners), 3)
        self.assertEqual(metrics['peakActiveFeeds'], 3)
        for row in results:
            self.assertEqual(row['status'], 'collected', row)
            self.assertEqual(len(row['value']), 1)
            article = row['value'][0]
            self.assertEqual(article['gameId'], row['gameId'])
            self.assertEqual(article['publishedOn'], '2026-10-04')
            self.assertEqual(parse_article(article)[0]['date'], '2026-11-11')


if __name__ == '__main__':
    unittest.main()
