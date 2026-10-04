"""Offline regressions for article identity, headline precedence, and failure evidence."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sources import FeedReadError, dom_article, headline_matches, merge_news_record, public_url, title_key, walk_news

FEED = {'url': 'https://hsr.hoyoverse.com/en-us/news', 'hosts': ['hsr.hoyoverse.com'],
        'articlePattern': r'/en-us/news/\d+$', 'titleStyle': 'number'}
URL = FEED['url'] + '/123'
HEADLINE = 'Version 4.7 Update and Maintenance Notice'
BODY = 'Maintenance begins on 2026/11/11 06:00 (UTC+8). More information follows in the official update notice.'


class ContentUnitTests(unittest.TestCase):
    """Test parser guardrails without opening the internet or a browser."""

    def test_structured_headline_survives_card_metadata(self):
        """A category/date card must never replace the clean API headline."""
        records = {}
        merge_news_record(records, {'url': URL, 'title': HEADLINE, '_titleRank': 3, 'text': BODY, '_detail': True})
        merge_news_record(records, {'url': URL, 'title': 'NEWS\n' + HEADLINE + '\n2026-10-04', '_titleRank': 1})
        self.assertEqual(records[URL]['title'], HEADLINE)
        self.assertEqual(records[URL]['text'], BODY)

    def test_structured_headline_wins_after_dom_discovery(self):
        """Source precedence must not depend on event arrival order."""
        records = {}
        merge_news_record(records, {'url': URL, 'title': 'NEWS\n' + HEADLINE, '_titleRank': 1})
        merge_news_record(records, {'url': URL, 'title': HEADLINE, '_titleRank': 3})
        self.assertEqual(records[URL]['title'], HEADLINE)

    def test_complete_body_survives_later_listing_excerpt(self):
        """A truncated list payload cannot overwrite a downloaded detail."""
        records = {}
        merge_news_record(records, {'url': URL, 'title': HEADLINE, 'text': BODY, '_detail': True, '_titleRank': 3})
        merge_news_record(records, {'url': URL, 'title': HEADLINE, 'text': 'A short excerpt...', '_detail': False, '_titleRank': 3})
        self.assertEqual(records[URL]['text'], BODY)

    def test_typographic_title_differences_match(self):
        """Typographic quotes, nonbreaking spaces, and zero-width marks are harmless."""
        record = {'title': 'Version 4.7 “An Update” — Details', '_titleRank': 3}
        self.assertTrue(headline_matches(record, 'Version\u00a04.7 "An\u200b Update" - Details'))

    def test_other_version_does_not_match(self):
        """Normalization must not erase the version number."""
        self.assertFalse(headline_matches({'title': HEADLINE, '_titleRank': 3}, HEADLINE.replace('4.7', '4.8')))

    def test_card_metadata_can_be_replaced_by_actual_heading(self):
        """A DOM-only fallback can recognize the actual heading inside a card label."""
        self.assertTrue(headline_matches({'title': 'NEWS\n' + HEADLINE + '\n2026-10-04', '_titleRank': 1}, HEADLINE))
        self.assertFalse(headline_matches({'title': HEADLINE, '_titleRank': 3}, 'Version 4.7'))

    def test_listing_content_not_a_complete_detail(self):
        """A list record can discover articles, but is never sufficient full-body evidence."""
        record = {'id': 123, 'title': HEADLINE, 'content': '<p>' + BODY + '</p>'}
        listing = walk_news({'data': {'list': [record]}}, FEED)[0]
        detail = walk_news({'data': record}, FEED)[0]
        self.assertFalse(listing['_detail'])
        self.assertTrue(detail['_detail'])

    def test_short_detail_is_not_discarded_by_character_threshold(self):
        """A complete short announcement is still article data."""
        result = walk_news({'data': {'id': 123, 'title': HEADLINE, 'content': '<p>Version 4.7 is coming.</p>'}}, FEED)
        self.assertEqual(result[0]['text'], 'Version 4.7 is coming.')

    def test_related_list_does_not_become_detail(self):
        """Related-story bodies keep their listing status at nested depths."""
        record = {'id': 123, 'title': HEADLINE, 'content': BODY}
        self.assertFalse(walk_news({'data': {'related': {'items': [record]}}}, FEED)[0]['_detail'])

    def test_jsonld_explicit_url_and_body(self):
        """NewsArticle data can identify a detail without guessing a numeric ID."""
        record = {'@type': 'NewsArticle', 'url': URL, 'headline': HEADLINE, 'articleBody': BODY, 'datePublished': '2026-10-04'}
        result = walk_news(record, FEED)[0]
        self.assertEqual(result['text'], BODY)
        self.assertEqual(result['publishedOn'], '2026-10-04')

    def test_generic_name_and_id_do_not_invent_article(self):
        """A site configuration item is not a news record just because it has an ID."""
        self.assertEqual(walk_news({'name': 'Account menu', 'id': 123}, FEED), [])

    def test_explicit_external_link_does_not_become_internal_article(self):
        """Never reinterpret an external navigation ID as a /news/ID route."""
        self.assertEqual(walk_news({'title': 'Community', 'id': 123, 'url': 'https://example.test/community'}, FEED), [])

    def test_empty_link_still_allows_real_article_id(self):
        """Publisher records with optional blank link fields remain discoverable."""
        result = walk_news({'title': HEADLINE, 'content_id': 123, 'link': ''}, FEED)
        self.assertEqual(result[0]['url'], URL)

    def test_generic_event_news_remains_discoverable(self):
        """Do not filter all non-release headlines; body text may announce a version."""
        result = walk_news({'title': 'An ordinary event announcement', 'id': 123}, FEED)
        self.assertEqual(len(result), 1)


    def snapshot(self, text=None, related=False):
        """Create a minimal isolated article probe."""
        return {'candidates': [{'headings': [HEADLINE], 'text': text or HEADLINE + '\n' + BODY,
                                'selector': 'article', 'related': related, 'hasMedia': False, 'loading': False}]}

    def test_isolated_article_body(self):
        """Matched article content is accepted rather than the full page."""
        result = dom_article({'title': HEADLINE, '_titleRank': 3}, self.snapshot())
        self.assertEqual(result['title'], HEADLINE)
        self.assertIn(BODY, result['text'])

    def test_wrong_heading_is_rejected(self):
        """An unrelated article cannot contribute version/date evidence."""
        probe = self.snapshot()
        probe['candidates'][0]['headings'] = [HEADLINE.replace('4.7', '4.8')]
        self.assertIsNone(dom_article({'title': HEADLINE, '_titleRank': 3}, probe))

    def test_loading_scope_is_not_ready(self):
        """A partially loaded article remains unverified."""
        probe = self.snapshot()
        probe['candidates'][0]['loading'] = True
        self.assertIsNone(dom_article({'title': HEADLINE, '_titleRank': 3}, probe))

    def test_related_dates_are_excluded(self):
        """Do not read a second update's date from related-story cards."""
        text = HEADLINE + '\n' + BODY + '\nRelated News\nVersion 4.8 arrives on December 22, 2026.'
        result = dom_article({'title': HEADLINE, '_titleRank': 3}, self.snapshot(text, True))
        self.assertNotIn('4.8', result['text'])

    def test_unseparated_related_container_is_rejected(self):
        """A wrapper with no safe related-story boundary is not article evidence."""
        self.assertIsNone(dom_article({'title': HEADLINE, '_titleRank': 3}, self.snapshot(related=True)))

    def test_challenge_scope_is_rejected(self):
        """Access checks must never become calendar data."""
        text = HEADLINE + '\nVerify you are human. Please complete the captcha challenge.'
        self.assertIsNone(dom_article({'title': HEADLINE, '_titleRank': 3}, self.snapshot(text)))

    def test_network_diagnostics_exclude_url_secrets(self):
        """Queries, fragments, and URL credentials must not enter the report."""
        self.assertEqual(public_url('https://user:password@example.test/path?token=secret#fragment'), 'https://example.test/path')

    def test_structured_failure_details_are_retained(self):
        """The error carries observations separately from its short printed message."""
        error = FeedReadError('Not ready', {'headings': [HEADLINE]})
        self.assertEqual(str(error), 'Not ready')
        self.assertEqual(error.details['headings'], [HEADLINE])


if __name__ == '__main__':
    unittest.main()
