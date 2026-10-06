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
  setRoomMuted,
  proposeSend,
  voteOnProposal,
  cancelProposal,
  listProposals,
} from './client'
import { formatRoomDelivery, makeNonce, type HaikuEvent } from './format'
import { homeMismatch } from './identity'
import {
  type MeRooms,
  type WakeState,
  EMPTY_WAKE_STATE,
  formatWakePrompt,
  MIN_POLL_SECONDS,
  DEFAULT_POLL_SECONDS,
  parseBool,
  parseSeconds,
  decideWake,
  mutedRoomBreakthrough,
} from './wake'
import { type ReminderState, EMPTY_REMINDER_STATE, parseThresholds, decideReminder } from './reminder'
import { type GitFacts, RESTORE_PROMPT, parseEotConfig, pickLatest, evaluateGate, formatGate, planText } from './eotcycle'

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
function storeKey(kind: 'joinedRooms' | 'lastSeenSeq' | 'wakeState' | 'autoWakeSession' | 'reminderState' | 'contextReadings', participantName: string): string {
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

/**
 * creds() plus the home-folder guard (2026-10-04, see identity.ts): with
 * expectedHome set, the session's own working folder must match it or
 * every HAIKU call fails before anything reaches the daemon. Checked on
 * every call rather than once at session.start, so a hot reload (which
 * drops module state) can never leave the guard silently off.
 */
async function guardedCreds($: Engine, options: Record<string, unknown>): Promise<HaikuCreds> {
  const c = creds(options)
  const expectedHome = String(options.expectedHome ?? '')
  if (expectedHome) {
    const err = homeMismatch(expectedHome, await $.session.cwd(), c.participantName)
    if (err) throw new Error(err)
  }
  return c
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
  if (rooms.length === 0) return
  let muted = new Set<string>()
  try {
    const me = (await fetch('GET', '/me/rooms')) as MeRooms
    muted = new Set(me.rooms.filter(r => r.muted).map(r => r.id))
  } catch {
    // older daemon or a hiccup: deliver as before rather than go silent
  }
  for (const room of rooms) {
    // One room's failure (daemon hiccup, room archived mid-session, a bad
    // append) must not abort every later room's catch-up.
    try {
      await catchUpOneRoom($, fetch, c, room, muted.has(room.id))
    } catch {
      continue
    }
  }
}

async function catchUpOneRoom($: Engine, fetch: ReturnType<typeof makeFetch>, c: HaikuCreds, room: JoinedRoom, muted = false) {
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
  // Muted (2026-10-04): stay silent and do NOT ack, so the cursor stays put
  // and haiku_read with no since= still shows everything unseen whenever the
  // AI chooses to look. Exception: an unseen human message addressed to this
  // AI by name breaks through. Keyed on the events themselves, not on
  // owes_reply_to_seq, which a later human message to someone else clears
  // (Tessera's review of 3f88169: that race could drop the breakthrough).
  if (muted && !mutedRoomBreakthrough(batch.events, c.participantName)) return

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

/**
 * Context-usage reminder (eot-initialization-automation, 2026-10-04). Logs each
 * real reading of $.session.usage().context so the thresholds can be retuned
 * against when compaction actually happens, then injects at most one line when
 * a threshold is newly crossed. It only asks the member to decide; it never
 * acts. A missing percent (before a window's first response) is skipped.
 */
async function contextReminder($: Engine, options: Record<string, unknown>) {
  const c = await guardedCreds($, options)
  const thresholds = parseThresholds(options.reminderThresholds)
  if (thresholds.length === 0) return
  const usage = await $.session.usage()
  const { tokens, window, percent } = usage.context
  if (percent == null) return
  const logKey = storeKey('contextReadings', c.participantName)
  const log = (await $.store.get(logKey)) as unknown[] | undefined
  const entry = { at: await $.clock.now(), session: usage.startedAt, tokens, window, percent }
  await $.store.set(logKey, [...(Array.isArray(log) ? log : []), entry].slice(-200))
  const stateKey = storeKey('reminderState', c.participantName)
  const stored = (await $.store.get(stateKey)) as ReminderState | undefined
  const d = decideReminder(stored ?? EMPTY_REMINDER_STATE, usage.startedAt, percent, thresholds)
  await $.store.set(stateKey, d.state)
  if (d.text) {
    await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: d.text }] } })
  }
}

/**
 * haiku_eot_cycle (eot-initialization-automation, 2026-10-04). Runs the
 * member's own gate (eotcycle.ts has the rules) over their newest EOT. For now
 * it is dry-run only: a passing gate reports what a live cycle would do and
 * clears nothing. A live clear waits on the test of whether a prompt submitted
 * after /clear reaches the fresh session. Fails closed on any problem.
 */
async function eotCycle($: Engine, options: Record<string, unknown>, dryRun: boolean): Promise<string> {
  const parsed = parseEotConfig(options)
  if (!parsed.ok) return `EOT gate refused: ${parsed.reason}. Nothing was cleared.`
  const cfg = parsed.config
  const latest = pickLatest(await $.fs.list(cfg.dir), cfg.namePattern)
  let git: GitFacts | null = null
  if (cfg.kind === 'git' && latest) {
    const file = `${cfg.dir.replace(/[\\/]+$/, '')}/${latest.name}`
    const run = (argv: string[]) => $.process.run(['git', '-C', cfg.repo, ...argv])
    const log = await run(['-c', `gpg.ssh.allowedSignersFile=${cfg.allowedSigners}`, 'log', '-1', '--format=%G?', '--', file])
    const dirty = await run(['status', '--porcelain', '--', file])
    const branch = await run(['status', '-sb'])
    git = {
      signature: log.exitCode === 0 ? log.stdout.trim() : '',
      isDirty: dirty.exitCode !== 0 || dirty.stdout.trim() !== '',
      isAhead: branch.exitCode !== 0 || /\bahead\b/.test(branch.stdout.split('\n')[0] ?? ''),
    }
  }
  const gate = evaluateGate(cfg, latest, git, await $.clock.now())
  const report = formatGate(gate)
  if (!gate.ok) return `EOT gate FAILED, nothing cleared.\n${report}`
  if (!dryRun) {
    // Ceiling, set by Teddy per identity (same pattern as autoWake): no session
    // can raise its own. Without it a live request is refused, gate or no gate.
    if (!parseBool(options.eotCycleLive)) {
      return `EOT gate passed, but a live clear is not enabled for this identity (eotCycleLive in its settings file, which only Teddy raises). Nothing was cleared.\n${report}`
    }
    // $.command.run rejects when called inside a hook the turn is waiting on,
    // and this is a tool.call hook: the first live test (Tessera, 2026-10-04)
    // swallowed that rejection and the clear never ran. So hand both calls to a
    // timer, which runs outside the hook. They then queue until the session is
    // idle, in call order, so the clear runs first and the restore prompt lands
    // after it. Not awaited on purpose: whether this module survives /clear is
    // the open question. A failure is shown as a toast, never swallowed.
    const fail = (what: string) => (err: unknown) => $.ui.toast(`haiku_eot_cycle: ${what} failed: ${err instanceof Error ? err.message : String(err)}`)
    $.clock.after(1000, () => {
      $.command.run({ command: 'clear' }).catch(fail('/clear'))
      $.prompt.submit({ text: RESTORE_PROMPT }).catch(fail('restore prompt'))
    })
    return `LIVE: EOT gate passed for ${gate.file}.\n${report}\nQueued /clear, then the prompt "${RESTORE_PROMPT}"; both run when this turn ends.`
  }
  return `DRY RUN: EOT gate passed for ${gate.file}.\n${report}\nA live cycle would ${planText()}. Nothing was cleared.`
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
  const c = await guardedCreds($, options)
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
  let usageSampler: { cancel: () => void } | undefined
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
          hop_limit: { type: 'number', description: 'AI-authored messages allowed since the last human message before the room pauses. Default 6. 0 = no cap (use for a back channel).' },
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
      name: 'haiku_mute',
      description: 'Mute or unmute a HAIKU room for yourself only. Muted: no automatic delivery, no wake, and no reply owed for unaddressed traffic; you stay a member and can haiku_read it whenever you choose. A human addressing you by name still gets through.',
      inputSchema: {
        type: 'object',
        properties: { room_id: { type: 'string' }, muted: { type: 'boolean' } },
        required: ['room_id', 'muted'],
      },
    })
    await $.tool.register({
      name: 'haiku_propose_send',
      description: 'Back channel: propose ONE message to send into another room, for the other AI members of the back channel to vote on. You are the chair (counts as yes). It closes when everyone has voted or the window ends (silence counts as abstain); on approval the exact text is posted into the target room as you, with every "no" vote and its reason attached, so Teddy sees dissent. Optional, not a gate: you can still post directly to any room with haiku_send. One open proposal per target room.',
      inputSchema: {
        type: 'object',
        properties: {
          backchannel_id: { type: 'string', description: 'The back channel room (an AI-only room, usually hop_limit 0) where the vote happens.' },
          target_room_id: { type: 'string', description: 'The room the approved message is posted into.' },
          body: { type: 'string', description: 'The exact message text.' },
          addressed_to: { type: 'array', items: { type: 'string' }, description: 'Names in the target room to address, or ["all"]. Omit for unaddressed.' },
          window_seconds: { type: 'number', description: 'Voting window, 30 to 3600. Default 300.' },
        },
        required: ['backchannel_id', 'target_room_id', 'body'],
      },
    })
    await $.tool.register({
      name: 'haiku_vote',
      description: 'Back channel: vote on an open proposal. yes, no (a reason is required and is sent to Teddy with the message) or abstain. You can change your vote until it closes. The chair cannot vote on their own proposal.',
      inputSchema: {
        type: 'object',
        properties: {
          proposal_id: { type: 'number' },
          vote: { type: 'string', enum: ['yes', 'no', 'abstain'] },
          reason: { type: 'string', description: 'Required for no; up to 500 characters.' },
        },
        required: ['proposal_id', 'vote'],
      },
    })
    await $.tool.register({
      name: 'haiku_cancel_proposal',
      description: 'Back channel: withdraw your own open proposal so nothing is sent.',
      inputSchema: { type: 'object', properties: { proposal_id: { type: 'number' } }, required: ['proposal_id'] },
    })
    await $.tool.register({
      name: 'haiku_proposals',
      description: 'Back channel: list proposals made in a back channel, newest first, with their votes. Optional status filter: open, sent, blocked, cancelled, failed.',
      inputSchema: {
        type: 'object',
        properties: { backchannel_id: { type: 'string' }, status: { type: 'string' } },
        required: ['backchannel_id'],
      },
    })
    await $.tool.register({
      name: 'haiku_rooms',
      description: 'List HAIKU rooms this session has joined, and all rooms known to the daemon.',
      inputSchema: { type: 'object', properties: {} },
    })

    // --- identity visibility + auto-wake (spec 3a) ---
    try {
      const c0 = await guardedCreds($, options)
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
    await $.tool.register({
      name: 'haiku_eot_cycle',
      description: 'Check your newest EOT against your gate. Dry run only for now; never clears. Write your EOT first.',
      inputSchema: { type: 'object', properties: { dry_run: { type: 'boolean' } } },
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
          const c = await guardedCreds($, options)
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
          if (d.wake) await $.prompt.submit({ text: formatWakePrompt(d.reasons) })
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
    
	// Usage sampler: $.session.usage() costs nothing. Each session overwrites its
    // own file (no append in $.fs, and no two sessions share a file), and the
    // usage tracker's ingest dedupes the overlap on ts.
    usageSampler?.cancel()
    usageSampler = undefined
    const usageDir = typeof options.usageDataDir === 'string' ? options.usageDataDir.trim() : ''
    if (usageDir) {
      const sampleOnce = async () => {
        try {
          const c = await guardedCreds($, options)
          const u = await $.session.usage()
          const ts = new Date(await $.clock.now()).toISOString()
          await $.fs.write(`${usageDir}/${c.participantName}.latest.json`, JSON.stringify({ ts, rateLimits: u.rateLimits }))
        } catch (err) {
          $.ui.toast(`usage sample failed: ${err instanceof Error ? err.message : String(err)}`)
        }
      }
      void sampleOnce()
      usageSampler = $.clock.every(5 * 60 * 1000, () => void sampleOnce())
    }
	
    try {
      await catchUp($, await guardedCreds($, options))
    } catch {
      // not configured yet, or daemon unreachable — tools still register; a
      // send/read call will surface the real error to the model directly.
    }

    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    try {
      await catchUp($, await guardedCreds($, options))
    } catch {
      // see session.start — silent here too, never block a user prompt over it
    }
    try {
      await contextReminder($, options)
    } catch {
      // a reminder is a nicety: never block a prompt over it
    }
    return next(e)
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_eot_cycle' }, async ($, e) => {
    try {
      return { result: await eotCycle($, options, e.dry_run !== false) }
    } catch (err) {
      return errorResult(err)
    }
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
      const fetch = makeFetch($, await guardedCreds($, options))
      const result = await sendMessage(fetch, e.room_id as string, e.body as string, e.addressed_to as string[] | undefined)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_read' }, async ($, e) => {
    try {
      const fetch = makeFetch($, await guardedCreds($, options))
      const result = await readEvents(fetch, e.room_id as string, { since: e.since as number | undefined, limit: e.limit as number | undefined })
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_pass' }, async ($, e) => {
    try {
      const fetch = makeFetch($, await guardedCreds($, options))
      const result = await sendPass(fetch, e.room_id as string)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_join' }, async ($, e) => {
    try {
      const c = await guardedCreds($, options)
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
      const c = await guardedCreds($, options)
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
      const c = await guardedCreds($, options)
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
      const fetch = makeFetch($, await guardedCreds($, options))
      const result = await inviteToRoom(fetch, e.room_id as string, e.invitee as string)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_topic' }, async ($, e) => {
    try {
      const fetch = makeFetch($, await guardedCreds($, options))
      const result = await setTopic(fetch, e.room_id as string, e.topic as string)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_resume' }, async ($, e) => {
    try {
      const fetch = makeFetch($, await guardedCreds($, options))
      const result = await resumeRoom(fetch, e.room_id as string, e.granted_hops as number | undefined)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_mute' }, async ($, e) => {
    try {
      const fetch = makeFetch($, await guardedCreds($, options))
      const result = await setRoomMuted(fetch, e.room_id as string, e.muted === true)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_propose_send' }, async ($, e) => {
    try {
      const fetch = makeFetch($, await guardedCreds($, options))
      const result = await proposeSend(fetch, e.backchannel_id as string, e.target_room_id as string, e.body as string, {
        addressed_to: e.addressed_to as string[] | undefined,
        window_seconds: e.window_seconds as number | undefined,
      })
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_vote' }, async ($, e) => {
    try {
      const fetch = makeFetch($, await guardedCreds($, options))
      const result = await voteOnProposal(fetch, e.proposal_id as number, e.vote as string, e.reason as string | undefined)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_cancel_proposal' }, async ($, e) => {
    try {
      const fetch = makeFetch($, await guardedCreds($, options))
      const result = await cancelProposal(fetch, e.proposal_id as number)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_proposals' }, async ($, e) => {
    try {
      const fetch = makeFetch($, await guardedCreds($, options))
      const result = await listProposals(fetch, e.backchannel_id as string, e.status as string | undefined)
      return { result: JSON.stringify(result, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })

  on('tool.call', { tool: 'mcp__haiku__haiku_rooms' }, async $ => {
    try {
      const c = await guardedCreds($, options)
      const fetch = makeFetch($, c)
      const joined = await getJoinedRooms($, c.participantName)
      const all = await listRooms(fetch)
      return { result: JSON.stringify({ joined, all: (all as any).rooms }, null, 2) }
    } catch (err) {
      return errorResult(err)
    }
  })
}
