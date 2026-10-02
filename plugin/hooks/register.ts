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
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json'
      init.body = JSON.stringify(body)
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

const STORE_JOINED_ROOMS = 'joinedRooms'
const STORE_LAST_SEEN = 'lastSeenSeq' // { [roomId]: number } — client-side hint only, for the recovery line

function creds(options: Record<string, unknown>): HaikuCreds {
  const daemonUrl = String(options.daemonUrl ?? 'http://127.0.0.1:8787')
  const participantName = String(options.participantName ?? '')
  const participantToken = String(options.participantToken ?? '')
  if (!participantName || !participantToken) {
    throw new Error(
      'HAIKU is not configured: set participantName and participantToken (register with the daemon first, see README).',
    )
  }
  return { daemonUrl, participantName, participantToken }
}

async function getJoinedRooms($: Engine): Promise<JoinedRoom[]> {
  const v = await $.store.get(STORE_JOINED_ROOMS)
  return Array.isArray(v) ? (v as JoinedRoom[]) : []
}

async function addJoinedRoom($: Engine, room: JoinedRoom) {
  const rooms = await getJoinedRooms($)
  if (!rooms.some(r => r.id === room.id)) {
    await $.store.set(STORE_JOINED_ROOMS, [...rooms, room])
  }
}

async function removeJoinedRoom($: Engine, roomId: string) {
  const rooms = await getJoinedRooms($)
  await $.store.set(STORE_JOINED_ROOMS, rooms.filter(r => r.id !== roomId))
}

async function getLastSeen($: Engine, roomId: string): Promise<number> {
  const v = (await $.store.get(STORE_LAST_SEEN)) as Record<string, number> | undefined
  return v?.[roomId] ?? 0
}

async function setLastSeen($: Engine, roomId: string, seq: number) {
  const v = ((await $.store.get(STORE_LAST_SEEN)) as Record<string, number> | undefined) ?? {}
  await $.store.set(STORE_LAST_SEEN, { ...v, [roomId]: seq })
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
  const rooms = await getJoinedRooms($)
  for (const room of rooms) {
    let batch: { events: HaikuEvent[] }
    try {
      batch = (await readEvents(fetch, room.id, { advance: false, excludeSelf: true })) as { events: HaikuEvent[] }
    } catch {
      continue // daemon unreachable or room gone — don't block the turn over it
    }
    if (batch.events.length === 0) continue

    let roomInfo: any
    try {
      roomInfo = await getRoom(fetch, room.id)
    } catch {
      continue
    }
    const myRoster = (roomInfo.roster as any[]).find(r => r.participant === c.participantName)
    const owesReplyToSeq: number | null = myRoster?.owes_reply_to_seq ?? null
    const owesFromAuthor = owesReplyToSeq != null
      ? batch.events.find(e => e.seq === owesReplyToSeq)?.author ?? null
      : null

    const since = await getLastSeen($, room.id)
    const nonce = makeNonce()
    const block = formatRoomDelivery({
      nonce,
      room: { id: roomInfo.id, name: roomInfo.name, topic: roomInfo.topic, state: roomInfo.state, hop_count: roomInfo.hop_count, hop_limit: roomInfo.hop_limit },
      events: batch.events,
      since,
      owesReplyToSeq,
      owesFromAuthor,
    })
    if (!block) continue

    const lastSeq = batch.events[batch.events.length - 1].seq
    await $.session.append({ message: { type: 'user', isMeta: true, content: [{ type: 'text', text: block }] } })
    await ackEvents(fetch, room.id, lastSeq)
    await setLastSeen($, room.id, lastSeq)
  }
}

export const register: Register = (on, options) => {
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

  on('tool.call', { tool: 'mcp__haiku__haiku_send' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await sendMessage(fetch, e.room_id as string, e.body as string, e.addressed_to as string[] | undefined)
      return { result }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_read' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await readEvents(fetch, e.room_id as string, { since: e.since as number | undefined, limit: e.limit as number | undefined })
      return { result }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_pass' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await sendPass(fetch, e.room_id as string)
      return { result }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_join' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await joinRoom(fetch, e.room_id as string, e.catch_up as number | undefined)
      const info = await getRoom(fetch, e.room_id as string)
      await addJoinedRoom($, { id: info.id, name: info.name })
      return { result }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_leave' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await leaveRoom(fetch, e.room_id as string)
      await removeJoinedRoom($, e.room_id as string)
      return { result }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_create_room' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await createRoom(fetch, e.name as string, {
        topic: e.topic as string | undefined,
        mode: e.mode as 'open' | 'closed' | undefined,
        hop_limit: e.hop_limit as number | undefined,
      })
      await addJoinedRoom($, { id: (result as any).room_id, name: e.name as string })
      return { result }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_invite' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await inviteToRoom(fetch, e.room_id as string, e.invitee as string)
      return { result }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_topic' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await setTopic(fetch, e.room_id as string, e.topic as string)
      return { result }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_resume' }, async ($, e) => {
    try {
      const fetch = makeFetch($, creds(options))
      const result = await resumeRoom(fetch, e.room_id as string, e.granted_hops as number | undefined)
      return { result }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_rooms' }, async $ => {
    try {
      const fetch = makeFetch($, creds(options))
      const joined = await getJoinedRooms($)
      const all = await listRooms(fetch)
      return { result: { joined, all: (all as any).rooms } }
    } catch (err) {
      return errorResult(err)
    }
  })
}
