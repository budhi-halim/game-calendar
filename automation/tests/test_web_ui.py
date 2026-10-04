"""Run the shipped UI in Chromium with local data fixtures and no network requests."""

import json
import os
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class WebUITests(unittest.TestCase):
    """Exercise real DOM, CSS, date navigation, and dialogs using simulated file reads."""

    @classmethod
    def setUpClass(cls):
        """Start Chromium; load exact shipped files with mocked fetch transport."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('Playwright is not installed.')
        cls.driver = sync_playwright().start()
        launch = {'headless': True}
        if os.environ.get('PATCH_CALENDAR_TEST_BROWSER'):
            launch['executable_path'] = os.environ['PATCH_CALENDAR_TEST_BROWSER']
        cls.browser = cls.driver.chromium.launch(**launch)
        cls.html = re.sub(r'<script type="module"[^>]*></script>', '', (ROOT / 'index.html').read_text())
        cls.html = re.sub(r'<link[^>]+>', '', cls.html)
        logic = (ROOT / 'js/logic.js').read_text().replace('export ', '')
        ui = re.sub(r'^import .*?;\n', '', (ROOT / 'js/ui.js').read_text())
        cls.script = logic + '\n' + ui + '\nwindow.__ui = {state, setMonth, revealRelease, refreshToday};'
        # Public dates evolve; assertions use a fixed snapshot so daily hunting cannot break the tests.
        cls.data = json.loads((ROOT / 'automation/tests/fixture-data.json').read_text(encoding='utf-8'))

    @classmethod
    def tearDownClass(cls):
        """Release the shared test browser."""
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        """Create an isolated browser page frozen on the sample dataset's reference day."""
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 844}, timezone_id='Asia/Jakarta', color_scheme='light')
        self.page = self.context.new_page()
        self.page.route('**/*', lambda route: route.abort())
        self.page.clock.install(time=datetime(2026, 10, 4, 5, tzinfo=timezone.utc))
        self.page.set_content(self.html)
        self.page.add_style_tag(content=(ROOT / 'css/style.css').read_text())
        self.page.evaluate('data => { window.__fixture = data; window.fetch = async url => ({ok:true, json:async() => structuredClone(data[/data\\/(\\w+)\\.json/.exec(url)[1]])}); }', self.data)
        self.page.add_script_tag(type='module', content=self.script)
        self.page.locator('#app').wait_for(state='visible', timeout=5000)

    def tearDown(self):
        """Close test-local state and any mocked image requests."""
        self.context.close()

    def test_responsive_widths(self):
        """The calendar does not cause horizontal overflow at mobile or desktop sizes."""
        for width in (320, 360, 390, 768, 1024, 1440):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 900})
                self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)

    def test_mobile_version_number_visible(self):
        """The confirmed future version name remains visible beside its projected date."""
        self.page.set_viewport_size({'width': 320, 'height': 800})
        marker = self.page.locator('.day-marker[data-game="zzz"] .marker-version').first
        self.assertEqual(marker.inner_text(), '3.3')
        self.assertTrue(marker.is_visible())
        self.assertGreaterEqual(marker.bounding_box()['height'], 10)

    def test_month_uses_only_needed_weeks(self):
        """The October reference month uses five weeks, not an extra empty sixth week."""
        self.assertEqual(self.page.locator('.calendar-row').count(), 5)

    def test_next_and_previous_release_navigation(self):
        """Jump directly between event dates while retaining selected-day context."""
        self.page.locator('#next-release').click()
        self.assertIn('15 October', self.page.locator('#agenda-title').inner_text())
        self.page.locator('#next-release').click()
        self.assertIn('21 October', self.page.locator('#agenda-title').inner_text())
        self.page.locator('#previous-release').click()
        self.assertIn('15 October', self.page.locator('#agenda-title').inner_text())

    def test_release_navigation_respects_filters(self):
        """Unselected games cannot become navigation destinations."""
        self.page.locator('.game-filter[data-game="endfield"]').click()
        self.page.locator('#next-release').click()
        self.assertIn('21 October', self.page.locator('#agenda-title').inner_text())

    def test_single_event_day_opens_details(self):
        """One click on an event day opens its full name and date details."""
        self.page.locator('.day-button').filter(has=self.page.locator('.day-marker[data-game="zzz"]')).first.click()
        self.assertTrue(self.page.locator('#details-dialog').is_visible())
        self.assertIn('3.3', self.page.locator('#details-title').inner_text())

    def test_show_in_calendar_from_upcoming(self):
        """A future sidebar event can locate its exact month and selected date."""
        self.page.locator('.upcoming-card[data-game="hsr"] .feature-release').click()
        self.page.locator('.details-locate').click()
        self.assertFalse(self.page.locator('#details-dialog').is_visible())
        self.assertEqual(self.page.locator('#month-title').inner_text(), 'November 2026')
        self.assertIn('11 November', self.page.locator('#agenda-title').inner_text())

    def test_far_future_is_calculated_on_demand(self):
        """A distant month contains projected releases without assigning invented labels."""
        self.page.evaluate('window.__ui.setMonth(2028, 12)')
        self.assertGreater(self.page.locator('.day-marker.projected').count(), 0)
        self.assertEqual(self.page.locator('.marker-version').count(), 0)

    def test_positive_date_boundary_navigation(self):
        """The final supported month stays usable and cannot navigate past its boundary."""
        self.page.evaluate('window.__ui.setMonth(275760, 9)')
        self.assertTrue(self.page.locator('#next-month').is_disabled())
        self.page.locator('#previous-release').click()
        self.assertIn('275760', self.page.locator('#month-title').inner_text())

    def test_empty_filters_disable_release_navigation(self):
        """An empty selection remains recoverable and cannot jump to a hidden event."""
        for game in ('hsr', 'zzz', 'endfield'):
            self.page.locator('.game-filter[data-game="' + game + '"]').click()
        self.assertTrue(self.page.locator('#previous-release').is_disabled())
        self.assertTrue(self.page.locator('#next-release').is_disabled())
        self.page.locator('#empty-show-all').click()
        self.assertFalse(self.page.locator('#next-release').is_disabled())

    def test_list_view_navigation(self):
        """Release navigation also works with the grid hidden."""
        self.page.locator('#list-view').click()
        self.page.locator('#next-release').click()
        self.assertFalse(self.page.locator('#month-grid-wrap').is_visible())
        self.assertIn('15 October', self.page.locator('#agenda-title').inner_text())

    def test_theme_toggle_and_mobile_tap_style(self):
        """The app retains light-dark() theming and suppresses the mobile tap highlight."""
        self.page.locator('#theme-button').click()
        self.assertEqual(self.page.evaluate('getComputedStyle(document.documentElement).colorScheme'), 'dark')
        self.assertEqual(self.page.locator('#next-month').evaluate('n => getComputedStyle(n).webkitTapHighlightColor'), 'rgba(0, 0, 0, 0)')

    def test_previous_release_matches_scanned_history(self):
        """The arithmetic predecessor matches a bounded brute-force historical scan."""
        checks = self.page.evaluate('''() => {
            const c = window.__ui.state.calendar;
            let count = 0;
            for (const g of c.games) {
                for (let day = g.anchors[0].day; day <= 21000; day += 17) {
                    const past = c.releasesBetween(g.anchors[0].day, day - 1, [g.id]).at(-1);
                    if ((c.previousRelease(g.id, day)?.id || null) !== (past?.id || null)) throw new Error(g.id + ':' + day);
                    count++;
                }
            }
            return count;
        }''')
        self.assertGreater(checks, 100)


if __name__ == '__main__':
    unittest.main()
