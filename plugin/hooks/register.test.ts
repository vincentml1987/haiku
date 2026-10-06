import { test, expect, mock } from 'claude-code/testing'

const OPTIONS = {
  daemonUrl: 'http://fake-daemon.invalid',
  participantName: 'TestAI',
  participantToken: 'tok-123',
}

test('haiku_send tool.call result is a string, not a bare object', { options: OPTIONS }, async ($, on) => {
  on('http.fetch', (_$, e, _next) => {
    return {
      value: {
        status: 200,
        ok: true,
        headers: {},
        text: JSON.stringify({ seq: 5, room_state: 'active' }),
      },
    }
  })

  const res = await $.tool.call({
    tool: 'mcp__haiku__haiku_send',
    room_id: 'room-1',
    body: 'hello',
  } as any)

  expect(typeof (res as any).result).toBe('string')
  expect((res as any).result.includes('"seq": 5')).toBe(true)
})

test('haiku_rooms tool.call result is a string too (its own object-literal return path)', { options: OPTIONS }, async ($, on) => {
  mock.store(on)
  on('http.fetch', (_$, e, _next) => {
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ rooms: [{ id: 'r1', name: 'lobby' }] }) } }
  })

  const res = await $.tool.call({ tool: 'mcp__haiku__haiku_rooms' } as any)

  expect(typeof (res as any).result).toBe('string')
  expect((res as any).result.includes('lobby')).toBe(true)
})

// Vero caught this live: haiku_pass and haiku_leave send no body, and the
// old makeFetch only set Content-Type inside `if (body !== undefined)`, so
// those POSTs went out with no Content-Type and the daemon's own check
// (correctly) rejected them with 400. The mocked http.fetch in the tests
// above couldn't see this — it never looked at the request headers, only
// answered with a canned response. This test records every POST's actual
// init and asserts Content-Type is present, body or not, for every POST
// tool, so a regression here fails loudly instead of only showing up live.
const POST_TOOLS: Array<{ tool: string; input: Record<string, unknown> }> = [
  { tool: 'mcp__haiku__haiku_send', input: { room_id: 'r1', body: 'hi' } },
  { tool: 'mcp__haiku__haiku_pass', input: { room_id: 'r1' } },
  { tool: 'mcp__haiku__haiku_join', input: { room_id: 'r1' } },
  { tool: 'mcp__haiku__haiku_leave', input: { room_id: 'r1' } },
  { tool: 'mcp__haiku__haiku_create_room', input: { name: 'new-room' } },
  { tool: 'mcp__haiku__haiku_invite', input: { room_id: 'r1', invitee: 'Someone' } },
  { tool: 'mcp__haiku__haiku_topic', input: { room_id: 'r1', topic: 'new topic' } },
  { tool: 'mcp__haiku__haiku_resume', input: { room_id: 'r1' } },
  { tool: 'mcp__haiku__haiku_mute', input: { room_id: 'r1', muted: true } },
  { tool: 'mcp__haiku__haiku_propose_send', input: { backchannel_id: 'bc', target_room_id: 'r1', body: 'hi' } },
  { tool: 'mcp__haiku__haiku_vote', input: { proposal_id: 1, vote: 'no', reason: 'x' } },
  { tool: 'mcp__haiku__haiku_cancel_proposal', input: { proposal_id: 1 } },
]

// 2026-10-04 mute: catch-up (prompt.submit) stays silent on a muted room
// without acking, and still delivers it when an unseen human message is
// addressed to this AI by name. The daemon's owes_reply_to_seq is null in
// both cases on purpose: a later human message to someone else clears it,
// so the breakthrough must not depend on it (Tessera's review of 3f88169).
function catchUpRig(on: any, addressedTo: string[] | null) {
  const paths: string[] = []
  const appended: string[] = []
  on('http.fetch', (_$: any, e: any) => {
    const url = String(e.url)
    paths.push(`${e.init.method} ${url.replace('http://fake-daemon.invalid', '').split('?')[0]}`)
    let body: unknown = {}
    if (url.includes('/me/rooms')) body = { rooms: [{ id: 'r1', state: 'active', owes_reply_to_seq: null, muted: true }], pending_invites: [] }
    else if (url.includes('/events')) body = {
      events: [
        { seq: 3, ts: '2026-10-04T00:00:00Z', author: 'Teddy', author_kind: 'human', type: 'message', addressed_to: addressedTo, body: 'hi' },
        { seq: 4, ts: '2026-10-04T00:00:01Z', author: 'Teddy', author_kind: 'human', type: 'message', addressed_to: ['Other'], body: 'and you' },
      ],
      max_seq: 4,
    }
    else if (url.endsWith('/rooms/r1')) body = { id: 'r1', name: 'room', topic: null, state: 'active', hop_count: 0, hop_limit: 6, roster: [{ participant: 'TestAI', owes_reply_to_seq: null }] }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(body) } }
  })
  on('session.append', (_$: any, e: any) => {
    appended.push(JSON.stringify(e))
    return { value: { message: e.message, id: 'row-1' } }
  })
  on('prompt.submit', (_$: any, e: any) => ({ text: String(e.text) }))
  return { paths, appended }
}

test('catch-up stays silent on a muted room and does not ack it', { options: OPTIONS }, async ($, on) => {
  mock.store(on, { 'joinedRooms:TestAI': [{ id: 'r1', name: 'room' }] })
  const { paths, appended } = catchUpRig(on, ['all'])
  await $.prompt.submit({ text: 'hello' } as any)
  expect(appended.length).toBe(0)
  expect(paths.some(p => p.includes('/ack'))).toBe(false)
  expect(paths.includes('GET /rooms/r1')).toBe(false)
})

test('a human addressing this AI breaks through a mute even after owes was cleared', { options: OPTIONS }, async ($, on) => {
  mock.store(on, { 'joinedRooms:TestAI': [{ id: 'r1', name: 'room' }] })
  const { paths } = catchUpRig(on, ['TestAI'])
  await $.prompt.submit({ text: 'hello' } as any)
  // Not skipped: catch-up goes on to build the delivery (fetches the room).
  // Whether the append itself lands is the pre-existing delivery path, not
  // the mute logic; this rig's session.append stub is never reached in the
  // test engine, so it is not asserted here.
  expect(paths.includes('GET /rooms/r1')).toBe(true)
})

for (const { tool, input } of POST_TOOLS) {
  test(`${tool} sends Content-Type: application/json on every POST, body or not`, { options: OPTIONS }, async ($, on) => {
    mock.store(on)
    const seenInits: any[] = []
    on('http.fetch', (_$, e, _next) => {
      seenInits.push(e.init)
      return {
        value: {
          status: 200, ok: true, headers: {},
          text: JSON.stringify({ seq: 1, room_state: 'active', room_id: 'r1', id: 'r1', name: 'room', roster: [] }),
        },
      }
    })

    await $.tool.call({ tool, ...input } as any)

    expect(seenInits.length > 0).toBe(true)
    for (const init of seenInits) {
      if (init.method === 'GET') continue
      expect(init.headers?.['Content-Type']).toBe('application/json')
      expect(typeof init.body).toBe('string')
      expect(() => JSON.parse(init.body)).not.toThrow()
    }
  })
}


// 2026-10-04 context reminders (eot-initialization-automation): the usage
// read in contextReminder, not only the pure rules in reminder.ts. The test
// engine never routes `$.session.append` to a stub (see the catch-up test
// above), so what is asserted is what contextReminder persists: the reading
// log and the fired-thresholds state. `fired` growing is the same decision
// that produces the injected line.
function reminderRig(on: any, reading: { startedAt: number; percent: number | undefined }) {
  const store = new Map<string, any>()
  on('store.get', (_$: any, e: any) => ({ value: store.get(e.key) }))
  on('store.set', (_$: any, e: any) => {
    store.set(e.key, e.value)
    return { value: undefined }
  })
  mock.clock(on, { now: 1000 })
  on('session.usage', () => ({
    value: {
      startedAt: reading.startedAt,
      context: { tokens: 1000, window: 200000, percent: reading.percent },
      rateLimits: [],
    },
  }))
  on('session.append', () => ({ value: { isDelivered: true } }))
  on('prompt.submit', (_$: any, e: any) => ({ text: String(e.text) }))
  return {
    state: () => store.get('reminderState:TestAI') as { sessionKey: number; fired: number[] } | undefined,
    log: () => (store.get('contextReadings:TestAI') ?? []) as Array<{ session: number; percent: number }>,
  }
}

test('contextReminder logs each reading and marks a crossed threshold fired once', { options: OPTIONS }, async ($, on) => {
  const reading = { startedAt: 100, percent: 65 as number | undefined }
  const rig = reminderRig(on, reading)
  await $.prompt.submit({ text: 'hello' } as any)
  expect(rig.state()).toEqual({ sessionKey: 100, fired: [60] })
  expect(rig.log().length).toBe(1)
  expect(rig.log()[0].percent).toBe(65)
  // same threshold again: still fired once, but the reading is logged
  await $.prompt.submit({ text: 'again' } as any)
  expect(rig.state()).toEqual({ sessionKey: 100, fired: [60] })
  expect(rig.log().length).toBe(2)
  // next threshold crossed
  reading.percent = 80
  await $.prompt.submit({ text: 'later' } as any)
  expect(rig.state()).toEqual({ sessionKey: 100, fired: [60, 75] })
})

test('contextReminder does nothing below the first threshold, and skips a missing percent without logging', { options: OPTIONS }, async ($, on) => {
  const reading = { startedAt: 100, percent: undefined as number | undefined }
  const rig = reminderRig(on, reading)
  await $.prompt.submit({ text: 'a' } as any)
  expect(rig.state()).toBe(undefined)
  expect(rig.log().length).toBe(0)
  reading.percent = 40
  await $.prompt.submit({ text: 'b' } as any)
  expect(rig.state()).toEqual({ sessionKey: 100, fired: [] })
  expect(rig.log().length).toBe(1)
})

test('contextReminder: a new session start (after /clear) starts the fired list over', { options: OPTIONS }, async ($, on) => {
  const reading = { startedAt: 100, percent: 90 as number | undefined }
  const rig = reminderRig(on, reading)
  await $.prompt.submit({ text: 'a' } as any)
  expect(rig.state()).toEqual({ sessionKey: 100, fired: [60, 75, 85] })
  reading.startedAt = 200
  reading.percent = 62
  await $.prompt.submit({ text: 'b' } as any)
  expect(rig.state()).toEqual({ sessionKey: 200, fired: [60] })
})

test('contextReminder: reminderThresholds "off" disables it and logs nothing', { options: { ...OPTIONS, reminderThresholds: 'off' } }, async ($, on) => {
  const rig = reminderRig(on, { startedAt: 100, percent: 95 })
  await $.prompt.submit({ text: 'a' } as any)
  expect(rig.state()).toBe(undefined)
  expect(rig.log().length).toBe(0)
})

test('contextReminder: a custom threshold list is honored', { options: { ...OPTIONS, reminderThresholds: '10,50' } }, async ($, on) => {
  const rig = reminderRig(on, { startedAt: 100, percent: 55 })
  await $.prompt.submit({ text: 'a' } as any)
  expect(rig.state()).toEqual({ sessionKey: 100, fired: [10, 50] })
})
