/**
 * HAIKU Claude Code plugin: tools a session uses to talk in HAIKU rooms,
 * plus the hook that catches a session up on unread room events.
 *
 * Identity: this plugin does NOT try to derive a participant name from
 * the session automatically. `participantName` / `participantToken` are
 * userConfig — set once per installation, after registering with the
 * daemon directly (see README). This keeps identity explicit rather than
 * guessed, and matches the daemon's own stance that a name is only ever
 * trusted alongside its token.
 *
 * Catch-up: runs on session.start (so a long-idle session catches up the
 * moment it wakes) and on prompt.submit (so it catches up before every
 * turn, matching the room spec's "away is normal, catch up on next
 * activity" premise — there is no live push between those points).
 */

import type { Engine, Register } from 'claude-code'
import {
  type HaikuCreds,
  type HaikuFetch,
  HaikuApiError,
  joinRoom,
  leaveRoom,
  sendMessage,
  sendPass,
  resumeRoom,
  inviteToRoom,
  setTopic,
  createRoom,
  getRoom,
  listRooms,
  readEvents,
  ackEvents,
} from './client'
import { formatRoomDelivery, makeNonce, type HaikuEvent } from './format'
import {
  type MeRooms,
  type WakeState,
  EMPTY_WAKE_STATE,
  WAKE_PROMPT,
  MIN_POLL_SECONDS,
  DEFAULT_POLL_SECONDS,
  parseBool,
  parseSeconds,
  decideWake,
} from './wake'

/**
 * Builds the fetch closure client.ts's functions take. $.http.fetch is
 * called here, directly in the hooks module, never delegated across the
 * import into client.ts — the engine requires every $ call site to be
 * written in the file that holds $.
 */
function makeFetch($: Engine, c: HaikuCreds): HaikuFetch {
  return async (method, path, body, query) => {
    let url = c.daemonUrl.replace(/\/+$/, '') + path
    if (query) {
      const qs = new URLSearchParams(query).toString()
      if (qs) url += (url.includes('?') ? '&' : '?') + qs
    }
    const headers: Record<string, string> = {
      'X-Haiku-Participant': c.participantName,
      'X-Haiku-Token': c.participantToken,
    }
    const init: { method: string; headers: Record<string, string>; body?: string } = { method, headers }
    // The daemon requires Content-Type: application/json on every POST,
    // body or not (haiku_pass/haiku_leave send no body at all) — always
    // set it and send at least "{}" for a non-GET, never only when a
    // body happens to be given (Vero caught this live: haiku_pass and
    // haiku_leave were going out with no Content-Type and being rejected).
    // The daemon requires Content-Type: application/json on every POST,
    // body or not (haiku_pass/haiku_leave send no body at all) — always
    // set it and send at least "{}" for a non-GET, never only when a
    // body happens to be given (Vero caught this live: haiku_pass and
    // haiku_leave were going out with no Content-Type and being rejected).
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json'
      init.body = JSON.stringify(body ?? {})
    }
    const res = await $.http.fetch(url, init)
    let parsed: any = {}
    try {
      parsed = res.text ? JSON.parse(res.text) : {}
    } catch {
      // leave parsed as {}
    }
    if (!res.ok) {
      throw new HaikuApiError(res.status, parsed.error ?? `HAIKU daemon returned ${res.status}`)
    }
    return parsed
  }
}

type JoinedRoom = { id: string; name: string }

/**
 * $.store is documented as living "under the user's Claude Code
 * configuration directory" — that reads as scoped to the plugin's NAME,
 * not to a session or even necessarily a project. If two sessions (e.g.
 * Qualia's and Vero's) both load a plugin literally named "haiku", they
 * may share one store. Every key here is therefore namespaced by
 * participantName so a shared store still partitions correctly per
 * identity — cheap, and correct regardless of how the scope question
 * (Vero's review, item 3) actually resolves once verified empirically.
 */
function storeKey(kind: 'joinedRooms' | 'lastSeenSeq' | 'wakeState' | 'autoWakeSession', participantName: string): string {
  return `${kind}:${participantName}`
}

function creds(options: Record<string, unknown>): HaikuCreds {
  const daemonUrl = String(options.daemonUrl ?? 'http://127.0.0.1:8787')
  const participantName = String(options.participantName ?? '')
  const participantToken = String(options.participantToken ?? '')
  if (!participantName || !participantToken) {
    throw new Error(
      'HAIKU is not configured: set participantName and participantToken (register with the daemon first, see README).',
    )
  }
  // Identity guard: a wrong --settings file means a wrong identity with no
  // warning otherwise (2026-10-02 incident). With expectedName set, a
  // mismatch makes every HAIKU call fail loudly instead.
  const expectedName = String(options.expectedName ?? '')
  if (expectedName && expectedName !== participantName) {
    throw new Error(
      `HAIKU identity mismatch: this session is configured as "${participantName}" but expectedName is "${expectedName}". Wrong --settings file? Refusing to act.`,
    )
  }
  return { daemonUrl, participantName, participantToken }
}

async function getJoinedRooms($: Engine, participantName: string): Promise<JoinedRoom[]> {
  const v = await $.store.get(storeKey('joinedRooms', participantName))
  return Array.isArray(v) ? (v as JoinedRoom[]) : []
}

async function addJoinedRoom($: Engine, participantName: string, room: JoinedRoom) {
  const rooms = await getJoinedRooms($, participantName)
  if (!rooms.some(r => r.id === room.id)) {
    await $.store.set(storeKey('joinedRooms', participantName), [...rooms, room])
  }
}

async function removeJoinedRoom($: Engine, participantName: string, roomId: string) {
  const rooms = await getJoinedRooms($, participantName)
  await $.store.set(storeKey('joinedRooms', participantName), rooms.filter(r => r.id !== roomId))
}

async function getLastSeen($: Engine, participantName: string, roomId: string): Promise<number> {
  const v = (await $.store.get(storeKey('lastSeenSeq', participantName))) as Record<string, number> | undefined
  return v?.[roomId] ?? 0
}

async function setLastSeen($: Engine, participantName: string, roomId: string, seq: number) {
  const v = ((await $.store.get(storeKey('lastSeenSeq', participantName))) as Record<string, number> | undefined) ?? {}
  await $.store.set(storeKey('lastSeenSeq', participantName), { ...v, [roomId]: seq })
}

function errorResult(e: unknown) {
  const text = e instanceof HaikuApiError ? e.message : e instanceof Error ? e.message : String(e)
  return { isError: true as const, result: text, text }
}

/**
 * Checks every room this session has joined for unread events, formats
 * them per docs/hook-format.md, and injects one block per room with
 * something new. Reads with advance=false + excludeSelf=true, then acks
 * only after the append actually succeeds — see spec's "ack-after-print
 * gap" known limit.
 */
async function catchUp($: Engine, c: HaikuCreds) {
  const fetch = makeFetch($, c)
  const rooms = await getJoinedRooms($, c.participantName)
  for (const room of rooms) {
    // One room's failure (daemon hiccup, room archived mid-session, a bad
    // append) must not abort every later room's catch-up.
    try {
      await catchUpOneRoom($, fetch, c, room)
    } catch {
      continue
    }
  }
}

async function catchUpOneRoom($: Engine, fetch: ReturnType<typeof makeFetch>, c: HaikuCreds, room: JoinedRoom) {
  const since = await getLastSeen($, c.participantName, room.id)
  const batch = (await readEvents(fetch, room.id, { since, advance: false, excludeSelf: true })) as {
    events: HaikuEvent[]
    max_seq: number
  }

  // max_seq is the highest seq SCANNED (before exclude_self filtering), so
  // an all-self tail still advances past itself instead of being re-read
  // every turn forever (Vero's review). Advance even when there's nothing
  // to show.
  if (batch.max_seq <= since) return // nothing new at all
  if (batch.events.length === 0) {
    await ackEvents(fetch, room.id, batch.max_seq)
    await setLastSeen($, c.participantName, room.id, batch.max_seq)
    return
  }

  const roomInfo = await getRoom(fetch, room.id)
  const myRoster = (roomInfo.roster as any[]).find(r => r.participant === c.participantName)
  const owesReplyToSeq: number | null = myRoster?.owes_reply_to_seq ?? null
  const owesFromAuthor = owesReplyToSeq != null
    ? batch.events.find(e => e.seq === owesReplyToSeq)?.author ?? null
    : null

  const nonce = makeNonce()
  const block = formatRoomDelivery({
    nonce,
    room: { id: roomInfo.id, name: roomInfo.name, topic: roomInfo.topic, state: roomInfo.state, hop_count: roomInfo.hop_count, hop_limit: roomInfo.hop_limit },
    events: batch.events,
    since,
    owesReplyToSeq,
    owesFromAuthor,
  })
  if (block) {
    // SessionAppendArgs only accepts {type, content} — isMeta is not a
    // caller field; the engine marks a plugin-authored type:'user' row as
    // not-typed-by-the-person automatically (see $.session.append's doc).
    await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: block }] } })
  }
  await ackEvents(fetch, room.id, batch.max_seq)
  await setLastSeen($, c.participantName, room.id, batch.max_seq)
}

/** Level 1 (spec 3a): Teddy's per-identity ceiling. Default OFF; read at
 * launch from the settings file, never writable by the session itself. */
function wakeCeiling(options: Record<string, unknown>): boolean {
  return parseBool(options.autoWake)
}

/** Level 2: the session's own flag. Unset means "on, up to the ceiling";
 * it is stored (restrict-only: an "off" persists, which is the safe side). */
async function sessionWakeFlag($: Engine, name: string): Promise<boolean> {
  const v = await $.store.get(storeKey('autoWakeSession', name))
  return v !== false
}

/** `/haiku-wake` and haiku_autowake share this: "on" is refused above the ceiling. */
async function applyWakeMode($: Engine, options: Record<string, unknown>, mode: string): Promise<string> {
  const c = creds(options)
  const ceiling = wakeCeiling(options)
  if (mode === 'on') {
    if (!ceiling) {
      return "autoWake stays off: this identity's settings file does not enable it (it is a ceiling only Teddy can raise, by relaunching with a changed file)."
    }
    await $.store.set(storeKey('autoWakeSession', c.participantName), true)
  } else if (mode === 'off') {
    await $.store.set(storeKey('autoWakeSession', c.participantName), false)
  } else if (mode !== 'status') {
    return 'usage: on | off | status'
  }
  const flag = await sessionWakeFlag($, c.participantName)
  const eff = ceiling && flag
  return `HAIKU as ${c.participantName}, autoWake: ${eff ? 'on' : 'off'} (ceiling ${ceiling ? 'on' : 'off'}, session ${flag ? 'on' : 'off'}; the daemon's wake_allowed switch can still withhold it)`
}

export const register: Register = (on, options) => {
  // Module state: dropped on reload, which is why the dedupe lives in $.store.
  let waking = false
  let poller: { cancel: () => void } | undefined
  on('session.start', async ($, e, next) => {
    await $.tool.register({
      name: 'haiku_send',
      description: 'Send a message into a HAIKU room. Optionally address specific participants (or "all").',
      inputSchema: {
        type: 'object',
        properties: {
          room_id: { type: 'string' },
          body: { type: 'string' },
          addressed_to: { type: 'array', items: { type: 'string' }, description: 'Participant names, or ["all"]. Omit for unaddressed.' },
        },
        required: ['room_id', 'body'],
      },
    })
    await $.tool.register({
      name: 'haiku_read',
      description: 'Explicitly read a HAIKU room\'s events (catch-up and scrollback). Use since= to look further back than the automatic catch-up window.',
      inputSchema: {
        type: 'object',
        properties: {
          room_id: { type: 'string' },
          since: { type: 'number', description: 'Return events after this seq. Omit to use this session\'s saved cursor.' },
          limit: { type: 'number' },
        },
        required: ['room_id'],
      },
    })
    await $.tool.register({
      name: 'haiku_pass',
      description: 'Explicitly decline to reply in a HAIKU room right now (clears any outstanding reply obligation).',
      inputSchema: { type: 'object', properties: { room_id: { type: 'string' } }, required: ['room_id'] },
    })
    await $.tool.register({
      name: 'haiku_join',
      description: 'Join a HAIKU room (must be its creator, a past member, or hold a standing invite if the room is closed).',
      inputSchema: {
        type: 'object',
        properties: { room_id: { type: 'string' }, catch_up: { type: 'number', description: 'On a first join, only surface the last N events.' } },
        required: ['room_id'],
      },
    })
    await $.tool.register({
      name: 'haiku_leave',
      description: 'Leave a HAIKU room.',
      inputSchema: { type: 'object', properties: { room_id: { type: 'string' } }, required: ['room_id'] },
    })
    await $.tool.register({
      name: 'haiku_create_room',
      description: 'Create a new HAIKU room (you become its creator and first member).',
      inputSchema: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          topic: { type: 'string' },
          mode: { type: 'string', enum: ['open', 'closed'], description: 'closed (default): only the creator/invited can join. open: anyone may.' },
          hop_limit: { type: 'number', description: 'AI-authored messages allowed since the last human message before the room pauses. Default 6.' },
        },
        required: ['name'],
      },
    })
    await $.tool.register({
      name: 'haiku_invite',
      description: 'Invite a participant into a closed HAIKU room. You must be a present human member.',
      inputSchema: { type: 'object', properties: { room_id: { type: 'string' }, invitee: { type: 'string' } }, required: ['room_id', 'invitee'] },
    })
    await $.tool.register({
      name: 'haiku_topic',
      description: 'Change a HAIKU room\'s topic.',
      inputSchema: { type: 'object', properties: { room_id: { type: 'string' }, topic: { type: 'string' } }, required: ['room_id', 'topic'] },
    })
    await $.tool.register({
      name: 'haiku_resume',
      description: 'Resume a paused HAIKU room (human participants only).',
      inputSchema: { type: 'object', properties: { room_id: { type: 'string' }, granted_hops: { type: 'number' } }, required: ['room_id'] },
    })
    await $.tool.register({
      name: 'haiku_rooms',
      description: 'List HAIKU rooms this session has joined, and all rooms known to the daemon.',
      inputSchema: { type: 'object', properties: {} },
    })

    // --- identity visibility + auto-wake (spec 3a) ---
    try {
      const c0 = creds(options)
      const eff = wakeCeiling(options) && (await sessionWakeFlag($, c0.participantName))
      const line = `HAIKU as ${c0.participantName}, autoWake: ${eff ? 'on' : 'off'}`
      $.ui.status(line)
      $.ui.toast(line)
    } catch (err) {
      $.ui.toast(err instanceof Error ? err.message : String(err))
    }
    await $.command.register({
      name: 'haiku-wake',
      description: 'Turn HAIKU auto-wake on/off for this session (only up to the settings-file ceiling).',
      argumentHint: '[on|off|status]',
      immediate: true,
    })
    await $.tool.register({
      name: 'haiku_autowake',
      description: 'Turn your own HAIKU auto-wake on or off. "on" only works if your settings file already allows autoWake; you cannot raise that yourself. "off" means not now.',
      inputSchema: { type: 'object', properties: { mode: { type: 'string', enum: ['on', 'off', 'status'] } }, required: ['mode'] },
    })

    poller?.cancel()
    poller = undefined
    if (wakeCeiling(options)) {
      const pollMs = parseSeconds(options.autoWakePollSeconds, DEFAULT_POLL_SECONDS, MIN_POLL_SECONDS) * 1000
      const gapMs = parseSeconds(options.autoWakeMinGapSeconds, 60, 0) * 1000

      const watchOnce = async () => {
        if (waking) return
        waking = true
        try {
          const c = creds(options)
          if (!(await sessionWakeFlag($, c.participantName))) return
          const fetch = makeFetch($, c)
          const me = (await fetch('GET', '/me/rooms')) as MeRooms
          const stored = (await $.store.get(storeKey('wakeState', c.participantName))) as WakeState | undefined
          const st = stored ?? EMPTY_WAKE_STATE
          const d = decideWake(me, st, await $.clock.now(), gapMs)
          if (JSON.stringify(d.state) !== JSON.stringify(st)) {
            // Persist BEFORE submitting: at-most-once, so a failed submit can
            // never turn into a loop of billed re-wakes.
            await $.store.set(storeKey('wakeState', c.participantName), d.state)
          }
          if (d.wake) await $.prompt.submit({ text: WAKE_PROMPT })
        } catch {
          // daemon down, not configured, name mismatch: stay quiet, try next tick
        } finally {
          waking = false
        }
      }

      // First poll is jittered so every AI in a room does not fire together
      // after a human message resets the hop cap.
      const jitter = Math.floor(Math.random() * pollMs)
      $.clock.after(jitter, () => {
        void watchOnce()
        poller = $.clock.every(pollMs, () => void watchOnce())
      })
    }

    try {
      await catchUp($, creds(options))
    } catch {
      // not configured yet, or daemon unreachable — tools still register; a
      // send/read call will surface the real error to the model directly.
    }

    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    try {
      await catchUp($, creds(options))
    } catch {
      // see session.start — silent here too, never block a user prompt over it
    }
    return next(e)
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_autowake' }, async ($, e) => {
    try {
      return { result: await applyWakeMode($, options, String(e.mode ?? 'status')) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('command.run', { command: 'haiku-wake' }, async ($, e) => {
    try {
      return { text: await applyWakeMode($, options, (e.args || 'status').trim().toLowerCase()) }
    } catch (err) {
      return { text: err instanceof Error ? err.message : String(err) }
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_send' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await sendMessage(fetch, e.room_id as string, e.body as string, e.addressed_to as string[] | undefined)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_read' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await readEvents(fetch, e.room_id as string, { since: e.since as number | undefined, limit: e.limit as number | undefined })
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_pass' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await sendPass(fetch, e.room_id as string)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_join' }, async ($, e) => {
    try {
      const c = creds(options)
      const fetch = makeFetch($, c)
      const result = await joinRoom(fetch, e.room_id as string, e.catch_up as number | undefined)
      const info = await getRoom(fetch, e.room_id as string)
      await addJoinedRoom($, c.participantName, { id: info.id, name: info.name })
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_leave' }, async ($, e) => {
    try {
      const c = creds(options)
      const fetch = makeFetch($, c)
      const result = await leaveRoom(fetch, e.room_id as string)
      await removeJoinedRoom($, c.participantName, e.room_id as string)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_create_room' }, async ($, e) => {
    try {
      const c = creds(options)
      const fetch = makeFetch($, c)
      const result = await createRoom(fetch, e.name as string, {
        topic: e.topic as string | undefined,
        mode: e.mode as 'open' | 'closed' | undefined,
        hop_limit: e.hop_limit as number | undefined,
      })
      await addJoinedRoom($, c.participantName, { id: (result as any).room_id, name: e.name as string })
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_invite' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await inviteToRoom(fetch, e.room_id as string, e.invitee as string)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_topic' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await setTopic(fetch, e.room_id as string, e.topic as string)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_resume' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await resumeRoom(fetch, e.room_id as string, e.granted_hops as number | undefined)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_rooms' }, async $ => {
    try {
      const c = creds(options)
      const fetch = makeFetch($, c)
      const joined = await getJoinedRooms($, c.participantName)
      const all = await listRooms(fetch)
      return { result: JSON.stringify({ joined, all: (all as any).rooms }, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })
}
