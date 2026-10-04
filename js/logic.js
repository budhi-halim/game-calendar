/** Date-only scheduling. No DOM, network, local time, or inferred version numbers. */
export const MAX_DAY = 100000000;
export const MAX_YEAR = 275760;
export const MONTHS = Object.freeze(['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']);
export const WEEKDAYS = Object.freeze(['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']);
export const mod = (value, divisor) => ((value % divisor) + divisor) % divisor;

// Proleptic Gregorian civil-date arithmetic; UTC epoch day 0 is 1970-01-01.
// Integer arithmetic avoids DST, the Date constructor's 0–99 year shortcut,
// and invalid Date objects in the final partially supported month.
export function civilToDay(year, month, day) {
  const y = year - (month <= 2 ? 1 : 0);
  const era = Math.floor(y / 400);
  const yearOfEra = y - era * 400;
  const dayOfYear = Math.floor((153 * (month + (month > 2 ? -3 : 9)) + 2) / 5) + day - 1;
  return era * 146097 + yearOfEra * 365 + Math.floor(yearOfEra / 4) - Math.floor(yearOfEra / 100) + dayOfYear - 719468;
}

export function dayToCivil(day) {
  const shifted = day + 719468;
  const era = Math.floor(shifted / 146097);
  const dayOfEra = shifted - era * 146097;
  const yearOfEra = Math.floor((dayOfEra - Math.floor(dayOfEra / 1460) + Math.floor(dayOfEra / 36524) - Math.floor(dayOfEra / 146096)) / 365);
  let year = yearOfEra + era * 400;
  const dayOfYear = dayOfEra - (365 * yearOfEra + Math.floor(yearOfEra / 4) - Math.floor(yearOfEra / 100));
  const mp = Math.floor((5 * dayOfYear + 2) / 153);
  const date = dayOfYear - Math.floor((153 * mp + 2) / 5) + 1;
  const month = mp + (mp < 10 ? 3 : -9);
  year += month <= 2 ? 1 : 0;
  return { year, month, day: date };
}

export const weekday = day => mod(day + 4, 7);
export const daysInMonth = (year, month) => civilToDay(year, month + 1, 1) - civilToDay(year, month, 1);

export function toISO(day) {
  const value = dayToCivil(day);
  const year = value.year <= 9999 ? String(value.year).padStart(4, '0') : `+${String(value.year).padStart(6, '0')}`;
  return `${year}-${String(value.month).padStart(2, '0')}-${String(value.day).padStart(2, '0')}`;
}

export function parseISO(value) {
  const match = /^(\d{4}|\+\d{6})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) throw new Error(`Invalid date "${value}". Use YYYY-MM-DD (or +YYYYYY-MM-DD beyond year 9999).`);
  const [year, month, day] = match.slice(1).map(Number);
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) throw new Error(`Invalid calendar date "${value}".`);
  const result = civilToDay(year, month, day);
  if (result > MAX_DAY) throw new Error(`Date "${value}" exceeds 13 September 275760.`);
  return result;
}

export function todayInZone(timeZone = 'Asia/Jakarta', now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-US-u-ca-gregory-nu-latn', { timeZone, year: 'numeric', month: 'numeric', day: 'numeric' }).formatToParts(now);
  const read = type => Number(parts.find(part => part.type === type).value);
  return civilToDay(read('year'), read('month'), read('day'));
}

export function formatDay(day, mode = 'long') {
  const date = dayToCivil(day);
  if (mode === 'short') return `${date.day} ${MONTHS[date.month - 1].slice(0, 3)}`;
  if (mode === 'weekday') return `${WEEKDAYS[weekday(day)]}, ${date.day} ${MONTHS[date.month - 1]} ${date.year}`;
  return `${date.day} ${MONTHS[date.month - 1]} ${date.year}`;
}

export function alignWeekday(day, intended, mode = 'nearest') {
  const forward = mod(intended - weekday(day), 7);
  const delta = mode === 'next' ? forward : mode === 'previous' ? (forward === 0 ? 0 : forward - 7) : (forward > 3 ? forward - 7 : forward);
  return day + delta;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function sequenceIndex(value, context) {
  assert(/^\d+$/.test(String(value)) && Number.isSafeInteger(Number(value)), `${context}: sequence must be a nonnegative integer.`);
  return Number(value);
}

function validateSources(ids, sources, context) {
  assert(Array.isArray(ids) && ids.length > 0, `${context}: add at least one source ID.`);
  for (const id of ids) assert(sources[id], `${context}: source "${id}" is missing from sources.json.`);
}

export function createCalendar(config, overrides, versions, sourceFile) {
  for (const [name, file] of Object.entries({ config, overrides, versions, sources: sourceFile })) {
    assert(file && file.schemaVersion === 1, `${name}.json: unsupported or missing schemaVersion.`);
    assert(file.datasetId === config.datasetId, `${name}.json belongs to a different dataset. Reload once all data files have deployed.`);
  }
  assert(config.timeZone === 'Asia/Jakarta', 'This dataset uses date-only values in Asia/Jakarta. Keep timeZone set to Asia/Jakarta.');
  assert(Number.isInteger(config.weekStartsOn) && config.weekStartsOn >= 0 && config.weekStartsOn <= 6, 'weekStartsOn must be 0–6 (Sunday–Saturday).');
  const minYear = config.minYear;
  assert(Number.isInteger(minYear) && minYear >= 1 && minYear <= MAX_YEAR, 'minYear is invalid.');
  const minDay = civilToDay(minYear, 1, 1);
  const checkedDay = parseISO(sourceFile.checkedOn);
  const sources = sourceFile.sources;
  assert(sources && typeof sources === 'object', 'sources.json requires a sources object.');
  for (const [id, source] of Object.entries(sources)) {
    assert(typeof source.title === 'string' && source.title.length > 0, `Source ${id} has no title.`);
    let url;
    try { url = new URL(source.url); } catch { throw new Error(`Source ${id} has an invalid URL.`); }
    assert(url.protocol === 'https:', `Source ${id} must use HTTPS.`);
  }
  assert(Array.isArray(config.games), 'config.json requires a games array.');
  assert(Number.isInteger(config.upcomingCount) && config.upcomingCount >= 1 && config.upcomingCount <= 10, 'upcomingCount must be an integer from 1 to 10.');
  const ids = new Set();
  const games = config.games.map(game => {
    assert(/^[a-z][a-z0-9-]*$/.test(game.id) && !ids.has(game.id), `Invalid or duplicate game ID: ${game.id}`);
    ids.add(game.id);
    assert(typeof game.name === 'string' && game.name.trim() && typeof game.shortName === 'string' && game.shortName.trim(), `${game.id}: name and shortName are required.`);
    assert(Number.isInteger(game.cadenceWeeks) && game.cadenceWeeks >= 1 && game.cadenceWeeks <= 5200, `${game.id}: cadenceWeeks must be an integer from 1 to 5200.`);
    assert(Number.isInteger(game.preferredWeekday) && game.preferredWeekday >= 0 && game.preferredWeekday <= 6, `${game.id}: preferredWeekday must be 0–6.`);
    assert(['nearest', 'next', 'previous'].includes(game.rounding), `${game.id}: unsupported rounding rule.`);
    const raw = overrides.games[game.id];
    assert(Array.isArray(raw) && raw.length > 0, `${game.id}: at least the launch date is required in overrides.json.`);
    const seqs = new Set();
    const anchors = raw.map(record => {
      const sequence = sequenceIndex(record.sequence, game.id);
      assert(!seqs.has(sequence), `${game.id}: duplicate sequence ${sequence}.`);
      seqs.add(sequence);
      const day = parseISO(record.date);
      validateSources(record.sources, sources, `${game.id} #${sequence}`);
      if (record.verification === 'official') assert(record.sources.some(id => sources[id].kind === 'official'), `${game.id} #${sequence}: official verification requires official source evidence.`);
      assert(['official', 'archive'].includes(record.verification), `${game.id} #${sequence}: verification must be official or archive.`);
      assert(record.status === 'confirmed', `${game.id} #${sequence}: overrides must be confirmed, not predictions.`);
      return { ...record, sequence, day };
    }).sort((a, b) => a.sequence - b.sequence);
    assert(anchors[0].sequence === 0, `${game.id}: sequence 0 must be the launch anchor.`);
    assert(anchors[0].day >= minDay, `${game.id}: launch precedes minYear.`);
    const period = game.cadenceWeeks * 7;
    const firstAfter = anchor => alignWeekday(anchor.day + period, game.preferredWeekday, game.rounding);
    for (let index = 1; index < anchors.length; index++) {
      const previous = anchors[index - 1];
      const next = anchors[index];
      assert(next.day > previous.day, `${game.id}: sequence ${next.sequence} must be after sequence ${previous.sequence}.`);
      const gaps = next.sequence - previous.sequence - 1;
      if (gaps > 0) assert(firstAfter(previous) + (gaps - 1) * period < next.day, `${game.id}: predictions before sequence ${next.sequence} overlap its confirmed date. Add the missing earlier override(s).`);
    }
    const names = new Map();
    for (const [key, value] of Object.entries(versions.games[game.id] || {})) {
      const sequence = sequenceIndex(key, game.id);
      assert(typeof value.label === 'string' || typeof value.title === 'string', `${game.id} #${sequence}: version record requires a label or title.`);
      if (value.label) assert(typeof value.label === 'string', `${game.id} #${sequence}: version labels must be strings, not decimals.`);
      validateSources(value.sources, sources, `${game.id} version #${sequence}`);
      names.set(sequence, value);
    }
    return { ...game, period, anchors, names, firstAfter };
  });
  assert(games.length > 0, 'config.json must include at least one game.');
  for (const id of Object.keys(overrides.games)) assert(ids.has(id), `Unknown game "${id}" in overrides.json.`);
  for (const id of Object.keys(versions.games)) assert(ids.has(id), `Unknown game "${id}" in versions.json.`);

  function makeRelease(game, sequence, day, anchor, exact) {
    if (day > MAX_DAY) return null;
    const name = game.names.get(sequence);
    return {
      id: `${game.id}-${sequence}`, gameId: game.id, sequence, day, date: toISO(day),
      label: name?.label || null, title: name?.title || null,
      nameVerification: name?.verification || (name?.sources?.some(id => sources[id]?.kind === 'official') ? 'official' : name ? 'archive' : null),
      status: exact ? 'confirmed' : 'projected',
      verification: exact ? anchor.verification || 'archive' : null,
      historicalEstimate: !exact && day <= checkedDay,
      anchorSequence: anchor.sequence, anchorDate: anchor.date,
      notes: exact ? anchor.notes || '' : '',
      sources: exact ? anchor.sources : [], nameSources: name?.sources || []
    };
  }

  function getGame(id) {
    const game = games.find(entry => entry.id === id);
    assert(game, `Unknown game ${id}.`);
    return game;
  }

  function release(id, sequence) {
    const game = getGame(id);
    if (!Number.isSafeInteger(sequence) || sequence < 0) return null;
    // Binary-search the most recent confirmed anchor, not the display name.
    let low = 0;
    let high = game.anchors.length - 1;
    while (low < high) {
      const mid = Math.ceil((low + high) / 2);
      if (game.anchors[mid].sequence <= sequence) low = mid;
      else high = mid - 1;
    }
    const anchor = game.anchors[low];
    const exact = sequence === anchor.sequence;
    const day = exact ? anchor.day : game.firstAfter(anchor) + (sequence - anchor.sequence - 1) * game.period;
    return makeRelease(game, sequence, day, anchor, exact);
  }

  function releasesBetween(start, end, selectedIds = games.map(game => game.id)) {
    assert(Number.isInteger(start) && Number.isInteger(end), 'Calendar bounds must be integer epoch days.');
    assert(end - start <= 36600, 'Request at most 100 years at once. Use nextReleases for distant events.');
    const selected = new Set(selectedIds);
    const result = [];
    for (const game of games.filter(entry => selected.has(entry.id))) {
      for (let index = 0; index < game.anchors.length; index++) {
        const anchor = game.anchors[index];
        const next = game.anchors[index + 1];
        if (anchor.day >= start && anchor.day <= end) result.push(makeRelease(game, anchor.sequence, anchor.day, anchor, true));
        const first = game.firstAfter(anchor);
        const lower = Math.max(0, Math.ceil((start - first) / game.period));
        const upper = Math.min(Math.floor((Math.min(end, MAX_DAY) - first) / game.period), next ? next.sequence - anchor.sequence - 2 : Infinity);
        for (let offset = lower; offset <= upper; offset++) result.push(makeRelease(game, anchor.sequence + 1 + offset, first + offset * game.period, anchor, false));
      }
    }
    return result.filter(Boolean).sort((a, b) => a.day - b.day || games.findIndex(game => game.id === a.gameId) - games.findIndex(game => game.id === b.gameId));
  }

  function nextReleases(id, from, count = 3) {
    assert(Number.isInteger(from) && Number.isInteger(count) && count >= 1 && count <= 100, 'Invalid upcoming-release request.');
    const game = getGame(id);
    const result = [];
    for (let index = 0; index < game.anchors.length && result.length < count; index++) {
      const anchor = game.anchors[index];
      const next = game.anchors[index + 1];
      if (anchor.day >= from) result.push(makeRelease(game, anchor.sequence, anchor.day, anchor, true));
      const first = game.firstAfter(anchor);
      let offset = Math.max(0, Math.ceil((from - first) / game.period));
      const upper = next ? next.sequence - anchor.sequence - 2 : Math.floor((MAX_DAY - first) / game.period);
      while (offset <= upper && result.length < count) {
        const value = makeRelease(game, anchor.sequence + 1 + offset, first + offset * game.period, anchor, false);
        if (!value) break;
        result.push(value);
        offset++;
      }
    }
    return result.slice(0, count);
  }

  function previousRelease(id, before) {
    assert(Number.isInteger(before), 'Invalid previous-release request.');
    const game = getGame(id);
    let best = null;
    for (let index = 0; index < game.anchors.length; index++) {
      const anchor = game.anchors[index];
      if (anchor.day >= before) break;
      best = makeRelease(game, anchor.sequence, anchor.day, anchor, true);
      const next = game.anchors[index + 1];
      const first = game.firstAfter(anchor);
      const upper = Math.min(Math.floor((Math.min(before - 1, MAX_DAY) - first) / game.period), next ? next.sequence - anchor.sequence - 2 : Infinity);
      if (upper >= 0) best = makeRelease(game, anchor.sequence + 1 + upper, first + upper * game.period, anchor, false);
    }
    return best;
  }

  return { games, config, sources, sourceFile, minDay, minYear, checkedDay, maxDay: MAX_DAY, getGame, release, releasesBetween, nextReleases, previousRelease };
}

export function monthCells(year, month, startsOn = 1) {
  const first = civilToDay(year, month, 1);
  const start = first - mod(weekday(first) - startsOn, 7);
  return Array.from({ length: 42 }, (_, index) => ({ ...dayToCivil(start + index), epochDay: start + index }));
}
