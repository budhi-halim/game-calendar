"""Discover official news through fixed publisher websites and parse explicit evidence."""

import html
import json
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from common import JAKARTA, digest, parse_day, today
from feed_pool import CollectionCancelled

VERSION = re.compile(r'\b(?:version|ver\.)\s*(\d+\.\d+)\b', re.I)
VERSION_GROUP = re.compile(r'\bversions?\s+(\d+\.\d+(?:\s*(?:[–—−-]|and|&|,|to)\s*\d+\.\d+)*)', re.I)
RELEASE_HEADLINE = re.compile(r'\bversion\b|update.{0,25}(?:notice|announcement|maintenance)|special program', re.I)
NOT_A_RELEASE = re.compile(r'hotfix|known issues|issue fixes|bug fixes|optimization update|test server|beta test', re.I)
MONTHS = {name.lower(): index for index, name in enumerate(
    ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'), 1)}
MONTHS.update({name[:3]: index for name, index in list(MONTHS.items())})
DATE = re.compile(r'(?P<iso>20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})|(?P<month>' + '|'.join(MONTHS) + r')\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?[,]?\s+(?P<year>20\d{2})', re.I)
ZONE = re.compile(r'UTC\s*([+−-])\s*(\d{1,2})(?::(\d{2}))?', re.I)


def clean(value):
    """Normalize horizontal whitespace without flattening paragraph boundaries."""
    return re.sub(r'[ \t\r\f\v]+', ' ', str(value or '')).strip()


def normalize_name(value):
    """Compare official titles without typography-only differences."""
    return re.sub(r'[^a-z0-9]+', '', str(value or '').casefold())


def html_text(value):
    """Extract rich-text article bodies without an extra HTML-parser dependency."""
    class Text(HTMLParser):
        """Retain paragraph boundaries and ignore executable or styling content."""
        def __init__(self):
            """Initialize the text accumulator."""
            super().__init__(convert_charrefs=True)
            self.parts, self.skip = [], 0

        def handle_starttag(self, tag, attrs):
            """Separate block elements and ignore scripts."""
            if tag in ('script', 'style'):
                self.skip += 1
            elif tag in ('p', 'br', 'div', 'li', 'h1', 'h2', 'h3', 'tr'):
                self.parts.append('\n')

        def handle_endtag(self, tag):
            """Close ignored content or a block element."""
            if tag in ('script', 'style'):
                self.skip = max(0, self.skip - 1)
            elif tag in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'tr'):
                self.parts.append('\n')

        def handle_data(self, text):
            """Keep only readable text."""
            if not self.skip:
                self.parts.append(text)
    parser = Text()
    parser.feed(str(value))
    return clean(''.join(parser.parts))


def canonical_article(url, feed):
    """Accept only article URLs on this game's configured publisher website."""
    parsed = urlsplit(urljoin(feed['url'], str(url or '')))
    if parsed.scheme != 'https' or parsed.hostname not in feed['hosts'] or parsed.username or parsed.password:
        return None
    path = parsed.path.rstrip('/')
    if not re.fullmatch(feed['articlePattern'], path):
        return None
    return urlunsplit(('https', parsed.netloc, path, '', ''))


def published_day(value):
    """Read explicit publication metadata, respecting offsets on ISO timestamps."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    try:
        if isinstance(value, (int, float)) or text.isdigit():
            number = float(value)
            if number > 1e12:
                number /= 1000
            if 1500000000 < number < 5000000000:
                return datetime.fromtimestamp(number, JAKARTA).date().isoformat()
        if re.match(r'^20\d{2}-\d{2}-\d{2}[T ]\d{2}:', text):
            instant = datetime.fromisoformat(text.replace('Z', '+00:00'))
            if instant.tzinfo:
                return instant.astimezone(JAKARTA).date().isoformat()
            return instant.date().isoformat()
        match = re.fullmatch(r'(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?', text)
        if match:
            result = '-'.join((match[1], match[2].zfill(2), match[3].zfill(2)))
            parse_day(result)
            return result
        match = DATE.fullmatch(text)
        if match:
            return datetime(int(match['year']), MONTHS[match['month'].lower()], int(match['day'])).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None
    return None


PUBLICATION_KEYS = ('datepublished', 'publishedon', 'publishedat', 'publishtime', 'publishdate',
                    'publicationdate', 'publicationtime', 'displaytime', 'displaydate', 'posttime', 'postdate',
                    'spublishtime', 'spublishdate', 'ipublishtime', 'publishedtimestamp',
                    'starttime', 'istarttime', 'createdat', 'createtime', 'releasedat', 'date', 'time')
PUBLICATION_WRAPPERS = {'metadata', 'meta', 'ext', 'extend', 'extra', 'extensions', 'attributes', 'fields'}


def publication_candidates(record):
    """Read only date fields on this article or its own metadata, never body/related records."""
    found = []

    def visit(value, prefix='', depth=0):
        """Inspect allowlisted metadata wrappers and key/value CMS fields with bounded depth."""
        if depth > 4:
            return
        if isinstance(value, str) and len(value) < 20000:
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return
        if isinstance(value, list):
            for entry in value[:40]:
                visit(entry, prefix, depth + 1)
        elif isinstance(value, dict):
            lower = {re.sub(r'[_-]', '', str(key).lower()): item for key, item in value.items()}
            field_name = lower.get('key') or lower.get('name')
            if isinstance(field_name, str):
                key = re.sub(r'[_-]', '', field_name.lower())
                if key in PUBLICATION_KEYS:
                    raw = lower.get('value')
                    found.append({'field': prefix + key, 'key': key, 'day': published_day(raw)})
            for key, item in lower.items():
                if key in PUBLICATION_KEYS:
                    raw = item.get('value') if isinstance(item, dict) else item
                    found.append({'field': prefix + key, 'key': key, 'day': published_day(raw)})
                elif key in PUBLICATION_WRAPPERS:
                    visit(item, prefix + key + '.', depth + 1)
    visit(record)
    return found[:40]


def publication_metadata(record):
    """Prefer publication fields; conflicting explicit values remain unknown."""
    candidates = [item for item in publication_candidates(record) if item['day']]
    strong = [item for item in candidates if item['key'] not in
              {'starttime', 'istarttime', 'createdat', 'createtime', 'releasedat', 'date', 'time'}]
    selected = strong or candidates
    if len({item['day'] for item in selected}) == 1:
        item = selected[0]
        return item['day'], 'publisher JSON: ' + item['field']
    return None, None


def navigation_title(value):
    """Exclude known site-menu labels, not ordinary non-release news articles."""
    label = re.sub(r'[\s_+–—-]+', ' ', html_text(value)).casefold().strip()
    return label in {'hoyolab', 'tour', 'redeem code', 'top up', 'social media', '社媒信息 bgm'}


def walk_news(value, feed, depth=0, in_collection=False):
    """Extract paired title/ID records from JSON loaded by the official news page."""
    if depth > 30:
        return []
    result = []
    if isinstance(value, list):
        for item in value:
            result.extend(walk_news(item, feed, depth + 1, True))
    elif isinstance(value, dict):
        lower = {re.sub(r'[_-]', '', key.lower()): item for key, item in value.items()}
        title = next((lower[key] for key in ('title', 'headline', 'newstitle', 'articletitle', 'stitle') if isinstance(lower.get(key), str) and len(lower[key].strip()) >= 3), None)
        if title and not navigation_title(title):
            url = next((canonical_article(lower[key], feed) for key in ('url', 'link', 'articleurl', 'newsurl', 'slink', '@id') if isinstance(lower.get(key), str) and canonical_article(lower[key], feed)), None)
            explicit_links = [lower[key] for key in ('url', 'link', 'articleurl', 'newsurl', 'slink', '@id') if isinstance(lower.get(key), str) and lower[key].strip()]
            if not url and not explicit_links:
                identifier = next((lower[key] for key in ('articleid', 'newsid', 'contentid', 'infoid', 'iinfoid', 'cid', 'nid', 'id') if isinstance(lower.get(key), (str, int)) and re.fullmatch(r'\d{1,16}', str(lower[key]))), None)
                if identifier is not None:
                    url = canonical_article(feed['url'].rstrip('/') + '/' + str(identifier), feed)
            if url:
                published, publication_source = publication_metadata(lower)
                body = next((html_text(lower[key]) for key in ('content', 'articlecontent', 'newscontent', 'scontent', 'articlebody', 'body') if isinstance(lower.get(key), str) and lower[key].strip()), None)
                result.append({'url': url, 'title': html_text(title), 'publishedOn': published, 'publicationSource': publication_source, 'publicationFields': publication_candidates(lower), 'text': body, '_titleRank': 3, '_detail': not in_collection})
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                collection = in_collection or re.sub(r'[_-]', '', key.lower()) in ('list', 'items', 'records', 'articles', 'related', 'recommendations')
                result.extend(walk_news(item, feed, depth + 1, collection))
    return result


class FeedReadError(RuntimeError):
    """Carry bounded, public-page diagnostics without cookies or request headers."""

    def __init__(self, message, details=None):
        """Keep structured failure information separate from the short terminal message."""
        super().__init__(message)
        self.details = details or {}


def title_key(value):
    """Normalize display typography, but retain numbers and word boundaries."""
    value = unicodedata.normalize('NFKC', html.unescape(str(value or '')))
    value = re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060\ufeff]', '', value)
    value = value.translate(str.maketrans({'“': '"', '”': '"', '‘': "'", '’': "'", '–': '-', '—': '-', '−': '-'}))
    return ' '.join(value.casefold().split())


def headline_matches(record, heading):
    """Require the actual headline; tolerate extra card metadata only for DOM fallbacks."""
    expected, actual = title_key(record.get('title')), title_key(heading)
    if not expected or not actual:
        return False
    if expected == actual:
        return True
    # An entire news-card label is not an authoritative publisher headline.
    if record.get('_titleRank', 1) <= 1 and len(actual) >= 12:
        return bool(re.search(r'(?<!\w)' + re.escape(actual) + r'(?!\w)', expected))
    return False


def merge_news_record(records, item):
    """Keep structured headlines and detail bodies ahead of lower-quality card labels."""
    old = records.get(item['url'], {})
    result = dict(old)
    for key, value in item.items():
        if value is not None and key not in ('title', '_titleRank', 'text', '_detail') and (key != 'publicationFields' or value):
            result[key] = value
    old_rank, new_rank = old.get('_titleRank', 1), item.get('_titleRank', 1)
    if item.get('title') and (not old.get('title') or new_rank > old_rank):
        result['title'], result['_titleRank'] = item['title'], new_rank
    if item.get('text') and (not old.get('text') or item.get('_detail') or not old.get('_detail')):
        result['text'], result['_detail'] = item['text'], item.get('_detail', False)
    records[item['url']] = result
    return result


CHALLENGE = re.compile(r'verify (?:that )?you are human|access denied|captcha challenge|checking your browser', re.I)
RELATED_BOUNDARY = re.compile(r'\n\s*(?:Related (?:News|Articles)|You [Mm]ay [Aa]lso [Ll]ike|Recommended (?:News|Articles)|Select Language|Back to (?:News|List))\s*(?:\n|$)')

# Read only article-shaped containers. The whole page is diagnostic text, never release evidence.
ARTICLE_SNAPSHOT = r"""() => {
    const visible = n => !!(n && n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden');
    const headingSelector = 'h1,h2,h3,[role="heading"],[class*="title" i]';
    const bodyText = document.body?.innerText || '';
    const headings = [...document.querySelectorAll(headingSelector)]
        .filter(n => visible(n) && !n.closest('a,nav,footer,aside'))
        .map(n => (n.innerText || '').trim()).filter(t => t.length >= 3 && t.length <= 600);
    const containers = [...document.querySelectorAll(
        'article,[role="article"],[itemprop="articleBody"],main,[class*="news-detail" i],[class*="news_detail" i],[class*="article-detail" i],[class*="article_detail" i]')];
    // Some publisher components are div-only. Ascend from a non-link heading, never to body.
    for (const heading of document.querySelectorAll(headingSelector)) {
        if (!visible(heading) || heading.closest('a,nav,footer,aside')) continue;
        let parent = heading.parentElement;
        for (let level = 0; parent && parent !== document.body && level < 3; level++, parent = parent.parentElement) {
            if (!parent.querySelector('p,[itemprop="articleBody"],[class*="content" i]')) continue;
            if (!containers.includes(parent)) containers.push(parent);
            break;
        }
    }
    const candidates = [];
    for (const node of containers) {
        if (!visible(node) || node === document.body || node.closest('nav,footer,aside,a')) continue;
        const text = (node.innerText || '').trim();
        if (!text || text.length > 350000) continue;
        const localHeadings = [...node.querySelectorAll(headingSelector)]
            .filter(n => visible(n) && !n.closest('a,nav,footer,aside'))
            .map(n => (n.innerText || '').trim()).filter(t => t.length >= 3 && t.length <= 600);
        const selector = node.tagName.toLowerCase() + (typeof node.className === 'string' && node.className ? '.' + node.className.trim().replace(/\s+/g, '.') : '');
        candidates.push({selector, text: text.slice(0,250000), headings: [...new Set(localHeadings)].slice(0,24),
            hasMedia: !!node.querySelector('img[src],video,iframe[src]'),
            loading: !!node.querySelector('[aria-busy="true"],[role="progressbar"]'),
            related: !!node.querySelector('[class*="related" i],[class*="recommend" i]')});
        if (candidates.length >= 20) break;
    }
    const dates = [...document.querySelectorAll('time[datetime],meta[property="article:published_time"],meta[name="pubdate"]')]
        .map(n => n.getAttribute('datetime') || n.getAttribute('content')).filter(Boolean);
    const jsonld = [...document.querySelectorAll('script[type="application/ld+json"]')]
        .map(n => n.textContent || '').filter(t => t.length < 500000).slice(0,8);
    return {url: location.href, documentTitle: document.title, readyState: document.readyState,
        bodyLength: bodyText.length, bodyExcerpt: bodyText.slice(0,3500), headings: [...new Set(headings)].slice(0,24),
        candidates, dates, jsonld};
}"""


PUBLICATION_SNAPSHOT = r"""() => {
    const meta = [...document.querySelectorAll('meta[property="article:published_time"],meta[name="publishdate"],meta[name="date"],meta[itemprop="datePublished"]')]
        .map(n => ({value: n.content, source: 'article publication meta'}));
    const excluded = n => n.closest('a,nav,footer,aside,[class*="related" i],[class*="recommend" i]');
    const fields = [...document.querySelectorAll('[itemprop="datePublished"],time[pubdate],article header time,[class*="news-date" i],[class*="news_date" i],[class*="news__date" i],[class*="article-date" i],[class*="article_date" i],[class*="article__date" i],[class*="publish-time" i],[class*="publish_time" i]')]
        .filter(n => !excluded(n) && n.getClientRects().length)
        .map(n => ({value: n.getAttribute('datetime') || n.getAttribute('content') || n.innerText, source: 'article publication element'}));
    // Metadata beside the headline is separate from dates in the body.
    for (const h of document.querySelectorAll('h1,[class*="article-title" i],[class*="article__title" i],[class*="news-title" i],[class*="news__title" i]')) {
        if (excluded(h)) continue;
        for (const n of h.parentElement?.children || []) {
            if (n === h || excluded(n) || !n.getClientRects().length) continue;
            if (/date|publish|time/i.test(String(n.className)) && (n.innerText || '').length < 65)
                fields.push({value: n.getAttribute('datetime') || n.innerText, source: 'headline-adjacent publication field'});
        }
    }
    const jsonld = [...document.querySelectorAll('script[type="application/ld+json"]')]
        .map(n => n.textContent || '').filter(t => t.length < 500000).slice(0,8);
    return {fields: [...meta, ...fields].slice(0,24), jsonld};
}"""


def publication_from_snapshot(snapshot, record, feed):
    """Only accept one unambiguous metadata day, or an ID-matched JSON-LD date."""
    for raw in snapshot.get('jsonld', []):
        try:
            for item in walk_news(json.loads(raw), feed):
                if item['url'] == record['url'] and item.get('publishedOn'):
                    return item['publishedOn'], item.get('publicationSource') or 'article JSON-LD'
        except (ValueError, TypeError):
            pass
    values = [(published_day(item['value']), item['source']) for item in snapshot.get('fields', [])]
    days = {value for value, _ in values if value}
    if len(days) == 1:
        return next((value, source) for value, source in values if value)
    return None, None


def dom_article(record, snapshot):
    """Return an isolated, matched article; never fall back to the site's whole body."""
    candidates = []
    for scope in snapshot.get('candidates', []):
        if scope.get('loading'):
            continue
        matched = next((heading for heading in scope['headings'] if headline_matches(record, heading)), None)
        if not matched:
            continue
        text = clean(scope['text'])
        start = text.find(matched)
        if start >= 0:
            text = text[start:]
        cut = RELATED_BOUNDARY.split(text, maxsplit=1)
        if scope.get('related') and len(cut) == 1:
            # A related-story container without a clear boundary could import another date.
            continue
        text = cut[0].strip()
        remainder = text.replace(matched, '', 1).strip()
        if CHALLENGE.search(text) or (len(remainder) < 20 and not scope.get('hasMedia')):
            continue
        # Media-only notices retain the headline; date parsing still requires explicit text.
        candidates.append((len(text), {'title': matched, 'text': text, 'readVia': 'article DOM', 'selector': scope['selector']}))
    return min(candidates, key=lambda entry: entry[0])[1] if candidates else None


def public_url(value):
    """Omit query strings, credentials, and fragments from network diagnostics."""
    try:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.hostname or '', parsed.path, '', ''))
    except (TypeError, ValueError):
        return '[invalid URL]'


class CollectedArticles(list):
    """Carry bounded listing coverage alongside ordinary article records."""

    def __init__(self):
        """Keep reporting metadata separate from article evidence."""
        super().__init__()
        self.discovery = {'pagesRead': 0, 'recordsDiscovered': 0, 'endReason': None, 'passes': []}


LISTING_LINKS = r"""nodes => nodes.filter(n => !n.closest('header,footer,aside,[role="navigation"]'))
    .map(n => {
        const heading = n.querySelector('h1,h2,h3,h4,[class*="title" i]');
        const explicit = heading?.innerText || n.getAttribute('aria-label') || n.getAttribute('title');
        return {url: n.href || n.dataset.href || n.dataset.url,
            title: explicit || n.innerText || '', _titleRank: explicit ? 2 : 1,
            publishedOn: n.querySelector('time')?.getAttribute('datetime') || n.querySelector('time,[class*="date" i]')?.textContent || null};
    })"""

NEWS_CATEGORIES = r"""() => {
    const visible = n => n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden';
    const nodes = [...document.querySelectorAll('[role="tab"],[class*="tab" i] button,[class*="tab" i] a,[class*="tab-item" i],[class*="tab_item" i],[class*="tab__item" i]')];
    const names = /^(all|all news|news|notices|announcements|updates)$/i;
    const found = [];
    for (const n of nodes) {
        if (!visible(n) || !n.closest('main,[class*="news" i]') || n.closest('header,footer,aside') || (n.tagName === 'A' && n.getAttribute('href') && !n.getAttribute('href').startsWith('#'))) continue;
        const text = (n.innerText || n.getAttribute('aria-label') || '').trim().replace(/\s+/g,' ');
        if (!names.test(text) || found.some(v => v.label.toLowerCase() === text.toLowerCase())) continue;
        const marker = 'category-' + found.length;
        n.setAttribute('data-patch-calendar-category', marker);
        found.push({label: text, marker, active: n.getAttribute('aria-selected') === 'true' || /(^|[ _-])(active|selected|current)([ _-]|$)/i.test(String(n.className))});
    }
    return found.slice(0,5);
}"""

# Identify a real next/list-more control; never click the site's generic navigation "More".
NEXT_LIST_CONTROL = r"""() => {
    const marker = 'data-patch-calendar-next';
    document.querySelectorAll('[' + marker + ']').forEach(n => n.removeAttribute(marker));
    const visible = n => n && n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden';
    const pagerSelector = '[class*="pagination" i],[class*="pager" i],nav[aria-label*="pagination" i]';
    const disabled = n => !!n.closest('[disabled],[aria-disabled="true"],.disabled,[class*="--disabled"],.is-disabled,.ant-pagination-disabled');
    const candidates = [];
    const controls = document.querySelectorAll('button,a,[role="button"],li,[class*="next" i],[class*="more" i]');
    for (const n of controls) {
        if (!visible(n)) continue;
        const pager = n.closest(pagerSelector);
        if (n.closest('header,footer,aside') || (!pager && n.closest('nav,[role="navigation"]'))) continue;
        const text = (n.innerText || '').trim().replace(/\s+/g, ' ');
        const label = n.getAttribute('aria-label') || n.getAttribute('title') || '';
        const classes = typeof n.className === 'string' ? n.className : '';
        const hasList = !!n.closest('[class*="news" i],[class*="list" i],main');
        let rank = 0;
        if (pager && (/next/i.test(label + ' ' + classes) || /^(next|›|»|→)$/i.test(text))) rank = 5;
        else if (n.getAttribute('rel') === 'next') rank = 5;
        else if (hasList && /^(load more|show more|more news|load more news|show more news)$/i.test(text)) rank = 4;
        else if (hasList && /^(next page|older posts|older news)$/i.test(label || text)) rank = 4;
        else if (hasList && /^(more|next)$/i.test(text) && /news|list|load|pagination|pager/i.test(classes)) rank = 3;
        if (rank) candidates.push({n, rank, disabled: disabled(n), label: label || text || classes});
    }
    // Number-only pagers: choose exactly the page after the active page.
    for (const pager of document.querySelectorAll(pagerSelector)) {
        if (!visible(pager)) continue;
        const current = pager.querySelector('[aria-current="page"],.active,.is-active,.selected,[class*="item-active"]');
        const number = Number((current?.innerText || '').trim());
        if (!Number.isInteger(number) || number < 1) continue;
        const next = [...pager.querySelectorAll('button,a,li,[role="button"]')]
            .find(n => visible(n) && (n.innerText || '').trim() === String(number + 1));
        if (next) candidates.push({n: next, rank: 2, disabled: disabled(next), label: 'page ' + (number + 1)});
    }
    candidates.sort((a,b) => b.rank - a.rank);
    const selected = candidates.find(c => !c.disabled);
    if (!selected) return {found: false, disabled: candidates.some(c => c.disabled)};
    selected.n.setAttribute(marker, 'true');
    return {found: true, disabled: false, label: selected.label.slice(0,180)};
}"""

LIST_SCROLL = r"""() => {
    for (const n of document.querySelectorAll('main,[class*="news" i],[class*="list" i]')) {
        if (n.scrollHeight > n.clientHeight + 10 && /auto|scroll/.test(getComputedStyle(n).overflowY))
            n.scrollTo(0, n.scrollHeight);
    }
    window.scrollTo(0, document.body.scrollHeight);
}"""


def collect_feed(game_id, feed, settings, executable=None, progress=None, headed=False, cancel=None):
    """Use fixed official feeds, exact article identities, and bounded content waits."""
    cancel = cancel or (lambda: None)
    cancel()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError('Install dependencies with python -m pip install -r automation/requirements.txt, then python -m playwright install chromium.') from error
    progress = progress or (lambda message: None)
    discovered, articles = {}, CollectedArticles()
    timeout = settings['timeoutSeconds'] * 1000
    anchors = set(settings.get('coverageAnchors', []))
    base_pages = settings['maxListPages']
    maximum_pages = max(base_pages, settings.get('maxAdaptiveListPages', 12)) if anchors else base_pages
    active = {'url': None, 'details': {}, 'network': [], 'pageErrors': [], 'jsonErrors': []}
    with sync_playwright() as playwright:
        launch = {'headless': not headed, 'timeout': timeout}
        cancel()
        progress('Launching Chromium' + (' with a visible window.' if headed else ' in headless mode.'))
        if executable:
            launch['executable_path'] = executable
        browser = playwright.chromium.launch(**launch)
        context = browser.new_context(locale='en-US', timezone_id='Asia/Jakarta', viewport={'width': 1440, 'height': 1000})
        context.set_default_timeout(timeout)
        # Do not abort images/fonts: a publisher's loading component may depend on them.
        page = context.new_page()

        def pause(milliseconds):
            """Pump Playwright events while checking shutdown at short intervals."""
            deadline = time.monotonic() + milliseconds / 1000
            while True:
                cancel()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                page.wait_for_timeout(min(200, max(1, remaining * 1000)))

        def network_entry(value):
            """Keep only a small public-URL error trail for the current navigation."""
            active['network'].append(value)
            del active['network'][:-30]

        def failed_request(request):
            """Record failed document/script/data loads, without cookies or request bodies."""
            if request.resource_type in ('document', 'script', 'xhr', 'fetch', 'stylesheet'):
                network_entry({'url': public_url(request.url), 'type': request.resource_type, 'failure': str(request.failure)[:300]})

        def response_seen(response):
            """Read status/headers only here; the body is not necessarily complete yet."""
            if response.status >= 400 and response.request.resource_type in ('document', 'script', 'xhr', 'fetch', 'stylesheet'):
                network_entry({'url': public_url(response.url), 'type': response.request.resource_type, 'status': response.status})

        def request_finished(request):
            """Read completed publisher JSON and retain clean headlines separately from cards."""
            host = urlsplit(request.url).hostname or ''
            approved = any(host == suffix or host.endswith('.' + suffix) for suffix in ('hoyoverse.com', 'hoyolab.com', 'mihoyo.com', 'gryphline.com', 'hypergryph.com', 'hg-cdn.com'))
            if not approved:
                return
            try:
                response = request.response()
                if not response or response.status >= 400 or 'json' not in response.headers.get('content-type', ''):
                    return
                items = walk_news(response.json(), feed)
                for item in items:
                    merge_news_record(discovered, item)
                    # A list/excerpt is never accepted as a complete article response.
                    if item['url'] == active['url'] and item.get('_detail') and item.get('text'):
                        active['details'][item['url']] = item
            except Exception as error:
                active['jsonErrors'].append({'url': public_url(request.url), 'error': f'{type(error).__name__}: {str(error).splitlines()[0][:200]}'})
                del active['jsonErrors'][:-10]

        def visit(url):
            """Retry navigation separately from content verification; never bypass a challenge."""
            last = None
            for attempt in range(3):
                cancel()
                try:
                    progress(f'Opening {url} (attempt {attempt + 1}/3; timeout {settings["timeoutSeconds"]}s).')
                    response = page.goto(url, wait_until='domcontentloaded', timeout=timeout)
                    cancel()
                    if response and response.status >= 400:
                        raise RuntimeError(f'Publisher returned HTTP {response.status}.')
                    if urlsplit(page.url).hostname not in feed['hosts']:
                        raise RuntimeError('Publisher redirected to an unconfigured host.')
                    progress(f'Page loaded: {page.url}')
                    return
                except CollectionCancelled:
                    raise
                except Exception as error:
                    cancel()
                    progress(f'Navigation failed: {type(error).__name__}: {str(error).splitlines()[0][:250]}')
                    last = error
                    if attempt < 2:
                        pause((attempt + 1) * 1500)
            raise RuntimeError(str(last).splitlines()[0][:250])

        def failure_details(record=None, snapshot=None):
            """Capture the failed page's heading, body sample, and network errors, not secrets."""
            result = {'stage': 'article' if record else 'listing', 'requestedURL': record['url'] if record else feed['url'],
                      'expectedTitle': record.get('title') if record else None,
                      'titleSource': 'structured JSON' if record and record.get('_titleRank', 1) >= 3 else 'DOM',
                      'networkErrors': list(active['network']), 'pageErrors': list(active['pageErrors']),
                      'jsonErrors': list(active['jsonErrors'])}
            try:
                snapshot = snapshot or page.evaluate(ARTICLE_SNAPSHOT)
                result.update({key: snapshot[key] for key in ('url', 'documentTitle', 'readyState', 'bodyLength', 'bodyExcerpt', 'headings')})
                result['candidateScopes'] = [{'selector': item['selector'], 'length': len(item['text']), 'headings': item['headings']} for item in snapshot['candidates']]
                result['exactTitleSubstringPresent'] = bool(record and ' '.join(record['title'].split()) in ' '.join(page.locator('body').inner_text(timeout=2000).split()))
            except Exception as error:
                result['snapshotError'] = f'{type(error).__name__}: {str(error).splitlines()[0][:200]}'
            if record:
                data = discovered.get(record['url'], {})
                result['structuredRecord'] = {'title': data.get('title'), 'bodyLength': len(data.get('text') or ''),
                                              'completeDetailResponseSeen': record['url'] in active['details']}
            return result

        def read_publication(item):
            """Allow delayed header metadata a bounded grace period, recording observed field names."""
            deadline = time.monotonic() + min(settings.get('publicationWaitSeconds', 1.0), settings['timeoutSeconds'])
            while True:
                cancel()
                metadata = discovered.get(item['url'], item)
                if metadata.get('publishedOn'):
                    return metadata['publishedOn'], metadata.get('publicationSource')
                snapshot = page.evaluate(PUBLICATION_SNAPSHOT)
                date, source = publication_from_snapshot(snapshot, item, feed)
                if date or time.monotonic() >= deadline:
                    return date, source
                pause(200)

        def read_article(item):
            """Prefer completed ID-matched article data; use a verified DOM container otherwise."""
            if canonical_article(page.url, feed) != item['url']:
                raise FeedReadError('Article redirected to a different article or a non-article page.', failure_details(item))
            deadline = time.monotonic() + settings['timeoutSeconds']
            snapshot, last_dom, stable_since = None, None, None
            progress('Waiting for this article\'s own JSON body or verified article container (not a full-card text match).')
            while True:
                cancel()
                if canonical_article(page.url, feed) != item['url']:
                    raise FeedReadError('The page changed to a different article while loading.', failure_details(item))
                metadata = discovered.get(item['url'], item)
                structured = active['details'].get(item['url'])
                if structured and clean(structured.get('text')) and not CHALLENGE.search(structured['text']):
                    if not structured.get('publishedOn'):
                        date, source = read_publication(item)
                        structured = dict(structured, publishedOn=date or metadata.get('publishedOn'), publicationSource=source or metadata.get('publicationSource'))
                    return dict(structured, readVia='publisher article JSON')
                snapshot = page.evaluate(ARTICLE_SNAPSHOT)
                # NewsArticle JSON-LD explicitly identifies both the article and its full body.
                for raw in snapshot['jsonld']:
                    try:
                        for entry in walk_news(json.loads(raw), feed):
                            if entry['url'] == item['url'] and entry.get('text') and not CHALLENGE.search(entry['text']):
                                return dict(entry, readVia='publisher article JSON-LD')
                    except (ValueError, TypeError):
                        pass
                if CHALLENGE.search(snapshot['bodyExcerpt']):
                    raise FeedReadError('The publisher returned a loading/access challenge, not readable article content.', failure_details(metadata, snapshot))
                found = dom_article(metadata, snapshot)
                if found:
                    signature = (found['title'], found['text'])
                    if signature != last_dom:
                        last_dom, stable_since = signature, time.monotonic()
                    elif time.monotonic() - stable_since >= 0.6:
                        published, origin = metadata.get('publishedOn'), metadata.get('publicationSource')
                        if not published:
                            published, origin = read_publication(item)
                        return dict(found, url=item['url'], publishedOn=published, publicationSource=origin)
                else:
                    last_dom, stable_since = None, None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    details = failure_details(metadata, snapshot)
                    raise FeedReadError('Article content was not verified before the content timeout. See failureDetails in hunt-report.json.', details)
                pause(min(250, max(1, remaining * 1000)))

        page.on('response', response_seen)
        page.on('requestfailed', failed_request)
        page.on('requestfinished', request_finished)
        page.on('pageerror', lambda error: active['pageErrors'].append(str(error)[:600]) if len(active['pageErrors']) < 12 else None)
        current = None
        try:
            visit(feed['url'])
            def capture_listing():
                """Merge only configured article links and count newly discovered identities."""
                records = page.locator('a[href], [data-href], [data-url]').evaluate_all(LISTING_LINKS)
                for item in records:
                    url = canonical_article(item.get('url'), feed)
                    title = clean(item.get('title'))
                    if url and len(title) >= 3 and not navigation_title(title):
                        published = published_day(item.get('publishedOn'))
                        merge_news_record(discovered, {'url': url, 'title': title, '_titleRank': item['_titleRank'],
                            'publishedOn': published, 'publicationSource': 'news-card date field' if published else None})

            def wait_for_new_records(before, seconds):
                """Confirm discovery actually advances, not merely that a button was clicked."""
                deadline = time.monotonic() + seconds
                while True:
                    cancel()
                    capture_listing()
                    if set(discovered) - before:
                        return True
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    pause(min(200, max(1, remaining * 1000)))

            progress('Loading the initial news listing.')
            pause(settings['settleSeconds'] * 1000)
            capture_listing()
            categories = page.evaluate(NEWS_CATEGORIES)
            articles.discovery['categories'] = [{'label': row['label'], 'active': row['active']} for row in categories]
            # Prefer an explicitly available All/News category over a selected video/events tab.
            candidate = next((row for preferred in ('all', 'all news', 'news', 'announcements', 'notices', 'updates')
                              for row in categories if row['label'].casefold() == preferred), None)
            if candidate and not candidate['active']:
                progress('Selecting news category: ' + candidate['label'])
                page.locator('[data-patch-calendar-category="' + candidate['marker'] + '"]').click()
                pause(settings['settleSeconds'] * 1000)
                capture_listing()
                articles.discovery['selectedCategory'] = candidate['label']
            for pass_number in range(maximum_pages):
                cancel()
                articles.discovery['pagesRead'] = pass_number + 1
                articles.discovery['recordsDiscovered'] = len(discovered)
                articles.discovery['passes'].append({'pass': pass_number + 1, 'distinctRecords': len(discovered)})
                progress(f'News listing page {pass_number + 1}/{maximum_pages}: {len(discovered)} distinct article records.')
                anchor_seen = sorted(anchors & set(discovered))
                if pass_number + 1 >= base_pages and (not anchors or anchor_seen):
                    articles.discovery['endReason'] = 'anchor-overlap' if anchor_seen else 'configured-page-limit'
                    break
                if pass_number + 1 >= maximum_pages:
                    articles.discovery['endReason'] = 'adaptive-page-limit'
                    break
                if pass_number + 1 == base_pages:
                    progress('Current release checkpoint is not in the listing yet; extending discovery within the adaptive limit.')
                before = set(discovered)
                control = page.evaluate(NEXT_LIST_CONTROL)
                if not control['found']:
                    # Support infinite-scroll lists, including lists inside a scroll pane.
                    page.evaluate(LIST_SCROLL)
                    if wait_for_new_records(before, settings['settleSeconds']):
                        continue
                    control = page.evaluate(NEXT_LIST_CONTROL)
                if not control['found']:
                    articles.discovery['endReason'] = 'next-control-disabled' if control['disabled'] else 'no-more-control-or-scroll-results'
                    progress('No further news-list control or new scroll results were found; stopping this bounded scan.')
                    break
                progress('Opening news-list control: ' + control['label'])
                page.locator('[data-patch-calendar-next="true"]').click()
                if not wait_for_new_records(before, settings['timeoutSeconds']):
                    details = failure_details()
                    details['pagination'] = {'control': control['label'], 'distinctRecords': len(discovered), 'advanced': False}
                    raise FeedReadError('A news-list next/load-more control did not reveal any new article IDs. No repeated page was counted as progress.', details)
                pause(min(settings['settleSeconds'], 1) * 1000)
                capture_listing()
            articles.discovery['discoveredURLs'] = sorted(discovered)
            articles.discovery['anchorURLsSeen'] = sorted(anchors & set(discovered))
            articles.discovery['checkpointReached'] = bool(articles.discovery['anchorURLsSeen'])
            articles.discovery['coverage'] = 'current-anchor-overlap' if articles.discovery['checkpointReached'] else 'bounded-unverified'
            articles.discovery['initialPageBudget'] = base_pages
            articles.discovery['maximumPageBudget'] = maximum_pages
            if articles.discovery['endReason'] == 'adaptive-page-limit' and anchors and not articles.discovery['checkpointReached']:
                raise FeedReadError('The extended news scan reached its safety limit before the current-release checkpoint. Refusing to silently truncate coverage.', {'stage': 'listing', 'discovery': articles.discovery})
            progress('Listing coverage: ' + str(articles.discovery['pagesRead']) + ' pages; ' + str(len(discovered)) + ' records; ' + articles.discovery['endReason'] + '.')
            if not discovered:
                raise FeedReadError('No official article links/records found. The publisher layout may have changed.', failure_details())
            seed_count = 0
            for seed in settings.get('seedArticles', []):
                if canonical_article(seed.get('url'), feed) == seed.get('url'):
                    seed_count += seed['url'] not in discovered
                    merge_news_record(discovered, dict(seed, _titleRank=0))
            articles.discovery['additionalKnownArticles'] = seed_count
            listing = list(discovered.values())
            listing.sort(key=lambda item: (bool(RELEASE_HEADLINE.search(item['title'])), item.get('publishedOn') or today()), reverse=True)
            recent = [item for item in listing if not item.get('publishedOn') or parse_day(today()) - parse_day(item['publishedOn']) <= settings['recentDays']]
            if len(recent) > settings['maxArticlesPerGame']:
                raise RuntimeError('The configured article limit would omit recent evidence. Increase maxArticlesPerGame in config.json.')
            progress(f'Reading {len(recent)} recent articles from {len(listing)} discovered records.')
            for article_number, item in enumerate(recent, 1):
                cancel()
                current = item
                active.update({'url': item['url'], 'details': {}, 'network': [], 'pageErrors': [], 'jsonErrors': []})
                progress(f'Article {article_number}/{len(recent)}: {item["title"]}')
                visit(item['url'])
                article = read_article(item)
                articles.append({'gameId': game_id, 'url': item['url'], 'title': article['title'],
                                 'publishedOn': article.get('publishedOn') or discovered.get(item['url'], {}).get('publishedOn'),
                                 'publicationSource': article.get('publicationSource') or discovered.get(item['url'], {}).get('publicationSource'),
                                 'publicationFields': discovered.get(item['url'], {}).get('publicationFields', []),
                                 'bodyHash': digest(article['text']), 'readVia': article['readVia'],
                                 'text': article['text'][:250000]})
                progress(f'Article {article_number}/{len(recent)} read via {article["readVia"]} ({len(article["text"])} characters).')
        except CollectionCancelled:
            progress('Cancelled this feed; no further articles will be opened.')
            raise
        except Exception as error:
            details = error.details if isinstance(error, FeedReadError) else failure_details(current)
            if details.get('expectedTitle'):
                progress('Expected title: ' + details['expectedTitle'])
            if details.get('headings'):
                progress('Observed headings: ' + ' | '.join(details['headings'][:4]))
            progress('Stopped this feed. Page and network evidence will be included in hunt-report.json.')
            if isinstance(error, FeedReadError):
                raise
            raise FeedReadError(str(error), details) from error
        finally:
            progress('Closing the browser for this feed.')
            browser.close()
    if not articles:
        raise FeedReadError('No recent, readable publisher articles were found.')
    return articles


def date_from_match(match, context, declared_zone=None):
    """Convert an explicit publisher timestamp into a Jakarta civil date."""
    if match['iso']:
        year, month, date = map(int, re.split(r'[-/.]', match['iso']))
    else:
        year, month, date = int(match['year']), MONTHS[match['month'].lower()], int(match['day'])
    tail = context[match.end():match.end() + 100]
    clock = re.match(r'\s*(?:at\s+)?(\d{1,2}):(\d{2})(?:\s*([AP]M))?', tail, re.I)
    zone = ZONE.search(tail.split('\n', 1)[0]) or declared_zone
    value = datetime(year, month, date)
    if clock:
        if not zone:
            raise ValueError('A release time has no explicit UTC offset; server time is not a global timezone.')
        offset = (int(zone[2]) * 60 + int(zone[3] or 0)) * (1 if zone[1] == '+' else -1)
        hour = int(clock[1])
        if clock[3]:
            if not 1 <= hour <= 12:
                raise ValueError('Invalid 12-hour release time.')
            hour = hour % 12 + (12 if clock[3].upper() == 'PM' else 0)
        value = value.replace(hour=hour, minute=int(clock[2]), tzinfo=timezone(timedelta(minutes=offset))).astimezone(JAKARTA)
    return value.date().isoformat()


def is_release_announcement(headline):
    """Distinguish a patch announcement from merchandise/events mentioning its number."""
    return bool(re.search(r'update\s+(?:and\s+maintenance\s+)?(?:details|notes|notice|announcement)|'
                          r'(?:update\s+)?maintenance|special program|livestream|preview program|'
                          r'content overview|version.{0,180}\btrailer\b|version.{0,180}\b(?:launches|arrives|releases)\b',
                          headline, re.I) or re.search(r'\bversion\s+\d+\.\d+(?:\s*[“\"].{0,160}[”\"])?\s+announcement\b', headline, re.I))


def parse_article(article, title_style='number'):
    """Return independent naming/date observations without inventing version numbers."""
    headline, body = clean(article['title']), clean(article['text'])
    if NOT_A_RELEASE.search(headline):
        return []
    version = VERSION.search(headline)
    label, title = version[1] if version else None, None
    if title_style == 'title':
        match = re.search(r'[\[“\"]([^\]”\"\n]{3,160})[\]”\"]\s*(?:Version|Special Program|Update)', headline, re.I)
        if match:
            title = clean(match[1])
    results = []
    primary = None
    if label or title:
        primary = {'gameId': article['gameId'], 'label': label, 'title': title, 'date': None,
                   'url': article['url'], 'sourceTitle': headline, 'publishedOn': article.get('publishedOn'),
                   'releaseAnnouncement': is_release_announcement(headline),
                   'explicitNext': bool(re.search(r'\b(?:next|upcoming)\s+(?:version|update)\b', headline, re.I)),
                   'publicationSource': article.get('publicationSource'), 'warnings': []}
        results.append(primary)
    labels = set()
    for group in VERSION_GROUP.finditer(body):
        labels.update(re.findall(r'\d+\.\d+', group[1]))
    for extra in sorted(labels, key=lambda value: tuple(map(int, value.split('.')))):
        if extra != label:
            results.append({'gameId': article['gameId'], 'label': extra, 'title': None, 'date': None,
                            'url': article['url'], 'sourceTitle': headline, 'publishedOn': article.get('publishedOn'),
                            'releaseAnnouncement': False, 'warnings': []})
    if not primary or not primary['releaseAnnouncement'] or re.search(r'special program|livestream|preview program', headline, re.I):
        return results
    # A global timezone declaration is allowed, but a timezone inferred from the user's locale is not.
    global_zone = None
    declaration = re.search(r'(?:all times?|times? (?:are|is)|time zone)[^\n]{0,65}UTC\s*[+−-]\s*\d{1,2}(?::\d{2})?', body, re.I)
    if declaration:
        global_zone = ZONE.search(declaration[0])
    patterns = [
        r'(?:^|\n)[^\w\n]{0,10}(?:version\s+)?update\s+(?:start\s+)?(?:time|date|schedule|period)(?:\s*(?:&|and)\s*time)?\s*[:：]?\s*[\s\S]{0,240}',
        r'(?:begin|start|commence)\s+maintenance[^\n]{0,200}',
        r'(?:update\s+)?maintenance\s+(?:will\s+)?(?:begin|start|commence)(?:s)?\b[^\n]{0,240}',
        r'(?:update\s+)?maintenance\s+(?:time|schedule|period)\s*[:：]?\s*[\s\S]{0,260}',
        r'update\s+(?:will\s+)?(?:begin|start|commence)(?:s)?\b[^\n]{0,180}',
    ]
    if label:
        patterns.append(r'(?:version|ver\.)\s*' + re.escape(label) + r'\b[^\n.!?]{0,140}\b(?:launches|arrives|releases|will be available|will be released|goes live)\b[^\n]{0,150}')
    if title:
        patterns.append(re.escape(title) + r'[^\n.!?]{0,80}\b(?:launches|arrives|releases|will be available|will be released|goes live)\b[^\n]{0,150}')
    candidates = set()
    for pattern in patterns:
        for window in re.finditer(pattern, body, re.I):
            context = window[0]
            found = DATE.search(context)
            if found:
                try:
                    candidates.add(date_from_match(found, context, global_zone))
                except ValueError as error:
                    primary['warnings'].append(str(error))
    if len(candidates) == 1 and not primary['warnings']:
        primary['date'] = next(iter(candidates))
    elif len(candidates) > 1:
        primary['warnings'].append('Conflicting explicit release dates: ' + ', '.join(sorted(candidates)))
    elif re.search(r'maintenance|pre-download\s*&?\s*update notice', headline, re.I) or re.search(r'(?:^|\n)[^\w\n]{0,10}(?:update|maintenance)\s+(?:start\s+)?(?:time|date|schedule|period)\b', body, re.I):
        primary['warnings'].append('A release notice was recognized but no unambiguous release date was parsed.')
    return results
