"""Retain credential-free execution evidence for later reliability reviews."""

import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from common import JAKARTA, ROOT, digest, parse_day, read_json, today, write_json


def run_identity():
    """Generate distinct local IDs even when several operations finish within one second."""
    return datetime.now(JAKARTA).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:10]


def safe_tree(value, clean):
    """Redact recursively before saving reports, never storing credentials or HTTP headers."""
    blocked = {'client_id', 'client_secret', 'refresh_token', 'access_token', 'id_token',
               'authorization', 'cookies', 'headers', 'calendar_id', 'calendar_ids'}
    if isinstance(value, dict):
        return {key: safe_tree(item, clean) for key, item in value.items() if key.lower() not in blocked}
    if isinstance(value, list):
        return [safe_tree(item, clean) for item in value]
    return clean(value) if isinstance(value, str) else value


def archive_run(directory, component, report, progress, retain_days=180):
    """Keep reports and redacted logs under the project; prune only our dated run folders."""
    run_id = report.setdefault('runId', run_identity())
    if not re.fullmatch(r'[a-z0-9-]+', component) or not re.fullmatch(r'\d{8}T\d{6}-[a-f0-9]{10}', run_id):
        raise ValueError('Invalid run archive identity.')
    base = Path(directory) / 'runs'
    if base.is_symlink():
        raise ValueError('Run history cannot be a symbolic link.')
    target = base / (run_id + '-' + component)
    if target.exists():
        raise ValueError('A history entry with this ID already exists.')
    target.mkdir(parents=True)
    write_json(target / 'report.json', safe_tree(report, progress.clean), private=True)
    if progress.path and progress.path.is_file():
        lines = progress.path.read_text(encoding='utf-8').splitlines()
        text = '\n'.join(progress.clean(line) for line in lines) + '\n'
        with open(target / 'progress.log', 'x', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        os.chmod(target / 'progress.log', 0o600)
    cutoff = (datetime.now(JAKARTA) - timedelta(days=max(30, min(730, retain_days)))).strftime('%Y%m%d')
    for folder in base.iterdir():
        if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r'\d{8}T\d{6}-[a-f0-9]{10}-(hunt|calendar|pipeline)', folder.name):
            continue
        children = list(folder.iterdir())
        if folder.name[:8] < cutoff and all(item.name in ('report.json', 'progress.log') and item.is_file() and not item.is_symlink() for item in children):
            for item in children:
                item.unlink()
            folder.rmdir()
    return target


def compact_hunt(report):
    """Publish only allowlisted operational counts and public release decisions, not log text."""
    games = {}
    for game_id in ('hsr', 'zzz', 'endfield'):
        game = report.get('games', {}).get(game_id, {})
        if not game:
            continue
        discovery = game.get('discovery', {})
        games[game_id] = {key: game.get(key, 0) for key in
                          ('articlesRead', 'observations', 'datedObservations', 'missingPublicationDates')}
        games[game_id]['elapsedSeconds'] = game.get('elapsedSeconds')
        games[game_id]['coverage'] = {key: discovery.get(key) for key in
                                    ('pagesRead', 'recordsDiscovered', 'endReason', 'coverage', 'checkpointReached')}
        # Hash only public article metadata for comparing captures without publishing article bodies.
        games[game_id]['confirmationHealth'] = game.get('confirmationHealth', {})
        games[game_id]['evidenceFingerprint'] = digest(game.get('articleMetadata', []))
    changes = []
    for change in report.get('proposedChanges', []):
        release_id = change.get('releaseId', '')
        if not re.fullmatch(r'(hsr|zzz|endfield)-\d+', release_id):
            continue
        item = {'releaseId': release_id}
        for side in ('before', 'after'):
            value = change.get(side, {})
            anchor, name = value.get('override') or {}, value.get('name') or {}
            item[side] = {'date': anchor.get('date'), 'label': name.get('label'), 'title': name.get('title'),
                          'dateVerification': anchor.get('verification'), 'nameVerification': name.get('verification')}
        for field in ('previousCalculatedDate', 'currentCalculatedDate'):
            item[field] = change.get(field)
        item['kind'] = 'schedule-or-name' if any(item['before'].get(key) != item['after'].get(key) for key in ('date', 'label', 'title')) else 'evidence-only'
        changes.append(item)
    return {'status': report.get('status', 'not-run'), 'checkOnly': report.get('checkOnly'),
            'elapsedSeconds': report.get('elapsedSeconds'), 'warningCount': len(report.get('notices', [])),
            'errorCount': len(report.get('errors', [])), 'deferredNames': len(report.get('deferredNames', [])),
            'games': games, 'changes': changes,
            'collection': {key: report.get('collection', {}).get(key) for key in
                           ('strategy', 'workerLimit', 'perHostLimit', 'peakActiveFeeds', 'completedFeeds',
                            'failedFeeds', 'cancelledFeeds', 'elapsedSeconds')}}


def compact_calendar(report):
    """Exclude event IDs, destination IDs, account details, tokens, and free-text API errors."""
    result = {key: report.get(key) for key in ('status', 'mode', 'elapsedSeconds', 'historicalEvents',
                                             'futureEvents', 'remoteChecked', 'writesMayHaveCompleted')}
    result['counts'] = {key: int(report.get('counts', {}).get(key, 0)) for key in
                       ('created', 'updated', 'removedFuture', 'unchanged', 'keptPast')}
    return result



def code_fingerprint(root=ROOT):
    """Identify the automation code used for a run without reading settings or credentials."""
    names = ('common.py', 'sources.py', 'hunt.py', 'google_api.py', 'calendar_sync.py',
             'daily.py', 'run_history.py', 'progress.py', 'feed_pool.py')
    files = {}
    for name in names:
        path = Path(root) / 'automation' / name
        if path.is_file() and not path.is_symlink():
            files[name] = digest(path.read_text(encoding='utf-8'))
    return digest(files)


def compact_pipeline(report):
    """Exclude local paths, progress/error text, and arbitrary diagnostics from public receipts."""
    fields = ('schemaVersion', 'component', 'runId', 'startedAt', 'finishedAt', 'mode', 'trigger',
              'status', 'huntApplied', 'elapsedSeconds', 'codeFingerprint', 'gitRevision', 'errorCategory')
    result = {key: report[key] for key in fields if key in report}
    # These two children were already reduced to the explicit compact component schemas.
    result['hunt'] = report.get('hunt', {'status': 'not-run'})
    result['calendar'] = report.get('calendar', {'status': 'not-run'})
    return result

def record_public_run(root, record):
    """Append a schema-controlled compact pipeline entry only when explicitly requested."""
    stamp = record['startedAt']
    month = stamp[:7]
    if not re.fullmatch(r'\d{4}-\d{2}', month):
        raise ValueError('Invalid public history month.')
    path = Path(root) / 'automation' / 'history' / (month + '.jsonl')
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError('History paths cannot be links.')
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding='utf-8') if path.exists() else ''
    records = [json.loads(line) for line in existing.splitlines() if line.strip()]
    if any(row.get('runId') == record['runId'] for row in records):
        return path
    handle, temporary = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(existing)
            if existing and not existing.endswith('\n'):
                stream.write('\n')
            stream.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path
