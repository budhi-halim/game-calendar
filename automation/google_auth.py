"""Initialize private Google settings, authorize locally, or list destination calendars."""

import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from common import ROOT, private_directory, read_json, write_json
from google_api import GoogleAPI, SCOPES, credentials, token_request
from progress import Progress, wait_for


def authorize(path, progress=None):
    """Use Desktop OAuth, PKCE, a state nonce, and a loopback-only callback."""
    owned_reporter = progress is None
    progress = progress or Progress('google-auth')
    fallback = None
    try:
        progress.info('Authorization 1/4 | Reading the local Desktop client settings.')
        values = read_json(path)
        if not values.get('client_id') or not values.get('client_secret'):
            raise ValueError('Fill client_id and client_secret in the private JSON first. Use a Desktop OAuth client.')
        progress.protect(*(values.get(key) for key in ('client_id', 'client_secret', 'refresh_token')))
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        state = secrets.token_urlsafe(32)
        progress.protect(verifier, challenge, state)
        result = {}

        class Callback(BaseHTTPRequestHandler):
            """Accept only this login's callback; never log authorization query strings."""

            def log_message(self, *args):
                """Suppress the HTTP server's default access log."""
                return

            def do_GET(self):
                """Validate the callback nonce before accepting a code."""
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                valid = parsed.path == '/' and hmac.compare_digest(query.get('state', [''])[0], state)
                if not valid:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'Invalid callback.')
                    return
                result.update({key: entries[0] for key, entries in query.items()})
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(b'Authorization received. Return to your terminal. You may close this tab.')

        with HTTPServer(('127.0.0.1', 0), Callback) as server:
            server.timeout = 1
            redirect = f'http://127.0.0.1:{server.server_port}/'
            url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
                'client_id': values['client_id'], 'redirect_uri': redirect,
                'response_type': 'code', 'scope': ' '.join(SCOPES),
                'access_type': 'offline', 'prompt': 'consent', 'state': state,
                'code_challenge': challenge, 'code_challenge_method': 'S256'})
            # A private, temporary fallback replaces printing a token-bearing login URL.
            descriptor, name = tempfile.mkstemp(prefix='authorization-', suffix='.html', dir=path.parent)
            fallback = Path(name)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                             '<title>Game Calendar authorization</title><h1>Game Calendar authorization</h1>'
                             '<p>Keep the terminal running. This link belongs to the current local authorization attempt.</p>'
                             '<p><a rel="noreferrer" href="' + html.escape(url, quote=True) + '">Continue with Google</a></p></html>')
            progress.info('Authorization 2/4 | Opening your browser; no Calendar events will be created.')
            progress.info('If the browser does not open, open this local file on this PC: ' + str(fallback))
            with wait_for(progress, 'Opening the local authorization browser'):
                opened = webbrowser.open(url)
            if not opened:
                progress.warning('The browser did not report opening. Use the local authorization file shown above.')
            progress.info('Waiting for you to approve all three permissions in the browser (up to 5 minutes).')
            deadline = time.monotonic() + 300
            with wait_for(progress, 'Browser consent on this PC; up to 5 minutes from the start of the wait'):
                while not result and time.monotonic() < deadline:
                    server.handle_request()
            if not result.get('code'):
                raise ValueError('Authorization was denied or timed out; the private JSON was not changed.')
            progress.protect(result['code'])
            progress.success('The browser callback was received and its state was validated.')
            progress.info('Authorization 3/4 | Exchanging the authorization code with Google.')
            token = token_request({'client_id': values['client_id'], 'client_secret': values['client_secret'],
                                   'grant_type': 'authorization_code', 'code': result['code'],
                                   'redirect_uri': redirect, 'code_verifier': verifier}, progress)
        progress.protect(*(token.get(key) for key in ('access_token', 'refresh_token', 'id_token')))
        if not token.get('refresh_token'):
            raise ValueError('Google did not return an offline refresh token. Revoke this app authorization and authorize again.')
        granted = set(token.get('scope', '').split())
        if granted and not set(SCOPES).issubset(granted):
            raise ValueError('All three requested Calendar permissions are required; no credentials were saved.')
        progress.info('Authorization 4/4 | Saving the offline authorization in the project-local settings.')
        values['refresh_token'] = token['refresh_token']
        write_json(path, values, private=True)
        progress.success('Offline authorization saved to ' + str(path) + '. No calendar events were created.')
    finally:
        if fallback is not None:
            try:
                fallback.unlink(missing_ok=True)
            except OSError:
                progress.warning('Could not remove the temporary authorization HTML file; keep it private and remove it after this attempt.')
        if owned_reporter:
            progress.close()


def main():
    """Report all Google setup operations while keeping credentials in this project."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--init', action='store_true')
    group.add_argument('--authorize', action='store_true')
    group.add_argument('--list-calendars', action='store_true')
    args = parser.parse_args()
    progress = Progress('google-auth')
    action = 'INIT; local template only, no Google requests.' if args.init else 'AUTHORIZE; browser consent, no Calendar writes.' if args.authorize else 'LIST CALENDARS; read-only Google requests.'
    progress.info('Google setup started: ' + action)
    try:
        progress.info('Checking the project-local runtime folder and Git ignore protection.')
        directory = private_directory()
        progress.attach(directory / 'google-auth.log')
        path = directory / 'google-calendar.json'
        if args.init:
            if path.exists():
                progress.success('Kept existing private file: ' + str(path))
            else:
                write_json(path, read_json(ROOT / 'automation' / 'google-calendar.example.json'), private=True)
                progress.success('Created private template: ' + str(path))
            return 0
        if args.authorize:
            authorize(path, progress)
            return 0
        progress.info('Loading saved authorization, then reading the account calendar list.')
        api = GoogleAPI(credentials(), progress=progress)
        owned = [item for item in api.list_all('/users/me/calendarList', {'maxResults': 250}) if item.get('accessRole') == 'owner']
        for index, item in enumerate(owned, 1):
            progress.console_data(json.dumps({'name': item.get('summary'), 'calendar_id': item['id'], 'primary': bool(item.get('primary')), 'timeZone': item.get('timeZone')}, ensure_ascii=False),
                                  f'Owned calendar {index}/{len(owned)}')
        progress.success(f'Calendar listing complete: {len(owned)} owned calendars. No events were changed.')
        progress.info('For your primary calendar, keep calendar_id="primary" and calendar_ids={}; no opaque ID is needed.')
        return 0
    except KeyboardInterrupt:
        progress.warning('Google setup interrupted by the user (Ctrl+C). No Calendar events were created.')
        return 130
    except Exception as error:
        progress.error('GOOGLE SETUP FAILED: ' + str(error))
        return 1
    finally:
        progress.close()


if __name__ == '__main__':
    sys.exit(main())
