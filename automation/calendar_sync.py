"""Preview or synchronize release history and three future dates per game."""

import argparse
import copy
import sys
from datetime import datetime
from urllib.parse import quote

from common import JAKARTA, exclusive_lock, digest, get_game, iso, load_data, next_releases, parse_day, private_directory, read_json, release, today, write_json
from google_api import ApiError, GoogleAPI, credentials
from progress import Progress, wait_for
from run_history import archive_run, run_identity


def settings_for(data):
    """Validate the supported all-day, history-preserving synchronization policy."""
    settings = data['config']['automation']['googleCalendar']
    if settings['futureCountPerGame'] != 3:
        raise ValueError('This synchronization contract requires three future releases per game.')
    if not settings['allDay'] or settings['rollover'] != 'midnight-jakarta' or not settings['keepPastEvents']:
        raise ValueError('This version supports all-day events, midnight Jakarta rollover, and retained history.')
    if settings['visibility'] != 'private' or settings['transparency'] != 'transparent' or settings['reminders'] != {'useDefault': False, 'overrides': []}:
        raise ValueError('This version requires private, free events without reminders.')
    if not settings.get('namespace') or len(settings['namespace']) > 100:
        raise ValueError('Set a stable namespace of 1–100 characters.')
    return settings


def event_id(namespace, release_id, generation=0):
    """Use valid base32hex characters; dates and display names never affect identity."""
    return 'pc' + digest([namespace, release_id, generation])[:48]


def event_body(data, item, settings):
    """Build a one-day private/free event with explicit, independent evidence status."""
    game = get_game(data, item['gameId'])
    title = game['shortName'] + ((' ' + item['label']) if item['label'] else (' · ' + item['title']) if item['title'] else ' update')
    if item['status'] == 'projected':
        title += settings['projectedSuffix']
    date_status = 'Projected — not an official release date.' if item['status'] == 'projected' else 'Confirmed from publisher material.' if item['verification'] == 'official' else 'Historical archive; not independently publisher-verified.' if item['verification'] == 'archive' else 'Recorded release date.'
    name_status = 'Publisher-confirmed' if item['nameVerification'] == 'official' else 'Historical archive' if item.get('label') or item.get('title') else 'Not announced in the dataset'
    lines = [game['name'], 'Date: ' + date_status, 'Version/name: ' + name_status + '.', 'All-day date in Asia/Jakarta.']
    if item.get('title') and item.get('label'):
        lines.append(item['title'])
    if item['status'] == 'projected':
        lines.append(f"Projection: {game['cadenceWeeks']} weeks, {game['rounding']} weekday alignment; anchored to {item['anchorDate']}.")
    if item.get('notes'):
        lines.append(item['notes'])
    evidence = list(dict.fromkeys(item['sources'] + item['nameSources']))
    if evidence:
        lines += ['', 'Evidence:'] + [data['sources']['sources'][key]['url'] for key in evidence]
    lines += ['', 'Managed release: ' + item['id']]
    if item['day'] >= parse_day('9999-12-31'):
        raise ValueError('Google Calendar export uses ordinary four-digit years; the web calendar has a much larger range.')
    return {'id': event_id(settings['namespace'], item['id']), 'summary': title,
            'description': '\n'.join(lines), 'start': {'date': item['date']},
            'end': {'date': iso(item['day'] + 1)},
            'status': 'tentative' if item['status'] == 'projected' else 'confirmed',
            'transparency': settings['transparency'], 'visibility': settings['visibility'],
            'reminders': copy.deepcopy(settings['reminders']),
            'extendedProperties': {'private': {'pcOwner': settings['namespace'], 'pcRelease': item['id'], 'pcGame': item['gameId']}}}


def desired_events(data, on_date=None):
    """Backfill real historical anchors and compute the rolling future window lazily."""
    settings = settings_for(data)
    day = parse_day(on_date or today())
    desired = {}
    for game in data['config']['games']:
        game_id = game['id']
        items = []
        if settings['backfillHistory']:
            items = [release(data, game_id, row['sequence']) for row in data['overrides']['games'][game_id] if parse_day(row['date']) <= day]
        items += next_releases(data, game_id, day + 1, settings['futureCountPerGame'])
        for item in items:
            desired[item['id']] = {'gameId': game_id, 'event': event_body(data, item, settings), 'future': item['day'] > day}
    return desired


def owned(event, namespace):
    """Recognize only this app's explicitly marked records."""
    return event.get('extendedProperties', {}).get('private', {}).get('pcOwner') == namespace


def managed_values(event):
    """Compare only fields this automation owns, ignoring Google-generated metadata."""
    reminders = event.get('reminders', {})
    return {key: event.get(key) for key in ('summary', 'description', 'status', 'transparency', 'visibility')} | {
        'start': event.get('start', {}).get('date'), 'end': event.get('end', {}).get('date'),
        'timed': bool(event.get('start', {}).get('dateTime')),
        'reminders': {'useDefault': reminders.get('useDefault', True), 'overrides': reminders.get('overrides', [])},
        'properties': {key: event.get('extendedProperties', {}).get('private', {}).get(key) for key in ('pcOwner', 'pcRelease', 'pcGame')}}


def insert_event(api, calendar_id, desired, namespace, progress=None, outcome=None):
    """Make creates retry-safe, including reintroducing previously deleted forecasts."""
    body = copy.deepcopy(desired)
    release_id = body['extendedProperties']['private']['pcRelease']
    path = api.calendar_path(calendar_id, '/events')
    for generation in range(20):
        body['id'] = event_id(namespace, release_id, generation)
        try:
            result = api.request('POST', path, {'sendUpdates': 'none'}, body)
            if outcome is not None:
                outcome['action'] = 'created'
            return result
        except ApiError as error:
            if error.status != 409:
                raise
            if progress:
                progress.warning(f'{release_id}: event ID already exists; checking ownership before retrying or updating.')
            try:
                existing = api.request('GET', path + '/' + body['id'])
            except ApiError as lookup:
                if lookup.status in (404, 410):
                    continue
                raise
            if existing.get('status') == 'cancelled':
                if progress:
                    progress.info(f'{release_id}: prior event was deleted; trying the next stable ID generation.')
                continue
            if not owned(existing, namespace) or existing.get('extendedProperties', {}).get('private', {}).get('pcRelease') != release_id:
                raise ValueError('An event-ID collision belongs to another event; no overwrite was attempted.')
            changed = patch_event(api, calendar_id, existing, desired)
            if outcome is not None:
                outcome['action'] = 'updated' if changed else 'unchanged'
            if progress:
                progress.success(f'{release_id}: recovered the existing managed event without creating a duplicate.')
            return existing
    raise ValueError('Too many deleted generations for a release; inspect its calendar history.')


def patch_event(api, calendar_id, existing, desired):
    """Update managed fields with an ETag guard and preserve unrelated private metadata."""
    if managed_values(existing) == managed_values(desired):
        return False
    if not existing.get('etag'):
        raise ValueError('Google returned an event without an ETag; refusing an unguarded update.')
    body = copy.deepcopy(desired)
    body.pop('id', None)
    body['extendedProperties']['private'] = existing.get('extendedProperties', {}).get('private', {}) | body['extendedProperties']['private']
    for field in ('start', 'end'):
        if existing.get(field, {}).get('dateTime'):
            body[field].update({'dateTime': None, 'timeZone': None})
    api.request('PATCH', api.calendar_path(calendar_id, '/events/' + quote(existing['id'], safe='')), {'sendUpdates': 'none'}, body, existing['etag'])
    return True


def reconcile(api, desired, targets, day, namespace, progress=None):
    """Change only managed events, preserving past history and unrelated appointments."""
    remote = {}
    destinations = sorted(set(targets.values()))
    for number, target in enumerate(destinations, 1):
        if progress:
            progress.info(f'Reading existing managed events: destination {number}/{len(destinations)}.')
        events = api.list_all(api.calendar_path(target, '/events'), {'privateExtendedProperty': 'pcOwner=' + namespace, 'showDeleted': 'false', 'maxResults': 2500})
        remote[target] = [event for event in events if owned(event, namespace) and event.get('status') != 'cancelled']
    # Validate the full working set before any write, including destination changes.
    lookup = {}
    if progress:
        progress.info(f'Validating {sum(len(items) for items in remote.values())} existing managed events before any write.')
    for target, events in remote.items():
        for event in events:
            release_id = event['extendedProperties']['private'].get('pcRelease')
            if not release_id:
                raise ValueError('A managed event has no release ID; review it before syncing.')
            if (target, release_id) in lookup:
                raise ValueError('Duplicate managed releases exist in a destination; inspect them before syncing.')
            lookup[(target, release_id)] = event
            if event.get('attendees') or event.get('recurrence'):
                raise ValueError('A managed event was given attendees or recurrence. It must be reviewed before automated changes.')
    counts = {'created': 0, 'updated': 0, 'removedFuture': 0, 'unchanged': 0, 'keptPast': 0}
    for index, (release_id, entry) in enumerate(desired.items(), 1):
        target = targets[entry['gameId']]
        existing = lookup.get((target, release_id))
        action = 'Check/update' if existing else 'Create'
        label = f"{entry['event']['summary']} | {entry['event']['start']['date']}"
        if progress:
            progress.info(f'Events {index}/{len(desired)} | {action}: {label}.')
        with wait_for(progress, f'{action} {release_id} (event {index}/{len(desired)})'):
            if existing:
                changed = patch_event(api, target, existing, entry['event'])
                outcome = 'updated' if changed else 'unchanged'
                counts[outcome] += 1
            else:
                result = {}
                insert_event(api, target, entry['event'], namespace, progress, outcome=result)
                outcome = result['action']
                counts[outcome] += 1
        if progress:
            progress.success(f'Events {index}/{len(desired)} completed | {release_id}: {outcome}.')
    obsolete = [(target, event) for target, events in remote.items() for event in events
                if event['extendedProperties']['private']['pcRelease'] not in desired
                or targets[desired[event['extendedProperties']['private']['pcRelease']]['gameId']] != target]
    if progress:
        progress.info(f'Checking {len(obsolete)} managed events outside the requested window; past history is retained.')
    checked = 0
    for target, events in remote.items():
        for event in events:
            release_id = event['extendedProperties']['private']['pcRelease']
            expected = desired.get(release_id)
            if expected and targets[expected['gameId']] == target:
                continue
            checked += 1
            date = event.get('start', {}).get('date')
            if not date or parse_day(date) <= day:
                counts['keptPast'] += 1
                if progress:
                    progress.success(f'History {checked}/{len(obsolete)} | Retained {release_id}.')
                continue
            if not event.get('etag'):
                raise ValueError('Cannot remove an obsolete future event without an ETag.')
            if progress:
                progress.info(f'Window cleanup {checked}/{len(obsolete)} | Removing obsolete future release {release_id}.')
            api.request('DELETE', api.calendar_path(target, '/events/' + quote(event['id'], safe='')), {'sendUpdates': 'none'}, etag=event['etag'])
            counts['removedFuture'] += 1
            if progress:
                progress.success(f'Window cleanup {checked}/{len(obsolete)} | Removed {release_id}.')
    return counts


def plan_fingerprint(data, desired, targets):
    """Detect actual data/config/window changes without forcing an update every day."""
    relevant = {key: value for key, value in settings_for(data).items() if key != 'enabled'}
    return digest({'policy': relevant, 'targets': targets, 'desired': desired,
                   'overrides': data['overrides']['games'], 'versions': data['versions']['games'],
                   'cadence': [{key: game[key] for key in ('id', 'cadenceWeeks', 'preferredWeekday', 'rounding')} for game in data['config']['games']]})


def main():
    """Default to a local preview and report every phase of explicit synchronization."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Explicitly write events using the private Google settings.')
    mode.add_argument('--workflow', action='store_true', help='Write only when googleCalendar.enabled is true.')
    parser.add_argument('--force', action='store_true', help='Recheck Google even when the local fingerprint is unchanged.')
    parser.add_argument('--allow-destination-change', action='store_true', help='Allow a new destination; the old calendar is not cleaned up automatically.')
    parser.add_argument('--date', help='Preview a different Jakarta date; not allowed when applying.')
    args = parser.parse_args()
    report = {'runId': run_identity(), 'component': 'calendar', 'startedAt': datetime.now(JAKARTA).isoformat(timespec='seconds'),
              'status': 'running', 'mode': 'apply' if args.apply else 'workflow' if args.workflow else 'preview',
              'remoteChecked': False, 'writesMayHaveCompleted': False, 'counts': {}}
    report_path = None
    retain_days = 180
    run_lock = None
    lock_acquired = False
    progress = Progress('calendar', report=report)
    total_steps = 6 if args.apply or args.workflow else 3
    writes_possible = False
    state_saved = False
    progress.info('Calendar started: ' + ('APPLY; this can create/update managed Google events.' if args.apply else 'WORKFLOW; writes require enabled=true.' if args.workflow else 'PREVIEW; no Google requests or writes.'))
    try:
        progress.info(f'Step 1/{total_steps} | Loading and validating the local release data and event policy.')
        data = load_data()
        settings = settings_for(data)
        retain_days = data['config'].get('automation', {}).get('monitoring', {}).get('runRetentionDays', 180)
        if args.workflow and not settings['enabled']:
            report['status'] = 'disabled'
            progress.success('Google Calendar sync is disabled in data/config.json. No Google requests were made.')
            return 0
        apply = args.apply or args.workflow
        if apply and args.date:
            raise ValueError('--date is for offline previews only.')
        on_date = args.date or today()
        progress.info(f'Step 2/{total_steps} | Calculating historical and next-three-per-game events for {on_date} (Jakarta).')
        desired = desired_events(data, on_date)
        future_count = sum(entry['future'] for entry in desired.values())
        historical_count = len(desired) - future_count
        report.update(historicalEvents=historical_count, futureEvents=future_count)
        progress.success(f'Plan ready: {historical_count} historical + {future_count} future events; {len(desired)} total.')
        progress.info('Checking the project-local runtime folder and Git ignore protection.')
        directory = private_directory()
        run_lock = exclusive_lock(directory / 'calendar-sync.lock')
        run_lock.__enter__()
        lock_acquired = True
        if not apply:
            # A preview must not rotate the log of an in-flight synchronization.
            progress.attach(directory / 'calendar-sync.log')
            report_path = directory / 'calendar-sync-report.json'
            preview = {'jakartaDate': on_date, 'futureEvents': future_count,
                       'historicalEvents': historical_count, 'releases': desired}
            path = directory / 'calendar-preview.json'
            progress.info('Step 3/3 | Writing the local event preview; no authentication is needed.')
            write_json(path, preview, private=True)
            progress.success(f"Preview only: {historical_count} historical + {future_count} future events. No Google requests or writes.")
            report['status'] = 'preview'
            progress.info('Preview: ' + str(path))
            return 0
        progress.info('Step 3/6 | Acquiring the local synchronization lock and checking the saved fingerprint.')
        # Open the live log only after the sync lock, preserving a concurrent run's log.
        progress.attach(directory / 'calendar-sync.log')
        report_path = directory / 'calendar-sync-report.json'
        values = credentials()
        progress.protect(*(values.get(key) for key in ('client_id', 'client_secret', 'refresh_token')))
        target_default = values.get('calendar_id', '').strip()
        target_overrides = values.get('calendar_ids') or {}
        targets = {game['id']: target_overrides.get(game['id']) or target_default for game in data['config']['games']}
        if not all(isinstance(value, str) and value.strip() for value in targets.values()):
            raise ValueError('Choose a destination calendar_id, or a calendar_ids mapping, in the private Google JSON.')
        fingerprint = plan_fingerprint(data, desired, targets)
        state_path = directory / 'calendar-state.json'
        state = read_json(state_path) if state_path.exists() else {}
        if state.get('targetHash') and state['targetHash'] != digest(targets) and not args.allow_destination_change:
            raise ValueError('The calendar destination changed. Review it, then use --allow-destination-change explicitly. Old calendar events will remain.')
        if state.get('fingerprint') == fingerprint and not args.force:
            report['status'] = 'unchanged'
            progress.success('Unchanged data and future window; Google API skipped.')
            progress.info('Used the saved fingerprint only; no remote audit or Calendar writes were performed.')
            return 0
        reason = 'Forced remote recheck requested.' if args.force else 'The event plan changed.' if state.get('fingerprint') else 'No successful synchronization fingerprint exists yet.'
        progress.info(reason)
        progress.info('Step 4/6 | Connecting to Google and verifying the selected calendar and privacy.')
        report['remoteChecked'] = True
        api = GoogleAPI(values, progress=progress)
        resolved = api.resolve_targets(targets, settings['requireUnsharedCalendar'])
        progress.info('Step 5/6 | Reading managed events, validating them, then reconciling the event plan.')
        writes_possible = True
        counts = reconcile(api, desired, resolved, parse_day(on_date), settings['namespace'], progress=progress)
        progress.info('Step 6/6 | Saving the successful synchronization fingerprint locally.')
        # Persist only after every write succeeded. Interrupted runs safely reconcile again.
        write_json(state_path, {'fingerprint': fingerprint, 'targetHash': digest(targets), 'lastSyncedOn': on_date}, private=True)
        state_saved = True
        report.update(status='synced', counts=counts)
        progress.success('Calendar sync complete: ' + ', '.join(f'{key}={value}' for key, value in counts.items()))
        progress.info('State saved: ' + str(state_path))
        return 0
    except KeyboardInterrupt:
        report.update(status='interrupted', writesMayHaveCompleted=writes_possible and not state_saved)
        progress.warning('Calendar operation interrupted by the user (Ctrl+C).')
        if writes_possible and not state_saved:
            progress.warning('Some Google writes may already have completed. The successful fingerprint was not advanced; keep the existing state and retry the same command.')
        return 130
    except Exception as error:
        report.update(status='failed', error=progress.clean(str(error)), writesMayHaveCompleted=writes_possible and not state_saved)
        progress.error('CALENDAR SYNC FAILED: ' + str(error))
        if writes_possible and not state_saved:
            progress.warning('Some Google writes may already have completed. The successful fingerprint was not advanced; keep the existing state for a safe retry.')
        return 1
    finally:
        if report_path:
            try:
                report['finishedAt'] = datetime.now(JAKARTA).isoformat(timespec='seconds')
                write_json(report_path, report, private=True)
                archive = archive_run(report_path.parent, 'calendar', report, progress, retain_days)
                progress.info('Result report: ' + str(report_path))
                progress.info('Run history: ' + str(archive))
            except Exception as error:
                progress.warning('Calendar result history could not be saved: ' + str(error))
        progress.close()
        if lock_acquired:
            run_lock.__exit__(None, None, None)


if __name__ == '__main__':
    sys.exit(main())
