"""Shared validation, deterministic date arithmetic, and safe local file handling."""

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DATA_NAMES = ('config', 'overrides', 'versions', 'sources')
JAKARTA = timezone(timedelta(hours=7), 'Asia/Jakarta')
MAX_DAY = 100000000


def today():
    """Return today's ISO civil date in Jakarta, independent of the host timezone."""
    return datetime.now(JAKARTA).date().isoformat()


def digest(value):
    """Hash structured content with stable ordering."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def read_json(path):
    """Read UTF-8 JSON, accepting a Windows editor's optional BOM."""
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value, private=False):
    """Atomically replace JSON; private output uses restrictive file permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if private:
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_directory():
    """Keep runtime files in the project's ignored folder, never the user profile."""
    base = ROOT / '.patch-calendar'
    requested = Path(os.environ.get('PATCH_CALENDAR_STATE_DIR') or base).expanduser()
    if not requested.is_absolute():
        requested = ROOT / requested
    path = requested.resolve()
    # A linked folder must not redirect private output outside this project.
    if base.resolve() != base or (path != base and base not in path.parents):
        raise ValueError("Runtime storage must stay inside this project's .patch-calendar folder. "
                         'Remove or correct PATCH_CALENDAR_STATE_DIR; external locations are no longer used.')
    ignore = ROOT / '.gitignore'
    patterns = ignore.read_text(encoding='utf-8-sig').splitlines() if ignore.is_file() else []
    if not any(line.strip() in ('.patch-calendar/', '/.patch-calendar/') for line in patterns):
        raise ValueError('The project .gitignore must contain /.patch-calendar/ before runtime files are created.')
    # A parent repository also counts; .gitignore does not protect already tracked files.
    git_root = next((folder for folder in (ROOT, *ROOT.parents) if (folder / '.git').exists()), None)
    if git_root:
        def git(*arguments):
            """Inspect Git without staging, deleting, or printing private contents."""
            return subprocess.run(['git', '-C', str(ROOT), *arguments], capture_output=True, timeout=15, check=False)
        tracked = git('ls-files', '-z', '--', '.patch-calendar')
        if tracked.returncode or tracked.stdout:
            raise ValueError('Cannot use runtime storage: Git already tracks files in .patch-calendar, '
                             'or its index could not be checked. Remove private files from Git first; '
                             'rotate any credentials that were published.')
        ignored = git('check-ignore', '--no-index', '-q', '--', '.patch-calendar/__runtime_probe__')
        if ignored.returncode != 0:
            raise ValueError('Git is not ignoring .patch-calendar. Correct .gitignore before running automation.')
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_symlink() or (hasattr(child, 'is_junction') and child.is_junction()):
            raise ValueError('Private runtime entries cannot be symbolic links or directory junctions.')
    return path


def load_data(root=ROOT):
    """Load the four shared website data files."""
    if (Path(root) / '.patch-calendar' / 'data-transaction.json').exists():
        raise ValueError('An interrupted data update needs recovery. Run python automation/hunt.py --recover before continuing; do not synchronize Calendar yet.')
    result = {name: read_json(Path(root) / 'data' / (name + '.json')) for name in DATA_NAMES}
    validate(result)
    return result


def civil_to_day(year, month, day):
    """Match the website's proleptic-Gregorian epoch-day arithmetic."""
    y = year - (month <= 2)
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    return era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy - 719468


def day_to_civil(day):
    """Convert an epoch day without Python datetime's year-9999 restriction."""
    shifted = day + 719468
    era = shifted // 146097
    doe = shifted - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    year = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    date = doy - (153 * mp + 2) // 5 + 1
    month = mp + (3 if mp < 10 else -9)
    return year + (month <= 2), month, date


def iso(day):
    """Format epoch days, including ECMAScript extended positive years."""
    year, month, date = day_to_civil(day)
    return (f'{year:04d}' if year < 10000 else f'+{year:06d}') + f'-{month:02d}-{date:02d}'


def parse_day(value):
    """Parse a strict, supported ISO civil date."""
    import re
    if not isinstance(value, str) or not re.fullmatch(r'(?:\d{4}|\+\d{6})-\d{2}-\d{2}', value):
        raise ValueError(f'Invalid ISO date: {value!r}')
    year, month, date = map(int, value.lstrip('+').split('-'))
    if year < 1 or not 1 <= month <= 12 or not 1 <= date <= 31:
        raise ValueError(f'Invalid date: {value}')
    result = civil_to_day(year, month, date)
    if day_to_civil(result) != (year, month, date) or result > MAX_DAY:
        raise ValueError(f'Unsupported date: {value}')
    return result


def align(day, game):
    """Round projections only; exact release anchors are never rounded."""
    forward = (game['preferredWeekday'] - (day + 4) % 7) % 7
    mode = game['rounding']
    delta = forward if mode == 'next' else (forward - 7 if forward else 0) if mode == 'previous' else forward - 7 if forward > 3 else forward
    return day + delta


def get_game(data, game_id):
    """Return a configured game or raise for an unknown ID."""
    return next(game for game in data['config']['games'] if game['id'] == game_id)


def release(data, game_id, sequence):
    """Resolve a permanent release sequence through its nearest prior anchor."""
    game = get_game(data, game_id)
    anchors = sorted(data['overrides']['games'][game_id], key=lambda row: row['sequence'])
    anchor = max((row for row in anchors if row['sequence'] <= sequence), key=lambda row: row['sequence'])
    exact = sequence == anchor['sequence']
    period = game['cadenceWeeks'] * 7
    day = parse_day(anchor['date']) if exact else align(parse_day(anchor['date']) + period, game) + (sequence - anchor['sequence'] - 1) * period
    name = data['versions']['games'][game_id].get(str(sequence), {})
    return {'id': f'{game_id}-{sequence}', 'gameId': game_id, 'sequence': sequence,
            'date': iso(day), 'day': day, 'label': name.get('label'), 'title': name.get('title'),
            'status': 'confirmed' if exact else 'projected', 'verification': anchor.get('verification') if exact else None,
            'nameVerification': name.get('verification') or ('official' if any(data['sources']['sources'][key]['kind'] == 'official' for key in name.get('sources', [])) else 'archive' if name else None),
            'sources': anchor.get('sources', []) if exact else [], 'nameSources': name.get('sources', []),
            'anchorDate': anchor['date'], 'notes': anchor.get('notes', '') if exact else ''}


def next_releases(data, game_id, from_day, count=3):
    """Jump directly to the requested window instead of iterating from launch."""
    game = get_game(data, game_id)
    period = game['cadenceWeeks'] * 7
    anchors = sorted(data['overrides']['games'][game_id], key=lambda row: row['sequence'])
    result = []
    for index, anchor in enumerate(anchors):
        if parse_day(anchor['date']) >= from_day:
            result.append(release(data, game_id, anchor['sequence']))
        if len(result) >= count:
            return result[:count]
        first = align(parse_day(anchor['date']) + period, game)
        offset = max(0, (from_day - first + period - 1) // period)
        upper = anchors[index + 1]['sequence'] - anchor['sequence'] - 2 if index + 1 < len(anchors) else (MAX_DAY - first) // period
        while offset <= upper and len(result) < count:
            result.append(release(data, game_id, anchor['sequence'] + 1 + offset))
            offset += 1
        if len(result) >= count:
            break
    return result[:count]


def validate(data):
    """Reject inconsistent shared data before it reaches the site or Calendar."""
    config = data['config']
    for name in DATA_NAMES:
        if data[name].get('schemaVersion') != 1 or data[name].get('datasetId') != config.get('datasetId'):
            raise ValueError(f'{name}.json has an incompatible schema or datasetId.')
    if config.get('timeZone') != 'Asia/Jakarta':
        raise ValueError('The dataset must use Asia/Jakarta.')
    source_map = data['sources']['sources']
    for source in source_map.values():
        url = urlsplit(source['url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('Evidence links must be HTTPS URLs without credentials.')
    ids = [game['id'] for game in config['games']]
    if len(set(ids)) != len(ids) or not ids:
        raise ValueError('Game IDs must be unique.')
    for game in config['games']:
        game_id = game['id']
        if type(game['cadenceWeeks']) is not int or not 1 <= game['cadenceWeeks'] <= 5200:
            raise ValueError('cadenceWeeks must be an integer between 1 and 5200.')
        if game['rounding'] not in ('nearest', 'next', 'previous') or game['preferredWeekday'] not in range(7):
            raise ValueError('Invalid weekday alignment.')
        anchors = sorted(data['overrides']['games'][game_id], key=lambda row: row['sequence'])
        if not anchors or anchors[0]['sequence'] != 0:
            raise ValueError('Each game requires its launch anchor.')
        previous = None
        for row in anchors:
            if type(row['sequence']) is not int or row['sequence'] < 0:
                raise ValueError('Sequence IDs must be nonnegative integers.')
            day = parse_day(row['date'])
            if row.get('status') != 'confirmed' or row.get('verification') not in ('official', 'archive'):
                raise ValueError('Only dated, officially verified or archived overrides are permitted.')
            sources = row.get('sources', [])
            if not sources:
                raise ValueError('Overrides need evidence.')
            if any(key not in source_map for key in sources):
                raise ValueError('Missing source record.')
            if row['verification'] == 'official' and not any(source_map[key]['kind'] == 'official' for key in sources):
                raise ValueError('Official dates require publisher evidence.')
            if previous:
                gap = row['sequence'] - previous['sequence']
                if gap <= 0 or day <= parse_day(previous['date']):
                    raise ValueError('Release sequence and dates must be strictly increasing.')
                if gap > 1 and align(parse_day(previous['date']) + game['cadenceWeeks'] * 7, game) + (gap - 2) * game['cadenceWeeks'] * 7 >= day:
                    raise ValueError('An intermediate prediction overlaps a confirmed release.')
            previous = row
        labels = set()
        for sequence, name in data['versions']['games'][game_id].items():
            if not sequence.isdigit() or not (name.get('label') or name.get('title')):
                raise ValueError('Invalid version-name record.')
            if name.get('label') is not None:
                if not isinstance(name['label'], str) or name['label'] in labels:
                    raise ValueError('Version labels must be unique strings.')
                labels.add(name['label'])
            if not name.get('sources'):
                raise ValueError('Version names require evidence.')
            if any(key not in source_map for key in name.get('sources', [])):
                raise ValueError('Missing version-name evidence.')


def restore_text(path, text):
    """Atomically restore an exact UTF-8 file snapshot without changing unrelated files."""
    path = Path(path)
    handle, temporary = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def recover_data(root=ROOT):
    """Restore a recorded interrupted transaction only if no external edits conflict."""
    root = Path(root)
    path = root / '.patch-calendar' / 'data-transaction.json'
    if not path.exists():
        return False
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('The recovery journal cannot be a symbolic link.')
    journal = read_json(path)
    if journal.get('schemaVersion') != 1 or set(journal.get('originalText', {})) != set(DATA_NAMES):
        raise ValueError('Unrecognized recovery journal; no files were changed.')
    before = {name: json.loads(journal['originalText'][name].lstrip('\ufeff')) for name in DATA_NAMES}
    validate(before)
    for name in DATA_NAMES:
        current = root / 'data' / (name + '.json')
        if current.is_symlink() or not current.is_file():
            raise ValueError('A data file moved during recovery; no files were changed.')
        value = read_json(current)
        if digest(value) not in (digest(before[name]), journal['plannedHashes'][name]):
            raise ValueError('A data file was edited outside the interrupted update. Recovery refused to overwrite it.')
    for name in DATA_NAMES:
        restore_text(root / 'data' / (name + '.json'), journal['originalText'][name])
    path.unlink()
    return True


def save_data(data, root=ROOT, expected=None):
    """Journal a validated data set, rollback ordinary write failures, and detect external edits."""
    root = Path(root)
    validate(data)
    paths = {name: root / 'data' / (name + '.json') for name in DATA_NAMES}
    originals = {name: path.read_bytes().decode('utf-8') for name, path in paths.items()}
    before = {name: json.loads(text.lstrip('\ufeff')) for name, text in originals.items()}
    if expected is not None and before != expected:
        raise ValueError('The local data changed while the hunt was running. No update was saved; rerun against the new files.')
    changed = [name for name in DATA_NAMES if before[name] != data[name]]
    if not changed:
        return []
    directory = root / '.patch-calendar'
    if directory.is_symlink():
        raise ValueError('The transaction journal must stay inside this project.')
    directory.mkdir(parents=True, exist_ok=True)
    journal = directory / 'data-transaction.json'
    if journal.exists():
        raise ValueError('A data recovery journal already exists; use hunt.py --recover first.')
    write_json(journal, {'schemaVersion': 1, 'originalText': originals,
                        'plannedHashes': {name: digest(data[name]) for name in DATA_NAMES}}, private=True)
    try:
        for name in changed:
            if read_json(paths[name]) != before[name]:
                raise ValueError('A data file changed while saving; recovery will not overwrite external edits.')
            write_json(paths[name], data[name])
        journal.unlink()
    except BaseException:
        recover_data(root)
        raise
    return [str(paths[name].relative_to(root)) for name in changed]


from contextlib import contextmanager


@contextmanager
def exclusive_lock(path):
    """Serialize local sync processes with an OS lock that releases after a crash."""
    with open(path, 'a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('Another local Calendar sync is running. No duplicate sync was started.') from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
