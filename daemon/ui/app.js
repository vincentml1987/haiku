/*
 * HAIKU human UI (docs/ui-spec.md). One file, no dependencies.
 *
 * Trust rule: every string that came from a participant (body, name, room
 * name, topic, reason) is untrusted. It is only ever put into the page with
 * textContent / createTextNode, never innerHTML, and never used to build a
 * URL, selector, id or handler. The only DOM-building helper is el().
 */
'use strict';

const LS_KEY = 'haiku.creds';
const LS_NOTIFY = 'haiku.notify';
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
  to: new Set(),         // selected addressees (empty = everyone)
  hasSummaryRoute: true,
  stickToBottom: true,
  unseenBelow: 0,
  bannerKey: null,       // what the pause banner currently shows
  peopleKey: null,       // what the people panel currently shows
  pollTimer: null,
  summaryTimer: null,
  polling: false,
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
    ['Archived', (r) => r.state === 'archived'],
  ];
  for (const [label, pred] of groups) {
    const rows = state.summary.filter(pred);
    if (!rows.length) continue;
    nav.appendChild(el('h2', { text: label + ' (' + rows.length + ')' }));
    for (const r of rows) {
      const kids = [el('span', { cls: 'room-name', text: r.name })];
      if (r.unread > 0) kids.push(el('span', { cls: 'badge', text: r.unread, title: r.unread + ' unread' }));
      if (r.state === 'paused') kids.push(el('span', { cls: 'chip paused', text: 'paused' }));
      if (r.state === 'archived') kids.push(el('span', { cls: 'chip', text: 'archived' }));
      const btn = el('button', {
        type: 'button',
        cls: 'room-row' + (r.id === state.roomId ? ' current' : ''),
        data: { room: r.id },
        aria: { current: r.id === state.roomId ? 'true' : 'false' },
        on: { click: () => openRoom(r.id) },
      }, kids);
      nav.appendChild(btn);
    }
  }
  if (!state.summary.length && !invites.length) nav.appendChild(el('p', { cls: 'muted', text: 'No rooms yet.' }));
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
  const body = el('div', { cls: 'msg-body' });
  const full = bodyText;
  if (lines.length > FOLD_LINES) {
    body.textContent = lines.slice(0, FOLD_LINES).join('\n');
    const more = el('button', {
      type: 'button', cls: 'linklike', text: 'show all ' + lines.length + ' lines',
      on: { click: () => { body.textContent = full; more.remove(); } },
    });
    const wrap = el('div', null, [body, more]);
    const art = el('article', { cls: 'msg ' + (isHuman ? 'by-human' : 'by-ai'), id: 'seq-' + ev.seq }, [head, wrap]);
    art.style.setProperty('--hue', String(hueOf(ev.author)));
    return art;
  }
  body.textContent = full;
  const art = el('article', { cls: 'msg ' + (isHuman ? 'by-human' : 'by-ai'), id: 'seq-' + ev.seq }, [head, body]);
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
  const key = JSON.stringify(rosterRows()) + '|' + Math.floor(Date.now() / 60000) + '|' + state.events.length;
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

async function sendCurrent() {
  const ta = $('compose');
  const body = ta.value.trim();
  if (!body || !state.roomId) return;
  const payload = { body };
  if (state.to.size) payload.addressed_to = [...state.to];
  $('btn-send').disabled = true;
  try {
    const res = await api('POST', '/rooms/' + encodeURIComponent(state.roomId) + '/send', payload);
    ta.value = '';
    setBanner('');
    $('compose-note').textContent = 'Sent as #' + res.seq + '.';
    state.stickToBottom = true;
    await pollOnce();
    const s = $('stream');
    s.scrollTop = s.scrollHeight;
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 401)) $('compose-note').textContent = 'Send failed: ' + e.message;
  } finally {
    $('btn-send').disabled = !!(state.room && state.room.state === 'archived');
  }
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
  $('btn-logout').addEventListener('click', () => signOut(''));
  $('btn-rooms').addEventListener('click', () => document.body.classList.toggle('show-rooms'));
  $('btn-people').addEventListener('click', () => document.body.classList.toggle('show-people'));
}

async function boot() {
  wire();
  loadNotifyPrefs();
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
