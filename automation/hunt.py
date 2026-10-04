"""Update the canonical release records from the same fixed official feeds on every run."""

import argparse
import copy
import sys
from contextlib import closing
from datetime import datetime

from common import JAKARTA, ROOT, digest, exclusive_lock, recover_data, get_game, load_data, parse_day, release, private_directory, read_json, save_data, today, validate, write_json
from sources import canonical_article, collect_feed, normalize_name, parse_article
from feed_pool import collect_parallel
from progress import Progress
from run_history import archive_run, run_identity


def version_pair(value):
    """Parse a numeric label only when the publisher actually supplied it."""
    import re
    return tuple(map(int, value.split('.'))) if isinstance(value, str) and re.fullmatch(r'\d+\.\d+', value) else None


def locate_sequence(data, game_id, observation):
    """Match stable sequences and defer distant names whose release order is unknown."""
    names = data['versions']['games'][game_id]
    label, title = observation.get('label'), observation.get('title')
    matches = [int(key) for key, value in names.items() if (label and value.get('label') == label) or (title and normalize_name(value.get('title')) == normalize_name(title))]
    if len(set(matches)) > 1:
        raise ValueError('A version label and title refer to different release sequences.')
    if matches:
        return matches[0]
    anchors = data['overrides']['games'][game_id]
    latest = max(anchors, key=lambda row: row['sequence'])
    if observation.get('date'):
        same_date = next((row for row in anchors if row['date'] == observation['date']), None)
        if same_date:
            existing = names.get(str(same_date['sequence']), {})
            if label and existing.get('label') and label != existing['label']:
                raise ValueError('Different numbered versions share a date; sequence mapping is ambiguous.')
            return same_date['sequence']
        if parse_day(observation['date']) <= parse_day(latest['date']):
            return None
    elif observation.get('publishedOn') and parse_day(observation['publishedOn']) < parse_day(latest['date']) - 21:
        return None
    # Naming-only announcements can extend an already-confirmed sequence of names.
    tip = max([latest['sequence']] + [int(key) for key in names])
    reference = version_pair(names.get(str(tip), {}).get('label'))
    proposed = version_pair(label)
    if proposed and reference:
        if proposed <= reference:
            return None
        adjacent_minor = proposed == (reference[0], reference[1] + 1)
        announced_major = proposed == (reference[0] + 1, 0) and observation.get('releaseAnnouncement')
        if not (adjacent_minor or announced_major):
            return None
        # An explicit adjacent number establishes its order without a publication date.
        # A major reset still needs dated/recent evidence or an explicit next-update claim.
        if announced_major and not any(observation.get(key) for key in ('date', 'publishedOn', 'explicitNext')):
            return None
    elif not observation.get('releaseAnnouncement') or not any(observation.get(key) for key in ('date', 'publishedOn', 'explicitNext')):
        return None
    sequence = tip + 1
    if observation.get('date') and sequence > latest['sequence'] + 1:
        raise ValueError('A dated release would skip an undated named release; wait for the missing maintenance notice.')
    return sequence


def merge_observation(data, item, settings):
    """Merge independent official date/name evidence into the one canonical schema."""
    game_id = item['gameId']
    if not canonical_article(item['url'], settings['feeds'][game_id]):
        raise ValueError('Evidence does not belong to the configured publisher feed.')
    sequence = locate_sequence(data, game_id, item)
    if sequence is None:
        if item.get('date') and parse_day(item['date']) > max(parse_day(row['date']) for row in data['overrides']['games'][game_id]):
            raise ValueError('A new dated version could not be mapped safely to a stable sequence.')
        return False
    names, anchors = data['versions']['games'][game_id], data['overrides']['games'][game_id]
    existing = next((row for row in anchors if row['sequence'] == sequence), None)
    if item.get('date'):
        proposed = parse_day(item['date'])
        if proposed > parse_day(today()) + settings['maximumAdvanceDays']:
            raise ValueError('A release date exceeds the automatic confirmation horizon.')
        previous = max((row for row in anchors if row['sequence'] < sequence), default=None, key=lambda row: row['sequence'])
        if previous and proposed - parse_day(previous['date']) < settings['minimumIntervalDays']:
            raise ValueError('The date is implausibly close to the previous release.')
        if existing and existing['date'] != item['date']:
            old_announcement = existing.get('announcedOn')
            if old_announcement and item.get('publishedOn') and item['publishedOn'] < old_announcement:
                return False
            if not item.get('publishedOn') or (not old_announcement and item['publishedOn'] < data['sources']['checkedOn']):
                raise ValueError('An old or undated notice conflicts with a curated anchor.')
    source_key = 'official-' + digest(item['url'])[:16]
    source = dict(data['sources']['sources'].get(source_key, {}))
    source.update({'title': item['sourceTitle'], 'url': item['url'], 'kind': 'official'})
    if item.get('publishedOn'):
        source['publishedOn'] = item['publishedOn']
    data['sources']['sources'][source_key] = source
    name = copy.deepcopy(names.get(str(sequence), {}))
    for field in ('label', 'title'):
        if item.get(field):
            name[field] = item[field]
    name.update({'sources': sorted(set(name.get('sources', []) + [source_key])), 'verification': 'official', 'managedBy': 'hunter'})
    names[str(sequence)] = name
    if item.get('date'):
        row = copy.deepcopy(existing) if existing else {'sequence': sequence}
        changed_date = existing and existing['date'] != item['date']
        old_sources = [] if changed_date else row.get('sources', [])
        row.update({'date': item['date'], 'status': 'confirmed', 'verification': 'official', 'sources': sorted(set(old_sources + [source_key])), 'managedBy': 'hunter'})
        if item.get('publishedOn'):
            row['announcedOn'] = max(item['publishedOn'], row.get('announcedOn') or item['publishedOn'])
        # Do not keep old archive-only caveats or old-date notes after new direct evidence.
        if changed_date or (existing and existing.get('verification') == 'archive'):
            row.pop('notes', None)
        if existing:
            anchors[anchors.index(existing)] = row
        else:
            anchors.append(row)
            anchors.sort(key=lambda entry: entry['sequence'])
    return True


def seed_articles(data, game_id, feed):
    """Revisit recorded official anchor/name notices even after they leave the first page."""
    anchors = data['overrides']['games'][game_id]
    latest = max(anchors, key=lambda row: row['sequence'])
    keys = set(latest.get('sources', []))
    for sequence, name in data['versions']['games'][game_id].items():
        if int(sequence) >= latest['sequence']:
            keys.update(name.get('sources', []))
    found = {}
    for key in sorted(keys):
        source = data['sources']['sources'][key]
        url = canonical_article(source['url'], feed)
        if source.get('kind') == 'official' and url:
            found[url] = {'url': url, 'title': source['title'], 'publishedOn': source.get('publishedOn')}
    return list(found.values())


def deferred_reason(data, item):
    """Explain unresolved ordering without substituting the crawl date for publication."""
    if not item.get('date') and not item.get('publishedOn'):
        return 'No publication/release date, and the confirmed name does not establish an unambiguous next sequence.'
    return 'Release order is not established by the stored anchors and this announcement; retained for review.'


def release_changes(before, after):
    """Show exact before/after release records, including evidence-only updates."""
    changes = []
    for game in after['config']['games']:
        game_id = game['id']
        dates_before = {str(row['sequence']): row for row in before['overrides']['games'][game_id]}
        dates_after = {str(row['sequence']): row for row in after['overrides']['games'][game_id]}
        names_before = before['versions']['games'][game_id]
        names_after = after['versions']['games'][game_id]
        for sequence in sorted(set(dates_before) | set(dates_after) | set(names_before) | set(names_after), key=int):
            old = {'override': dates_before.get(sequence), 'name': names_before.get(sequence)}
            new = {'override': dates_after.get(sequence), 'name': names_after.get(sequence)}
            if old != new:
                changes.append({'releaseId': f'{game_id}-{sequence}', 'before': old, 'after': new,
                                'previousCalculatedDate': release(before, game_id, int(sequence))['date'],
                                'currentCalculatedDate': release(after, game_id, int(sequence))['date']})
    return changes


class HuntLog(Progress):
    """Keep the established hunt log path with the shared reporter implementation."""

    def __init__(self, directory, report):
        """Initialize the timestamped console, heartbeat, and private run log."""
        super().__init__('hunt', report=report)
        try:
            self.attach(directory / 'hunt-progress.log')
        except BaseException:
            self.close()
            raise


def main():
    """Run a fail-fast hunt with visible progress and project-local diagnostics."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Run the live hunt and validation without saving data.')
    mode.add_argument('--recover', action='store_true', help='Restore an interrupted data transaction locally; no network or Calendar writes.')
    mode.add_argument('--validate', action='store_true', help='Validate local canonical data only; no network.')
    parser.add_argument('--browser-executable', help='Optional path to an installed Chromium executable.')
    parser.add_argument('--game', help='Check only one configured game; requires --check and never saves data.')
    parser.add_argument('--workers', type=int, choices=range(1, 4), help='Override feed concurrency for this run (1-3); not saved to config.')
    parser.add_argument('--headed', action='store_true', help='Show the browser window for local diagnosis.')
    args = parser.parse_args()
    if args.game and not args.check:
        parser.error('--game requires --check; partial-feed runs cannot update public data.')
    report = {'runId': run_identity(), 'component': 'hunt', 'checkedOn': today(), 'startedAt': datetime.now(JAKARTA).isoformat(timespec='seconds'),
              'status': 'running', 'ok': False, 'checkOnly': args.check, 'scope': [],
              'games': {}, 'errors': [], 'notices': [], 'changed': [], 'deferredNames': []}
    log, path, interrupted = Progress('hunt', report=report), None, False
    run_lock = None
    lock_acquired = False
    retain_days = 180

    def emit(message):
        """Print early errors even if private diagnostics could not be initialized."""
        if str(message).startswith('HUNT FAILED'):
            log.error(message)
        elif str(message).startswith('NOTICE:') or 'interrupted' in str(message).lower():
            log.warning(message)
        elif str(message).startswith(('Canonical data validated.', 'Live check passed;', 'Single-game live check passed;', 'Updated:')):
            log.success(message)
        else:
            log(message)

    try:
        emit('Hunt started: ' + ('VALIDATE; local data only, no network.' if args.validate else 'CHECK ONLY; no public data writes.' if args.check else 'Data update mode; public files change only after every feed validates.'))
        if not args.validate:
            emit('Checking the project-local runtime folder and Git ignore protection.')
            directory = private_directory()
            run_lock = exclusive_lock(directory / 'hunt.lock')
            run_lock.__enter__()
            lock_acquired = True
            path = directory / 'hunt-report.json'
            log.attach(directory / 'hunt-progress.log')
            write_json(path, report, private=True)
            emit('Report: ' + str(path))
        if args.recover:
            changed = recover_data()
            report.update(ok=True, status='recovered' if changed else 'nothing-to-recover')
            emit('Interrupted data transaction restored. No network or Calendar writes.' if changed else 'No interrupted data transaction exists. Nothing changed.')
            return 0
        emit('Loading and validating the four canonical data files.')
        original = load_data()
        data = copy.deepcopy(original)
        if args.validate:
            emit('Canonical data validated. No network requests or data changes.')
            return 0
        settings = data['config']['automation']['sources']
        for key, default in {'maxAdaptiveListPages': max(12, settings['maxListPages']), 'publicationWaitSeconds': 1.0,
                             'unchangedListingWarningDays': 14, 'confirmationWarningDays': 3, 'feedWorkers': 3}.items():
            settings.setdefault(key, default)
        monitoring = data['config']['automation'].setdefault('monitoring', {})
        monitoring.setdefault('runRetentionDays', 180)
        monitoring.setdefault('stageTimeoutSeconds', 1200)
        retain_days = monitoring['runRetentionDays']
        report['effectiveSettings'] = {key: settings[key] for key in ('maxListPages', 'maxAdaptiveListPages',
            'maxArticlesPerGame', 'recentDays', 'publicationWaitSeconds', 'unchangedListingWarningDays', 'confirmationWarningDays', 'feedWorkers')}
        if type(settings['maxAdaptiveListPages']) is not int or not settings['maxListPages'] <= settings['maxAdaptiveListPages'] <= 30:
            raise ValueError('maxAdaptiveListPages must be an integer from maxListPages through 30.')
        if not isinstance(settings['publicationWaitSeconds'], (int, float)) or not 0 <= settings['publicationWaitSeconds'] <= 5:
            raise ValueError('publicationWaitSeconds must be between 0 and 5.')
        if args.game and args.game not in settings['feeds']:
            raise ValueError('Unknown game. Choose: ' + ', '.join(settings['feeds']))
        feeds = {key: value for key, value in settings['feeds'].items() if not args.game or key == args.game}
        report['scope'] = list(feeds)
        if type(settings['feedWorkers']) is not int or not 1 <= settings['feedWorkers'] <= 3:
            raise ValueError('feedWorkers must be an integer from 1 through 3.')
        worker_limit = args.workers or (1 if args.headed else settings['feedWorkers'])
        observations, jobs, results = [], [], {}
        report['collection'] = {}
        for game_id, feed in feeds.items():
            latest = max(data['overrides']['games'][game_id], key=lambda row: row['sequence'])
            checkpoint = [canonical_article(data['sources']['sources'][key]['url'], feed) for key in latest.get('sources', [])]
            jobs.append({'gameId': game_id, 'feed': feed,
                         'settings': dict(settings, seedArticles=seed_articles(data, game_id, feed),
                                          coverageAnchors=[url for url in checkpoint if url])})
            report['games'][game_id] = {'status': 'queued'}
        emit(f'Collection pool: up to {min(worker_limit, len(feeds))} independent feed threads; one browser flow per publisher host. Pagination and articles within each feed remain sequential.')

        def collect_job(job, cancel):
            """Keep the Playwright instance and parsed evidence local to the worker thread."""
            game_id, feed = job['gameId'], job['feed']
            with log.task(game_id):
                emit(f'[{game_id}] Feed started: {feed["url"]}')
                articles = collect_feed(game_id, feed, job['settings'], args.browser_executable,
                                        progress=lambda message: emit(f'[{game_id}] {message}'),
                                        headed=args.headed, cancel=cancel)
                cancel()
                emit(f'[{game_id}] Parsing release evidence from {len(articles)} articles.')
                parsed = [item for article in articles for item in parse_article(article, feed['titleStyle'])]
                if not parsed:
                    raise ValueError('No version/name evidence was recognized in this feed.')
                warnings = [f"{item['url']}: {warning}" for item in parsed for warning in item['warnings']]
                if warnings:
                    raise ValueError('Ambiguous release evidence: ' + '; '.join(warnings))
                emit(f'[{game_id}] Found {len(parsed)} naming/date observations.')
                return {'articles': articles, 'parsed': parsed}

        # Only this caller writes reports or merges canonical records; browser objects never
        # cross thread boundaries. Closing the iterator signals and joins outstanding workers.
        with closing(collect_parallel(jobs, collect_job, worker_limit, report['collection'])) as completed:
            for result in completed:
                game_id = result['gameId']
                results[game_id] = result
                report['games'][game_id] = {'status': result['status'], 'elapsedSeconds': result['elapsedSeconds']}
                if result['status'] == 'failed':
                    message = f"{game_id}: {result['error']}"
                    report['errors'].append(message)
                    if result.get('failureDetails'):
                        report['games'][game_id]['failureDetails'] = result['failureDetails']
                    log.error(message)
                    log.warning('Stopping other collectors at their next cancellation checkpoint; an in-flight navigation can take up to its configured timeout to return.')
                elif result['status'] == 'cancelled':
                    log.warning(f'[{game_id}] Cancelled; this feed is not counted as successfully checked.')
                else:
                    article_count = len(result['value']['articles'])
                    log.success(f'[{game_id}] Feed completed in {result["elapsedSeconds"]:.1f}s: {article_count} articles. Completed {report["collection"]["completedFeeds"]}/{len(feeds)} feeds.')
                with log.guard:
                    write_json(path, report, private=True)
        emit(f'Collection phase completed in {report["collection"]["elapsedSeconds"]:.1f}s; peak active feeds: {report["collection"]["peakActiveFeeds"]}. Merging results in configured game order.')
        for game_id in feeds:
            result = results.get(game_id)
            if not result or result['status'] != 'collected':
                continue
            articles, parsed = result['value']['articles'], result['value']['parsed']
            report['games'][game_id].update({
                'articlesRead': len(articles), 'observations': len(parsed),
                'datedObservations': sum(bool(item.get('date')) for item in parsed),
                'discovery': getattr(articles, 'discovery', {}),
                'missingPublicationDates': sum(not article.get('publishedOn') for article in articles),
                'articleMetadata': [{'url': article.get('url'), 'title': article.get('title'),
                    'publishedOn': article.get('publishedOn'), 'publicationSource': article.get('publicationSource'),
                    'publicationFields': article.get('publicationFields', []), 'bodyHash': article.get('bodyHash'),
                    'readVia': article.get('readVia')} for article in articles]})
            discovery = report['games'][game_id]['discovery']
            if discovery.get('coverage') == 'bounded-unverified':
                report['notices'].append(f'{game_id}: listing coverage is bounded without a current-release checkpoint; seed articles are not counted as discovery coverage.')
            previous_hunt = original['sources'].get('lastHunt', {})
            previous_game = previous_hunt.get('games', {}).get(game_id, {})
            previous_ids = previous_game.get('discovery', {}).get('discoveredURLs', [])
            current_ids = discovery.get('discoveredURLs', [])
            listing_changed = sorted(previous_ids) != sorted(current_ids)
            first_same = previous_game.get('listingUnchangedSince') or previous_hunt.get('checkedOn') or today()
            report['games'][game_id]['listingUnchangedSince'] = today() if listing_changed else first_same
            if not listing_changed and parse_day(today()) - parse_day(first_same) >= settings.get('unchangedListingWarningDays', 14):
                report['notices'].append(f'{game_id}: the discovered listing has been unchanged for at least 14 days; check source freshness.')
            observations.extend(parsed)
            report['observations'] = observations
            if report['games'][game_id]['missingPublicationDates']:
                report['notices'].append(f'{game_id}: publication metadata is missing for {report["games"][game_id]["missingPublicationDates"]} articles; dates are not inferred from the crawl time.')
        if any(result['status'] != 'collected' for result in results.values()) and not report['errors']:
            report['errors'].append('Collection was cancelled before all feeds completed.')
        if report['errors']:
            raise ValueError('An official feed is incomplete or ambiguous; stopped without changing public data.')
        emit('Matching observations to stable release IDs and validating the result.')
        # Dated history establishes sequence order before naming-only future observations.
        observations.sort(key=lambda item: (0 if item.get('date') else 1, item.get('date') or item.get('publishedOn') or today(), version_pair(item.get('label')) or (0, 0), item['url']))
        for item in observations:
            try:
                if not merge_observation(data, item, settings):
                    report['deferredNames'].append({'gameId': item['gameId'], 'label': item.get('label'), 'title': item.get('title'), 'source': item['url'], 'reason': deferred_reason(data, item)})
            except Exception as error:
                report['errors'].append(f"{item['gameId']}: {item['url']}: {error}")
        report['proposedChanges'] = release_changes(original, data)
        for game_id in feeds:
            latest = max(data['overrides']['games'][game_id], key=lambda row: row['sequence'])
            latest_day = parse_day(latest['date'])
            projected = release(data, game_id, latest['sequence'] + 1)
            overdue = max(0, parse_day(today()) - projected['day'])
            report['games'][game_id]['confirmationHealth'] = {'latestDatedRelease': latest['date'],
                'nextProjectedDate': projected['date'], 'daysPastProjectionWithoutConfirmation': overdue}
            if overdue > settings['confirmationWarningDays']:
                report['notices'].append(f'{game_id}: the next projected date has passed by {overdue} days without a newer confirmed date; this may be a delay or missing evidence, not proof that the projection was correct.')
            if parse_day(today()) - latest_day > settings['maximumAnchorAgeDays']:
                report['errors'].append(f'{game_id}: the latest dated release is too old for a healthy feed; inspect the publisher adapter.')
        if report['errors']:
            raise ValueError('Ambiguous or stale evidence needs review; no public data was changed.')
        data['sources']['checkedOn'] = today()
        data['sources']['collectionMethod'] = 'official-feeds'
        data['sources']['lastHunt'] = {'checkedOn': today(), 'scope': list(feeds),
            'pendingNames': len(report['deferredNames']),
            'games': {key: {field: value[field] for field in ('articlesRead', 'observations', 'datedObservations', 'missingPublicationDates', 'discovery', 'listingUnchangedSince')} for key, value in report['games'].items()}}
        dataset_id = 'release-calendar-' + digest({'overrides': data['overrides']['games'], 'versions': data['versions']['games']})[:16]
        for value in data.values():
            value['datasetId'] = dataset_id
        validate(data)
        report['proposedChanges'] = release_changes(original, data)
        report['observations'] = observations
        emit(f'Release records with proposed changes: {len(report["proposedChanges"])}; before/after values are in hunt-report.json.')
        report['changed'] = [f'data/{name}.json' for name in ('config', 'overrides', 'versions', 'sources') if read_json(ROOT / 'data' / f'{name}.json') != data[name]]
        if not args.check:
            emit('Saving validated public data.')
            save_data(data, expected=original)
        for notice in report['notices']:
            emit('NOTICE: ' + notice)
        if report['deferredNames']:
            emit(f'{len(report["deferredNames"])} names were not placed; see deferredNames and reasons in the report.')
        report['ok'] = True
        report['status'] = 'passed'
        prefix = 'Single-game live check passed; other feeds were not checked; would update: ' if args.game else 'Live check passed; would update: ' if args.check else 'Updated: '
        emit(prefix + (', '.join(report['changed']) or 'nothing'))
    except KeyboardInterrupt:
        interrupted = True
        report['status'] = 'interrupted'
        report['errors'].append('Interrupted by the user (Ctrl+C).')
        for value in report['games'].values():
            if value.get('status') in ('running', 'queued'):
                value['status'] = 'interrupted'
        emit('Hunt interrupted.' + (' No public data was changed.' if args.check else ''))
    except Exception as error:
        report['status'] = 'failed'
        report['errors'].append(str(error))
        emit('HUNT FAILED: ' + str(error))
    finally:
        try:
            if path:
                report['finishedAt'] = datetime.now(JAKARTA).isoformat(timespec='seconds')
                emit('Report: ' + str(path))
                if log.path:
                    emit('Progress log: ' + str(log.path))
                try:
                    write_json(path, report, private=True)
                    archive = archive_run(path.parent, 'hunt', report, log, retain_days)
                    log.info('Run history: ' + str(archive))
                except Exception as error:
                    report['ok'] = False
                    log.error('Could not save the final hunt report: ' + str(error))
        finally:
            log.close()
            if lock_acquired:
                run_lock.__exit__(None, None, None)
    return 130 if interrupted else 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
