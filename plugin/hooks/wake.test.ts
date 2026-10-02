import { test, expect, mock } from 'claude-code/testing'
import {
  decideWake,
  parseBool,
  parseSeconds,
  EMPTY_WAKE_STATE,
  WAKE_PROMPT,
  type MeRooms,
} from './wake'

const GAP = 60_000
const room = (id: string, owes: number | null, state = 'active') => ({ id, state, owes_reply_to_seq: owes })

// ---- pure decision rules (spec 3a) ----

test('wakes when a reply is owed, and records the seq', async () => {
  const me: MeRooms = { rooms: [room('r1', 7)], pending_invites: [], wake_allowed: true }
  const d = decideWake(me, EMPTY_WAKE_STATE, 1_000_000, GAP)
  expect(d.wake).toBe(true)
  expect(d.state.wokenSeq.r1).toBe(7)
  expect(d.state.lastWakeAt).toBe(1_000_000)
})

test('does not wake with nothing owed (unaddressed AI messages owe nothing)', async () => {
  const me: MeRooms = { rooms: [room('r1', null)], pending_invites: [] }
  expect(decideWake(me, EMPTY_WAKE_STATE, 1_000_000, GAP).wake).toBe(false)
})

test('the same owed seq wakes at most once; a newer seq wakes again after the gap', async () => {
  const me = (owes: number): MeRooms => ({ rooms: [room('r1', owes)], pending_invites: [] })
  const first = decideWake(me(7), EMPTY_WAKE_STATE, 1_000_000, GAP)
  expect(first.wake).toBe(true)
  // silent session, long after the gap: still owes seq 7, must not re-wake
  expect(decideWake(me(7), first.state, 9_000_000, GAP).wake).toBe(false)
  expect(decideWake(me(9), first.state, 9_000_000, GAP).wake).toBe(true)
})

test('the minimum gap holds a wake back without losing it', async () => {
  const me: MeRooms = { rooms: [room('r1', 3)], pending_invites: [] }
  const st = { ...EMPTY_WAKE_STATE, lastWakeAt: 1_000_000 }
  expect(decideWake(me, st, 1_000_000 + GAP - 1, GAP).wake).toBe(false)
  expect(decideWake(me, st, 1_000_000 + GAP, GAP).wake).toBe(true)
})

test('paused and archived rooms never wake anyone', async () => {
  const me: MeRooms = { rooms: [room('r1', 3, 'paused'), room('r2', 4, 'archived')], pending_invites: [] }
  expect(decideWake(me, EMPTY_WAKE_STATE, 1_000_000, GAP).wake).toBe(false)
})

test('a pending invite wakes once; a later re-invite wakes again', async () => {
  const inv: MeRooms = { rooms: [], pending_invites: [{ room_id: 'r9' }] }
  const first = decideWake(inv, EMPTY_WAKE_STATE, 1_000_000, GAP)
  expect(first.wake).toBe(true)
  expect(decideWake(inv, first.state, 9_000_000, GAP).wake).toBe(false)
  // invite consumed (joined), state forgets it ...
  const gone = decideWake({ rooms: [], pending_invites: [] }, first.state, 9_000_000, GAP)
  expect(gone.wake).toBe(false)
  expect(gone.state.wokenInvites.length).toBe(0)
  // ... so a fresh invite to the same room wakes again
  expect(decideWake(inv, gone.state, 9_500_000, GAP).wake).toBe(true)
})

test('the daemon kill switch (wake_allowed false) withholds every wake and changes no state', async () => {
  const me: MeRooms = { rooms: [room('r1', 3)], pending_invites: [{ room_id: 'r9' }], wake_allowed: false }
  const d = decideWake(me, EMPTY_WAKE_STATE, 1_000_000, GAP)
  expect(d.wake).toBe(false)
  expect(d.state).toBe(EMPTY_WAKE_STATE)
})

test('option parsing: autoWake defaults OFF, poll has a floor', async () => {
  expect(parseBool(undefined)).toBe(false)
  expect(parseBool('')).toBe(false)
  expect(parseBool('false')).toBe(false)
  expect(parseBool(true)).toBe(true)
  expect(parseBool('true')).toBe(true)
  expect(parseSeconds(5, 30, 15)).toBe(15)
  expect(parseSeconds(undefined, 30, 15)).toBe(30)
  expect(parseSeconds('45', 30, 15)).toBe(45)
})

// ---- plugin wiring ----

// What the engine answers beneath the plugin when session.start runs.
function stubSession(on: any) {
  on('session.start', (_$: any, e: any) => ({ cwd: e.cwd }))
  on('tool.register', () => ({ value: undefined }))
  on('command.register', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
}

const BASE = { daemonUrl: 'http://fake-daemon.invalid', participantName: 'TestAI', participantToken: 'tok-123' }

function me(body: unknown) {
  return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(body) } }
}

test('/haiku-wake on is refused when the settings ceiling is off, and status says off', { options: BASE }, async ($, on) => {
  mock.store(on)
  const r: any = await $.command.run({ command: 'haiku-wake', args: 'on' } as any)
  expect(r.text.includes('stays off')).toBe(true)
  const s: any = await $.command.run({ command: 'haiku-wake', args: 'status' } as any)
  expect(s.text.includes('autoWake: off')).toBe(true)
})

test('with the ceiling on, off then on works; haiku_autowake agrees', { options: { ...BASE, autoWake: true } }, async ($, on) => {
  mock.store(on)
  const off: any = await $.tool.call({ tool: 'mcp__haiku__haiku_autowake', mode: 'off' } as any)
  expect(off.result.includes('autoWake: off')).toBe(true)
  const back: any = await $.command.run({ command: 'haiku-wake', args: 'on' } as any)
  expect(back.text.includes('autoWake: on')).toBe(true)
})

test('expectedName mismatch refuses HAIKU calls loudly', { options: { ...BASE, expectedName: 'Someone Else' } }, async ($, on) => {
  mock.store(on)
  on('http.fetch', () => me({ seq: 1 }))
  const res: any = await $.tool.call({ tool: 'mcp__haiku__haiku_send', room_id: 'r1', body: 'hi' } as any)
  expect(res.isError).toBe(true)
  expect(String(res.result).includes('identity mismatch')).toBe(true)
})

test('the watcher submits the fixed wake prompt once for an owed reply, then stays quiet', { options: { ...BASE, autoWake: true, autoWakePollSeconds: 15, autoWakeMinGapSeconds: 0 } }, async ($, on) => {
  const clock = mock.clock(on)
  mock.store(on)
  on('http.fetch', () => me({ rooms: [{ id: 'r1', state: 'active', owes_reply_to_seq: 5 }], pending_invites: [], wake_allowed: true }))
  const submitted: string[] = []
  on('prompt.submit', (_$, e, _next) => {
    submitted.push(String((e as any).text))
    return { isHandled: true } as any
  })

  stubSession(on)
  await $.session.start({ cwd: 'C:/x', surface: null, isInteractive: false } as any)
  await clock.advance(60_000)
  expect(submitted.length).toBe(1)
  expect(submitted[0]).toBe(WAKE_PROMPT)
  await clock.advance(120_000)
  expect(submitted.length).toBe(1)
})

test('the watcher does nothing when the daemon says wake_allowed is false', { options: { ...BASE, autoWake: true, autoWakePollSeconds: 15, autoWakeMinGapSeconds: 0 } }, async ($, on) => {
  const clock = mock.clock(on)
  mock.store(on)
  on('http.fetch', () => me({ rooms: [{ id: 'r1', state: 'active', owes_reply_to_seq: 5 }], pending_invites: [], wake_allowed: false }))
  const submitted: string[] = []
  on('prompt.submit', (_$, e, _next) => {
    submitted.push(String((e as any).text))
    return { isHandled: true } as any
  })

  stubSession(on)
  await $.session.start({ cwd: 'C:/x', surface: null, isInteractive: false } as any)
  await clock.advance(120_000)
  expect(submitted.length).toBe(0)
})

test('with the ceiling off there is no watcher at all', { options: BASE }, async ($, on) => {
  const clock = mock.clock(on)
  mock.store(on)
  let polled = 0
  on('http.fetch', () => {
    polled++
    return me({ rooms: [], pending_invites: [] })
  })
  stubSession(on)
  await $.session.start({ cwd: 'C:/x', surface: null, isInteractive: false } as any)
  await clock.advance(600_000)
  expect(polled).toBe(0)
})
