"""Pre-release evidence regressions; all transport and Google writes are simulated."""
import copy
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hunt
from common import parse_day, release
from sources import (parse_article, press_dateline, article_media, CollectedArticles,
                     PUBLICATION_SNAPSHOT, publication_from_snapshot, walk_news)
from calendar_sync import desired_events, reconcile, settings_for, plan_fingerprint
from test_automation import FakeAPI

# Short exact publisher sentence retrieved October 12, 2026 from the DEV Comm:
# https://endfield.gryphline.com/en-us/news/7014 (published October 7).
ENDFIELD_STATEMENT = "Arknights: Endfield's Core Chapter Version, [Sanctuary of Ink], will officially launch on Oct. 15."


def article(body, headline='Version 3.3 "Journey to the Sky" Special Program | Intel Report', published='2026-10-09'):
    """All ZZZ text fixtures below are synthetic, not captured page transcripts."""
    return {'gameId': 'zzz', 'url': 'https://zenless.hoyoverse.com/en-us/news/999001',
            'title': headline, 'text': body, 'publishedOn': published}


def endfield():
    return {'gameId': 'endfield', 'url': 'https://endfield.gryphline.com/en-us/news/7014',
            'title': 'Arknights: Endfield [Sanctuary of Ink] Version DEV Comm',
            'publishedOn': '2026-10-07', 'text': ENDFIELD_STATEMENT}


class PreReleaseParserTests(unittest.TestCase):
    def test_real_endfield_sentence_is_confirmable_before_release(self):
        item = parse_article(endfield(), 'title')[0]
        self.assertEqual(item['date'], '2026-10-15')
        self.assertEqual(item['title'], 'Sanctuary of Ink')
        self.assertEqual(item['dateEvidence'][0]['yearSource'], 'article-publication-date')
        self.assertFalse(item['warnings'])

    def test_stream_report_release_not_stream_time(self):
        item = parse_article(article('The broadcast begins October 9, 2026.\nVersion 3.3 will arrive on October 21, 2026.\nThe code expires October 11, 2026.'))[0]
        self.assertEqual(item['date'], '2026-10-21')

    def test_stream_only_does_not_confirm(self):
        item = parse_article(article('The special program starts on October 9, 2026 at 19:30 (UTC+8).'))[0]
        self.assertIsNone(item['date'])
        self.assertIn('broadcast-or-reward-date-is-not-update-date', item['dateParsing']['reasonCodes'])

    def test_stream_subject_never_borrows_version_identity(self):
        for text in ('Version 3.3 Special Program will launch on October 9, 2026.',
                     'The Special Program for Version 3.3 will arrive on October 9, 2026.'):
            self.assertIsNone(parse_article(article(text))[0]['date'])

    def test_code_expiry_only_does_not_confirm(self):
        item = parse_article(article('The special-program code will expire on October 11 at 23:59:59 (UTC+8).'))[0]
        self.assertIsNone(item['date'])
        self.assertEqual(item['dateParsing']['dateMentionsForReviewOnly'][0]['dateText'], 'October 11')

    def test_yearless_release_uses_publication_not_crawl(self):
        with patch('sources.today', return_value='2041-01-01'):
            value = parse_article(article('Version 3.3 will arrive on October 21.'))[0]
        self.assertEqual(value['date'], '2026-10-21')

    def test_yearless_without_publication_is_explained(self):
        item = parse_article(article('Version 3.3 will arrive on October 21.', published=None))[0]
        self.assertIsNone(item['date'])
        self.assertIn('missing-or-ambiguous-year-reference', item['dateParsing']['reasonCodes'])

    def test_full_date_needs_no_publication(self):
        item = parse_article(article('Version 3.3 will arrive on October 21, 2026.', published=None))[0]
        self.assertEqual(item['date'], '2026-10-21')

    def test_year_boundary(self):
        item = parse_article(article('Version 3.3 will arrive on January 12.', published='2026-12-28'))[0]
        self.assertEqual(item['date'], '2027-01-12')

    def test_distant_yearless_date_stays_unknown(self):
        item = parse_article(article('Version 3.3 will arrive on October 21.', published='2026-01-01'))[0]
        self.assertIsNone(item['date'])

    def test_month_day_and_day_month_forms(self):
        for date in ('Oct. 21, 2026', 'October 21st, 2026', '21 October 2026', '21st Oct. 2026', '2026/10/21'):
            with self.subTest(date=date):
                self.assertEqual(parse_article(article('Version 3.3 will arrive on ' + date + '.'))[0]['date'], '2026-10-21')

    def test_release_verb_forms(self):
        for verb in ('will arrive', 'will officially launch', 'will be released', 'will be available', 'goes live',
                     'launches', 'arrives', 'is scheduled to launch', 'is scheduled for'):
            with self.subTest(verb=verb):
                preposition = '' if verb.endswith('for') else 'on '
                self.assertEqual(parse_article(article(f'Version 3.3 {verb} {preposition}October 21, 2026.'))[0]['date'], '2026-10-21')

    def test_only_headline_contains_release_date(self):
        item = parse_article(article('New content is coming.', 'Zenless Zone Zero Version 3.3 launches October 21, 2026'))[0]
        self.assertEqual(item['date'], '2026-10-21')

    def test_explicit_update_block_inside_report(self):
        item = parse_article(article('Update Start Time\n2026/10/21 06:00:00 (UTC+8)\nCode expiry October 11, 2026.'))[0]
        self.assertEqual(item['date'], '2026-10-21')

    def test_update_block_does_not_borrow_later_deadline(self):
        item = parse_article(article('Release date\nComing soon.\nCode expires October 11, 2026.'))[0]
        self.assertIsNone(item['date'])

    def test_generic_event_update_time_is_not_release(self):
        item = parse_article(article('Update Time\n2026/10/21', 'Version 3.3 Character Avatars'))[0]
        self.assertIsNone(item['date'])

    def test_platform_date_is_not_patch_date(self):
        value = endfield()
        value['text'] = 'Arknights: Endfield Steam Version will officially launch on Oct. 15.'
        self.assertIsNone(parse_article(value, 'title')[0]['date'])
        self.assertIsNone(parse_article(article('Version 3.3 will launch on Steam on October 21, 2026.'))[0]['date'])

    def test_nonversion_subject_is_rejected(self):
        for noun in ('banner', 'event', 'Agent Phoenix', 'outfits', 'special program', 'trailer', 'store'):
            with self.subTest(noun=noun):
                self.assertIsNone(parse_article(article(f'Version 3.3 {noun} will arrive on October 21, 2026.'))[0]['date'])

    def test_speculation_is_rejected(self):
        for text in ('Version 3.3 is expected to arrive on October 21, 2026.',
                     'Version 3.3 may launch on October 21, 2026.',
                     'Version 3.3 never launches October 21, 2026.'):
            self.assertIsNone(parse_article(article(text))[0]['date'])

    def test_other_version_does_not_set_primary_date(self):
        item = parse_article(article('Version 3.4 will arrive on December 2, 2026.'))[0]
        self.assertIsNone(item['date'])

    def test_separate_sentence_cannot_borrow_identity(self):
        item = parse_article(article('Version 3.3 is being previewed. A special gift arrives October 21, 2026.'))[0]
        self.assertIsNone(item['date'])

    def test_conflicting_dates_fail_closed(self):
        item = parse_article(article('Version 3.3 launches October 21, 2026.\nVersion 3.3 arrives October 28, 2026.'))[0]
        self.assertIsNone(item['date'])
        self.assertTrue(item['warnings'])

    def test_invalid_calendar_date_is_not_imported(self):
        item = parse_article(article('Version 3.3 arrives February 30, 2027.'))[0]
        self.assertIsNone(item['date'])
        self.assertTrue(item['warnings'])

    def test_date_only_does_not_require_invented_timezone(self):
        self.assertEqual(parse_article(article('Version 3.3 goes live October 21, 2026.'))[0]['date'], '2026-10-21')

    def test_clock_without_offset_is_rejected(self):
        item = parse_article(article('Version 3.3 goes live October 21, 2026 at 00:30.'))[0]
        self.assertIsNone(item['date'])
        self.assertTrue(item['warnings'])

    def test_clock_is_converted_to_jakarta(self):
        item = parse_article(article('Version 3.3 goes live October 21, 2026 at 00:30:00 (UTC+8).'))[0]
        self.assertEqual(item['date'], '2026-10-20')

    def test_meaningful_alt_text_can_supply_readable_evidence(self):
        value = article('See image below.')
        value['media'] = [{'kind': 'img', 'url': 'https://example.test/banner.png',
                           'alt': 'Version 3.3 will launch on October 21, 2026.'}]
        self.assertEqual(parse_article(value)[0]['date'], '2026-10-21')

    def test_image_filename_is_never_date_evidence(self):
        value = article('See image below.')
        value['media'] = [{'kind': 'img', 'url': 'https://example.test/20261021.png', 'alt': ''}]
        item = parse_article(value)[0]
        self.assertIsNone(item['date'])
        self.assertIn('media-present-needs-readable-release-date', item['dateParsing']['reasonCodes'])

    def test_media_urls_strip_tokens(self):
        values = article_media('<img src="https://cdn.example.test/img.png?token=SECRET#x" alt="Version poster"><img src="data:image/png;base64,NO">', 'https://example.test/news/1')
        self.assertEqual(values, [{'kind': 'img', 'url': 'https://cdn.example.test/img.png', 'alt': 'Version poster'}])

    def test_date_excerpts_are_bounded(self):
        item = parse_article(article(('Code expires October 11, 2026. ' * 5000)))[0]
        self.assertLessEqual(len(item['dateParsing']['textExcerpt']), 1600)
        self.assertLessEqual(len(item['dateParsing']['dateMentionsForReviewOnly']), 6)

    def test_press_dateline_is_not_arbitrary_body_date(self):
        self.assertEqual(press_dateline('October 9, 2026, Singapore — News.'), '2026-10-09')
        self.assertIsNone(press_dateline('Maintenance begins October 21, 2026.'))
        self.assertIsNone(press_dateline('Code expires October 11, 2026.'))


class PreReleaseMergeTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path(__file__).with_name('fixture-data.json').read_text(encoding='utf-8'))
        self.settings = self.data['config']['automation']['sources']
        self.settings['pressFallback'] = copy.deepcopy(hunt.DEFAULT_PRESS_FALLBACK)

    def test_future_dates_confirm_now_and_do_not_recreate_calendar(self):
        before = desired_events(self.data, '2026-10-12')
        api = FakeAPI()
        targets = {game['id']: 'games' for game in self.data['config']['games']}
        ns = settings_for(self.data)['namespace']
        reconcile(api, before, targets, parse_day('2026-10-12'), ns)
        original_ids = {key: value['event']['id'] for key, value in before.items()}
        with patch('hunt.today', return_value='2026-10-12'):
            for entry in (parse_article(endfield(), 'title')[0], parse_article(article('Version 3.3 will arrive on October 21, 2026.'))[0]):
                hunt.merge_observation(self.data, entry, self.settings)
        after = desired_events(self.data, '2026-10-12')
        counts = reconcile(api, after, targets, parse_day('2026-10-12'), ns)
        self.assertEqual(counts['created'], 0)
        # Two confirmations plus new anchor descriptions for four later forecasts.
        self.assertEqual(counts['updated'], 6)
        self.assertEqual({key: value['event']['id'] for key, value in after.items()}, original_ids)
        for key in ('endfield-6', 'zzz-20'):
            self.assertNotIn('Projected', after[key]['event']['summary'])
            self.assertEqual(before[key]['event']['start'], after[key]['event']['start'])
        self.assertNotEqual(plan_fingerprint(self.data, before, targets), plan_fingerprint(self.data, after, targets))

    def test_supplemental_official_host_is_game_filtered(self):
        value = article('Version 3.3 arrives October 21, 2026.', 'Zenless Zone Zero Version 3.3 Launch Preview')
        value['url'] = 'https://www.hoyoverse.com/en-us/news/999007'
        observation = parse_article(value)[0]
        self.assertTrue(hunt.trusted_evidence(observation, self.settings))
        observation['sourceTitle'] = 'Honkai Star Rail Version 3.3'
        self.assertFalse(hunt.trusted_evidence(observation, self.settings))
        observation['sourceTitle'] = value['title']
        observation['url'] = 'https://example.test/en-us/news/999007'
        self.assertFalse(hunt.trusted_evidence(observation, self.settings))

    def test_fallback_is_not_needed_for_date_already_read(self):
        parsed = parse_article(article('Version 3.3 arrives October 21, 2026.'))
        self.assertFalse(hunt.press_needed(self.data, 'zzz', parsed))
        self.assertTrue(hunt.press_needed(self.data, 'zzz', parse_article(article('See the image.'))))

    def test_fallback_uses_same_fixed_listing_and_readable_press_evidence(self):
        primary = CollectedArticles(); primary.append(article('See the image.'))
        corporate = article('Version 3.3 will arrive October 21, 2026.', 'Zenless Zone Zero Version 3.3 Preview')
        corporate['url'] = 'https://www.hoyoverse.com/en-us/news/999007'
        extra = CollectedArticles(); extra.append(corporate)
        with patch.object(hunt, 'collect_feed', return_value=extra) as collect:
            parsed, articles = hunt.add_press_evidence(self.data, 'zzz', parse_article(primary[0]), primary,
                self.settings, None, lambda text: None, False, lambda: None)
        self.assertEqual(collect.call_args.args[1]['url'], 'https://www.hoyoverse.com/en-us/news')
        self.assertEqual(articles.discovery['pressFallback']['status'], 'collected')
        self.assertEqual([i['date'] for i in parsed if i.get('date')], ['2026-10-21'])

    def test_optional_fallback_failure_is_visible_not_date_confirmation(self):
        primary = CollectedArticles(); primary.append(article('See the image.'))
        with patch.object(hunt, 'collect_feed', side_effect=RuntimeError('fixture network unavailable')):
            parsed, articles = hunt.add_press_evidence(self.data, 'zzz', parse_article(primary[0]), primary,
                self.settings, None, lambda text: None, False, lambda: None)
        self.assertEqual(articles.discovery['pressFallback']['status'], 'unavailable')
        self.assertFalse(any(i['date'] for i in parsed))

    def test_confirmation_review_explains_pending_broadcast(self):
        with patch('hunt.today', return_value='2026-10-12'):
            review = hunt.confirmation_review(self.data, parse_article(article('Code expires October 11, 2026.')))
        value = next(v for v in review if v['releaseId'] == 'zzz-20')
        self.assertEqual(value['status'], 'projected')
        self.assertEqual(value['daysUntilDate'], 9)
        self.assertTrue(value['articles'][0]['dateParsing'])

    def test_public_source_saves_method_not_article_excerpt(self):
        with patch('hunt.today', return_value='2026-10-12'):
            value = parse_article(endfield(), 'title')[0]
            hunt.merge_observation(self.data, value, self.settings)
        source = next(v for v in self.data['sources']['sources'].values() if v['url'] == value['url'])
        self.assertIn('releaseDateEvidence', source)
        self.assertNotIn('text', source['releaseDateEvidence'][0])


class HeaderEvidenceBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.driver = sync_playwright().start()
        opts = {'headless': True}
        if os.environ.get('PATCH_CALENDAR_TEST_BROWSER'):
            opts['executable_path'] = os.environ['PATCH_CALENDAR_TEST_BROWSER']
        cls.browser = cls.driver.chromium.launch(**opts)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.driver.stop()

    def test_plain_adjacent_publication_date_is_scoped_to_exact_headline(self):
        page = self.browser.new_page()
        try:
            page.set_content('<article><div>News 2026/10/09</div><h2>Version 3.3 Intel Report</h2><p>Version 3.3 arrives October 21, 2026.</p></article>')
            data = page.evaluate(PUBLICATION_SNAPSHOT, 'Version 3.3 Intel Report')
            self.assertEqual(data['fields'][-1]['value'], '2026/10/09')
            other = page.evaluate(PUBLICATION_SNAPSHOT, 'Version 9.9')
            self.assertFalse(other['fields'])
        finally:
            page.close()

    def test_publication_reader_does_not_use_release_paragraph(self):
        page = self.browser.new_page()
        try:
            page.set_content('<article><h2>Version 3.3 Intel Report</h2><p>2026/10/21</p></article>')
            result = page.evaluate(PUBLICATION_SNAPSHOT, 'Version 3.3 Intel Report')
            self.assertFalse(result['fields'])
        finally:
            page.close()


if __name__ == '__main__':
    unittest.main()
