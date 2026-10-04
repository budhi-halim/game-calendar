"""Regression coverage for publication metadata, patch notices, and undated names."""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import load_data, release
from hunt import locate_sequence, merge_observation, seed_articles
from sources import parse_article, publication_from_snapshot, published_day, walk_news


class HunterReliabilityTests(unittest.TestCase):
    """Use explicit publisher-shaped fixtures; never infer a missing publisher value."""

    def setUp(self):
        """Load independent canonical records and source settings."""
        self.data = json.loads(Path(__file__).with_name('fixture-data.json').read_text(encoding='utf-8'))
        self.settings = self.data['config']['automation']['sources']
        self.feed = self.settings['feeds']['hsr']

    def article(self, title='Version 4.7 Update Details', text='A new update.', date=None):
        """Build a synthetic article without pretending it is a live source."""
        return {'gameId': 'hsr', 'url': self.feed['url'] + '/999999', 'title': title,
                'text': text, 'publishedOn': date}

    def test_publication_aliases(self):
        """Metadata variants on the exact record must survive structured extraction."""
        for key in ('start_time', 'iStartTime', 'display_date', 'published_on', 'datePublished', 'publishDate'):
            with self.subTest(key=key):
                item = walk_news({'data': {'id': 123, 'title': 'Release notice', key: '2026-10-04'}}, self.feed)[0]
                self.assertEqual(item['publishedOn'], '2026-10-04')
                self.assertTrue(item['publicationSource'].startswith('publisher JSON:'))

    def test_iso_publication_offset(self):
        """A timestamp with its own offset is converted into Jakarta's date."""
        self.assertEqual(published_day('2026-10-04T22:00:00Z'), '2026-10-05')
        self.assertEqual(published_day('2026-10-04T00:30:00+08:00'), '2026-10-03')

    def test_missing_and_invalid_metadata(self):
        """Missing or malformed metadata stays missing, never today."""
        for value in (None, '', '2026-02-30', True, 'next week', 'Updated October 4, 2026'):
            with self.subTest(value=value):
                self.assertIsNone(published_day(value))

    def test_no_publication_from_body(self):
        """Release dates embedded in prose are not article publication dates."""
        item = walk_news({'data': {'id': 123, 'title': 'Release notice', 'content': 'Maintenance begins 2026-11-11.'}}, self.feed)[0]
        self.assertIsNone(item['publishedOn'])

    def test_conflicting_dom_publication_stays_unknown(self):
        """Conflicting article metadata is not resolved by taking the last field."""
        value = {'fields': [{'value': '2026-10-04', 'source': 'meta'}, {'value': '2026-10-05', 'source': 'element'}]}
        self.assertEqual(publication_from_snapshot(value, {'url': self.feed['url'] + '/123'}, self.feed), (None, None))

    def test_other_article_jsonld_publication_ignored(self):
        """A related article cannot supply the current article's publication date."""
        value = {'jsonld': [json.dumps({'url': self.feed['url'] + '/456', 'headline': 'Another notice', 'datePublished': '2026-10-04'})]}
        self.assertEqual(publication_from_snapshot(value, {'url': self.feed['url'] + '/123'}, self.feed), (None, None))

    def test_update_time_heading(self):
        """HoYoverse-style date blocks are read independently of body paragraph breaks."""
        for heading in ('Update Time', '〓Update Time〓', 'Update Start Time', 'Update Date & Time'):
            item = parse_article(self.article(text=heading + '\n2026/11/11 06:00:00 (UTC+8)\nDuration: five hours.'))[0]
            self.assertEqual(item['date'], '2026-11-11', heading)

    def test_update_time_heading_jakarta_previous_day(self):
        """The new heading form preserves the Jakarta conversion rule."""
        item = parse_article(self.article(text='Update Start Time\n2026/11/11 00:30 (UTC+8)'))[0]
        self.assertEqual(item['date'], '2026-11-10')

    def test_update_time_with_unrecognized_date_is_not_silent(self):
        """A recognized schedule block needs a date or an explicit parser failure."""
        item = parse_article(self.article(text='Update Time\nSometime next month.'))[0]
        self.assertIsNone(item['date'])
        self.assertTrue(item['warnings'])

    def test_event_headlines_only_confirm_names(self):
        """Names on avatars, stores, funds, and Twitch events are not patch schedules."""
        for ending in ('Character Avatars', 'New Stock in the Store', 'New Eridu City Fund Details', 'Twitch Drops Event Now Live'):
            item = parse_article(self.article('Version 4.7 ' + ending, 'Update Time\n2026/11/11 06:00 (UTC+8)'))[0]
            self.assertEqual(item['label'], '4.7')
            self.assertFalse(item['releaseAnnouncement'])
            self.assertIsNone(item['date'])

    def test_undated_explicit_minor_name_is_imported(self):
        """An explicitly printed adjacent minor name does not need a fabricated timestamp."""
        item = parse_article(self.article())[0]
        self.assertEqual(locate_sequence(self.data, 'hsr', item), 31)
        merge_observation(self.data, item, self.settings)
        self.assertEqual(release(self.data, 'hsr', 31)['label'], '4.7')
        self.assertEqual(release(self.data, 'hsr', 31)['status'], 'projected')
        self.assertIsNone(item['publishedOn'])

    def test_undated_adjacent_names_in_body_remain_ordered(self):
        """Already printed adjacent names are real evidence, not inferred continuation."""
        for item in parse_article(self.article('Rewards Details', 'Applies in Versions 4.7 and 4.8.')):
            merge_observation(self.data, item, self.settings)
        self.assertEqual(release(self.data, 'hsr', 32)['label'], '4.8')
        self.assertIsNone(release(self.data, 'hsr', 33)['label'])

    def test_undated_major_requires_order_evidence(self):
        """A far-future major teaser is not automatically the immediate next patch."""
        item = parse_article(self.article('Version 5.0 Special Program'))[0]
        self.assertIsNone(locate_sequence(self.data, 'hsr', item))

    def test_undated_major_next_version_claim(self):
        """An explicit next-version claim can establish the order without publication metadata."""
        item = parse_article(self.article('Next Version 5.0 Special Program'))[0]
        self.assertEqual(locate_sequence(self.data, 'hsr', item), 31)

    def test_missing_publication_does_not_resolve_conflicting_date(self):
        """Broader name matching must not weaken protections for changing exact dates."""
        item = parse_article(self.article('Version 4.6 Update Details', 'Update Time\n2026/10/01 06:00 (UTC+8)'))[0]
        with self.assertRaises(ValueError):
            merge_observation(self.data, item, self.settings)

    def test_known_notice_seeds_stay_on_configured_publisher(self):
        """Revisited notices use approved article URLs rather than arbitrary search results."""
        for game_id, feed in self.settings['feeds'].items():
            for item in seed_articles(self.data, game_id, feed):
                self.assertTrue(item['url'].startswith(feed['url'] + '/'))

    def test_workflow_restores_calendar_behind_explicit_gates(self):
        """The single workflow calls the ordered pipeline without enabling scheduled writes."""
        text = (Path(__file__).resolve().parents[2] / '.github/workflows/update-calendar.yml').read_text()
        self.assertIn("cron: '0 19 * * *'", text)
        self.assertIn('python automation/daily.py --apply --publish-history', text)
        self.assertIn("vars.PATCH_CALENDAR_AUTOMATION == 'true'", text)
        self.assertIn('default: check', text)
        self.assertIn('secrets.PATCH_CALENDAR_GOOGLE_JSON', text)
        self.assertNotIn('git add .\n', text)


if __name__ == '__main__':
    unittest.main()
