"""Real Chromium DOM fixtures with simulated navigation/responses, without network access."""

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sources import ARTICLE_SNAPSHOT, FeedReadError, collect_feed

FEED = {'url': 'https://hsr.hoyoverse.com/en-us/news', 'hosts': ['hsr.hoyoverse.com'],
        'articlePattern': r'/en-us/news/\d+$', 'titleStyle': 'number'}
URL = FEED['url'] + '/123'
HEADLINE = 'Version 4.7 Update and Maintenance Notice'
BODY = 'Maintenance begins on 2026/11/11 06:00 (UTC+8). This is a full publisher announcement for this isolated browser test.'
SETTINGS = {'timeoutSeconds': 1.5, 'settleSeconds': 0.01, 'maxListPages': 1,
            'recentDays': 100, 'maxArticlesPerGame': 10}


class CollectorBrowserTests(unittest.TestCase):
    """Use the real DOM/readiness loop, but simulate page navigation and JSON transport."""

    @classmethod
    def setUpClass(cls):
        """Start the installed Chromium for offline fixtures; never navigate to a website."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('Playwright is not installed.')
        cls.driver = sync_playwright().start()
        launch = {'headless': True}
        executable = os.environ.get('PATCH_CALENDAR_TEST_BROWSER')
        if executable:
            launch['executable_path'] = executable
        try:
            cls.browser = cls.driver.chromium.launch(**launch)
        except Exception:
            cls.driver.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        """Close the isolated shared browser."""
        cls.browser.close()
        cls.driver.stop()

    def collect(self, article, listing=None, listing_data=None, article_data=None, fetch_status=None, settings=None, extra_pages=None):
        """Serve fixtures through set_content and explicitly simulated response callbacks."""
        browser = self.browser
        contexts, log = [], []
        listing = listing or '<a data-href="' + URL + '">NEWS<br>' + HEADLINE + '<br>2026-10-04</a>'
        content = {FEED['url']: listing, URL: article, **(extra_pages or {})}

        class PageProxy:
            """Run DOM operations in Chromium without requesting external URLs."""

            def __init__(self, page):
                """Store the real page and simulated navigation state."""
                self.page, self.url, self.handlers = page, 'about:blank', {}

            def on(self, name, callback):
                """Register collector callbacks without creating external requests."""
                self.handlers[name] = callback

            def goto(self, url, **kwargs):
                """Set fixture HTML and dispatch a completed, simulated JSON response."""
                self.url = url
                self.page.set_content(content[url])
                payload = listing_data if url == FEED['url'] else article_data
                if payload is not None or (fetch_status and url == URL):
                    response = SimpleNamespace(url=FEED['url'] + '/fixture-api', status=fetch_status or 200,
                        headers={'content-type': 'application/json'}, json=lambda: payload)
                    request = SimpleNamespace(url=response.url, resource_type='fetch', response=lambda: response)
                    response.request = request
                    self.handlers['response'](response)
                    self.handlers['requestfinished'](request)
                return SimpleNamespace(status=200)

            def evaluate(self, script, *args, **kwargs):
                """Evaluate the production DOM probe while keeping the simulated location."""
                result = self.page.evaluate(script, *args, **kwargs)
                if script == ARTICLE_SNAPSHOT:
                    result['url'] = self.url
                return result

            def __getattr__(self, name):
                """Forward locators and browser-driven polling to the real page."""
                return getattr(self.page, name)

        class ContextProxy:
            """Create real DOM pages with simulated navigation only."""

            def __init__(self, context):
                """Retain the real isolated browser context."""
                self.context = context

            def new_page(self):
                """Give the collector a DOM-capable fixture page."""
                return PageProxy(self.context.new_page())

            def set_default_timeout(self, timeout):
                """Forward the collector's timeout to DOM operations."""
                self.context.set_default_timeout(timeout)

        class BrowserProxy:
            """Provide production code with offline contexts from the fixture browser."""

            def new_context(self, **kwargs):
                """Create an isolated browser context without external requests."""
                context = browser.new_context(**kwargs)
                contexts.append(context)
                return ContextProxy(context)

            def close(self):
                """Close only contexts owned by this test run."""
                for context in contexts:
                    context.close()

        class DriverProxy:
            """Keep the real Playwright event loop while simulating browser creation."""

            def __enter__(self):
                """Expose a Chromium launcher backed by the fixture browser."""
                return SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: BrowserProxy()))

            def __exit__(self, *args):
                """Leave the shared browser alive for the remaining tests."""
                return False

        with patch('playwright.sync_api.sync_playwright', return_value=DriverProxy()), patch('sources.today', return_value='2026-10-04'):
            result = collect_feed('hsr', FEED, settings or SETTINGS, progress=log.append)
        return result, log

    def test_original_full_card_predicate_reproduces_false_negative(self):
        """Prove the old exact substring rule rejects a valid, fully rendered fixture."""
        page = self.browser.new_page()
        try:
            page.set_content('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>')
            card = 'NEWS\n' + HEADLINE + '\n2026-10-04'
            old_ready = page.evaluate(r"title => document.body.innerText.replace(/\s+/g, ' ').includes(title.replace(/\s+/g, ' ')) && document.body.innerText.length > 160", card)
            self.assertFalse(old_ready)
            self.assertIn(HEADLINE, page.inner_text('body'))
        finally:
            page.close()

    def test_delayed_article_body_is_waited_for(self):
        """The presence of a title alone must not end the content-readiness wait."""
        article = '<article><h1>' + HEADLINE + '</h1><p id="content"></p></article><script>setTimeout(() => document.querySelector("#content").textContent = ' + json.dumps(BODY) + ', 100)</script>'
        result, _ = self.collect(article)
        self.assertIn(BODY, result[0]['text'])

    def test_card_metadata_mismatch_still_reads_actual_heading(self):
        """The old full-card includes() condition fails for this working article."""
        result, log = self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>')
        self.assertEqual(result[0]['title'], HEADLINE)
        self.assertEqual(len(result), 1)
        self.assertTrue(any('article DOM' in line for line in log))

    def test_json_body_does_not_wait_for_unrendered_heading(self):
        """A completed, ID-matched article response works even when the UI is still a shell."""
        result, log = self.collect('<div>Loading...</div>',
            article_data={'data': {'id': 123, 'title': HEADLINE, 'content': '<p>' + BODY + '</p>'}})
        self.assertEqual(result[0]['text'], BODY)
        self.assertTrue(any('publisher article JSON' in line for line in log))

    def test_list_excerpt_is_not_imported_as_detail(self):
        """The same response wrapped as a list is not sufficient article evidence."""
        with self.assertRaises(FeedReadError) as error:
            self.collect('<div>Loading...</div>',
                article_data={'data': {'list': [{'id': 123, 'title': HEADLINE, 'content': '<p>' + BODY + '</p>'}]}})
        self.assertFalse(error.exception.details['structuredRecord']['completeDetailResponseSeen'])
        self.assertIn('Loading', error.exception.details['bodyExcerpt'])

    def test_unrelated_json_cannot_supply_requested_article_body(self):
        """The exact URL/ID, not merely a version-looking body, gates JSON acceptance."""
        with self.assertRaises(FeedReadError) as error:
            self.collect('<div>Loading...</div>', article_data={'data': {'id': 999, 'title': HEADLINE, 'content': BODY}})
        self.assertEqual(error.exception.details['requestedURL'], URL)
        self.assertFalse(error.exception.details['structuredRecord']['completeDetailResponseSeen'])

    def test_failed_fetch_is_reported(self):
        """Simulated HTTP data errors appear beside the expected headline and page sample."""
        with self.assertRaises(FeedReadError) as error:
            self.collect('<h1>Loading...</h1>', fetch_status=404)
        self.assertTrue(any(item.get('status') == 404 for item in error.exception.details['networkErrors']))
        self.assertIn(HEADLINE, error.exception.details['expectedTitle'])

    def test_unicode_heading_normalization(self):
        """A structured headline matches display typography without accepting another version."""
        title = 'Version 4.7 “Title” — Update Details'
        article = '<article><h1>Version 4.7 "Title" - Update Details</h1><p>' + BODY + '</p></article>'
        result, _ = self.collect(article, listing_data={'data': {'list': [{'id': 123, 'title': title}]}})
        self.assertIn('Version 4.7', result[0]['title'])

    def test_related_news_date_is_not_read(self):
        """Return only the requested update, not a related update."""
        article = '<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p><section class="related"><h2>Related News</h2><p>Version 4.8 arrives on December 22, 2026.</p></section></article>'
        result, _ = self.collect(article)
        self.assertNotIn('4.8', result[0]['text'])

    def test_jsonld_article_body_is_read(self):
        """The article's own JSON-LD explicitly associates its title and body."""
        data = {'@type': 'NewsArticle', 'url': URL, 'headline': HEADLINE, 'articleBody': BODY}
        result, log = self.collect('<script type="application/ld+json">' + json.dumps(data) + '</script><div>Loading...</div>')
        self.assertEqual(result[0]['text'], BODY)
        self.assertTrue(any('JSON-LD' in line for line in log))

    def test_access_challenge_is_rejected(self):
        """Do not bypass or read through access-challenge pages."""
        with self.assertRaises(FeedReadError) as error:
            self.collect('<h1>Verify you are human</h1><p>Please finish the captcha challenge.</p>')
        self.assertIn('challenge', str(error.exception))


    def test_header_more_is_not_pagination(self):
        """The site's generic More navigation must never be clicked as a news page."""
        listing = '<header><button onclick="throw Error(\'Wrong navigation clicked\')">More</button></header><main><a data-href="' + URL + '">' + HEADLINE + '</a></main>'
        result, log = self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>',
            listing=listing, settings=dict(SETTINGS, maxListPages=4))
        self.assertEqual(result.discovery['pagesRead'], 1)
        self.assertEqual(result.discovery['endReason'], 'no-more-control-or-scroll-results')
        self.assertFalse(any('Opening news-list control' in line for line in log))

    def test_disabled_next_does_not_repeat_page(self):
        """Disabled pagination is an end boundary, not four repeated listing passes."""
        listing = '<main><a data-href="' + URL + '">' + HEADLINE + '</a><div class="pagination"><button class="next" disabled>Next</button></div></main>'
        result, _ = self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>',
            listing=listing, settings=dict(SETTINGS, maxListPages=4))
        self.assertEqual(result.discovery['pagesRead'], 1)
        self.assertEqual(result.discovery['endReason'], 'next-control-disabled')

    def test_stuck_load_more_fails_instead_of_counting_progress(self):
        """An actionable news control that returns no new IDs is a collection defect."""
        listing = '<main><a data-href="' + URL + '">' + HEADLINE + '</a><button>Load more</button></main>'
        with self.assertRaises(FeedReadError) as error:
            self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>',
                listing=listing, settings=dict(SETTINGS, maxListPages=4, timeoutSeconds=0.3))
        self.assertFalse(error.exception.details['pagination']['advanced'])
        self.assertEqual(error.exception.details['pagination']['distinctRecords'], 1)

    def test_load_more_requires_new_article_identity(self):
        """Actually discover and read the next article, not another count of the first one."""
        import html
        second_url = FEED['url'] + '/124'
        second_title = 'Version 4.8 Special Program Announcement'
        click = "this.insertAdjacentHTML('beforebegin', " + json.dumps('<a data-href="' + second_url + '">' + second_title + '</a>') + "); this.disabled = true;"
        listing = '<main><a data-href="' + URL + '">' + HEADLINE + '</a><button onclick="' + html.escape(click, quote=True) + '">Load more</button></main>'
        result, _ = self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>',
            listing=listing, settings=dict(SETTINGS, maxListPages=4),
            extra_pages={second_url: '<article><h1>' + second_title + '</h1><p>More information will follow from the publisher.</p></article>'})
        self.assertEqual({item['url'] for item in result}, {URL, second_url})
        self.assertEqual(result.discovery['pagesRead'], 2)
        self.assertEqual([item['distinctRecords'] for item in result.discovery['passes']], [1, 2])

    def test_footer_and_navigation_ids_are_not_news(self):
        """Reproduce the menu contamination seen in the user's successful live log."""
        payload = {'data': {'list': [{'id': 123, 'title': HEADLINE},
            {'id': 111140, 'title': 'Redeem Code'}, {'id': 101842, 'title': 'hoyolab'},
            {'id': 101822, 'title': '社媒信息+bgm'}, {'id': 998, 'title': 'External destination', 'url': 'https://example.test/account'}]}}
        result, _ = self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>', listing_data=payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.discovery['recordsDiscovered'], 1)

    def test_numeric_pager_selects_next_number(self):
        """An unlabeled next page works without clicking a generic website More link."""
        import html
        second_url = FEED['url'] + '/124'
        second_title = 'A publisher news article'
        click = "document.querySelector('main').insertAdjacentHTML('afterbegin', " + json.dumps('<a data-href="' + second_url + '">' + second_title + '</a>') + ");"
        listing = '<main><a data-href="' + URL + '">' + HEADLINE + '</a><div class="pagination"><button aria-current="page">1</button><button onclick="' + html.escape(click, quote=True) + '">2</button></div></main>'
        result, _ = self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>',
            listing=listing, settings=dict(SETTINGS, maxListPages=2),
            extra_pages={second_url: '<article><h1>' + second_title + '</h1><p>This is a complete publisher news article body.</p></article>'})
        self.assertEqual(len(result), 2)
        self.assertEqual(result.discovery['endReason'], 'configured-page-limit')


    def test_structured_body_still_reads_publication_meta(self):
        """A complete JSON body must not skip publication metadata in the page head."""
        article = '<meta property="article:published_time" content="2026-10-04T22:00:00Z"><article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>'
        payload = {'data': {'id': 123, 'title': HEADLINE, 'content': '<p>' + BODY + '</p>'}}
        result, _ = self.collect(article, article_data=payload)
        self.assertEqual(result[0]['publishedOn'], '2026-10-05')
        self.assertEqual(result[0]['publicationSource'], 'article publication meta')

    def test_article_header_date_and_body_release_date_are_separate(self):
        """A body schedule cannot overwrite the publication date beside the headline."""
        article = '<article><header><h1>' + HEADLINE + '</h1><time datetime="2026-10-04">October 4, 2026</time></header><p>' + BODY + '</p></article>'
        result, _ = self.collect(article)
        self.assertEqual(result[0]['publishedOn'], '2026-10-04')

    def test_body_only_date_is_not_publication(self):
        """Missing metadata stays missing even when the article contains an exact release date."""
        result, _ = self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>')
        self.assertIsNone(result[0]['publishedOn'])

    def test_news_card_publication_is_preserved(self):
        """A listing date remains available when the article body has no metadata."""
        listing = '<a data-href="' + URL + '"><h2>' + HEADLINE + '</h2><time datetime="2026-10-04">October 4, 2026</time></a>'
        result, _ = self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>', listing=listing)
        self.assertEqual(result[0]['publishedOn'], '2026-10-04')

    def test_adaptive_scan_extends_until_current_checkpoint(self):
        """An unseen current release expands the listing budget instead of truncating silently."""
        import html
        other = FEED['url'] + '/111'
        title = 'A general publisher news article'
        click = "document.querySelector('main').insertAdjacentHTML('afterbegin', " + json.dumps('<a data-href="' + URL + '">' + HEADLINE + '</a>') + ");"
        listing = '<main><a data-href="' + other + '">' + title + '</a><button class="news-next" onclick="' + html.escape(click, quote=True) + '">Next</button></main>'
        result, log = self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>', listing=listing,
            settings=dict(SETTINGS, maxListPages=1, maxAdaptiveListPages=3, coverageAnchors=[URL], publicationWaitSeconds=0),
            extra_pages={other: '<article><h1>' + title + '</h1><p>A complete public news article with no release schedule.</p></article>'})
        self.assertEqual(result.discovery['pagesRead'], 2)
        self.assertTrue(result.discovery['checkpointReached'])
        self.assertEqual(result.discovery['endReason'], 'anchor-overlap')
        self.assertEqual(len(result), 2)

    def test_adaptive_limit_without_checkpoint_fails(self):
        """A fully consumed extended budget cannot claim safe coverage without its checkpoint."""
        import html
        other = FEED['url'] + '/111'
        click = "document.querySelector('main').insertAdjacentHTML('afterbegin', " + json.dumps('<a data-href="' + FEED['url'] + '/222">Second general news article</a>') + ");"
        listing = '<main><a data-href="' + other + '">First general news article</a><button class="news-next" onclick="' + html.escape(click, quote=True) + '">Next</button></main>'
        with self.assertRaises(FeedReadError) as error:
            self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>', listing=listing,
                settings=dict(SETTINGS, maxListPages=1, maxAdaptiveListPages=2, coverageAnchors=[URL]))
        self.assertEqual(error.exception.details['discovery']['endReason'], 'adaptive-page-limit')

    def test_all_news_category_is_selected_without_top_navigation(self):
        """A recognized news tab can expose the missing list without clicking a site-menu More."""
        import html
        click = "document.querySelector('main').insertAdjacentHTML('beforeend', " + json.dumps('<a data-href="' + URL + '">' + HEADLINE + '</a>') + ");"
        listing = '<header><button>More</button></header><main><div class="news-tabs"><button role="tab" aria-selected="true">Videos</button><button role="tab" aria-selected="false" onclick="' + html.escape(click, quote=True) + '">All</button></div></main>'
        result, _ = self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>', listing=listing,
            settings=dict(SETTINGS, publicationWaitSeconds=0))
        self.assertEqual(result.discovery['selectedCategory'], 'All')
        self.assertEqual(len(result), 1)

    def test_delayed_publication_meta_is_read(self):
        """The article JSON body can arrive before the visible header publication metadata."""
        article = '<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article><script>setTimeout(()=>{const m=document.createElement("meta");m.setAttribute("property","article:published_time");m.content="2026-10-04";document.head.append(m)},250)</script>'
        payload = {'data': {'id': 123, 'title': HEADLINE, 'content': '<p>' + BODY + '</p>'}}
        result, _ = self.collect(article, article_data=payload)
        self.assertEqual(result[0]['publishedOn'], '2026-10-04')
        self.assertEqual(len(result[0]['bodyHash']), 64)

    def test_seed_article_does_not_count_as_discovered_checkpoint(self):
        """A revisited seed proves that notice can be read, not that listing discovery worked."""
        seed_url = FEED['url'] + '/888'
        seed_title = 'Version 4.6 Update Details'
        result, _ = self.collect('<article><h1>' + HEADLINE + '</h1><p>' + BODY + '</p></article>',
            settings=dict(SETTINGS, maxAdaptiveListPages=2, coverageAnchors=[seed_url], publicationWaitSeconds=0,
                          seedArticles=[{'url': seed_url, 'title': seed_title}]),
            extra_pages={seed_url: '<article><h1>' + seed_title + '</h1><p>Another complete public article for the seed check.</p></article>'})
        self.assertFalse(result.discovery['checkpointReached'])
        self.assertEqual(result.discovery['coverage'], 'bounded-unverified')
        self.assertEqual(result.discovery['additionalKnownArticles'], 1)


if __name__ == '__main__':
    unittest.main()
