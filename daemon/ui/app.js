/*
 * HAIKU human UI (docs/ui-spec.md). One file, no dependencies.
 *
 * Trust rule: every string that came from a participant (body, name, room
 * name, topic, reason) is untrusted. It is only ever put into the page with
 * textContent / createTextNode, never innerHTML, and never used to build a
 * URL, selector, id or handler. The only DOM-building helpers are el() and
 * the markdown renderer below, which builds elements one by one with
 * createElement/textContent and never parses HTML. Its one exception to
 * "never build a URL" is a link's href, which is only set after new URL()
 * accepts it with an http:, https: or mailto: scheme (safeUrl()).
 */
'use strict';

const LS_KEY = 'haiku.creds';
const LS_NOTIFY = 'haiku.notify';
const LS_VIEW = 'haiku.view';
const POLL_ACTIVE_MS = 2000;
const POLL_BG_MS = 15000;
const SUMMARY_MS = 5000;
const FOLD_LINES = 40;
const FIRST_LOAD_TAIL = 200;

const state = {
  creds: null,           // { name, token }
  summary: [],           // rooms from /me/rooms (or /rooms fallback)
  invites: [],           // pending_invites from /me/rooms
  roomId: null,
  room: null,            // GET /rooms/{id}
  events: [],            // events of the open room, ascending seq
  lastSeq: 0,
  dividerAfterSeq: null, // "new since you looked" sits after this seq
  participants: [],
  wake: null,            // { name: bool } from GET /participants (human callers only); null = not available
  wakeLoadedAt: 0,
  to: new Set(),        // selected addressees (empty = everyone)
  hasSummaryRoute: true,
  stickToBottom: true,
  unseenBelow: 0,
  bannerKey: null,       // what the pause banner currently shows
  peopleKey: null,       // what the people panel currently shows
  pollTimer: null,
  summaryTimer: null,
  polling: false,
  isHuman: null,         // null = not checked yet; humans get the admin room list
  allRooms: [],          // GET /rooms (humans see every room)
  view: { markdown: true, showArchived: false },
};

const $ = (id) => document.getElementById(id);

/* ---------- safe DOM helper ---------- */

function el(tag, opts, children) {
  const node = document.createElement(tag);
  if (opts) {
    if (opts.cls) node.className = opts.cls;
    if (opts.text != null) node.textContent = String(opts.text);
    if (opts.title != null) node.setAttribute('title', String(opts.title));
    if (opts.id != null) node.id = opts.id;
    if (opts.type) node.type = opts.type;
    if (opts.aria) for (const [k, v] of Object.entries(opts.aria)) node.setAttribute('aria-' + k, String(v));
    if (opts.data) for (const [k, v] of Object.entries(opts.data)) node.dataset[k] = String(v);
    if (opts.on) for (const [k, v] of Object.entries(opts.on)) node.addEventListener(k, v);
  }
  if (children) for (const c of children) if (c) node.appendChild(c);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function hueOf(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return h % 360;
}

function fmtTime(ts) {
  const d = new Date(ts);
  if (isNaN(d)) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function ago(ts) {
  const d = new Date(ts);
  if (isNaN(d)) return '';
  const s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  if (s < 86400) return Math.round(s / 3600) + 'h';
  return Math.round(s / 86400) + 'd';
}

function asList(v) {
  if (v == null) return [];
  if (Array.isArray(v)) return v;
  if (typeof v === 'string') {
    try {
      const p = JSON.parse(v);
      if (Array.isArray(p)) return p;
    } catch (e) { /* plain string */ }
    return v ? [v] : [];
  }
  return [];
}

/* ---------- markdown (Teddy, 2026-10-04) ----------
 *
 * A small CommonMark-ish subset, rendered straight to DOM nodes. There is no
 * HTML parsing step at all: raw HTML in a message shows as literal text,
 * because every piece of text goes in through createTextNode/textContent.
 * Supported: headings, paragraphs (single newlines kept as line breaks),
 * fenced code, block quotes, bulleted and numbered lists, horizontal rules,
 * `code`, **bold**, *italic*, ~~strike~~, [links](url), <autolinks> and bare
 * http(s) URLs. Images are shown as links, never loaded (the CSP blocks
 * remote images anyway, and a fetched image would leak that Teddy read it).
 * Nesting depth is capped so a hostile message can't blow the stack.
 */

const MD_MAX_DEPTH = 8;

function safeUrl(raw) {
  const s = String(raw || '').trim();
  if (!s) return null;
  let u;
  try { u = new URL(s); } catch (e) { return null; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:' && u.protocol !== 'mailto:') return null;
  return u.href;
}

function linkNode(href, children, isImage) {
  const a = document.createElement('a');
  a.href = href;
  a.target = '_blank';
  a.rel = 'noopener noreferrer nofollow';
  a.title = href;
  a.className = isImage ? 'md-imglink' : '';
  if (isImage) a.appendChild(document.createTextNode('[image] '));
  for (const c of children) a.appendChild(c);
  if (!a.childNodes.length) a.textContent = href;
  return a;
}

const RE_AUTOLINK = /<((?:https?:\/\/|mailto:)[^\s<>]+)>/y;
const RE_BAREURL = /https?:\/\/[^\s<>()\[\]]*[^\s<>()\[\].,;:!?'"*_~]/y;
const RE_ESCAPABLE = /[\\`*_{}\[\]()#+\-.!~>|<]/;

function isWordChar(ch) { return !!ch && /[\p{L}\p{N}]/u.test(ch); }

// Finds "[text](url)" starting at s[i] === '['. Returns {text, url, end} or null.
function matchLink(s, i) {
  let depth = 0;
  let j = i;
  for (; j < s.length; j++) {
    const ch = s[j];
    if (ch === '\\') { j++; continue; }
    if (ch === '[') depth++;
    else if (ch === ']') { depth--; if (depth === 0) break; }
    else if (ch === '\n' && s[j + 1] === '\n') return null;
  }
  if (j >= s.length || s[j + 1] !== '(') return null;
  const close = s.indexOf(')', j + 2);
  if (close < 0) return null;
  let inner = s.slice(j + 2, close).trim();
  const titled = /^(\S+)\s+(?:"[^"]*"|'[^']*')$/.exec(inner);
  if (titled) inner = titled[1];
  if (inner.startsWith('<') && inner.endsWith('>')) inner = inner.slice(1, -1);
  return { text: s.slice(i + 1, j), url: inner, end: close + 1 };
}

function mdInline(s, depth) {
  const out = [];
  let buf = '';
  const flush = () => { if (buf) { out.push(document.createTextNode(buf)); buf = ''; } };
  let i = 0;
  while (i < s.length) {
    const c = s[i];

    if (c === '\\' && i + 1 < s.length && RE_ESCAPABLE.test(s[i + 1])) {
      buf += s[i + 1]; i += 2; continue;
    }

    if (c === '`') {
      let n = 0;
      while (s[i + n] === '`') n++;
      const fence = '`'.repeat(n);
      let end = s.indexOf(fence, i + n);
      while (end >= 0 && s[end + n] === '`') end = s.indexOf(fence, end + n + 1);
      if (end >= 0) {
        flush();
        let code = s.slice(i + n, end).replace(/\n/g, ' ');
        if (code.length > 2 && code[0] === ' ' && code[code.length - 1] === ' ') code = code.slice(1, -1);
        out.push(el('code', { text: code }));
        i = end + n;
        continue;
      }
      buf += fence; i += n; continue;
    }

    if ((c === '[' || (c === '!' && s[i + 1] === '[')) && depth < MD_MAX_DEPTH) {
      const isImage = c === '!';
      const m = matchLink(s, isImage ? i + 1 : i);
      if (m) {
        const href = safeUrl(m.url);
        flush();
        if (href) {
          out.push(linkNode(href, mdInline(m.text, depth + 1), isImage));
        } else {
          // Unsafe or unparseable target: keep the text, drop the link.
          for (const n of mdInline(m.text, depth + 1)) out.push(n);
          out.push(document.createTextNode(' (' + m.url + ')'));
        }
        i = m.end;
        continue;
      }
    }

    if (c === '<') {
      RE_AUTOLINK.lastIndex = i;
      const m = RE_AUTOLINK.exec(s);
      const href = m && safeUrl(m[1]);
      if (href) { flush(); out.push(linkNode(href, [document.createTextNode(m[1])], false)); i = RE_AUTOLINK.lastIndex; continue; }
    }

    if (c === 'h' && !isWordChar(s[i - 1])) {
      RE_BAREURL.lastIndex = i;
      const m = RE_BAREURL.exec(s);
      const href = m && safeUrl(m[0]);
      if (href) { flush(); out.push(linkNode(href, [document.createTextNode(m[0])], false)); i = RE_BAREURL.lastIndex; continue; }
    }

    if ((c === '*' || c === '_' || c === '~') && depth < MD_MAX_DEPTH) {
      let run = 0;
      while (s[i + run] === c) run++;
      let len;
      let tag;
      if (c === '~') { len = 2; tag = 'del'; if (run !== 2) { buf += c.repeat(run); i += run; continue; } }
      else if (run >= 2) { len = 2; tag = 'strong'; }
      else { len = 1; tag = 'em'; }
      const after = s[i + len];
      const leftOk = after && !/\s/.test(after) && !(c === '_' && isWordChar(s[i - 1]));
      if (leftOk) {
        const marker = c.repeat(len);
        let j = s.indexOf(marker, i + len);
        while (j >= 0) {
          while (s[j + len] === c) j++;          // close at the END of a run: ***x*** = strong(em(x))
          const before = s[j - 1];
          const okClose = j > i + len && before && !/\s/.test(before) && !(c === '_' && isWordChar(s[j + len]));
          if (okClose) break;
          j = s.indexOf(marker, j + len);
        }
        if (j >= 0) {
          flush();
          const node = document.createElement(tag);
          for (const n of mdInline(s.slice(i + len, j), depth + 1)) node.appendChild(n);
          out.push(node);
          i = j + len;
          continue;
        }
      }
      buf += c.repeat(run); i += run; continue;
    }

    buf += c;
    i++;
  }
  flush();
  return out;
}

const RE_FENCE = /^ {0,3}(`{3,}|~{3,})\s*([^`\s]*)[^`]*$/;
const RE_HEADING = /^ {0,3}(#{1,6})(?:\s+(.*?))?\s*#*\s*$/;
const RE_HR = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
const RE_QUOTE = /^ {0,3}> ?(.*)$/;
const RE_LIST = /^( {0,3})([-*+]|\d{1,9}[.)])\s+(.*)$/;

function startsBlock(line) {
  return RE_FENCE.test(line) || RE_HEADING.test(line) || RE_HR.test(line) || RE_QUOTE.test(line) || RE_LIST.test(line);
}

function mdBlocks(lines, depth, into) {
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    let m = RE_FENCE.exec(line);
    if (m) {
      const fence = m[1];
      const body = [];
      i++;
      const isClose = (l) => {
        const t = l.trim();
        return t.length >= fence.length && t.split('').every((ch) => ch === fence[0]);
      };
      while (i < lines.length && !isClose(lines[i])) {
        body.push(lines[i]);
        i++;
      }
      i++; // closing fence (or end of input)
      const code = el('code', { text: body.join('\n') });
      if (m[2]) code.dataset.lang = m[2].slice(0, 20);
      into.appendChild(el('pre', null, [code]));
      continue;
    }

    m = RE_HEADING.exec(line);
    if (m) {
      const h = document.createElement('h' + m[1].length);
      for (const n of mdInline(m[2] || '', depth)) h.appendChild(n);
      into.appendChild(h);
      i++;
      continue;
    }

    if (RE_HR.test(line)) { into.appendChild(document.createElement('hr')); i++; continue; }

    if (RE_QUOTE.test(line)) {
      const inner = [];
      while (i < lines.length && lines[i].trim()) {
        const q = RE_QUOTE.exec(lines[i]);
        inner.push(q ? q[1] : lines[i]);
        i++;
      }
      const bq = document.createElement('blockquote');
      if (depth < MD_MAX_DEPTH) mdBlocks(inner, depth + 1, bq);
      else bq.textContent = inner.join('\n');
      into.appendChild(bq);
      continue;
    }

    m = RE_LIST.exec(line);
    if (m) {
      const ordered = /\d/.test(m[2]);
      const list = document.createElement(ordered ? 'ol' : 'ul');
      if (ordered) {
        const start = parseInt(m[2], 10);
        if (start !== 1 && start < 1e9) list.start = start;
      }
      const items = [];
      while (i < lines.length) {
        const cur = lines[i];
        const lm = RE_LIST.exec(cur);
        if (lm && /\d/.test(lm[2]) === ordered && !RE_HR.test(cur)) {
          items.push([lm[3]]);
          i++;
          continue;
        }
        if (!cur.trim()) {
          const next = lines[i + 1];
          if (next !== undefined && (/^\s{2,}\S/.test(next) || (RE_LIST.test(next) && /\d/.test(RE_LIST.exec(next)[2]) === ordered))) {
            items[items.length - 1].push('');
            i++;
            continue;
          }
          break;
        }
        if (/^\s{2,}\S/.test(cur)) {
          items[items.length - 1].push(cur.replace(/^ {2,4}|^\t/, ''));
          i++;
          continue;
        }
        const last = items[items.length - 1];
        if (last[last.length - 1].trim() && !startsBlock(cur)) { last.push(cur); i++; continue; }
        break;
      }
      for (const it of items) {
        const li = document.createElement('li');
        if (depth < MD_MAX_DEPTH) mdBlocks(it, depth + 1, li);
        else li.textContent = it.join('\n');
        list.appendChild(li);
      }
      into.appendChild(list);
      continue;
    }

    // Paragraph: runs until a blank line or the start of another block.
    const para = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !startsBlock(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    const p = document.createElement('p');
    para.forEach((pl, k) => {
      if (k) p.appendChild(document.createElement('br'));
      for (const n of mdInline(pl.replace(/^\s+/, ''), depth)) p.appendChild(n);
    });
    into.appendChild(p);
  }
  return into;
}

function renderMarkdown(text) {
  const frag = document.createDocumentFragment();
  mdBlocks(String(text == null ? '' : text).replace(/\r\n?/g, '\n').split('\n'), 0, frag);
  return frag;
}

/* ---------- API ---------- */

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body, query) {
  let url = path;
  if (query) {
    const qs = new URLSearchParams(query).toString();
    if (qs) url += '?' + qs;
  }
  const headers = {
    'X-Haiku-Participant': state.creds.name,
    'X-Haiku-Token': state.creds.token,
  };
  const init = { method, headers };
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body || {});
  }
  const res = await fetch(url, init);
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (res.status === 401) {
    signOut('Your credentials were rejected. Run the login command again.');
    throw new ApiError(401, 'unauthorized');
  }
  if (!res.ok) throw new ApiError(res.status, data.error || ('HTTP ' + res.status));
  return data;
}

/* ---------- credentials ---------- */

function loadCreds() {
  // First: a fragment from the login command. It is read, stored, and
  // removed from the address bar before anything else runs.
  if (location.hash.length > 1) {
    const p = new URLSearchParams(location.hash.slice(1));
    const name = p.get('name');
    const token = p.get('token');
    history.replaceState(null, '', location.pathname + location.search);
    if (name && token) {
      try { localStorage.setItem(LS_KEY, JSON.stringify({ name, token })); } catch (e) { /* ignore */ }
    }
  }
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) {
      const c = JSON.parse(raw);
      if (c && c.name && c.token) return { name: String(c.name), token: String(c.token) };
    }
  } catch (e) { /* ignore */ }
  return null;
}

function signOut(message) {
  try { localStorage.removeItem(LS_KEY); } catch (e) { /* ignore */ }
  state.creds = null;
  stopPolling();
  $('layout').hidden = true;
  $('signin').hidden = false;
  $('whoami').textContent = '';
  setBanner(message || '');
  document.title = 'HAIKU';
}

function setBanner(text) {
  const b = $('banner');
  b.textContent = text;
  b.hidden = !text;
}

/* ---------- room list ---------- */

function needsMe(r) { return !!r.needs_me; }

async function refreshSummary() {
  let rooms;
  let invites = [];
  if (state.hasSummaryRoute) {
    try {
      const data = await api('GET', '/me/rooms');
      rooms = data.rooms;
      invites = asList(data.pending_invites);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) state.hasSummaryRoute = false;
      else throw e;
    }
  }
  if (!state.hasSummaryRoute) {
    rooms = (await api('GET', '/rooms')).rooms;
  }
  state.summary = rooms || [];
  state.invites = invites;
  // A human is HAIKU's admin (Teddy, 2026-10-04) and may see and join every
  // room. The daemon decides that from the authenticated kind; this only
  // decides whether to show the extra list. /participants returns the
  // wake_allowed field to human callers only.
  if (state.isHuman === null) {
    await loadWake();
    state.isHuman = state.wake !== null;
  }
  if (state.isHuman) {
    try { state.allRooms = (await api('GET', '/rooms')).rooms || []; } catch (e) {
      if (e instanceof ApiError && e.status === 401) throw e;
    }
  }
  checkNotifications();
  renderRoomList();
  updateTitle();
}

/* ---------- desktop notifications (docs/ui-spec.md, Decisions) ----------
 *
 * Opt-in, off by default, polling only (works while the tab is open).
 * Triggers are the three things that need a human: a room paused at its hop
 * cap, a message addressed to the human, a new invite. The text is a fixed
 * template; the only participant-chosen string is the room name, cleaned and
 * truncated, and never a message body. OS notifications render outside the
 * page's fence and CSP, so message content stays out of them entirely.
 */

const notify = { enabled: false, muted: new Set() };
const notifySeen = { rooms: new Map(), invites: new Set(), primed: false };

function loadNotifyPrefs() {
  try {
    const raw = localStorage.getItem(LS_NOTIFY);
    if (!raw) return;
    const p = JSON.parse(raw);
    notify.enabled = !!(p && p.enabled);
    notify.muted = new Set(Array.isArray(p && p.muted) ? p.muted.map(String) : []);
  } catch (e) { /* defaults */ }
}

function saveNotifyPrefs() {
  try {
    localStorage.setItem(LS_NOTIFY, JSON.stringify({ enabled: notify.enabled, muted: [...notify.muted] }));
  } catch (e) { /* ignore */ }
}

function notifySupported() { return typeof Notification !== 'undefined'; }

function notifyActive() {
  return notify.enabled && notifySupported() && Notification.permission === 'granted';
}

function cleanName(name) {
  const t = String(name == null ? '' : name)
    .replace(/[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩]/g, ' ')
    .replace(/\s+/g, ' ').trim();
  return t.length > 40 ? t.slice(0, 39) + '…' : t;
}

function fireNotification(tag, body, roomId) {
  try {
    const n = new Notification('HAIKU', { body, tag });
    n.onclick = () => {
      window.focus();
      if (roomId && state.creds) openRoom(roomId);
      n.close();
    };
  } catch (e) { /* notifications are best effort */ }
}

function checkNotifications() {
  const wasPrimed = notifySeen.primed;
  notifySeen.primed = true;
  // First summary only seeds what already exists. And if the page is focused
  // the title badge and room list already cover it.
  const quiet = !wasPrimed || !notifyActive() || document.hasFocus();

  for (const r of state.summary) {
    if (r.state === 'archived') continue;
    const prev = notifySeen.rooms.get(r.id) || { paused: false, owes: null };
    const cur = {
      paused: r.state === 'paused',
      owes: Number.isInteger(r.owes_reply_to_seq) ? r.owes_reply_to_seq : null,
    };
    notifySeen.rooms.set(r.id, cur);
    if (quiet || notify.muted.has(String(r.id))) continue;
    const label = '"' + cleanName(r.name) + '"';
    if (cur.paused && !prev.paused) {
      fireNotification('paused:' + r.id, 'Room ' + label + ' is paused and needs you.', r.id);
    }
    if (cur.owes !== null && cur.owes !== prev.owes) {
      fireNotification('addressed:' + r.id, 'You were addressed in room ' + label + '.', r.id);
    }
  }

  for (const inv of state.invites) {
    if (!inv || inv.room_id == null) continue;
    const key = String(inv.room_id);
    if (notifySeen.invites.has(key)) continue;
    notifySeen.invites.add(key);
    if (quiet || notify.muted.has(key)) continue;
    fireNotification('invite:' + key, 'You have an invite to room "' + cleanName(inv.room_name) + '".', null);
  }
}

async function toggleNotify() {
  if (notify.enabled) {
    notify.enabled = false;
  } else if (notifySupported()) {
    let perm = Notification.permission;
    if (perm === 'default') {
      try { perm = await Notification.requestPermission(); } catch (e) { perm = 'denied'; }
    }
    notify.enabled = perm === 'granted';
  }
  saveNotifyPrefs();
  updateNotifyUi();
}

function toggleMute() {
  if (!state.roomId) return;
  const id = String(state.roomId);
  if (notify.muted.has(id)) notify.muted.delete(id); else notify.muted.add(id);
  saveNotifyPrefs();
  updateNotifyUi();
}

function updateNotifyUi() {
  const b = $('btn-notify');
  const supported = notifySupported();
  const denied = supported && Notification.permission === 'denied';
  if (!supported) b.textContent = 'Notify: unavailable';
  else if (denied) b.textContent = 'Notify: blocked';
  else b.textContent = notifyActive() ? 'Notify: on' : 'Notify: off';
  b.setAttribute('aria-pressed', notifyActive() ? 'true' : 'false');
  b.disabled = !supported;
  b.title = denied
    ? 'Notifications are blocked for this page in the browser settings.'
    : 'Desktop alerts for pauses, messages addressed to you, and invites. Alerts never include message text.';
  const m = $('btn-mute');
  const muted = state.roomId != null && notify.muted.has(String(state.roomId));
  m.hidden = !notifyActive();
  m.textContent = muted ? 'Unmute alerts' : 'Mute alerts';
  m.setAttribute('aria-pressed', muted ? 'true' : 'false');
}

function updateTitle() {
  const n = state.summary.filter((r) => r.state !== 'archived' && needsMe(r)).length + state.invites.length;
  document.title = (n ? '(' + n + ') ' : '') + 'HAIKU';
}

function renderRoomList() {
  const nav = $('rooms');
  clear(nav);
  const invites = state.invites.filter((i) => i && i.room_id != null);
  if (invites.length) {
    nav.appendChild(el('h2', { text: 'Invites (' + invites.length + ')' }));
    for (const inv of invites) {
      nav.appendChild(el('div', { cls: 'invite-row' }, [
        el('span', { cls: 'room-name', text: inv.room_name }),
        el('button', {
          type: 'button',
          cls: 'invite-accept',
          text: 'Accept',
          title: 'Join this room. You see only its name and topic until you do.',
          on: { click: (ev) => acceptInvite(inv.room_id, ev.currentTarget) },
        }),
      ]));
    }
  }
  const groups = [
    ['Needs you', (r) => r.state !== 'archived' && needsMe(r)],
    ['Active', (r) => r.state !== 'archived' && !needsMe(r)],
  ];
  for (const [label, pred] of groups) {
    const rows = state.summary.filter(pred);
    if (!rows.length) continue;
    nav.appendChild(el('h2', { text: label + ' (' + rows.length + ')' }));
    for (const r of rows) nav.appendChild(memberRow(r));
  }

  // Rooms the human isn't in (admin view). Joining is an ordinary, visible
  // join event, so the button says so.
  const mine = new Set(state.summary.map((r) => r.id));
  const invited = new Set(invites.map((i) => i.room_id));
  const others = state.allRooms.filter((r) => !mine.has(r.id) && !invited.has(r.id));
  const otherActive = others.filter((r) => r.state !== 'archived');
  if (otherActive.length) {
    nav.appendChild(el('h2', { text: 'Other rooms (' + otherActive.length + ')' }));
    for (const r of otherActive) nav.appendChild(otherRow(r));
  }

  const archivedMine = state.summary.filter((r) => r.state === 'archived');
  const archivedOther = others.filter((r) => r.state === 'archived');
  const nArchived = archivedMine.length + archivedOther.length;
  const ab = $('btn-archived');
  ab.hidden = nArchived === 0;
  ab.textContent = state.view.showArchived ? 'Hide archived' : 'Show archived (' + nArchived + ')';
  ab.setAttribute('aria-pressed', state.view.showArchived ? 'true' : 'false');
  if (state.view.showArchived && nArchived) {
    nav.appendChild(el('h2', { text: 'Archived (' + nArchived + ')' }));
    for (const r of archivedMine) nav.appendChild(memberRow(r));
    for (const r of archivedOther) nav.appendChild(otherRow(r));
  }

  if (!state.summary.length && !invites.length && !others.length) nav.appendChild(el('p', { cls: 'muted', text: 'No rooms yet.' }));
}

function memberRow(r) {
  const kids = [el('span', { cls: 'room-name', text: r.name, title: r.name })];
  if (r.unread > 0) kids.push(el('span', { cls: 'badge', text: r.unread, title: r.unread + ' unread' }));
  if (r.state === 'paused') kids.push(el('span', { cls: 'chip paused', text: 'paused' }));
  if (r.state === 'archived') kids.push(el('span', { cls: 'chip', text: 'archived' }));
  return el('button', {
    type: 'button',
    cls: 'room-row' + (r.id === state.roomId ? ' current' : ''),
    data: { room: r.id },
    aria: { current: r.id === state.roomId ? 'true' : 'false' },
    on: { click: () => openRoom(r.id) },
  }, kids);
}

function otherRow(r) {
  const info = [el('span', { cls: 'room-name', text: r.name, title: r.topic ? r.name + ' (' + r.topic + ')' : r.name })];
  info.push(el('span', { cls: 'chip', text: r.mode === 'open' ? 'open' : 'closed' }));
  return el('div', { cls: 'other-row' }, [
    el('div', { cls: 'other-info' }, info),
    el('button', {
      type: 'button',
      cls: 'invite-accept',
      text: 'Join',
      title: 'Join to read and post. Your join shows in the room like anyone else\'s.',
      on: { click: (ev) => acceptInvite(r.id, ev.currentTarget) },
    }),
  ]);
}

function toggleArchived() {
  state.view.showArchived = !state.view.showArchived;
  saveView();
  renderRoomList();
}

/* ---------- creating a room ---------- */

function showNewRoom(open) {
  const f = $('new-room');
  f.hidden = !open;
  $('nr-note').textContent = '';
  if (open) $('nr-name').focus();
}

async function createRoom() {
  const name = $('nr-name').value.trim();
  const topic = $('nr-topic').value.trim();
  const hops = parseInt($('nr-hops').value, 10);
  if (!name) { $('nr-note').textContent = 'A room needs a name.'; return; }
  const body = { name, mode: $('nr-mode').value === 'open' ? 'open' : 'closed' };
  if (topic) body.topic = topic;
  if (Number.isInteger(hops) && hops >= 1 && hops <= 100) body.hop_limit = hops;
  $('nr-create').disabled = true;
  try {
    const res = await api('POST', '/rooms', body);
    $('new-room').reset();
    showNewRoom(false);
    await refreshSummary();
    await openRoom(res.room_id);
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 401)) $('nr-note').textContent = 'Could not create: ' + e.message;
  } finally {
    $('nr-create').disabled = false;
  }
}

/* ---------- view preferences (this browser only) ---------- */

function loadView() {
  try {
    const p = JSON.parse(localStorage.getItem(LS_VIEW) || '{}');
    if (typeof p.markdown === 'boolean') state.view.markdown = p.markdown;
    if (typeof p.showArchived === 'boolean') state.view.showArchived = p.showArchived;
  } catch (e) { /* defaults */ }
}

function saveView() {
  try { localStorage.setItem(LS_VIEW, JSON.stringify(state.view)); } catch (e) { /* ignore */ }
}

function updateMdButton() {
  const b = $('btn-md');
  b.textContent = state.view.markdown ? 'Markdown: on' : 'Markdown: off';
  b.setAttribute('aria-pressed', state.view.markdown ? 'true' : 'false');
}

function toggleMarkdownView() {
  state.view.markdown = !state.view.markdown;
  saveView();
  updateMdButton();
  if (state.roomId && state.room) {
    const s = $('stream');
    const top = s.scrollTop;
    const atBottom = isNearBottom();
    renderStream();
    s.scrollTop = atBottom ? s.scrollHeight : top;
  }
}

async function acceptInvite(roomId, btn) {
  btn.disabled = true;
  try {
    await api('POST', '/rooms/' + encodeURIComponent(roomId) + '/join', {});
    await refreshSummary();
    await openRoom(roomId);
  } catch (e) {
    btn.disabled = false;
    if (!(e instanceof ApiError && e.status === 401)) setBanner('Could not join: ' + e.message);
  }
}

/* ---------- opening a room ---------- */

async function openRoom(id) {
  state.roomId = id;
  state.events = [];
  state.lastSeq = 0;
  state.dividerAfterSeq = null;
  state.to = new Set();
  state.stickToBottom = true;
  state.unseenBelow = 0;
  state.bannerKey = null;
  state.peopleKey = null;
  document.body.classList.remove('show-rooms');

  const summ = state.summary.find((r) => r.id === id) || {};
  const lastSeqKnown = Number.isInteger(summ.last_seq) ? summ.last_seq : null;
  const myCursor = Number.isInteger(summ.my_cursor) ? summ.my_cursor : null;

  $('empty').hidden = true;
  $('room-view').hidden = false;

  try {
    state.room = await api('GET', '/rooms/' + encodeURIComponent(id));
    const since = lastSeqKnown != null ? Math.max(0, lastSeqKnown - FIRST_LOAD_TAIL) : 0;
    await loadEvents(since, true);
    if (myCursor != null && myCursor < state.lastSeq) state.dividerAfterSeq = myCursor;
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 401)) setBanner('Could not open room: ' + e.message);
    return;
  }
  setBanner('');
  renderRoom();
  renderRoomList();
  scrollToDividerOrBottom();
  maybeAck();
  startPolling();
}

async function loadEvents(since, first) {
  const data = await api('GET', '/rooms/' + encodeURIComponent(state.roomId) + '/events', null, {
    since: String(since),
    advance: 'false',
    exclude_self: 'false',
    limit: String(FIRST_LOAD_TAIL),
  });
  const evs = data.events || [];
  const known = new Set(state.events.map((e) => e.seq));
  const fresh = evs.filter((e) => !known.has(e.seq));
  if (fresh.length) {
    state.events = state.events.concat(fresh).sort((a, b) => a.seq - b.seq);
  }
  const top = Math.max(state.lastSeq, ...(evs.map((e) => e.seq)), Number.isInteger(data.max_seq) ? data.max_seq : 0);
  state.lastSeq = top;
  return fresh;
}

/* ---------- polling ---------- */

function startPolling() {
  stopPolling();
  schedulePoll();
  state.summaryTimer = setInterval(summaryTick, SUMMARY_MS);
}

// A hidden tab skips the summary poll unless alerts are on; then it keeps
// polling, at the slower background rate, so the alerts can actually fire.
let lastSummaryAt = 0;
function summaryTick() {
  if (!state.creds) return;
  if (document.hidden) {
    if (!notifyActive() || Date.now() - lastSummaryAt < POLL_BG_MS) return;
  }
  lastSummaryAt = Date.now();
  refreshSummary().catch(onPollError);
}

function stopPolling() {
  if (state.pollTimer) clearTimeout(state.pollTimer);
  if (state.summaryTimer) clearInterval(state.summaryTimer);
  state.pollTimer = null;
  state.summaryTimer = null;
}

function schedulePoll() {
  if (state.pollTimer) clearTimeout(state.pollTimer);
  const wait = document.hidden ? POLL_BG_MS : POLL_ACTIVE_MS;
  state.pollTimer = setTimeout(pollOnce, wait);
}

let pollFailures = 0;

async function pollOnce() {
  if (!state.creds || !state.roomId || state.polling) return schedulePoll();
  state.polling = true;
  try {
    const fresh = await loadEvents(state.lastSeq, false);
    state.room = await api('GET', '/rooms/' + encodeURIComponent(state.roomId));
    if (fresh.length) appendFresh(fresh);
    if (Date.now() - state.wakeLoadedAt > 30000) await loadWake();
    renderHead();
    renderPeople();
    renderComposerEffect();
    maybeAck();
    pollFailures = 0;
    setBanner('');
  } catch (e) {
    onPollError(e);
  } finally {
    state.polling = false;
    schedulePoll();
  }
}

function onPollError(e) {
  if (e instanceof ApiError && e.status === 401) return;
  pollFailures += 1;
  if (pollFailures >= 2) setBanner('Daemon unreachable or erroring. Retrying quietly. (' + e.message + ')');
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    schedulePoll();
    maybeAck();
  }
});

async function maybeAck() {
  if (!state.creds || !state.roomId || document.hidden || !state.lastSeq) return;
  if (!state.stickToBottom) return;
  try {
    await api('POST', '/rooms/' + encodeURIComponent(state.roomId) + '/ack', { through_seq: state.lastSeq });
  } catch (e) { /* read receipts are best effort */ }
}

/* ---------- rendering the room ---------- */

function renderRoom() {
  renderHead();
  renderStream();
  renderPeople();
  renderToChips();
  renderComposerEffect();
}

function renderHead() {
  const r = state.room;
  if (!r) return;
  $('room-title').textContent = r.name;
  $('room-topic').textContent = r.topic ? 'Topic: ' + r.topic : '';
  const meter = $('hop-meter');
  meter.max = r.hop_limit || 6;
  meter.value = Math.min(r.hop_count || 0, meter.max);
  $('hop-label').textContent = 'AI replies since you spoke: ' + (r.hop_count || 0) + '/' + (r.hop_limit || 6);
  $('btn-pause').hidden = r.state !== 'active';
  $('btn-archive').hidden = r.state === 'archived';
  updateNotifyUi();

  // The banner holds an input the person may be typing in, so it is only
  // rebuilt when what it shows actually changed, never on every poll tick.
  const lastAiMsg = [...state.events].reverse().find((e) => e.type === 'message' && e.author_kind === 'ai');
  const bannerKey = [r.state, r.hop_limit, lastAiMsg ? lastAiMsg.seq : 0].join('|');
  if (bannerKey === state.bannerKey) return finishHead(r);
  state.bannerKey = bannerKey;

  const pb = $('pause-banner');
  clear(pb);
  if (r.state === 'paused') {
    pb.hidden = false;
    const lastAi = [...state.events].reverse().find((e) => e.type === 'message' && e.author_kind === 'ai');
    pb.appendChild(el('strong', { text: 'Paused: the AIs reached ' + (r.hop_limit || 6) + ' replies. ' }));
    if (lastAi) {
      pb.appendChild(el('div', { cls: 'digest' }, [
        el('span', { cls: 'digest-by', text: lastAi.author + ' said last:' }),
        el('div', { cls: 'digest-body', text: (lastAi.body || '').slice(0, 600) }),
      ]));
    }
    const n = el('input', { type: 'number', id: 'resume-n', aria: { label: 'Extra replies' } });
    n.min = '1'; n.max = '100'; n.value = String(r.hop_limit || 6);
    pb.appendChild(el('button', { type: 'button', text: 'Continue (+' + (r.hop_limit || 6) + ')', on: { click: () => doResume(null) } }));
    pb.appendChild(n);
    pb.appendChild(el('button', { type: 'button', text: 'Continue N', on: { click: () => doResume(parseInt(n.value, 10)) } }));
  } else if (r.state === 'archived') {
    pb.hidden = false;
    pb.appendChild(el('strong', { text: 'This room is archived. It stays readable; nothing more can be sent.' }));
  } else {
    pb.hidden = true;
  }
  finishHead(r);
}

function finishHead(r) {
  $('compose').disabled = r.state === 'archived';
  $('btn-send').disabled = r.state === 'archived';
  $('btn-editor').disabled = r.state === 'archived';
  const note = $('compose-note');
  note.textContent = r.state === 'paused' ? 'Room is paused. Your message will be posted but will not resume it.' : '';
}

function systemLine(ev) {
  let text;
  const who = ev.author;
  switch (ev.type) {
    case 'join': text = who + ' joined'; break;
    case 'leave': text = who + ' left'; break;
    case 'pass': text = who + ' passed'; break;
    case 'topic_change': text = who + ' changed the topic' + (ev.body ? ': ' + ev.body : ''); break;
    case 'pause': text = 'Room paused' + (ev.body ? ': ' + ev.body : ''); break;
    case 'resume': text = 'Room resumed' + (ev.body ? ': ' + ev.body : ''); break;
    case 'archive': text = who + ' archived the room' + (ev.body ? ' (' + ev.body + ')' : ''); break;
    default: text = who + ' ' + ev.type;
  }
  return el('div', { cls: 'sys', id: 'seq-' + ev.seq, title: 'seq ' + ev.seq + ' ' + ev.ts, text });
}

function messageNode(ev) {
  const to = asList(ev.addressed_to);
  const toText = to.length ? 'to ' + to.join(', ') : 'to everyone';
  const isHuman = ev.author_kind === 'human';
  const head = el('div', { cls: 'msg-head' }, [
    el('span', { cls: 'author', text: ev.author }),
    el('span', { cls: 'kind ' + (isHuman ? 'human' : 'ai'), text: isHuman ? 'human' : 'AI' }),
    el('span', { cls: 'meta', text: fmtTime(ev.ts) }),
    el('span', { cls: 'meta', text: toText }),
    el('span', { cls: 'seq', text: '#' + ev.seq }),
  ]);
  const bodyText = ev.body == null ? '' : String(ev.body);
  const lines = bodyText.split('\n');
  const md = state.view.markdown;
  const body = el('div', { cls: md ? 'msg-body md' : 'msg-body' });
  const fill = (text) => {
    clear(body);
    if (md) body.appendChild(renderMarkdown(text));
    else body.textContent = text;
  };
  let content = body;
  if (lines.length > FOLD_LINES) {
    fill(lines.slice(0, FOLD_LINES).join('\n'));
    const more = el('button', {
      type: 'button', cls: 'linklike', text: 'show all ' + lines.length + ' lines',
      on: { click: () => { fill(bodyText); more.remove(); } },
    });
    content = el('div', null, [body, more]);
  } else {
    fill(bodyText);
  }
  const art = el('article', { cls: 'msg ' + (isHuman ? 'by-human' : 'by-ai'), id: 'seq-' + ev.seq }, [head, content]);
  art.style.setProperty('--hue', String(hueOf(ev.author)));
  return art;
}

function eventNode(ev) {
  return ev.type === 'message' ? messageNode(ev) : systemLine(ev);
}

function dividerNode() {
  return el('div', { cls: 'divider', id: 'new-divider', text: 'new since you looked' });
}

function renderStream() {
  const s = $('stream');
  clear(s);
  for (const ev of state.events) {
    s.appendChild(eventNode(ev));
    if (state.dividerAfterSeq != null && ev.seq === state.dividerAfterSeq) s.appendChild(dividerNode());
  }
  if (!state.events.length) s.appendChild(el('p', { cls: 'muted', text: 'No events yet.' }));
}

function appendFresh(fresh) {
  const s = $('stream');
  const wasNearBottom = isNearBottom();
  state.stickToBottom = wasNearBottom;
  for (const ev of fresh) s.appendChild(eventNode(ev));
  if (wasNearBottom) {
    s.scrollTop = s.scrollHeight;
    state.unseenBelow = 0;
  } else {
    state.unseenBelow += fresh.length;
  }
  updateNewBelow();
}

function isNearBottom() {
  const s = $('stream');
  return s.scrollHeight - s.scrollTop - s.clientHeight < 40;
}

function updateNewBelow() {
  const b = $('new-below');
  if (state.unseenBelow > 0) {
    b.hidden = false;
    b.textContent = state.unseenBelow + ' new below';
  } else {
    b.hidden = true;
  }
}

function scrollToDividerOrBottom() {
  const s = $('stream');
  const d = document.getElementById('new-divider');
  if (d) d.scrollIntoView({ block: 'center' });
  else s.scrollTop = s.scrollHeight;
  state.stickToBottom = isNearBottom();
}

function jumpToSeq(seq) {
  const n = document.getElementById('seq-' + seq);
  if (n) {
    n.scrollIntoView({ block: 'center' });
    n.classList.add('flash');
    setTimeout(() => n.classList.remove('flash'), 1500);
  }
}

/* ---------- people ---------- */

function rosterRows() {
  return (state.room && state.room.roster) || [];
}

function renderPeople() {
  const box = $('people');
  // Rebuild only when the roster (or the minute, for the "last active" ages)
  // changed, and carry the invite picker across a rebuild, so a poll tick
  // never closes something the person has open.
  const key = JSON.stringify(rosterRows()) + '|' + Math.floor(Date.now() / 60000) + '|' + state.events.length + '|' + JSON.stringify(state.wake);
  if (key === state.peopleKey) return;
  state.peopleKey = key;
  const keptInvite = document.getElementById('invite-box');
  if (keptInvite) keptInvite.remove();
  clear(box);
  box.appendChild(el('h2', { text: 'People' }));
  for (const p of rosterRows()) {
    if (p.status === 'left') continue;
    const kind = p.kind || null;
    const row = el('div', { cls: 'person' });
    row.style.setProperty('--hue', String(hueOf(p.participant)));
    const top = el('div', { cls: 'person-top' }, [
      el('button', {
        type: 'button', cls: 'linklike person-name', text: p.participant === state.creds.name ? p.participant + ' (you)' : p.participant,
        on: { click: () => { if (kind === 'ai') toggleTo(p.participant); } },
        title: kind === 'ai' ? 'Click to address this participant' : '',
      }),
      kind ? el('span', { cls: 'kind ' + (kind === 'human' ? 'human' : 'ai'), text: kind === 'human' ? 'human' : 'AI' }) : null,
    ]);
    row.appendChild(top);
    let status = p.status;
    if (p.last_active_ts) status += ' · last active ' + ago(p.last_active_ts) + ' ago';
    row.appendChild(el('div', { cls: 'person-status', text: status }));
    if (p.owes_reply_to_seq != null) {
      const ev = state.events.find((e) => e.seq === p.owes_reply_to_seq);
      const age = ev ? ' (' + ago(ev.ts) + ')' : '';
      const mine = p.participant === state.creds.name;
      const text = mine ? 'waiting on you: #' + p.owes_reply_to_seq + age : 'owes a reply to #' + p.owes_reply_to_seq + age;
      row.appendChild(el('button', {
        type: 'button', cls: 'linklike obligation', text,
        on: { click: () => jumpToSeq(p.owes_reply_to_seq) },
      }));
    }
    // Wake kill switch (spec 3a, level 3): humans only, AI rows only. The
    // daemon only sends the flag to human callers, so no flag means no control.
    if (kind === 'ai' && p.muted) {
      row.appendChild(el('div', {
        cls: 'person-status muted-note', text: 'muted this room',
        title: 'This AI chose not to be delivered this room. Messages you address to it by name still reach it.',
      }));
    }
    if (kind === 'ai' && state.wake && typeof state.wake[p.participant] === 'boolean') {
      const allowed = state.wake[p.participant];
      row.appendChild(el('button', {
        type: 'button', cls: 'linklike wake-toggle' + (allowed ? '' : ' wake-off'),
        text: allowed ? 'Wake, all rooms: allowed' : 'Wake, all rooms: blocked',
        title: allowed
          ? 'This AI may be woken for replies if its own settings allow it. Click to block it in every room.'
          : 'This AI will not be woken in any room, whatever its own settings say. Click to allow again.',
        aria: { pressed: !allowed },
        on: { click: () => setWake(p.participant, !allowed) },
      }));
      // Per-room switch (Teddy, 2026-10-04). Restrict-only: ANDed with the
      // all-rooms switch above and the AI's own settings.
      if (typeof p.room_wake_allowed === 'boolean') {
        const here = p.room_wake_allowed;
        row.appendChild(el('button', {
          type: 'button', cls: 'linklike wake-toggle' + (here ? '' : ' wake-off'),
          text: here ? 'Wake, this room: allowed' : 'Wake, this room: blocked',
          title: here
            ? 'Click to stop this AI being woken by this room only.'
            : 'This AI will not be woken by this room. Click to allow again (other switches still apply).',
          aria: { pressed: !here },
          on: { click: () => setRoomWake(p.participant, !here) },
        }));
      }
    }
    box.appendChild(row);
  }
  box.appendChild(el('button', { type: 'button', id: 'btn-invite', text: 'Invite…', on: { click: openInvite } }));
  if (keptInvite) {
    box.appendChild(keptInvite);
  } else {
    box.appendChild(el('div', { id: 'invite-box' }));
    $('invite-box').hidden = true;
  }
}

async function loadWake() {
  state.wakeLoadedAt = Date.now();
  try {
    const data = await api('GET', '/participants');
    const list = data.participants || [];
    // AI callers get rows without the field; leave the controls hidden then.
    if (!list.some((p) => typeof p.wake_allowed === 'boolean')) { state.wake = null; return; }
    const m = {};
    for (const p of list) if (typeof p.wake_allowed === 'boolean') m[p.name] = p.wake_allowed;
    state.wake = m;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) throw e;
    /* leave the last known state; the next tick retries */
  }
}

async function setWake(name, allowed) {
  try {
    await api('PUT', '/participants/' + encodeURIComponent(name) + '/wake_allowed', { allowed });
    await loadWake();
    renderPeople();
    setBanner((allowed ? 'Allowed waking for ' : 'Blocked waking for ') + name + '.');
  } catch (e) {
    setBanner('Could not change wake setting: ' + e.message);
  }
}

async function setRoomWake(name, allowed) {
  const roomId = state.roomId;
  try {
    await api('PUT', '/rooms/' + encodeURIComponent(roomId) + '/wake_allowed/' + encodeURIComponent(name), { allowed });
    if (state.roomId === roomId) {
      state.room = await api('GET', '/rooms/' + encodeURIComponent(roomId));
      renderPeople();
    }
    setBanner((allowed ? 'Allowed waking for ' : 'Blocked waking for ') + name + ' in this room.');
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 401)) setBanner('Could not change wake setting: ' + e.message);
  }
}

async function openInvite() {
  const box = $('invite-box');
  clear(box);
  box.hidden = false;
  try {
    const data = await api('GET', '/participants');
    state.participants = data.participants || [];
  } catch (e) {
    box.appendChild(el('p', { cls: 'muted', text: 'Could not load participants: ' + e.message }));
    return;
  }
  const inRoom = new Set(rosterRows().filter((r) => r.status !== 'left').map((r) => r.participant));
  const candidates = state.participants.filter((p) => !inRoom.has(p.name));
  if (!candidates.length) {
    box.appendChild(el('p', { cls: 'muted', text: 'Everyone registered is already here.' }));
    return;
  }
  for (const p of candidates) {
    box.appendChild(el('button', {
      type: 'button', cls: 'invite-pick', text: p.name + (p.kind ? ' (' + p.kind + ')' : ''),
      on: { click: () => doInvite(p.name) },
    }));
  }
}

async function doInvite(name) {
  try {
    await api('POST', '/rooms/' + encodeURIComponent(state.roomId) + '/invite', { invitee: name });
    $('invite-box').hidden = true;
    setBanner('Invited ' + name + '.');
  } catch (e) {
    setBanner('Invite failed: ' + e.message);
  }
}

/* ---------- composer ---------- */

function aiRoster() {
  return rosterRows().filter((r) => r.kind === 'ai' && r.status !== 'left');
}

function toggleTo(name) {
  if (state.to.has(name)) state.to.delete(name); else state.to.add(name);
  renderToChips();
  renderComposerEffect();
}

function renderToChips() {
  const box = $('to-chips');
  clear(box);
  box.appendChild(el('button', {
    type: 'button', cls: 'chip pick' + (state.to.size === 0 ? ' on' : ''), text: 'everyone',
    aria: { pressed: state.to.size === 0 ? 'true' : 'false' },
    on: { click: () => { state.to.clear(); renderToChips(); renderComposerEffect(); } },
  }));
  for (const p of aiRoster()) {
    const on = state.to.has(p.participant);
    box.appendChild(el('button', {
      type: 'button', cls: 'chip pick' + (on ? ' on' : ''), text: '@' + p.participant,
      aria: { pressed: on ? 'true' : 'false' },
      on: { click: () => toggleTo(p.participant) },
    }));
  }
}

function renderComposerEffect() {
  const box = $('to-effect');
  if (!state.room) { box.textContent = ''; return; }
  const ais = aiRoster();
  if (state.to.size === 0) {
    box.textContent = ais.length
      ? 'Everyone: ' + ais.length + ' AI' + (ais.length === 1 ? '' : 's') + ' will each owe you a reply.'
      : 'No AIs are in this room right now.';
  } else {
    box.textContent = [...state.to].join(', ') + (state.to.size === 1 ? ' owes' : ' each owe') + ' you a reply.';
  }
}

// Posts `text` to the open room with the current To: selection. Returns
// true on success; on failure writes the reason into `noteEl`.
async function sendBody(text, noteEl) {
  const body = text.trim();
  if (!body || !state.roomId) return false;
  const payload = { body };
  if (state.to.size) payload.addressed_to = [...state.to];
  try {
    const res = await api('POST', '/rooms/' + encodeURIComponent(state.roomId) + '/send', payload);
    setBanner('');
    $('compose-note').textContent = 'Sent as #' + res.seq + '.';
    state.stickToBottom = true;
    await pollOnce();
    const s = $('stream');
    s.scrollTop = s.scrollHeight;
    return true;
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 401)) noteEl.textContent = 'Send failed: ' + e.message;
    return false;
  }
}

async function sendCurrent() {
  const ta = $('compose');
  $('btn-send').disabled = true;
  try {
    if (await sendBody(ta.value, $('compose-note'))) ta.value = '';
  } finally {
    $('btn-send').disabled = !!(state.room && state.room.state === 'archived');
  }
}

/* ---------- markdown editor dialog (Teddy, 2026-10-04) ----------
 *
 * A pop-out editor with formatting buttons and a source/preview switch. It
 * shares the quick box's To: selection. Closing it any way other than Send
 * or Discard puts the text back in the quick box, so nothing is lost.
 */

const editor = { outcome: null, preview: false };

function openEditor() {
  if (!state.roomId || (state.room && state.room.state === 'archived')) return;
  const d = $('md-dialog');
  const t = $('md-text');
  t.value = $('compose').value;
  editor.outcome = null;
  setEditorPreview(false);
  $('md-to').textContent = $('to-effect').textContent;
  $('md-note').textContent = 'Ctrl+Enter sends. Ctrl+P switches to the preview.';
  d.showModal();
  t.focus();
  t.setSelectionRange(t.value.length, t.value.length);
}

function closeEditor(outcome) {
  editor.outcome = outcome;
  $('md-dialog').close();
}

function onEditorClosed() {
  // Escape, or the Back button: keep the text in the quick box.
  if (editor.outcome !== 'sent' && editor.outcome !== 'discard') $('compose').value = $('md-text').value;
  if (editor.outcome === 'sent') $('compose').value = '';
  editor.outcome = null;
  $('compose').focus();
}

function setEditorPreview(on) {
  editor.preview = on;
  const t = $('md-text');
  const p = $('md-preview');
  const b = $('md-view');
  if (on) {
    clear(p);
    p.appendChild(renderMarkdown(t.value));
    if (!t.value.trim()) p.appendChild(el('p', { cls: 'muted', text: 'Nothing to preview yet.' }));
    p.style.setProperty('min-height', t.offsetHeight + 'px');
  }
  t.hidden = on;
  p.hidden = !on;
  b.textContent = on ? 'Edit' : 'Preview';
  b.setAttribute('aria-pressed', on ? 'true' : 'false');
  for (const btn of document.querySelectorAll('#md-toolbar [data-md]')) btn.disabled = on;
  if (!on) t.focus();
}

// Replace the textarea's selection, keeping the browser's undo history
// where execCommand still works, falling back to setRangeText.
function replaceSelection(t, text, selStart, selEnd) {
  t.focus();
  const start = t.selectionStart;
  let ok = false;
  try { ok = document.execCommand('insertText', false, text); } catch (e) { ok = false; }
  if (!ok) t.setRangeText(text, t.selectionStart, t.selectionEnd, 'end');
  if (selStart != null) t.setSelectionRange(start + selStart, start + selEnd);
}

function wrapSelection(t, before, after, placeholder) {
  const sel = t.value.slice(t.selectionStart, t.selectionEnd);
  const inner = sel || placeholder;
  replaceSelection(t, before + inner + after, before.length, before.length + inner.length);
}

// Prefix every line the selection touches (headings, quotes, lists).
function prefixLines(t, makePrefix) {
  const v = t.value;
  const ls = t.selectionStart === 0 ? 0 : v.lastIndexOf('\n', t.selectionStart - 1) + 1;
  let end = t.selectionEnd;
  if (end > t.selectionStart && v[end - 1] === '\n') end--; // selection ending at a line break
  let le = v.indexOf('\n', end);
  if (le < 0) le = v.length;
  const lines = v.slice(ls, le).split('\n');
  const out = lines.map((l, k) => makePrefix(k) + l).join('\n');
  t.setSelectionRange(ls, le);
  replaceSelection(t, out, 0, out.length);
}

function applyFormat(kind) {
  const t = $('md-text');
  switch (kind) {
    case 'bold': wrapSelection(t, '**', '**', 'bold text'); break;
    case 'italic': wrapSelection(t, '*', '*', 'italic text'); break;
    case 'strike': wrapSelection(t, '~~', '~~', 'struck text'); break;
    case 'code': wrapSelection(t, '`', '`', 'code'); break;
    case 'codeblock': {
      const atLineStart = t.selectionStart === 0 || t.value[t.selectionStart - 1] === '\n';
      wrapSelection(t, (atLineStart ? '' : '\n') + '```\n', '\n```\n', 'code');
      break;
    }
    case 'link': {
      const sel = t.value.slice(t.selectionStart, t.selectionEnd);
      if (safeUrl(sel)) {
        replaceSelection(t, '[link text](' + sel + ')', 1, 10);
      } else {
        const label = sel || 'link text';
        const text = '[' + label + '](https://)';
        replaceSelection(t, text, label.length + 3, label.length + 11);
      }
      break;
    }
    case 'heading': prefixLines(t, () => '## '); break;
    case 'quote': prefixLines(t, () => '> '); break;
    case 'ul': prefixLines(t, () => '- '); break;
    case 'ol': prefixLines(t, (k) => (k + 1) + '. '); break;
    case 'hr': replaceSelection(t, '\n\n---\n\n', 7, 7); break;
    default: break;
  }
}

async function sendFromEditor() {
  const b = $('md-send');
  b.disabled = true;
  try {
    if (await sendBody($('md-text').value, $('md-note'))) closeEditor('sent');
  } finally {
    b.disabled = false;
  }
}

function wireEditor() {
  const d = $('md-dialog');
  $('btn-editor').addEventListener('click', openEditor);
  $('md-toolbar').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-md]');
    if (btn && !btn.disabled) applyFormat(btn.dataset.md);
  });
  $('md-view').addEventListener('click', () => setEditorPreview(!editor.preview));
  $('md-send').addEventListener('click', sendFromEditor);
  $('md-back').addEventListener('click', () => closeEditor('back'));
  $('md-cancel').addEventListener('click', () => closeEditor('discard'));
  d.addEventListener('close', onEditorClosed);
  d.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    const k = e.key.toLowerCase();
    if (k === 'enter') { e.preventDefault(); sendFromEditor(); }
    else if (k === 'p') { e.preventDefault(); setEditorPreview(!editor.preview); }
    else if (!editor.preview && (k === 'b' || k === 'i' || k === 'k')) {
      e.preventDefault();
      applyFormat(k === 'b' ? 'bold' : k === 'i' ? 'italic' : 'link');
    }
  });
}

/* ---------- room controls ---------- */

async function doResume(n) {
  try {
    const body = Number.isInteger(n) && n > 0 ? { granted_hops: n } : {};
    await api('POST', '/rooms/' + encodeURIComponent(state.roomId) + '/resume', body);
    await pollOnce();
  } catch (e) { setBanner('Resume failed: ' + e.message); }
}

async function doPause() {
  try {
    await api('POST', '/rooms/' + encodeURIComponent(state.roomId) + '/pause', {});
    await pollOnce();
  } catch (e) { setBanner('Pause failed: ' + e.message); }
}

async function doArchive() {
  // Confirmation is inline (no browser dialogs): a second click within a
  // few seconds commits.
  const b = $('btn-archive');
  if (b.dataset.armed !== '1') {
    b.dataset.armed = '1';
    b.textContent = 'Click again to archive';
    setTimeout(() => { b.dataset.armed = '0'; b.textContent = 'Archive'; }, 4000);
    return;
  }
  b.dataset.armed = '0';
  b.textContent = 'Archive';
  try {
    await api('POST', '/rooms/' + encodeURIComponent(state.roomId) + '/archive', {});
    await pollOnce();
    await refreshSummary();
  } catch (e) { setBanner('Archive failed: ' + e.message); }
}

/* ---------- wiring ---------- */

function wire() {
  $('composer').addEventListener('submit', (e) => { e.preventDefault(); sendCurrent(); });
  $('compose').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendCurrent(); }
  });
  $('stream').addEventListener('scroll', () => {
    state.stickToBottom = isNearBottom();
    if (state.stickToBottom) { state.unseenBelow = 0; updateNewBelow(); maybeAck(); }
  });
  $('new-below').addEventListener('click', () => {
    const s = $('stream');
    s.scrollTop = s.scrollHeight;
  });
  $('btn-pause').addEventListener('click', doPause);
  $('btn-archive').addEventListener('click', doArchive);
  $('btn-notify').addEventListener('click', toggleNotify);
  $('btn-mute').addEventListener('click', toggleMute);
  $('btn-md').addEventListener('click', toggleMarkdownView);
  $('btn-archived').addEventListener('click', toggleArchived);
  $('btn-new-room').addEventListener('click', () => showNewRoom($('new-room').hidden));
  $('nr-cancel').addEventListener('click', () => { $('new-room').reset(); showNewRoom(false); });
  $('new-room').addEventListener('submit', (e) => { e.preventDefault(); createRoom(); });
  wireEditor();
  $('btn-logout').addEventListener('click', () => signOut(''));
  $('btn-rooms').addEventListener('click', () => document.body.classList.toggle('show-rooms'));
  $('btn-people').addEventListener('click', () => document.body.classList.toggle('show-people'));
}

async function boot() {
  wire();
  loadNotifyPrefs();
  loadView();
  updateMdButton();
  updateNotifyUi();
  state.creds = loadCreds();
  if (!state.creds) { signOut(''); return; }
  $('signin').hidden = true;
  $('layout').hidden = false;
  $('whoami').textContent = state.creds.name;
  try {
    await refreshSummary();
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 401)) setBanner('Could not reach the daemon: ' + e.message);
  }
  state.summaryTimer = setInterval(summaryTick, SUMMARY_MS);
}

boot();
