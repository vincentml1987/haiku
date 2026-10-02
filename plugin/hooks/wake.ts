/**
 * Auto-wake decision logic (docs/haiku-room-spec.md §3a). No `$` here, same
 * rule as client.ts: register.ts owns every engine call and hands this
 * module plain data, so the rules are unit-testable without an engine.
 */

/** The fixed, neutral wake prompt. It carries no event content, ever: room
 * messages are other participants' text and must only reach the model
 * through the nonce-framed block of format.ts. */
export const WAKE_PROMPT = 'HAIKU: new activity, check your rooms'

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
  rooms: Array<{ id: string; state: string; owes_reply_to_seq: number | null }>
  pending_invites: Array<{ room_id: string }>
  /** level 3, the daemon kill switch; absent (older daemon) counts as allowed */
  wake_allowed?: boolean
}

export type WakeDecision = {
  wake: boolean
  /** state to persist; equals the input state unless wake is true */
  state: WakeState
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
  const none: WakeDecision = { wake: false, state: st }
  if (me.wake_allowed === false) return none
  if (now - st.lastWakeAt < minGapMs) return none

  const wokenSeq = { ...st.wokenSeq }
  let owed = false
  for (const room of me.rooms) {
    if (room.state === 'paused' || room.state === 'archived') continue
    const owes = room.owes_reply_to_seq
    if (owes == null) continue
    if (owes > (wokenSeq[room.id] ?? 0)) {
      wokenSeq[room.id] = owes
      owed = true
    }
  }

  // Forget invites that are no longer pending, so a later re-invite wakes again.
  const pending = me.pending_invites.map(i => i.room_id)
  const stillWoken = st.wokenInvites.filter(id => pending.includes(id))
  const newInvites = pending.filter(id => !stillWoken.includes(id))

  if (!owed && newInvites.length === 0) return { wake: false, state: { ...st, wokenInvites: stillWoken } }
  return {
    wake: true,
    state: { wokenSeq, wokenInvites: [...stillWoken, ...newInvites], lastWakeAt: now },
  }
}
