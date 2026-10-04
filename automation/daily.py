"""Run hunt then Calendar with explicit write controls and retained audit evidence."""

import argparse
import os
import re
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from common import JAKARTA, ROOT, exclusive_lock, load_data, private_directory, read_json, write_json
from progress import Progress
from run_history import archive_run, code_fingerprint, compact_calendar, compact_hunt, compact_pipeline, record_public_run, run_identity


def stage(command, progress, timeout, google=False):
    """Forward child progress live; terminate only our child tree when a stage times out."""
    environment = dict(os.environ, PYTHONUNBUFFERED='1')
    if not google:
        environment.pop('PATCH_CALENDAR_GOOGLE_JSON', None)
    options = {'cwd': ROOT, 'env': environment}
    if os.name != 'nt':
        options['start_new_session'] = True
    process = subprocess.Popen([sys.executable, *command], **options)
    try:
        with progress.waiting('Running ' + command[0] + '; child progress is printed above'):
            return process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, check=False)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name != 'nt':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait(timeout=5)
        raise


def fresh_report(path, previous_id):
    """Reject an old report left behind when a child fails before initializing its output."""
    if not path.is_file():
        return None
    report = read_json(path)
    return report if report.get('runId') and report['runId'] != previous_id else None


def prior_id(path):
    """Read just the prior invocation identity without deleting its diagnostics."""
    try:
        return read_json(path).get('runId') if path.is_file() else None
    except (ValueError, OSError):
        return None


def clean_state(value):
    """Permit only fingerprints and a validated civil date in a shared CI cache."""
    from common import parse_day
    result = {key: value.get(key) for key in ('fingerprint', 'targetHash', 'lastSyncedOn')}
    if not all(isinstance(result[key], str) and re.fullmatch('[0-9a-f]{64}', result[key]) for key in ('fingerprint', 'targetHash')):
        raise ValueError('Invalid cached synchronization fingerprint.')
    parse_day(result['lastSyncedOn'])
    return result


def check_default_branch(progress):
    """Before external writes in Actions, reject a branch that moved during collection."""
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        return
    branch = os.environ.get('PATCH_CALENDAR_DEFAULT_BRANCH', '')
    if not branch or branch.startswith('-') or '\n' in branch:
        raise ValueError('A safe default-branch name is required in the workflow.')
    progress.info('Checking that the default branch has not changed before Calendar synchronization.')
    subprocess.run(['git', 'fetch', 'origin', branch], cwd=ROOT, check=True, capture_output=True, timeout=45)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).strip()
    remote = subprocess.check_output(['git', 'rev-parse', 'FETCH_HEAD'], cwd=ROOT).strip()
    if head != remote:
        raise ValueError('The default branch changed during the hunt. Calendar was not started; rerun on the latest branch.')


def main():
    """Default to a live check and offline Calendar preview, never implicit Google writes."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Live hunt check and offline Calendar preview; no public data or Google writes (default).')
    mode.add_argument('--apply', action='store_true', help='Save validated data; Calendar still obeys googleCalendar.enabled.')
    parser.add_argument('--sync-calendar', action='store_true', help='With --apply, explicitly sync Google regardless of the enabled setting.')
    parser.add_argument('--publish-history', action='store_true', help='With --apply, write compact public-safe run history under automation/history; no Git commands locally.')
    parser.add_argument('--workers', type=int, choices=range(1, 4), help='Override hunter feed concurrency (1-3) for this run; no config edit.')
    parser.add_argument('--browser-executable', help='Optional installed Chromium executable for the hunter.')
    args = parser.parse_args()
    if (args.sync_calendar or args.publish_history) and not args.apply:
        parser.error('--sync-calendar and --publish-history require --apply.')
    progress = Progress('daily')
    directory, report_path = None, None
    result = {'schemaVersion': 1, 'component': 'pipeline', 'runId': run_identity(),
              'startedAt': datetime.now(JAKARTA).isoformat(timespec='seconds'),
              'mode': 'apply' if args.apply else 'check', 'trigger': os.environ.get('GITHUB_EVENT_NAME') or 'local',
              'status': 'running', 'huntApplied': False, 'hunt': {'status': 'not-run'},
              'calendar': {'status': 'not-run'}, 'publicHistoryWritten': False}
    progress.report = result
    exit_code = 1
    run_lock = None
    lock_acquired = False
    retain_days = 180
    try:
        progress.info('Daily pipeline: ' + ('APPLY; validated local data can change.' if args.apply else 'CHECK; live publisher reads and offline preview only.'))
        directory = private_directory()
        report_path = directory / 'pipeline-report.json'
        run_lock = exclusive_lock(directory / 'pipeline.lock')
        run_lock.__enter__()
        lock_acquired = True
        result['codeFingerprint'] = code_fingerprint(ROOT)
        revision = os.environ.get('GITHUB_SHA', '')
        if re.fullmatch('[0-9a-f]{40,64}', revision):
            result['gitRevision'] = revision
        progress.attach(directory / 'pipeline-progress.log')
        write_json(report_path, result, private=True)
        data = load_data()
        retain_days = data['config'].get('automation', {}).get('monitoring', {}).get('runRetentionDays', 180)
        timeout = data['config'].get('automation', {}).get('monitoring', {}).get('stageTimeoutSeconds', 1200)
        if type(timeout) is not int or not 30 <= timeout <= 2400:
            raise ValueError('stageTimeoutSeconds must be between 30 and 2400.')
        state_path = directory / 'calendar-state.json'
        cache_path = directory / 'ci-state' / 'calendar-state.json'
        if os.environ.get('GITHUB_ACTIONS') == 'true' and not state_path.exists() and cache_path.exists():
            if cache_path.is_symlink() or cache_path.parent.is_symlink():
                raise ValueError('Cache paths cannot be symbolic links.')
            write_json(state_path, clean_state(read_json(cache_path)), private=True)
            progress.info('Restored the nonsecret Calendar fingerprint cache; no credentials are cached.')
        progress.info('Stage 1/2 | Hunting the configured official sources.')
        hunt_path = directory / 'hunt-report.json'
        previous = prior_id(hunt_path)
        command = ['automation/hunt.py'] + ([] if args.apply else ['--check'])
        if args.browser_executable:
            command += ['--browser-executable', args.browser_executable]
        if args.workers is not None:
            command += ['--workers', str(args.workers)]
        code = stage(command, progress, timeout)
        hunt = fresh_report(hunt_path, previous)
        if hunt:
            result['hunt'] = compact_hunt(hunt)
        if code or not hunt or not hunt.get('ok') or hunt.get('checkOnly') != (not args.apply):
            result['status'] = 'hunt-failed'
            raise RuntimeError('The hunt did not complete successfully. Calendar synchronization was not started.')
        result['huntApplied'] = bool(args.apply)
        write_json(report_path, result, private=True)
        # Reload the validated files after the hunter, not its earlier check-only proposal.
        data = load_data()
        calendar_enabled = data['config']['automation']['googleCalendar']['enabled']
        if args.apply and not (calendar_enabled or args.sync_calendar):
            result['calendar'] = {'status': 'disabled', 'mode': 'workflow', 'counts': {}}
            progress.warning('Stage 2/2 | Calendar is disabled in config; no Google request will be made.')
        else:
            progress.info('Stage 2/2 | ' + ('Synchronizing Google Calendar.' if args.apply else 'Building an offline preview from saved data; check proposals are not applied.'))
            if args.apply:
                check_default_branch(progress)
            path = directory / 'calendar-sync-report.json'
            previous = prior_id(path)
            command = ['automation/calendar_sync.py'] + (['--apply'] if args.sync_calendar else ['--workflow'] if args.apply else [])
            code = stage(command, progress, timeout, google=args.apply)
            calendar = fresh_report(path, previous)
            if calendar:
                result['calendar'] = compact_calendar(calendar)
            if code or not calendar or calendar.get('status') not in ('synced', 'unchanged', 'preview'):
                result['status'] = 'calendar-failed'
                raise RuntimeError('Calendar did not complete. Validated hunter data remains saved; remote writes may already have completed. See calendar-sync-report.json.')
        result['status'] = 'passed-with-warnings' if result['hunt']['warningCount'] or result['hunt']['deferredNames'] else 'passed'
        if args.apply and result['calendar']['status'] == 'disabled':
            result['status'] = 'passed-calendar-disabled'
        if os.environ.get('GITHUB_ACTIONS') == 'true' and state_path.exists() and result['calendar']['status'] in ('synced', 'unchanged'):
            write_json(cache_path, clean_state(read_json(state_path)), private=True)
            result['cacheReady'] = True
        exit_code = 0
        progress.success('Pipeline finished: ' + result['status'] + '.')
    except KeyboardInterrupt:
        result['status'] = 'interrupted'
        progress.warning('Pipeline interrupted. Inspect the child reports before retrying; completed Google writes cannot be rolled back by this process.')
        exit_code = 130
    except Exception as error:
        if result['status'] == 'running':
            result['status'] = 'failed'
        result['errorCategory'] = type(error).__name__
        progress.error('PIPELINE FAILED: ' + str(error))
    finally:
        if directory and report_path and lock_acquired:
            try:
                result['finishedAt'] = datetime.now(JAKARTA).isoformat(timespec='seconds')
                if args.publish_history:
                    # This object has only allowlisted counts/public game decisions, not raw errors or credentials.
                    result['publicHistoryWritten'] = True
                    public_path = record_public_run(ROOT, compact_pipeline(result))
                    result['publicHistoryFile'] = str(public_path.relative_to(ROOT))
                    progress.info('Compact public history prepared: ' + result['publicHistoryFile'])
                write_json(report_path, result, private=True)
                archive_run(directory, 'pipeline', result, progress, retain_days)
                progress.info('Pipeline report: ' + str(report_path))
                output = os.environ.get('GITHUB_OUTPUT')
                if output:
                    with open(output, 'a', encoding='utf-8') as stream:
                        stream.write('hunt_applied=' + str(result['huntApplied']).lower() + '\n')
                        stream.write('history_file=' + result.get('publicHistoryFile', '') + '\n')
                        stream.write('cache_ready=' + str(result.get('cacheReady', False)).lower() + '\n')
                summary = os.environ.get('GITHUB_STEP_SUMMARY')
                if summary:
                    with open(summary, 'a', encoding='utf-8') as stream:
                        stream.write('## Patch Calendar run\n\n')
                        stream.write(f"Result: **{result['status']}**. Hunt: {result['hunt']['status']}. Calendar: {result['calendar']['status']}.\n\n")
                        stream.write('A successful execution is not an independent audit of announcement completeness.\n')
            except Exception as error:
                exit_code = exit_code or 1
                progress.error('Could not retain the complete pipeline record: ' + str(error))
        progress.close()
        if lock_acquired:
            run_lock.__exit__(None, None, None)
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
