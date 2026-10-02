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
