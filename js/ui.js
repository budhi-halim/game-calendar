import { createCalendar, civilToDay, dayToCivil, daysInMonth, todayInZone, formatDay, monthCells, MONTHS, WEEKDAYS, MAX_DAY, MAX_YEAR, mod } from './logic.js?v=3';

const $ = id => document.getElementById(id);
const root = document.documentElement;
const mediaTheme = window.matchMedia('(prefers-color-scheme: dark)');
const state = { calendar: null, today: todayInZone(), year: 0, month: 0, selected: new Set(), selectedDay: null, focusDay: null, view: 'grid', neighbors: { previous: null, next: null } };
const storageKey = 'patch-calendar.preferences.v1';

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function icon(name, small = false) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.classList.add('icon');
  if (small) svg.classList.add('small');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

function isDark() {
  return root.classList.contains('theme-dark') || (!root.classList.contains('theme-light') && mediaTheme.matches);
}

function updateThemeButton() {
  const label = `Switch to ${isDark() ? 'light' : 'dark'} theme`;
  $('theme-button').setAttribute('aria-label', label);
  $('theme-button').title = label;
  $('theme-icon').setAttribute('href', isDark() ? '#i-sun' : '#i-moon');
}

$('theme-button').addEventListener('click', () => {
  const dark = !isDark();
  root.classList.toggle('theme-dark', dark);
  root.classList.toggle('theme-light', !dark);
  updateThemeButton();
});
mediaTheme.addEventListener('change', updateThemeButton);
updateThemeButton();
$('retry-button').addEventListener('click', () => location.reload());

function loadPreferences() {
  const ids = state.calendar.games.map(game => game.id);
  state.selected = new Set(ids);
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (Array.isArray(saved?.games)) state.selected = new Set(saved.games.filter(id => ids.includes(id)));
    if (saved?.view === 'list') state.view = 'list';
  } catch { /* A blocked or corrupt preference store must not block the calendar. */ }
}

function savePreferences() {
  try { localStorage.setItem(storageKey, JSON.stringify({ games: [...state.selected], view: state.view })); } catch { /* Preferences are optional. */ }
}

function statusName(release) {
  return release.status === 'projected' ? (release.historicalEstimate ? 'Calculated' : 'Projected') : release.verification === 'official' ? 'Confirmed' : 'Archive';
}

function statusClass(release) {
  return release.status === 'projected' ? 'projected' : release.verification === 'official' ? 'official' : 'archive';
}

function statusBadge(release) {
  const badge = node('span', 'release-status');
  badge.append(node('i', `status-symbol ${statusClass(release)}`), node('span', '', statusName(release)));
  return badge;
}

function releaseLabel(release) {
  const game = state.calendar.getGame(release.gameId);
  return `${game.name}${release.label ? ` ${release.label}` : ''}${release.title ? ` — ${release.title}` : ''}`;
}

function visibleIds() { return [...state.selected]; }

function renderFilters() {
  const fragment = document.createDocumentFragment();
  for (const game of state.calendar.games) {
    const label = node('label', 'game-filter');
    label.dataset.game = game.id;
    const input = node('input');
    input.type = 'checkbox';
    input.value = game.id;
    input.checked = state.selected.has(game.id);
    input.setAttribute('aria-label', game.name);
    const check = node('span', 'filter-check');
    check.setAttribute('aria-hidden', 'true');
    check.append(icon('check'));
    label.append(input, check, node('span', 'filter-full', game.name), node('span', 'filter-short', game.shortName));
    input.addEventListener('change', () => {
      input.checked ? state.selected.add(game.id) : state.selected.delete(game.id);
      savePreferences();
      renderCalendar();
      renderUpcoming();
      updateFilterState();
      announce(`${state.selected.size} games selected.`);
    });
    fragment.append(label);
  }
  $('game-filters').replaceChildren(fragment);
  updateFilterState();
}

function updateFilterState() {
  $('all-games').hidden = state.selected.size === state.calendar.games.length;
  $('selection-empty').hidden = state.selected.size !== 0;
}

function showAll() {
  state.selected = new Set(state.calendar.games.map(game => game.id));
  savePreferences();
  renderFilters();
  renderCalendar();
  renderUpcoming();
  announce('All games are visible.');
}

function announce(text) { $('announcement').textContent = text; }

function setMonth(year, month, { selectedDay = null, focus = false } = {}) {
  const ordinal = Math.max(state.calendar.minYear * 12, Math.min(MAX_YEAR * 12 + 8, year * 12 + month - 1));
  state.year = Math.floor(ordinal / 12);
  state.month = mod(ordinal, 12) + 1;
  state.selectedDay = selectedDay;
  state.focusDay = selectedDay ?? civilToDay(state.year, state.month, 1);
  renderCalendar();
  if (focus) focusCell(state.focusDay);
}

function navigateMonth(delta) {
  setMonth(state.year, state.month + delta);
  announce(`${MONTHS[state.month - 1]} ${state.year}.`);
}

function focusCell(day) {
  $('month-grid').querySelector(`[data-day="${day}"]`)?.focus({ preventScroll: true });
}

function selectDay(day) {
  if (day < state.calendar.minDay || day > MAX_DAY) return;
  const date = dayToCivil(day);
  state.year = date.year;
  state.month = date.month;
  state.selectedDay = state.selectedDay === day ? null : day;
  state.focusDay = day;
  renderCalendar();
  focusCell(day);
  const releases = state.calendar.releasesBetween(day, day, visibleIds());
  announce(`${formatDay(day)}. ${releases.length} ${releases.length === 1 ? 'release' : 'releases'}.`);
}

function handleGridKey(event) {
  const button = event.target.closest('[data-day]');
  if (!button) return;
  const current = Number(button.dataset.day);
  const date = dayToCivil(current);
  let target;
  if (event.key === 'ArrowLeft') target = current - 1;
  if (event.key === 'ArrowRight') target = current + 1;
  if (event.key === 'ArrowUp') target = current - 7;
  if (event.key === 'ArrowDown') target = current + 7;
  if (event.key === 'Home') target = current - mod(newWeekday(current) - state.calendar.config.weekStartsOn, 7);
  if (event.key === 'End') target = current + 6 - mod(newWeekday(current) - state.calendar.config.weekStartsOn, 7);
  if (event.key === 'PageUp' || event.key === 'PageDown') {
    const delta = (event.key === 'PageUp' ? -1 : 1) * (event.shiftKey ? 12 : 1);
    const monthIndex = date.year * 12 + date.month - 1 + delta;
    const year = Math.floor(monthIndex / 12);
    const month = mod(monthIndex, 12) + 1;
    target = civilToDay(year, month, Math.min(date.day, daysInMonth(year, month)));
  }
  if (target === undefined) return;
  event.preventDefault();
  target = Math.max(state.calendar.minDay, Math.min(MAX_DAY, target));
  const next = dayToCivil(target);
  state.year = next.year;
  state.month = next.month;
  state.focusDay = target;
  state.selectedDay = null;
  renderCalendar();
  focusCell(target);
}

function newWeekday(day) { return mod(day + 4, 7); }

function renderCalendar() {
  const { calendar, year, month } = state;
  $('month-title').textContent = `${MONTHS[month - 1]} ${year}`;
  $('month-button').setAttribute('aria-label', `${MONTHS[month - 1]} ${year}. Choose another month or year.`);
  $('previous-month').disabled = year === calendar.minYear && month === 1;
  $('next-month').disabled = year === MAX_YEAR && month === 9;
  $('month-grid-wrap').hidden = state.view !== 'grid';
  $('grid-view').setAttribute('aria-pressed', String(state.view === 'grid'));
  $('list-view').setAttribute('aria-pressed', String(state.view === 'list'));
  const first = civilToDay(year, month, 1);
  const last = Math.min(MAX_DAY, first + daysInMonth(year, month) - 1);
  const allCells = monthCells(year, month, calendar.config.weekStartsOn);
  const weeks = Math.ceil((first - allCells[0].epochDay + last - first + 1) / 7);
  const cells = allCells.slice(0, weeks * 7);
  const rangeStart = Math.max(calendar.minDay, cells[0].epochDay);
  const rangeEnd = Math.min(MAX_DAY, cells.at(-1).epochDay);
  const releases = calendar.releasesBetween(rangeStart, rangeEnd, visibleIds());
  const eventMap = new Map();
  for (const release of releases) {
    if (!eventMap.has(release.day)) eventMap.set(release.day, []);
    eventMap.get(release.day).push(release);
  }
  const inMonth = state.focusDay !== null && state.focusDay >= first && state.focusDay <= last;
  const focusDay = inMonth ? state.focusDay : (state.today >= first && state.today <= last ? state.today : first);
  const fragment = document.createDocumentFragment();
  for (let week = 0; week < weeks; week++) {
    const row = node('div', 'calendar-row');
    row.setAttribute('role', 'row');
    for (const cell of cells.slice(week * 7, week * 7 + 7)) {
      const day = cell.epochDay;
      const events = eventMap.get(day) || [];
      const wrapper = node('div', 'calendar-cell');
      wrapper.setAttribute('role', 'gridcell');
      wrapper.setAttribute('aria-selected', String(state.selectedDay === day));
      const button = node('button', 'day-button');
      button.dataset.day = day;
      button.tabIndex = day === focusDay ? 0 : -1;
      button.disabled = day < calendar.minDay || day > MAX_DAY;
      if (cell.month !== month) button.classList.add('other-month');
      if (day === state.today) {
        button.classList.add('today');
        button.setAttribute('aria-current', 'date');
      }
      const labels = events.map(release => `${releaseLabel(release)}, ${statusName(release)}`).join('; ');
      button.setAttribute('aria-label', `${formatDay(day, 'weekday')}${day === state.today ? ', today' : ''}${labels ? `. ${labels}` : '. No releases'}`);
      button.append(node('span', 'day-number', cell.day));
      const markers = node('span', 'day-markers');
      markers.setAttribute('aria-hidden', 'true');
      for (const release of events) {
        const game = calendar.getGame(release.gameId);
        const marker = node('span', `day-marker ${statusClass(release)}`);
        marker.dataset.game = release.gameId;
        marker.title = releaseLabel(release);
        marker.append(node('span', 'marker-game', game.shortName));
        if (release.label || release.title) marker.append(node('span', `marker-version${release.label ? '' : ' marker-title'}`, release.label || release.title));
        markers.append(marker);
      }
      button.append(markers);
      wrapper.append(button);
      row.append(wrapper);
    }
    fragment.append(row);
  }
  $('month-grid').replaceChildren(fragment);
  const monthReleases = releases.filter(release => release.day >= first && release.day <= last);
  renderAgenda(state.selectedDay === null ? monthReleases : releases.filter(release => release.day === state.selectedDay));
  updateReleaseNavigation();
}

function updateReleaseNavigation() {
  const first = civilToDay(state.year, state.month, 1);
  const previousBefore = state.selectedDay ?? first;
  const nextFrom = state.selectedDay === null ? first : state.selectedDay + 1;
  const previous = visibleIds().map(id => state.calendar.previousRelease(id, previousBefore)).filter(Boolean).sort((a, b) => b.day - a.day)[0];
  const next = visibleIds().flatMap(id => state.calendar.nextReleases(id, nextFrom, 1)).sort((a, b) => a.day - b.day)[0];
  state.neighbors = { previous, next };
  for (const [direction, release] of Object.entries(state.neighbors)) {
    const button = $(`${direction}-release`);
    button.disabled = !release;
    button.title = release ? `${releaseLabel(release)} · ${formatDay(release.day)}` : 'No release in this direction';
  }
}

function revealRelease(release) {
  if (!release) return;
  const date = dayToCivil(release.day);
  setMonth(date.year, date.month, { selectedDay: release.day });
  const heading = state.view === 'grid' ? $('month-button') : $('agenda-heading');
  heading.scrollIntoView({ block: 'start', behavior: 'auto' });
  if (state.view === 'grid') focusCell(release.day);
  else heading.focus({ preventScroll: true });
  announce(`${releaseLabel(release)}. ${formatDay(release.day)}. ${statusName(release)}.`);
}

function renderAgenda(releases) {
  const selected = state.selectedDay !== null;
  $('agenda-title').textContent = selected ? formatDay(state.selectedDay) : 'This month';
  $('clear-day').hidden = !selected;
  $('month-count').hidden = selected;
  $('month-count').textContent = `${releases.length} ${releases.length === 1 ? 'release' : 'releases'}`;
  const fragment = document.createDocumentFragment();
  for (const release of releases) {
    const game = state.calendar.getGame(release.gameId);
    const date = dayToCivil(release.day);
    const button = node('button', 'agenda-item');
    button.dataset.game = game.id;
    button.setAttribute('aria-label', `${releaseLabel(release)}, ${formatDay(release.day)}, ${statusName(release)}. View details.`);
    const tile = node('span', 'date-tile');
    tile.append(node('strong', '', date.day), node('span', '', MONTHS[date.month - 1].slice(0, 3)));
    const copy = node('span', 'release-copy');
    const title = node('strong');
    const gameName = node('span');
    gameName.append(node('i', 'game-dot'), document.createTextNode(game.name));
    title.append(gameName);
    if (release.label) title.append(node('span', 'release-version', release.label));
    copy.append(title, node('span', 'release-secondary', release.title || WEEKDAYS[newWeekday(release.day)]));
    button.append(tile, copy, statusBadge(release));
    button.addEventListener('click', () => showRelease(release));
    fragment.append(button);
  }
  if (!releases.length) fragment.append(node('p', 'agenda-empty', state.selected.size === 0 ? 'Select a game to see its dates.' : selected ? 'No updates on this date.' : 'No updates this month.'));
  $('agenda').replaceChildren(fragment);
}

function attachArtwork(button, game) {
  button.append(node('span', 'art-fallback', game.shortName.toUpperCase()));
  const art = game.artwork;
  if (!art?.remote && !art?.local) return;
  const image = node('img', 'game-art');
  image.alt = '';
  image.loading = 'lazy';
  image.decoding = 'async';
  image.draggable = false;
  image.referrerPolicy = 'no-referrer';
  let fallbackUsed = !art.local;
  image.addEventListener('error', () => {
    if (!fallbackUsed && art.remote) { fallbackUsed = true; image.src = art.remote; }
    else image.remove();
  });
  image.src = art.local || art.remote;
  button.append(image);
}

function renderUpcoming() {
  const fragment = document.createDocumentFragment();
  const count = Math.max(1, Math.min(10, state.calendar.config.upcomingCount || 3));
  for (const game of state.calendar.games.filter(game => state.selected.has(game.id))) {
    const releases = state.calendar.nextReleases(game.id, state.today, count);
    if (!releases.length) continue;
    const card = node('article', 'upcoming-card');
    card.dataset.game = game.id;
    const first = releases[0];
    const feature = node('button', 'feature-release');
    feature.setAttribute('aria-label', `${releaseLabel(first)}, ${formatDay(first.day)}, ${statusName(first)}. View details.`);
    attachArtwork(feature, game);
    feature.append(node('span', 'feature-game', game.name));
    const date = node('span', 'feature-date');
    const distance = first.day - state.today;
    date.append(node('strong', '', formatDay(first.day, 'short')), node('span', '', distance === 0 ? 'Today' : distance === 1 ? 'Tomorrow' : `in ${distance} days`));
    const bottom = node('span', 'feature-bottom');
    bottom.append(statusBadge(first));
    if (first.label) bottom.append(node('span', 'feature-name', `Version ${first.label}`));
    else if (first.title) bottom.append(node('span', 'feature-name', first.title));
    else bottom.append(node('span', 'feature-name', WEEKDAYS[newWeekday(first.day)]));
    feature.append(date, bottom);
    feature.addEventListener('click', () => showRelease(first));
    card.append(feature);
    if (releases.length > 1) {
      const later = node('div', 'upcoming-later');
      for (const release of releases.slice(1)) {
        const button = node('button', 'later-release');
        const sameYear = dayToCivil(release.day).year === dayToCivil(state.today).year;
        button.append(node('i', `status-symbol ${statusClass(release)}`), node('span', 'later-date', formatDay(release.day, sameYear ? 'short' : 'long')), node('span', 'later-meta', release.label ? `v${release.label} · ${statusName(release)}` : release.title ? `${release.title} · ${statusName(release)}` : statusName(release)), icon('right', true));
        button.setAttribute('aria-label', `${releaseLabel(release)}, ${formatDay(release.day)}, ${statusName(release)}. View details.`);
        button.addEventListener('click', () => showRelease(release));
        later.append(button);
      }
      card.append(later);
    }
    fragment.append(card);
  }
  if (state.selected.size === 0) fragment.append(node('div', 'upcoming-empty', 'Select a game to show its next releases.'));
  $('upcoming-games').replaceChildren(fragment);
}

function sourceLink(sourceId) {
  const source = state.calendar.sources[sourceId];
  const link = node('a', 'source-link');
  link.href = source.url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  const title = node('span');
  title.append(node('span', '', source.title), node('span', 'source-kind', source.kind === 'official' ? 'Official publisher material' : 'Secondary archive / reporting'));
  link.append(title, icon('arrow', true));
  return link;
}

function showRelease(release) {
  const game = state.calendar.getGame(release.gameId);
  const body = $('details-body');
  body.dataset.game = game.id;
  const title = node('h2', '', release.label ? `Version ${release.label}` : release.title || 'Update');
  title.id = 'details-title';
  const elements = [node('p', 'details-game', game.name), title];
  if (release.label && release.title) elements.push(node('p', 'detail-copy', release.title));
  elements.push(node('p', 'details-date', `${formatDay(release.day, 'weekday')} · Jakarta (UTC+7)`));
  const badges = node('div', 'details-badges');
  badges.append(statusBadge(release));
  if (release.label || release.title) badges.append(node('span', 'release-status', release.nameVerification === 'official' ? 'Name confirmed' : 'Archived name'));
  elements.push(badges);
  const locate = node('button', 'button details-locate', 'Show in calendar');
  locate.addEventListener('click', () => {
    $('details-dialog').close();
    revealRelease(release);
  });
  elements.push(locate);
  const copy = node('div', 'detail-copy');
  if (release.status === 'projected') {
    copy.append(node('p', '', release.historicalEstimate ? 'This historical date is calculated. No confirmed date override is stored for this release.' : 'This date is a projection. It has not been confirmed by an announcement in this dataset.'));
    copy.append(node('p', '', `Calculated from the ${formatDay(state.calendar.release(game.id, release.anchorSequence).day)} anchor, using ${game.cadenceWeeks}-week intervals and ${game.rounding} ${WEEKDAYS[game.preferredWeekday]} alignment. A new override will shift subsequent projections.`));
  } else {
    copy.append(node('p', '', release.verification === 'official' ? 'This date is pinned to publisher material. It is kept exactly as recorded, even when it falls on an unusual weekday.' : 'This date is pinned to a historical archive or secondary report. It was not independently reverified here against an original publisher maintenance notice.'));
  }
  if (!release.label && !release.title) copy.append(node('p', '', 'No confirmed version name is stored. The calendar does not guess the next minor or major number.'));
  if (release.notes) copy.append(node('p', '', release.notes));
  elements.push(copy);
  const sourceIds = new Set([...release.sources, ...release.nameSources]);
  if (release.status === 'projected') for (const id of game.anchors.find(anchor => anchor.sequence === release.anchorSequence).sources) sourceIds.add(id);
  if (sourceIds.size) {
    elements.push(node('h3', '', release.status === 'projected' ? 'Anchor / naming sources' : 'Sources'));
    for (const id of sourceIds) elements.push(sourceLink(id));
  }
  const id = node('div', 'detail-id');
  id.append(document.createTextNode('Stable release ID: '), node('code', '', release.id), node('br'), document.createTextNode(`Game “${game.id}” · sequence ${release.sequence}.`));
  elements.push(id);
  body.replaceChildren(...elements);
  $('details-dialog').showModal();
}

function buildSources() {
  const body = $('sources-body');
  const intro = node('div', 'detail-copy');
  intro.append(node('p', '', `Snapshot checked ${formatDay(state.calendar.checkedDay)}. Dates and names are separate records. Scheduled collection runs outside this webpage; projections are calculated here.`));
  const rules = node('div', 'rule-table');
  for (const game of state.calendar.games) {
    const row = node('div', 'rule-row');
    row.dataset.game = game.id;
    row.append(node('span', '', game.shortName), node('span', '', `${game.cadenceWeeks} weeks · ${WEEKDAYS[game.preferredWeekday]} · ${game.rounding}`));
    rules.append(row);
  }
  const explanation = node('div', 'detail-copy');
  for (const text of [
    'Overrides re-anchor the timeline. They are never moved onto a preferred weekday. Only calculated dates are rounded; “nearest” can move a prediction up to three days earlier or later.',
    'A release’s sequence number is permanent. Version labels are added separately only when supported by a source. No minor-number continuation or major-version reset is assumed.',
    `Calendar navigation runs from January ${state.calendar.minYear} through 13 September 275760. It calculates the visible month on demand instead of generating every release in advance.`,
    'The Up next panel starts from today in Jakarta and includes an update scheduled for today. It has no maintenance-hour information; it does not claim the update is already playable.'
  ]) explanation.append(node('p', '', text));
  const coverage = node('div', 'detail-copy coverage');
  coverage.append(node('h3', '', 'Research coverage'));
  for (const game of state.calendar.games) coverage.append(node('p', '', `${game.shortName}: ${state.calendar.sourceFile.coverage[game.id]}`));
  for (const text of state.calendar.sourceFile.caveats.slice(0, 3)) coverage.append(node('p', '', text));
  const citations = node('details', 'source-section');
  citations.append(node('summary', '', `Source index (${Object.keys(state.calendar.sources).length})`));
  for (const id of Object.keys(state.calendar.sources)) citations.append(sourceLink(id));
  const artwork = node('details', 'source-section');
  artwork.append(node('summary', '', 'Artwork credits'));
  const note = node('p', 'detail-copy', 'Official public promotional images are used decoratively. Local artwork is used first, with the official remote image as a fallback. Artwork is not covered by an open-source license. Rights remain with the respective publishers.');
  artwork.append(note);
  for (const game of state.calendar.games) {
    artwork.append(node('p', 'detail-copy', `${game.artwork.credit}. Local-first artwork.`), sourceLink(game.artwork.source));
  }
  const collection = state.calendar.sourceFile.lastHunt;
  if (collection) {
    const summary = node('div', 'detail-copy collection-summary');
    summary.append(node('h3', '', 'Last successful collection'));
    for (const game of state.calendar.games) {
      const info = collection.games?.[game.id];
      if (!info) continue;
      summary.append(node('p', '', `${game.shortName}: ${info.articlesRead} articles checked; ${info.datedObservations} date observations. ${info.missingPublicationDates} articles without publication metadata.`));
    }
    if (collection.pendingNames) summary.append(node('p', '', `${collection.pendingNames} naming observations still need release-order evidence. Unconfirmed dates remain projected.`));
    coverage.append(summary);
  }
  body.replaceChildren(intro, rules, explanation, coverage, citations, artwork);
}

function openSources() { if (state.calendar) $('sources-dialog').showModal(); }

function openJump() {
  $('jump-month').value = String(state.month);
  $('jump-year').value = String(state.year);
  $('jump-error').textContent = '';
  $('jump-dialog').showModal();
  $('jump-year').focus();
  $('jump-year').select();
}

function changeView(view) {
  state.view = view;
  state.selectedDay = null;
  savePreferences();
  renderCalendar();
}

function refreshToday() {
  if (!state.calendar) return;
  const today = todayInZone(state.calendar.config.timeZone);
  if (today === state.today) return;
  const old = dayToCivil(state.today);
  const followToday = state.year === old.year && state.month === old.month && state.selectedDay === null;
  state.today = today;
  if (followToday) {
    const current = dayToCivil(today);
    state.year = current.year;
    state.month = current.month;
  }
  updateTodayLabel();
  updateDataStatus();
  renderCalendar();
  renderUpcoming();
}

function updateTodayLabel() {
  $('today-label').textContent = `${formatDay(state.today, 'weekday').toUpperCase()}`;
}

function updateDataStatus() {
  const pending = state.calendar.sourceFile.lastHunt?.pendingNames || 0;
  $('data-checked').textContent = `Data checked ${formatDay(state.calendar.checkedDay)}${state.today - state.calendar.checkedDay > 7 ? ' · Check overdue' : ''}${pending ? ` · ${pending} names awaiting placement` : ''}`;
}

function attachEvents() {
  $('all-games').addEventListener('click', showAll);
  $('empty-show-all').addEventListener('click', showAll);
  $('month-grid').addEventListener('click', event => {
    const button = event.target.closest('[data-day]');
    if (button && !button.disabled) {
      const day = Number(button.dataset.day);
      selectDay(day);
      const releases = state.calendar.releasesBetween(day, day, visibleIds());
      if (releases.length === 1) showRelease(releases[0]);
      else if (releases.length > 1) {
        $('agenda-heading').scrollIntoView({ block: 'center', behavior: 'auto' });
        $('agenda-heading').focus({ preventScroll: true });
      }
    }
  });
  $('month-grid').addEventListener('keydown', handleGridKey);
  $('previous-month').addEventListener('click', () => navigateMonth(-1));
  $('next-month').addEventListener('click', () => navigateMonth(1));
  $('today-button').addEventListener('click', () => {
    refreshToday();
    const current = dayToCivil(state.today);
    setMonth(current.year, current.month);
    state.focusDay = state.today;
    renderCalendar();
    announce(`Showing ${MONTHS[current.month - 1]} ${current.year}. Today is ${formatDay(state.today)} in Jakarta.`);
  });
  $('month-button').addEventListener('click', openJump);
  $('previous-release').addEventListener('click', () => revealRelease(state.neighbors.previous));
  $('next-release').addEventListener('click', () => revealRelease(state.neighbors.next));
  $('grid-view').addEventListener('click', () => changeView('grid'));
  $('list-view').addEventListener('click', () => changeView('list'));
  $('clear-day').addEventListener('click', () => { state.selectedDay = null; renderCalendar(); });
  $('sources-button').addEventListener('click', openSources);
  $('footer-sources').addEventListener('click', openSources);
  $('jump-form').addEventListener('submit', event => {
    event.preventDefault();
    const year = Number($('jump-year').value);
    const month = Number($('jump-month').value);
    if (!Number.isInteger(year) || year < state.calendar.minYear || year > MAX_YEAR || (year === MAX_YEAR && month > 9)) {
      $('jump-error').textContent = `Choose a date from January ${state.calendar.minYear} to September ${MAX_YEAR}.`;
      return;
    }
    $('jump-dialog').close();
    setMonth(year, month);
    announce(`Showing ${MONTHS[month - 1]} ${year}.`);
  });
  for (const dialog of document.querySelectorAll('dialog')) {
    dialog.addEventListener('click', event => {
      if (event.target.closest('[data-close]')) dialog.close();
      if (event.target === dialog) {
        const bounds = dialog.getBoundingClientRect();
        if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
      }
    });
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshToday(); });
  window.addEventListener('pageshow', refreshToday);
  window.setInterval(refreshToday, 60000);
}

async function readJSON(name) {
  try {
    const response = await fetch(`data/${name}.json`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } catch (error) { throw new Error(`Unable to read data/${name}.json: ${error.message}. Check that all data files are uploaded and contain valid JSON.`); }
}

async function init() {
  const [config, overrides, versions, sources] = await Promise.all(['config', 'overrides', 'versions', 'sources'].map(readJSON));
  state.calendar = createCalendar(config, overrides, versions, sources);
  if (typeof config.title === 'string' && config.title.trim()) {
    document.title = config.title;
    document.querySelector('.brand > span:last-child').textContent = config.title;
    document.querySelector('.brand').setAttribute('aria-label', `${config.title} home`);
  }
  state.today = todayInZone(config.timeZone);
  const today = dayToCivil(state.today);
  state.year = Math.max(config.minYear, today.year);
  state.month = today.year < config.minYear ? 1 : today.month;
  state.focusDay = state.today;
  loadPreferences();
  for (let index = 0; index < 7; index++) $('weekdays').append(node('span', '', WEEKDAYS[(config.weekStartsOn + index) % 7].slice(0, 3)));
  for (let index = 0; index < 12; index++) {
    const option = node('option', '', MONTHS[index]);
    option.value = index + 1;
    $('jump-month').append(option);
  }
  $('jump-year').min = String(config.minYear);
  $('jump-help').textContent = `${config.minYear}–${MAX_YEAR}. The final supported date is 13 September ${MAX_YEAR}.`;
  updateDataStatus();
  updateTodayLabel();
  attachEvents();
  renderFilters();
  renderCalendar();
  renderUpcoming();
  buildSources();
  $('loading').hidden = true;
  $('app').hidden = false;
}

init().catch(error => {
  console.error(error);
  $('loading').hidden = true;
  $('error-message').textContent = error.message;
  $('load-error').hidden = false;
});
