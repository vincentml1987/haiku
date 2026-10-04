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
]

// 2026-10-04 mute: catch-up (prompt.submit) skips a muted room without
// acking, and still delivers it when a human got through (owes set).
function catchUpRig(on: any, owes: number | null) {
  const paths: string[] = []
  const appended: string[] = []
  on('http.fetch', (_$: any, e: any) => {
    const url = String(e.url)
    paths.push(`${e.init.method} ${url.replace('http://fake-daemon.invalid', '').split('?')[0]}`)
    let body: unknown = {}
    if (url.includes('/me/rooms')) body = { rooms: [{ id: 'r1', state: 'active', owes_reply_to_seq: owes, muted: true }], pending_invites: [] }
    else if (url.includes('/events')) body = { events: [{ seq: 3, ts: '2026-10-04T00:00:00Z', author: 'Teddy', author_kind: 'human', type: 'message', addressed_to: ['TestAI'], body: 'hi' }], max_seq: 3 }
    else if (url.endsWith('/rooms/r1')) body = { id: 'r1', name: 'room', topic: null, state: 'active', hop_count: 0, hop_limit: 6, roster: [{ participant: 'TestAI', owes_reply_to_seq: owes }] }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(body) } }
  })
  on('session.append', (_$: any, e: any) => {
    appended.push(JSON.stringify(e))
    return { value: { message: e.message, id: 'row-1' } }
  })
  on('prompt.submit', (_$: any, e: any) => ({ text: String(e.text) }))
  return { paths, appended }
}

test('catch-up skips a muted room and does not ack it', { options: OPTIONS }, async ($, on) => {
  mock.store(on, { 'joinedRooms:TestAI': [{ id: 'r1', name: 'room' }] })
  const { paths, appended } = catchUpRig(on, null)
  await $.prompt.submit({ text: 'hello' } as any)
  expect(appended.length).toBe(0)
  expect(paths.some(p => p.includes('/events') || p.includes('/ack'))).toBe(false)
})

test('catch-up still delivers a muted room when a human addressed this AI', { options: OPTIONS }, async ($, on) => {
  mock.store(on, { 'joinedRooms:TestAI': [{ id: 'r1', name: 'room' }] })
  const { paths } = catchUpRig(on, 3)
  await $.prompt.submit({ text: 'hello' } as any)
  // Not skipped: catch-up goes on to read the room's events. (Whether the
  // append itself lands is the pre-existing delivery path, not the mute
  // logic; this rig's session.append stub is never reached in the test
  // engine, so it is not asserted here.)
  expect(paths.includes('GET /rooms/r1/events')).toBe(true)
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
