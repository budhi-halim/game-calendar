"""Summarize retained execution history without contacting publishers or Google."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from common import ROOT, iso, parse_day, private_directory, read_json, today, write_json
from progress import Progress


def summarize(records, since, through):
    """Measure execution outcomes separately from unmeasured announcement accuracy."""
    unique = {row['runId']: row for row in records if row.get('runId') and since <= row.get('startedAt', '')[:10] <= through}
    pipelines = [row for row in unique.values() if row.get('component') == 'pipeline']
    applied = [row for row in pipelines if row.get('mode') == 'apply']
    scheduled = [row for row in applied if row.get('trigger') == 'schedule']
    counts = Counter(row.get('status', 'unknown') for row in applied)
    receipts = sorted({row['startedAt'][:10] for row in scheduled})
    missing = []
    if receipts:
        # Only complete dates after the first observed schedule receipt can be assessed.
        missing = [iso(day) for day in range(parse_day(receipts[0]), parse_day(through)) if iso(day) not in receipts]
    changes = [change for row in applied for change in row.get('hunt', {}).get('changes', [])]
    return {'from': since, 'through': through, 'uniqueRecords': len(unique),
            'pipelineChecks': len(pipelines) - len(applied), 'appliedPipelineRuns': len(applied),
            'appliedOutcomes': dict(counts), 'scheduledReceipts': len(scheduled),
            'daysWithoutRecordedScheduledReceipt': missing,
            'sourceWarningRuns': sum(bool(row.get('hunt', {}).get('warningCount')) for row in applied),
            'scheduleOrNameChanges': sum(change.get('kind') == 'schedule-or-name' for change in changes),
            'evidenceOnlyChanges': sum(change.get('kind') == 'evidence-only' for change in changes),
            'standaloneComponents': {component: dict(Counter(row.get('status', 'unknown') for row in unique.values() if row.get('component') == component)) for component in ('hunt', 'calendar')},
            'announcementCompleteness': 'Not independently measured by execution logs.',
            'dateAndNamingAccuracy': 'Requires comparison with official announcements; a green run is not proof of correctness.',
            'receiptGapMeaning': 'A missing receipt may mean the workflow did not run, history was not retained, or publication of that receipt failed.'}


def main():
    """Create an offline review from local archives and optional committed compact receipts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=180, help='Review this many recent Jakarta dates (default: 180).')
    args = parser.parse_args()
    if not 1 <= args.days <= 3660:
        parser.error('--days must be between 1 and 3660.')
    progress = Progress('review')
    try:
        directory = private_directory()
        progress.attach(directory / 'reliability-review.log')
        progress.info('Reading retained local reports and compact public run receipts. No network or Calendar writes.')
        records, unreadable = [], []
        for path in sorted((directory / 'runs').glob('*/report.json')):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            try:
                records.append(read_json(path))
            except (ValueError, OSError):
                unreadable.append(str(path.relative_to(ROOT)))
        for path in sorted((ROOT / 'automation' / 'history').glob('*.jsonl')):
            if path.is_symlink():
                continue
            try:
                for line in path.read_text(encoding='utf-8').splitlines():
                    if line.strip():
                        records.append(json.loads(line))
            except (ValueError, OSError):
                unreadable.append(str(path.relative_to(ROOT)))
        result = summarize(records, iso(parse_day(today()) - args.days + 1), today())
        result['unreadableHistoryFiles'] = unreadable
        path = directory / 'reliability-review.json'
        write_json(path, result, private=True)
        progress.success(f"Review: {result['appliedPipelineRuns']} applied pipeline runs; {result['pipelineChecks']} pipeline checks; {result['scheduledReceipts']} scheduled receipts.")
        progress.info('Applied outcomes: ' + (', '.join(f'{key}={value}' for key, value in sorted(result['appliedOutcomes'].items())) or 'none recorded yet'))
        progress.info(f"Scheduled dates without a retained receipt: {len(result['daysWithoutRecordedScheduledReceipt'])}.")
        progress.info('Execution success is not an accuracy score. Verify captured releases against independent official announcements at the later review.')
        if unreadable:
            progress.warning(f'{len(unreadable)} history files could not be read; results are partial.')
        progress.info('Review report: ' + str(path))
        return 0
    except Exception as error:
        progress.error('HISTORY REVIEW FAILED: ' + str(error))
        return 1
    finally:
        progress.close()


if __name__ == '__main__':
    sys.exit(main())
