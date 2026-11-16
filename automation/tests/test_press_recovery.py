"""Publisher fallback and recovery tests; all network collection is simulated."""
import copy
import json
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hunt
from sources import CollectedArticles, parse_article
from feed_pool import CollectionCancelled

class PressRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path(__file__).with_name('fixture-data.json').read_text())
        self.settings = self.data['config']['automation']['sources']
        hunt.configure_press_fallback(self.settings)

    def pending(self, game='hsr'):
        label = '4.7' if game == 'hsr' else '3.3'
        return parse_article({'gameId': game, 'url': self.settings['feeds'][game]['url'] + '/999001',
            'title': 'Version ' + label + ' Special Program', 'text': 'Details to be announced.',
            'publishedOn': '2026-10-30'})

    def test_anchored_name_only_reference_is_not_refetched(self):
        source = {'url': 'https://hsr.hoyoverse.com/en-us/news/166189',
                  'title': 'Astral Imagea Draw Rates Notice', 'kind': 'official'}
        self.data['sources']['sources']['retired-name-reference'] = source
        self.data['versions']['games']['hsr']['30']['sources'].append('retired-name-reference')
        before = copy.deepcopy(self.data)
        seeds = hunt.seed_articles(self.data, 'hsr', self.settings['feeds']['hsr'])
        self.assertNotIn(source['url'], [row['url'] for row in seeds])
        self.assertEqual(self.data, before)  # Evidence/history is not deleted.

    def test_date_anchor_sources_remain_required(self):
        source = {'url': 'https://hsr.hoyoverse.com/en-us/news/999008',
                  'title': 'Version 4.6 Update Details', 'kind': 'official'}
        self.data['sources']['sources']['current-date-reference'] = source
        self.data['overrides']['games']['hsr'][-1]['sources'].append('current-date-reference')
        seeds = hunt.seed_articles(self.data, 'hsr', self.settings['feeds']['hsr'])
        self.assertIn(source['url'], [row['url'] for row in seeds])

    def test_future_undated_name_reference_is_still_revisited(self):
        source = {'url': 'https://hsr.hoyoverse.com/en-us/news/999009',
                  'title': 'Version 4.7 Special Program', 'kind': 'official'}
        self.data['sources']['sources']['future-reference'] = source
        self.data['versions']['games']['hsr']['31'] = {'label': '4.7', 'sources': ['future-reference']}
        seeds = hunt.seed_articles(self.data, 'hsr', self.settings['feeds']['hsr'])
        self.assertIn(source['url'], [row['url'] for row in seeds])

    def test_both_publishers_configured(self):
        self.assertEqual(set(self.settings['pressFallback']), {'hsr', 'zzz'})
        self.assertEqual(self.settings['pressFallback']['hsr']['requiredTitle'], 'Honkai: Star Rail')

    def test_migration_preserves_existing_zzz_customization(self):
        settings = {'pressFallback': {'zzz': {'enabled': False, 'maxListPages': 3}}}
        hunt.configure_press_fallback(settings)
        self.assertEqual(settings['pressFallback']['zzz'], {'enabled': False, 'maxListPages': 3})
        self.assertIn('hsr', settings['pressFallback'])

    def test_migration_idempotent(self):
        before = copy.deepcopy(self.settings)
        hunt.configure_press_fallback(self.settings)
        self.assertEqual(before, self.settings)

    def test_hsr_unresolved_program_requests_press(self):
        self.assertTrue(hunt.press_needed(self.data, 'hsr', self.pending()))

    def test_non_broadcast_explicit_name_also_requests_press(self):
        pending = self.pending()
        pending[0]['sourceTitle'] = 'Version 4.7 Feature Preview'
        self.assertTrue(hunt.press_needed(self.data, 'hsr', pending))

    def test_date_already_recovered_does_not_request_press(self):
        pending = self.pending(); pending[0]['date'] = '2026-11-11'
        self.assertFalse(hunt.press_needed(self.data, 'hsr', pending))

    def test_existing_anchor_does_not_request_press(self):
        pending = self.pending(); pending[0]['label'] = '4.6'
        self.assertFalse(hunt.press_needed(self.data, 'hsr', pending))

    def test_city_dateline_supplies_real_year(self):
        article = {'publishedOn': None, 'text': 'SINGAPORE, October 30, 2026 — Version 4.7 will launch on November 11.'}
        self.assertEqual(hunt.press_publication(article)['publishedOn'], '2026-10-30')
        self.assertIsNone(article['publishedOn'])

    def test_city_dateline_does_not_replace_existing_metadata(self):
        article = {'publishedOn': '2026-10-31', 'text': 'SINGAPORE, October 30, 2026 — Press statement.'}
        self.assertEqual(hunt.press_publication(article)['publishedOn'], '2026-10-31')

    def test_arbitrary_body_dates_are_not_publication(self):
        for text in ['Launches November 11, 2026.', 'The event in Singapore, October 30, 2026 — begins.',
                     'SINGAPORE, October 30 — News.', 'SINGAPORE, February 30, 2026 — News.',
                     'one\ntwo\nthree\nfour\nSINGAPORE, October 30, 2026 — News.']:
            with self.subTest(text=text):
                self.assertIsNone(hunt.press_publication({'publishedOn': None, 'text': text})['publishedOn'])

    def test_hsr_corporate_evidence_is_game_filtered(self):
        item = self.pending()[0]; item.update(url='https://www.hoyoverse.com/en-us/news/999001',
                                             sourceTitle='Honkai: Star Rail Version 4.7 Announcement')
        self.assertTrue(hunt.trusted_evidence(item, self.settings))
        item['sourceTitle'] = 'Zenless Zone Zero Version 4.7 Announcement'
        self.assertFalse(hunt.trusted_evidence(item, self.settings))

    def test_hsr_dateline_evidence_works_before_release(self):
        article = {'gameId': 'hsr', 'url': 'https://www.hoyoverse.com/en-us/news/999001',
                   'title': 'Honkai: Star Rail Version 4.7 Update Announcement',
                   'publishedOn': None,
                   'text': 'SINGAPORE, October 30, 2026 — Version 4.7 will officially launch on November 11.'}
        extra = CollectedArticles(); extra.append(article)
        with patch.object(hunt, 'collect_feed', return_value=extra), patch.object(hunt, 'today', return_value='2026-10-30'):
            parsed, articles = hunt.add_press_evidence(self.data, 'hsr', self.pending(), CollectedArticles(),
                self.settings, None, lambda message: None, False, lambda: None)
        dated = [row for row in parsed if row.get('date')]
        self.assertEqual([row['date'] for row in dated], ['2026-11-11'])
        self.assertEqual(dated[0]['publishedOn'], '2026-10-30')

    def test_disabled_fallback_is_respected(self):
        self.settings['pressFallback']['hsr']['enabled'] = False
        with patch.object(hunt, 'collect_feed') as collect:
            hunt.add_press_evidence(self.data, 'hsr', self.pending(), CollectedArticles(),
                self.settings, None, lambda m: None, False, lambda: None)
        collect.assert_not_called()

    def test_missing_primary_checkpoint_recovers_near_release(self):
        primary = CollectedArticles(); primary.discovery['coverage'] = 'bounded-unverified'
        pending = self.pending(); pending[0]['label'] = '4.6'
        with patch.object(hunt, 'today', return_value='2026-11-10'), patch.object(hunt, 'collect_feed', return_value=CollectedArticles()) as collect:
            hunt.add_press_evidence(self.data, 'hsr', pending, primary,
                self.settings, None, lambda m: None, False, lambda: None)
        collect.assert_called_once()

    def test_missing_checkpoint_far_from_release_does_not_poll_press(self):
        primary = CollectedArticles(); primary.discovery['coverage'] = 'bounded-unverified'
        pending = self.pending(); pending[0]['label'] = '4.6'
        with patch.object(hunt, 'today', return_value='2026-10-01'), patch.object(hunt, 'collect_feed') as collect:
            hunt.add_press_evidence(self.data, 'hsr', pending, primary,
                self.settings, None, lambda m: None, False, lambda: None)
        collect.assert_not_called()

    def test_shared_corporate_host_is_serialized(self):
        active = 0; peak = 0; guard = threading.Lock()
        def collect(*args, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1; peak = max(peak, active)
            time.sleep(.05)
            with guard:
                active -= 1
            return CollectedArticles()
        def run(game):
            return hunt.add_press_evidence(self.data, game, self.pending(game), CollectedArticles(),
                self.settings, None, lambda m: None, False, lambda: None)
        with patch.object(hunt, 'collect_feed', side_effect=collect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(run, ['hsr', 'zzz']))
        self.assertEqual(peak, 1)

    def test_cancellation_releases_host_lock(self):
        def cancel():
            raise CollectionCancelled('test')
        with self.assertRaises(CollectionCancelled):
            hunt.add_press_evidence(self.data, 'hsr', self.pending(), CollectedArticles(),
                self.settings, None, lambda m: None, False, cancel)
        self.assertTrue(hunt._PRESS_LOCK.acquire(blocking=False))
        hunt._PRESS_LOCK.release()

    def test_network_failure_releases_host_lock_and_remains_visible(self):
        with patch.object(hunt, 'collect_feed', side_effect=RuntimeError('test outage')):
            _, articles = hunt.add_press_evidence(self.data, 'hsr', self.pending(), CollectedArticles(),
                self.settings, None, lambda m: None, False, lambda: None)
        self.assertEqual(articles.discovery['pressFallback']['status'], 'unavailable')
        self.assertTrue(hunt._PRESS_LOCK.acquire(blocking=False)); hunt._PRESS_LOCK.release()

if __name__ == '__main__':
    unittest.main()
