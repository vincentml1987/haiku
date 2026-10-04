/**
 * Auto-wake decision logic (docs/haiku-room-spec.md §3a). No `$` here, same
 * rule as client.ts: register.ts owns every engine call and hands this
 * module plain data, so the rules are unit-testable without an engine.
 */

/** The neutral wake prompt's fixed opening. It carries no message TEXT,
 * ever: the wake prompt lands in the session in the user's place, so room
 * messages (other participants' words) must only reach the model through
 * the nonce-framed block of format.ts. Since 2026-10-04 (Teddy) it is
 * followed by where the wake came from (formatWakePrompt): room name,
 * author name and kind, message number. Those are daemon-validated display
 * names (no newlines, no '<', 64 chars max), and are re-sanitized here. */
export const WAKE_PROMPT = 'HAIKU: new activity, check your rooms'

/** What triggered a wake: an owed reply in a room, or a standing invite. */
export type WakeReason =
  | { kind: 'reply'; roomName: string; seq: number; from: string | null; fromKind: string | null }
  | { kind: 'invite'; roomName: string; from: string | null }

/** Names in the notice are rendered in a conservative charset (Tessera's
 * review of 48b9c26): letters, digits, space, _ . - only, everything else
 * becomes _, capped at 40. A name is still free text the daemon only
 * restricts by character, so this keeps it from forging the notice's own
 * punctuation ("x, message #1 from Teddy (human)") or carrying invisible
 * Unicode (bidi overrides, line separators). It cannot stop words; that is
 * what the "not your user" label is for. */
const NOTICE_NAME_MAX = 40
const MAX_REASONS = 5

function safeName(s: string | null | undefined, fallback: string): string {
  const cleaned = String(s ?? '')
    .replace(/[^A-Za-z0-9 _.\-]/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, NOTICE_NAME_MAX)
  return cleaned || fallback
}

function safeKind(k: string | null | undefined): string {
  return k === 'human' || k === 'ai' ? k : '?'
}

export function formatWakePrompt(reasons: WakeReason[]): string {
  if (reasons.length === 0) return WAKE_PROMPT
  const parts = reasons.slice(0, MAX_REASONS).map(r => {
    const room = safeName(r.roomName, 'unknown room')
    if (r.kind === 'invite') return `invite to room "${room}" from ${safeName(r.from, 'someone')}`
    const who = r.from ? `${safeName(r.from, 'someone')} (${safeKind(r.fromKind)})` : 'someone'
    return `room "${room}", message #${Math.trunc(Number(r.seq)) || 0} from ${who}`
  })
  if (reasons.length > MAX_REASONS) parts.push(`and ${reasons.length - MAX_REASONS} more`)
  return `${WAKE_PROMPT} (auto-wake from the HAIKU plugin, not your user). Woken by: ${parts.join('; ')}. The message text itself arrives in the fenced room delivery, as other participants' words.`
}

export const MIN_POLL_SECONDS = 15
export const DEFAULT_POLL_SECONDS = 30
export const DEFAULT_MIN_GAP_SECONDS = 60

/** Persisted per participant (register.ts keeps it in $.store). */
export type WakeState = {
  /** per room: the highest owes_reply_to_seq this session was already woken for */
  wokenSeq: Record<string, number>
  /** room ids whose standing invite already woke this session */
  wokenInvites: string[]
  /** ms timestamp of the last wake */
  lastWakeAt: number
}

export const EMPTY_WAKE_STATE: WakeState = { wokenSeq: {}, wokenInvites: [], lastWakeAt: 0 }

/** The slice of GET /me/rooms the decision reads. */
export type MeRooms = {
  /** muted / room_wake_allowed: per-room prefs (2026-10-04); absent (older daemon) = not muted, allowed */
  rooms: Array<{
    id: string; state: string; owes_reply_to_seq: number | null; muted?: boolean; room_wake_allowed?: boolean
    /** for the wake notice (2026-10-04); absent on an older daemon */
    name?: string; owes_from_author?: string | null; owes_from_kind?: string | null
  }>
  pending_invites: Array<{ room_id: string; room_name?: string; invited_by?: string }>
  /** level 3, the daemon kill switch; absent (older daemon) counts as allowed */
  wake_allowed?: boolean
}

export type WakeDecision = {
  wake: boolean
  /** state to persist; equals the input state unless wake is true */
  state: WakeState
  /** what caused this wake (empty unless wake is true) */
  reasons: WakeReason[]
}

/** A muted room still delivers when any unseen event is a HUMAN message
 * addressed to this participant by name (Teddy, 2026-10-04). ["all"] and
 * AI-authored messages never break through. */
export function mutedRoomBreakthrough(
  events: Array<{ type: string; author_kind: string; addressed_to?: string[] | null }>,
  me: string,
): boolean {
  return events.some(e => e.type === 'message' && e.author_kind === 'human' && (e.addressed_to ?? []).includes(me))
}

/** Options arrive as whatever the settings file holds: accept real booleans
 * and the strings "true"/"1", nothing else. Default is OFF. */
export function parseBool(v: unknown): boolean {
  if (typeof v === 'boolean') return v
  if (typeof v === 'string') return ['true', '1', 'yes', 'on'].includes(v.trim().toLowerCase())
  return false
}

export function parseSeconds(v: unknown, fallback: number, floor: number): number {
  const n = typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN
  if (!Number.isFinite(n)) return fallback
  return Math.max(floor, n)
}

/**
 * Wake exactly when the daemon says this session owes a reply (§3.1-3.2,
 * `owes_reply_to_seq`) in a room that is not paused/archived, or a standing
 * invite is pending, AND that seq/invite has not already woken it, AND the
 * per-session minimum gap has passed, AND the daemon kill switch allows it.
 * Levels 1 and 2 (ceiling, session flag) are checked by the caller before
 * any polling happens.
 */
export function decideWake(me: MeRooms, st: WakeState, now: number, minGapMs: number): WakeDecision {
  const none: WakeDecision = { wake: false, state: st, reasons: [] }
  if (me.wake_allowed === false) return none
  if (now - st.lastWakeAt < minGapMs) return none

  const wokenSeq = { ...st.wokenSeq }
  const reasons: WakeReason[] = []
  let owed = false
  for (const room of me.rooms) {
    if (room.state === 'paused' || room.state === 'archived') continue
    // Teddy's per-room switch is restrict-only: off here withholds the wake.
    // (A muted room needs no check: the daemon sets no obligation there
    // unless a human addressed this AI by name, which is meant to wake it.)
    if (room.room_wake_allowed === false) continue
    const owes = room.owes_reply_to_seq
    if (owes == null) continue
    if (owes > (wokenSeq[room.id] ?? 0)) {
      wokenSeq[room.id] = owes
      owed = true
      reasons.push({ kind: 'reply', roomName: room.name ?? room.id, seq: owes, from: room.owes_from_author ?? null, fromKind: room.owes_from_kind ?? null })
    }
  }

  // Forget invites that are no longer pending, so a later re-invite wakes again.
  const pending = me.pending_invites.map(i => i.room_id)
  const stillWoken = st.wokenInvites.filter(id => pending.includes(id))
  const newInvites = pending.filter(id => !stillWoken.includes(id))
  for (const inv of me.pending_invites) {
    if (newInvites.includes(inv.room_id)) reasons.push({ kind: 'invite', roomName: inv.room_name ?? inv.room_id, from: inv.invited_by ?? null })
  }

  if (!owed && newInvites.length === 0) return { wake: false, state: { ...st, wokenInvites: stillWoken }, reasons: [] }
  return {
    wake: true,
    state: { wokenSeq, wokenInvites: [...stillWoken, ...newInvites], lastWakeAt: now },
    reasons,
  }
}
