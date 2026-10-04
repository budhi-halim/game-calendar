"""Small Google Calendar REST client with bounded retries and no token logging."""

import json
import os
import random
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from common import private_directory, read_json
from progress import wait_for

SCOPES = [
    'https://www.googleapis.com/auth/calendar.events.owned',
    'https://www.googleapis.com/auth/calendar.calendarlist.readonly',
    'https://www.googleapis.com/auth/calendar.acls.readonly',
]


class ApiError(RuntimeError):
    """Expose an HTTP status without printing credentials or request URLs."""

    def __init__(self, status, reason):
        """Retain a machine-readable status for conflict handling."""
        self.status = status
        super().__init__(f'Google API HTTP {status}: {reason}')


def credentials():
    """Prefer a GitHub secret, otherwise load the private development JSON."""
    raw = os.environ.get('PATCH_CALENDAR_GOOGLE_JSON', '').strip()
    if raw:
        value = json.loads(raw)
    else:
        path = private_directory() / 'google-calendar.json'
        if not path.exists():
            raise ValueError('Run python automation/google_auth.py --init, then complete the private JSON and authorize it.')
        value = read_json(path)
    for field in ('client_id', 'client_secret', 'refresh_token'):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f'The private Google JSON needs {field}.')
    return value


def token_request(fields, progress=None):
    """Exchange OAuth credentials without exposing token endpoint error bodies."""
    request = Request('https://oauth2.googleapis.com/token', data=urlencode(fields).encode(), method='POST', headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with wait_for(progress, 'Google OAuth token exchange (30s socket timeout)'):
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read(1024 * 1024))
    except HTTPError as error:
        reason = 'Token exchange failed. Reauthorize if the refresh token expired or access was revoked.'
        raise ApiError(error.code, reason) from None
    except URLError:
        raise RuntimeError('Cannot connect to the Google OAuth endpoint.') from None


class GoogleAPI:
    """Authenticated REST operations limited to the Calendar API."""

    def __init__(self, values, progress=None):
        """Keep access tokens in memory only."""
        self.values = values
        self.progress = progress
        if progress:
            progress.protect(*(values.get(key) for key in ('client_id', 'client_secret', 'refresh_token')))
        self.access_token = None
        self.expires = 0

    def refresh(self):
        """Get a temporary access token using offline authorization."""
        if self.progress:
            self.progress.info('Refreshing Google access authorization; credentials are not logged.')
        token = token_request({key: self.values[key] for key in ('client_id', 'client_secret', 'refresh_token')} | {'grant_type': 'refresh_token'}, self.progress)
        self.access_token = token['access_token']
        if self.progress:
            self.progress.protect(self.access_token, token.get('id_token'))
            self.progress.success('Google access authorization refreshed.')
        self.expires = time.monotonic() + int(token.get('expires_in', 3600)) - 90

    def resource_label(self, path):
        """Describe a request without exposing IDs, email addresses, or query strings."""
        if path == '/users/me/calendarList':
            return 'calendar list'
        if path.endswith('/acl'):
            return 'sharing permissions'
        if '/events/' in path:
            return 'managed event'
        if path.endswith('/events'):
            return 'managed events'
        return 'Calendar resource'

    def pause_retry(self, attempt, reason):
        """Explain the bounded backoff before sleeping; the heartbeat covers long waits."""
        delay = min(30, 2 ** attempt + random.random())
        if self.progress:
            self.progress.warning(f'{reason} Retrying in {delay:.1f}s (next attempt {attempt + 2}/5).')
        with wait_for(self.progress, f'Google retry backoff: {delay:.1f}s'):
            time.sleep(delay)

    def request(self, method, path, params=None, body=None, etag=None):
        """Send a request, retrying throttling/transient errors and one expired token."""
        if not path.startswith('/') or '://' in path:
            raise ValueError('Only Calendar API paths are allowed.')
        url = 'https://www.googleapis.com/calendar/v3' + path
        if params:
            url += '?' + urlencode(params, doseq=True)
        refreshed = False
        for attempt in range(5):
            if not self.access_token or time.monotonic() >= self.expires:
                self.refresh()
            headers = {'Authorization': 'Bearer ' + self.access_token, 'Accept': 'application/json'}
            if body is not None:
                headers['Content-Type'] = 'application/json'
            if etag:
                headers['If-Match'] = etag
            request = Request(url, method=method, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            try:
                label = f'Google {method} {self.resource_label(path)} (attempt {attempt + 1}/5; 35s socket timeout)'
                with wait_for(self.progress, label):
                    with urlopen(request, timeout=35) as response:
                        raw = response.read(10 * 1024 * 1024)
                        return json.loads(raw) if raw else {}
            except HTTPError as error:
                if error.code == 401 and not refreshed:
                    if self.progress:
                        self.progress.warning('Google rejected the access token; refreshing it once before retrying.')
                    self.refresh()
                    refreshed = True
                    continue
                rate_limited = False
                if error.code == 403:
                    try:
                        payload = json.loads(error.read(4096))
                        rate_limited = any(item.get('reason') in ('rateLimitExceeded', 'userRateLimitExceeded') for item in payload.get('error', {}).get('errors', []))
                    except (ValueError, TypeError):
                        pass
                if (error.code in (429, 500, 502, 503, 504) or rate_limited) and attempt < 4:
                    self.pause_retry(attempt, f'Google returned HTTP {error.code}; temporary throttling or server error.')
                    continue
                reason = {403: 'Permission denied or quota exceeded.', 404: 'Calendar or event not found.', 409: 'Event ID already exists.', 410: 'The event was deleted.', 412: 'The event changed concurrently; retry the sync.'}.get(error.code, 'The request was rejected.')
                raise ApiError(error.code, reason) from None
            except (URLError, TimeoutError):
                if attempt < 4:
                    self.pause_retry(attempt, 'Google connection failed or timed out.')
                    continue
                raise RuntimeError('Google Calendar could not be reached after retries.') from None
        raise RuntimeError('Google request retry limit reached.')

    def list_all(self, path, params=None):
        """Follow every page without losing the filter or looping on a bad token."""
        query = dict(params or {})
        result, seen = [], set()
        number = 0
        while True:
            number += 1
            if self.progress:
                self.progress.info(f'Reading {self.resource_label(path)}: page {number}.')
            page = self.request('GET', path, query)
            result.extend(page.get('items', []))
            if self.progress:
                self.progress.success(f'Read {self.resource_label(path)} page {number}: {len(page.get("items", []))} records; {len(result)} total.')
            token = page.get('nextPageToken')
            if not token:
                return result
            if token in seen:
                raise RuntimeError('Google returned a repeated pagination token.')
            seen.add(token)
            query['pageToken'] = token

    def calendar_path(self, calendar_id, suffix=''):
        """Encode opaque IDs rather than interpolating them into URL structure."""
        return '/calendars/' + quote(calendar_id, safe='') + suffix

    def resolve_targets(self, requested, require_private=True):
        """Require ownership and reject calendars shared with any other principal."""
        if self.progress:
            self.progress.info('Checking destination ownership and primary-calendar selection.')
        calendars = self.list_all('/users/me/calendarList', {'maxResults': 250})
        result = {}
        for game_id, identifier in requested.items():
            match = next((item for item in calendars if (item.get('primary') if identifier == 'primary' else item['id'] == identifier)), None)
            if not match or match.get('accessRole') != 'owner':
                raise ValueError('Every selected calendar must be owned by the authorized account.')
            result[game_id] = match['id']
        if require_private:
            destinations = sorted(set(result.values()))
            for index, identifier in enumerate(destinations, 1):
                if self.progress:
                    self.progress.info(f'Checking private sharing: destination {index}/{len(destinations)}.')
                rules = self.list_all(self.calendar_path(identifier, '/acl'), {'maxResults': 250})
                # An unshared personal calendar has only its owner's active ACL entry.
                if any(rule.get('role') not in ('owner', 'none') for rule in rules) or sum(rule.get('role') == 'owner' for rule in rules) != 1:
                    raise ValueError('A destination calendar is shared. Use an unshared calendar; no events were changed.')
        if self.progress:
            self.progress.success('Destination ownership and requested privacy checks passed; no events written yet.')
        return result
