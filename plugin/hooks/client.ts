/**
 * Shaping for the HAIKU daemon's HTTP API (daemon/server.py). No `$` here
 * at all — the engine requires every `$.noun.method(...)` call to be
 * written directly in the hooks module, never delegated across an
 * import. register.ts builds a `HaikuFetch` closure around $.http.fetch
 * and passes IT in here; these functions just shape requests/responses.
 */

export type HaikuCreds = {
  daemonUrl: string
  participantName: string
  participantToken: string
}

/** (method, path, body?, query?) -> parsed JSON response. Throws HaikuApiError on a non-2xx status. */
export type HaikuFetch = (method: string, path: string, body?: unknown, query?: Record<string, string>) => Promise<any>

export class HaikuApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export function joinRoom(fetch: HaikuFetch, roomId: string, catchUp?: number) {
  return fetch('POST', `/rooms/${roomId}/join`, catchUp != null ? { catch_up: catchUp } : {})
}

export function leaveRoom(fetch: HaikuFetch, roomId: string) {
  return fetch('POST', `/rooms/${roomId}/leave`)
}

export function sendMessage(fetch: HaikuFetch, roomId: string, body: string, addressedTo?: string[]) {
  return fetch('POST', `/rooms/${roomId}/send`, { body, addressed_to: addressedTo })
}

export function sendPass(fetch: HaikuFetch, roomId: string) {
  return fetch('POST', `/rooms/${roomId}/pass`)
}

export function resumeRoom(fetch: HaikuFetch, roomId: string, grantedHops?: number) {
  return fetch('POST', `/rooms/${roomId}/resume`, grantedHops != null ? { granted_hops: grantedHops } : {})
}

export function inviteToRoom(fetch: HaikuFetch, roomId: string, invitee: string) {
  return fetch('POST', `/rooms/${roomId}/invite`, { invitee })
}

export function setTopic(fetch: HaikuFetch, roomId: string, topic: string) {
  return fetch('POST', `/rooms/${roomId}/topic`, { topic })
}

export function createRoom(
  fetch: HaikuFetch, name: string,
  opts?: { topic?: string; mode?: 'open' | 'closed'; hop_limit?: number },
) {
  return fetch('POST', '/rooms', { name, ...opts })
}

export function getRoom(fetch: HaikuFetch, roomId: string) {
  return fetch('GET', `/rooms/${roomId}`)
}

export function listRooms(fetch: HaikuFetch) {
  return fetch('GET', '/rooms')
}

export function readEvents(
  fetch: HaikuFetch, roomId: string,
  opts?: { since?: number; limit?: number; advance?: boolean; excludeSelf?: boolean },
) {
  const query: Record<string, string> = {}
  if (opts?.since != null) query.since = String(opts.since)
  if (opts?.limit != null) query.limit = String(opts.limit)
  if (opts?.advance != null) query.advance = String(opts.advance)
  if (opts?.excludeSelf != null) query.exclude_self = String(opts.excludeSelf)
  return fetch('GET', `/rooms/${roomId}/events`, undefined, query)
}

/** Mutes/unmutes a room for this participant only (2026-10-04). */
export function setRoomMuted(fetch: HaikuFetch, roomId: string, muted: boolean) {
  return fetch('PUT', `/rooms/${roomId}/mute`, { muted })
}

/** Back-channel send proposals (2026-10-06); see daemon/proposals.py. */
export function proposeSend(
  fetch: HaikuFetch, backchannelId: string, targetRoomId: string, body: string,
  opts?: { addressed_to?: string[]; window_seconds?: number },
) {
  return fetch('POST', `/rooms/${backchannelId}/proposals`, { target_room_id: targetRoomId, body, ...opts })
}

export function voteOnProposal(fetch: HaikuFetch, proposalId: number, vote: string, reason?: string) {
  return fetch('POST', `/proposals/${proposalId}/vote`, reason != null ? { vote, reason } : { vote })
}

export function cancelProposal(fetch: HaikuFetch, proposalId: number) {
  return fetch('POST', `/proposals/${proposalId}/cancel`)
}

export function listProposals(fetch: HaikuFetch, backchannelId: string, status?: string) {
  return fetch('GET', `/rooms/${backchannelId}/proposals`, undefined, status ? { status } : undefined)
}

export function ackEvents(fetch: HaikuFetch, roomId: string, throughSeq: number) {
  return fetch('POST', `/rooms/${roomId}/ack`, { through_seq: throughSeq })
}
