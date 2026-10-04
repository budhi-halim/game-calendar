"""Run isolated publisher collectors concurrently without sharing browser objects."""

import copy
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urlsplit


class CollectionCancelled(Exception):
    """Signal cooperative shutdown between bounded browser operations."""


def check_cancelled(stop):
    """Stop pending work without trying to kill a thread inside Playwright."""
    if stop.is_set():
        raise CollectionCancelled('Collection stopped because another feed failed or the run was interrupted.')


def collect_parallel(jobs, collect, workers=3, metrics=None):
    """Yield completed results on the caller thread; one collector per publisher host."""
    if type(workers) is not int or not 1 <= workers <= 3:
        raise ValueError('feedWorkers must be an integer from 1 through 3.')
    metrics = metrics if metrics is not None else {}
    queue = [copy.deepcopy(job) for job in jobs]
    identities = [job['gameId'] for job in queue]
    if len(set(identities)) != len(identities):
        raise ValueError('Each game must have exactly one collection job.')
    for job in queue:
        parsed = urlsplit(job['feed']['url'])
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Feed workers require a valid HTTPS publisher URL.')
        job['_host'] = parsed.hostname.lower()
    limit = min(workers, len(queue))
    metrics.update({'strategy': 'threaded-feeds', 'workerLimit': limit,
                    'perHostLimit': 1, 'peakActiveFeeds': 0, 'completedFeeds': 0,
                    'failedFeeds': 0, 'cancelledFeeds': 0})
    if not queue:
        metrics['elapsedSeconds'] = 0.0
        return
    stop, guard = threading.Event(), threading.Lock()
    active, peak = 0, 0
    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=limit, thread_name_prefix='publisher-feed')
    pending, hosts = {}, set()

    def execute(job):
        """Create and use all browser resources inside this single owner thread."""
        nonlocal active, peak
        began = time.monotonic()
        counted = False
        try:
            check_cancelled(stop)
            with guard:
                active += 1
                peak = max(peak, active)
                counted = True
            value = collect(job, lambda: check_cancelled(stop))
            return {'gameId': job['gameId'], 'status': 'collected', 'value': value,
                    'elapsedSeconds': round(time.monotonic() - began, 3)}
        except CollectionCancelled as error:
            return {'gameId': job['gameId'], 'status': 'cancelled', 'error': str(error),
                    'elapsedSeconds': round(time.monotonic() - began, 3)}
        except Exception as error:
            stop.set()
            result = {'gameId': job['gameId'], 'status': 'failed',
                      'error': f'{type(error).__name__}: {str(error)[:1200]}',
                      'elapsedSeconds': round(time.monotonic() - began, 3)}
            if getattr(error, 'details', None):
                result['failureDetails'] = error.details
            return result
        except BaseException:
            stop.set()
            raise
        finally:
            if counted:
                with guard:
                    active -= 1

    def record(result):
        """Update metrics only on the caller thread before its reporting callback runs."""
        field = {'collected': 'completedFeeds', 'failed': 'failedFeeds', 'cancelled': 'cancelledFeeds'}[result['status']]
        metrics[field] += 1
        with guard:
            metrics['peakActiveFeeds'] = peak
        metrics['elapsedSeconds'] = round(time.monotonic() - started, 3)
        return result

    try:
        while queue or pending:
            # Scheduling by hostname avoids simultaneous flows even if two configs share a site.
            while len(pending) < limit and not stop.is_set():
                index = next((i for i, job in enumerate(queue) if job['_host'] not in hosts), None)
                if index is None:
                    break
                job = queue.pop(index)
                hosts.add(job['_host'])
                pending[pool.submit(execute, job)] = job
            if not pending:
                break
            done, _ = wait(tuple(pending), timeout=0.2, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=lambda item: identities.index(pending[item]['gameId'])):
                job = pending.pop(future)
                hosts.remove(job['_host'])
                yield record(future.result())
        for job in queue:
            yield record({'gameId': job['gameId'], 'status': 'cancelled',
                          'error': 'Not started after another feed failed.', 'elapsedSeconds': 0.0})
    finally:
        # Python cannot safely terminate an executing thread. The collector checks this signal
        # between operations, and an in-flight navigation retains its configured timeout.
        stop.set()
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        with guard:
            metrics['peakActiveFeeds'] = peak
        metrics['elapsedSeconds'] = round(time.monotonic() - started, 3)
