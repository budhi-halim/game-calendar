"""Deterministic offline regression tests; no publisher or Google requests are sent."""

import copy
import json
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ROOT, MAX_DAY, civil_to_day, day_to_civil, digest, exclusive_lock, iso, load_data, next_releases, parse_day, release, validate
from sources import canonical_article, html_text, parse_article, published_day, walk_news
from hunt import locate_sequence, merge_observation
from calendar_sync import desired_events, event_body, event_id, insert_event, plan_fingerprint, reconcile, settings_for
from google_api import ApiError, GoogleAPI


class FakeAPI(GoogleAPI):
    """Emulate only the REST operations used by reconciliation."""
    def __init__(self):
        """Create an isolated in-memory calendar."""
        super().__init__({})
        self.events, self.calls, self.version = {}, [], 0

    def list_all(self, path, params=None):
        """Return the requested calendar's events."""
        target = path.split('/')[2]
        return [copy.deepcopy(event) for (calendar, _), event in self.events.items() if calendar == target]

    def request(self, method, path, params=None, body=None, etag=None):
        """Apply a Google-like ETag and ID contract without contacting Google."""
        self.calls.append((method, path))
        parts = path.split('/')
        target = parts[2]
        identifier = body['id'] if method == 'POST' else parts[-1]
        key = (target, identifier)
        existing = self.events.get(key)
        if method == 'GET':
            if not existing:
                raise ApiError(404, 'Missing')
            return copy.deepcopy(existing)
        if method == 'POST' and existing:
            raise ApiError(409, 'Conflict')
        if method in ('PATCH', 'DELETE') and (not existing or existing.get('etag') != etag):
            raise ApiError(412, 'ETag changed')
        if method == 'DELETE':
            self.events[key]['status'] = 'cancelled'
            return {}
        self.version += 1
        value = (copy.deepcopy(existing) if existing else {}) | copy.deepcopy(body)
        value.update({'id': identifier, 'etag': f'"{self.version}"'})
        self.events[key] = value
        return copy.deepcopy(value)


class CalendarTests(unittest.TestCase):
    """Date math, sequence identity, rolling windows, and safe Calendar writes."""
    def setUp(self):
        """Use a fresh copy of the public canonical data."""
        self.data = json.loads(Path(__file__).with_name('fixture-data.json').read_text(encoding='utf-8'))
        self.settings = settings_for(self.data)
        self.targets = {game['id']: 'games' for game in self.data['config']['games']}
        self.day = parse_day('2026-10-04')

    def test_launch_history_and_nine_future(self):
        plan = desired_events(self.data, '2026-10-04')
        self.assertEqual(sum(item['future'] for item in plan.values()), 9)
        self.assertEqual(sum(not item['future'] for item in plan.values()), 57)
        for game in self.targets:
            self.assertIn(game + '-0', plan)
            self.assertEqual(sum(item['future'] and item['gameId'] == game for item in plan.values()), 3)

    def test_positive_javascript_date_boundary(self):
        self.assertEqual(iso(MAX_DAY), '+275760-09-13')
        self.assertEqual(parse_day('+275760-09-13'), MAX_DAY)
        with self.assertRaises(ValueError):
            parse_day('+275760-09-14')
        values = next_releases(self.data, 'hsr', MAX_DAY - 365, 3)
        self.assertTrue(values)
        self.assertTrue(all(item['day'] <= MAX_DAY for item in values))

    def test_gregorian_roundtrip(self):
        for year in (1, 99, 100, 400, 1900, 2000, 2026, 9999, 10000, 275759):
            for month in (1, 2, 3, 12):
                self.assertEqual(day_to_civil(civil_to_day(year, month, 12)), (year, month, 12))
                if year <= 9999:
                    self.assertEqual(civil_to_day(year, month, 12), (date(year, month, 12) - date(1970, 1, 1)).days)

    def test_invalid_dates_rejected(self):
        for value in ('2026-02-29', '1900-02-29', '2026-13-01', '2026-04-31', '0000-01-01', '2026/02/03'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_day(value)

    def test_overrides_reanchor_without_compensation(self):
        anchors = self.data['overrides']['games']['hsr']
        last = copy.deepcopy(anchors[-1])
        last.update({'sequence': 31, 'date': '2026-11-04'})
        anchors.append(last)
        validate(self.data)
        self.assertEqual(release(self.data, 'hsr', 31)['date'], '2026-11-04')
        self.assertEqual(release(self.data, 'hsr', 32)['date'], '2026-12-16')

    def test_confirmed_weekday_never_rounded(self):
        self.assertEqual(release(self.data, 'hsr', 30)['date'], '2026-09-28')
        self.assertEqual(release(self.data, 'hsr', 31)['date'], '2026-11-11')
        self.assertEqual(release(self.data, 'endfield', 6)['date'], '2026-10-15')

    def test_future_names_are_independent(self):
        zzz = release(self.data, 'zzz', 20)
        self.assertEqual(zzz['label'], '3.3')
        self.assertEqual(zzz['status'], 'projected')
        self.assertEqual(zzz['nameVerification'], 'official')
        self.assertIsNone(release(self.data, 'zzz', 21)['label'])
        self.assertEqual(release(self.data, 'endfield', 6)['title'], 'Sanctuary of Ink')
        self.assertIsNone(release(self.data, 'endfield', 6)['label'])

    def test_event_fields(self):
        event = event_body(self.data, release(self.data, 'zzz', 20), self.settings)
        self.assertEqual(event['summary'], 'ZZZ 3.3 · Projected')
        self.assertEqual(event['status'], 'tentative')
        self.assertEqual(event['start'], {'date': '2026-10-21'})
        self.assertEqual(event['end'], {'date': '2026-10-22'})
        self.assertEqual(event['reminders'], {'useDefault': False, 'overrides': []})
        self.assertEqual(event['transparency'], 'transparent')
        self.assertEqual(event['visibility'], 'private')
        self.assertNotIn('attendees', event)
        self.assertNotIn('conferenceData', event)

    def test_calendar_ids_do_not_include_dates_or_names(self):
        before = event_body(self.data, release(self.data, 'zzz', 20), self.settings)
        self.data['versions']['games']['zzz']['20']['label'] = '4.0'
        self.data['overrides']['games']['zzz'].append({'sequence': 20, 'date': '2026-10-28', 'status': 'confirmed', 'verification': 'official', 'sources': ['zzz-3-3-name']})
        after = event_body(self.data, release(self.data, 'zzz', 20), self.settings)
        self.assertEqual(before['id'], after['id'])
        self.assertNotEqual(before['start'], after['start'])
        self.assertRegex(after['id'], r'^[0-9a-v]{5,1024}$')

    def test_repeated_sync_is_idempotent(self):
        api = FakeAPI()
        plan = desired_events(self.data, '2026-10-04')
        first = reconcile(api, plan, self.targets, self.day, self.settings['namespace'])
        second = reconcile(api, plan, self.targets, self.day, self.settings['namespace'])
        self.assertEqual(first['created'], 66)
        self.assertEqual(second['unchanged'], 66)
        self.assertEqual(second['created'] + second['updated'] + second['removedFuture'], 0)

    def test_past_events_kept_when_window_rolls(self):
        api = FakeAPI()
        reconcile(api, desired_events(self.data, '2026-10-04'), self.targets, self.day, self.settings['namespace'])
        counts = reconcile(api, desired_events(self.data, '2026-10-15'), self.targets, parse_day('2026-10-15'), self.settings['namespace'])
        self.assertEqual(counts['created'], 1)
        self.assertEqual(counts['keptPast'], 1)
        active = [item for item in api.events.values() if item['status'] != 'cancelled']
        self.assertEqual(len(active), 67)
        self.assertEqual(sum(parse_day(item['start']['date']) > parse_day('2026-10-15') for item in active), 9)

    def test_date_and_name_update_existing_event(self):
        api = FakeAPI()
        reconcile(api, desired_events(self.data, '2026-10-04'), self.targets, self.day, self.settings['namespace'])
        identifier = event_id(self.settings['namespace'], 'zzz-20')
        self.data['overrides']['games']['zzz'].append({'sequence': 20, 'date': '2026-10-28', 'status': 'confirmed', 'verification': 'official', 'sources': ['zzz-3-3-name']})
        reconcile(api, desired_events(self.data, '2026-10-04'), self.targets, self.day, self.settings['namespace'])
        event = api.events[('games', identifier)]
        self.assertEqual(event['summary'], 'ZZZ 3.3')
        self.assertEqual(event['start']['date'], '2026-10-28')
        self.assertEqual(event['status'], 'confirmed')

    def test_unrelated_events_untouched(self):
        api = FakeAPI()
        api.events[('games', 'personal')] = {'id': 'personal', 'summary': 'Personal appointment', 'etag': '1'}
        reconcile(api, desired_events(self.data, '2026-10-04'), self.targets, self.day, self.settings['namespace'])
        self.assertEqual(api.events[('games', 'personal')]['summary'], 'Personal appointment')
        self.assertFalse(any(path.endswith('/personal') for _, path in api.calls))

    def test_deleted_id_retry_uses_generation(self):
        api = FakeAPI()
        body = event_body(self.data, release(self.data, 'zzz', 20), self.settings)
        api.events[('games', body['id'])] = {'id': body['id'], 'status': 'cancelled'}
        created = insert_event(api, 'games', body, self.settings['namespace'])
        self.assertEqual(created['id'], event_id(self.settings['namespace'], 'zzz-20', 1))

    def test_foreign_id_collision_never_overwritten(self):
        api = FakeAPI()
        body = event_body(self.data, release(self.data, 'zzz', 20), self.settings)
        api.events[('games', body['id'])] = {'id': body['id'], 'status': 'confirmed'}
        with self.assertRaises(ValueError):
            insert_event(api, 'games', body, self.settings['namespace'])

    def test_managed_guest_addition_stops_before_writes(self):
        api = FakeAPI()
        body = event_body(self.data, release(self.data, 'zzz', 20), self.settings)
        body['attendees'] = [{'email': 'example@example.invalid'}]
        api.events[('games', body['id'])] = body
        with self.assertRaises(ValueError):
            reconcile(api, desired_events(self.data, '2026-10-04'), self.targets, self.day, self.settings['namespace'])
        self.assertFalse(api.calls)

    def test_fingerprint_skips_unchanged_day(self):
        first = desired_events(self.data, '2026-10-04')
        second = desired_events(self.data, '2026-10-05')
        self.assertEqual(plan_fingerprint(self.data, first, self.targets), plan_fingerprint(self.data, second, self.targets))
        before = plan_fingerprint(self.data, first, self.targets)
        self.data['sources']['checkedOn'] = '2026-10-05'
        self.assertEqual(before, plan_fingerprint(self.data, second, self.targets))
        rolled = desired_events(self.data, '2026-10-15')
        self.assertNotEqual(before, plan_fingerprint(self.data, rolled, self.targets))

    def test_fingerprint_includes_schedule_policy(self):
        before = plan_fingerprint(self.data, desired_events(self.data, '2026-10-04'), self.targets)
        self.data['config']['games'][0]['cadenceWeeks'] = 7
        self.assertNotEqual(before, plan_fingerprint(self.data, desired_events(self.data, '2026-10-04'), self.targets))

    def test_local_lock_released(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'lock'
            with exclusive_lock(path):
                with self.assertRaises(RuntimeError):
                    with exclusive_lock(path):
                        pass
            with exclusive_lock(path):
                pass

    def test_private_calendar_guard(self):
        api = GoogleAPI({})
        calendars = [{'id': 'games', 'accessRole': 'owner', 'primary': True}]
        private = [{'role': 'owner', 'scope': {'type': 'user'}}]
        with patch.object(api, 'list_all', side_effect=[calendars, private]):
            self.assertEqual(api.resolve_targets({'hsr': 'primary'}), {'hsr': 'games'})
        with patch.object(api, 'list_all', side_effect=[calendars, private + [{'role': 'reader', 'scope': {'type': 'default'}}]]):
            with self.assertRaises(ValueError):
                api.resolve_targets({'hsr': 'primary'})


class HunterTests(unittest.TestCase):
    """Synthetic publisher fixtures test parsing and stable sequence assignment."""
    def setUp(self):
        """Load independent canonical data for each test."""
        self.data = json.loads(Path(__file__).with_name('fixture-data.json').read_text(encoding='utf-8'))
        self.settings = self.data['config']['automation']['sources']
        self.feed = self.settings['feeds']['hsr']

    def article(self, title, text, game='hsr'):
        """Build a synthetic official-page fixture, not another user-facing format."""
        return {'gameId': game, 'url': self.settings['feeds'][game]['url'] + '/999999', 'title': title, 'text': text, 'publishedOn': '2026-10-04'}

    def test_iso_maintenance(self):
        items = parse_article(self.article('Version 4.7 Update and Maintenance Notice', 'Maintenance will begin on 2026/11/11 06:00 (UTC+8).'))
        self.assertEqual(items[0]['date'], '2026-11-11')

    def test_jakarta_crosses_previous_date(self):
        items = parse_article(self.article('Version 4.7 Update and Maintenance Notice', 'Maintenance begins on 2026/11/11 00:30 (UTC+8).'))
        self.assertEqual(items[0]['date'], '2026-11-10')

    def test_named_endfield_and_two_regions(self):
        text = 'Pre-download is available on October 10, 2026.\nVersion Maintenance Time\nAsia Server: October 15, 2026 at 06:00 – October 15, 2026 at 12:00 (UTC+8)\nAmericas / Europe Server: October 14, 2026 at 17:00 – October 14, 2026 at 23:00 (UTC-5)'
        item = parse_article(self.article('[Example Season] Version Pre-Download & Update Notice', text, 'endfield'), 'title')[0]
        self.assertEqual(item['title'], 'Example Season')
        self.assertEqual(item['date'], '2026-10-15')

    def test_begin_maintenance_word_order(self):
        item = parse_article(self.article('[Example Season] Version Pre-Download & Update Notice', 'We plan to begin maintenance for the client update on October 15, 2026 at 06:00 (UTC+8).', 'endfield'), 'title')[0]
        self.assertEqual(item['date'], '2026-10-15')

    def test_special_program_never_sets_release_date(self):
        item = parse_article(self.article('Version 4.7 Special Program', 'The broadcast starts October 10, 2026. Version 4.7 arrives October 20, 2026.'))[0]
        self.assertEqual(item['label'], '4.7')
        self.assertIsNone(item['date'])

    def test_event_body_can_confirm_a_future_number_only(self):
        items = parse_article(self.article('Example Rewards Event Details', 'Available after the update until 2026/11/30. Missions unlock during Versions 3.2 – 3.3.', 'zzz'))
        self.assertEqual([item['label'] for item in items], ['3.2', '3.3'])
        self.assertTrue(all(item['date'] is None for item in items))

    def test_broadcast_title_with_quotes(self):
        item = parse_article(self.article('Arknights: Endfield “Example Season” Special Program', 'The special program begins October 10, 2026.', 'endfield'), 'title')[0]
        self.assertEqual(item['title'], 'Example Season')
        self.assertIsNone(item['date'])

    def test_unknown_timezone_rejected(self):
        item = parse_article(self.article('Version 4.7 Update and Maintenance Notice', 'Maintenance begins on 2026/11/11 06:00 (server time).'))[0]
        self.assertIsNone(item['date'])
        self.assertTrue(item['warnings'])

    def test_conflicting_dates_rejected(self):
        item = parse_article(self.article('Version 4.7 Update and Maintenance Notice', 'Maintenance begins on 2026/11/11 06:00 (UTC+8).\nMaintenance starts on 2026/11/12 06:00 (UTC+8).'))[0]
        self.assertIsNone(item['date'])
        self.assertTrue(item['warnings'])

    def test_hotfix_not_a_release(self):
        self.assertFalse(parse_article(self.article('Version 4.7 Hotfix', 'Maintenance begins on 2026/11/11 06:00 (UTC+8).')))

    def test_plain_release_date(self):
        item = parse_article(self.article('Version 4.7 Announcement', 'Version 4.7 arrives on November 11, 2026.'))[0]
        self.assertEqual(item['date'], '2026-11-11')

    def test_body_dates_without_release_context_ignored(self):
        item = parse_article(self.article('Version 4.7 Update Details', 'A banner ends 2026/12/01. An event begins 2026/12/02.'))[0]
        self.assertIsNone(item['date'])

    def test_same_version_matches_stable_sequence(self):
        item = parse_article(self.article('Version 4.6 Update Details', 'Version 4.6 information.'))[0]
        self.assertEqual(locate_sequence(self.data, 'hsr', item), 30)

    def test_announced_major_rollover(self):
        item = parse_article(self.article('Version 5.0 Special Program', 'A new version will be presented.'))[0]
        self.assertEqual(locate_sequence(self.data, 'hsr', item), 31)
        merge_observation(self.data, item, self.settings)
        self.assertEqual(self.data['versions']['games']['hsr']['31']['label'], '5.0')
        self.assertEqual(release(self.data, 'hsr', 31)['status'], 'projected')

    def test_distant_major_mention_deferred(self):
        item = parse_article(self.article('Development discussion', 'Version 6.0 is in development.'))[0]
        self.assertIsNone(locate_sequence(self.data, 'hsr', item))

    def test_two_announced_minor_names_remain_ordered(self):
        items = parse_article(self.article('Development discussion', 'The next features span Versions 4.7 and 4.8.'))
        for item in items:
            merge_observation(self.data, item, self.settings)
        self.assertEqual(self.data['versions']['games']['hsr']['31']['label'], '4.7')
        self.assertEqual(self.data['versions']['games']['hsr']['32']['label'], '4.8')

    def test_date_merge_and_repeat_idempotent(self):
        item = parse_article(self.article('Version 4.7 Update and Maintenance Notice', 'Maintenance begins on 2026/11/04 06:00 (UTC+8).'))[0]
        merge_observation(self.data, item, self.settings)
        validate(self.data)
        before = digest(self.data)
        merge_observation(self.data, item, self.settings)
        self.assertEqual(before, digest(self.data))
        self.assertEqual(release(self.data, 'hsr', 32)['date'], '2026-12-16')

    def test_wrong_host_rejected(self):
        item = parse_article(self.article('Version 4.7 Announcement', 'Version 4.7 information.'))[0]
        item['url'] = 'https://untrusted.example/news/999999'
        with self.assertRaises(ValueError):
            merge_observation(self.data, item, self.settings)

    def test_canonical_article_validation(self):
        self.assertEqual(canonical_article('/en-us/news/123?tracking=1', self.feed), 'https://hsr.hoyoverse.com/en-us/news/123')
        self.assertIsNone(canonical_article('https://hsr.hoyoverse.com.evil.invalid/en-us/news/123', self.feed))
        self.assertIsNone(canonical_article('javascript:alert(1)', self.feed))

    def test_official_json_discovery_includes_general_news(self):
        payload = {'data': {'list': [{'content_id': 123, 'title': 'Example Event Details', 'publish_time': '2026-10-04', 'content': '<p>' + 'Example text. ' * 20 + '</p>'}]}}
        items = walk_news(payload, self.feed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['url'], 'https://hsr.hoyoverse.com/en-us/news/123')
        self.assertEqual(items[0]['publishedOn'], '2026-10-04')
        self.assertTrue(items[0]['text'])

    def test_html_parser_ignores_script(self):
        self.assertEqual(html_text('<p>Hello &amp; goodbye</p><script>malicious()</script>'), 'Hello & goodbye')

    def test_publication_timestamp(self):
        self.assertEqual(published_day('2026-10-04 12:00'), '2026-10-04')
        self.assertIsNone(published_day('not a timestamp'))


if __name__ == '__main__':
    unittest.main()
