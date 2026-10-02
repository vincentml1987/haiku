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

export function ackEvents(fetch: HaikuFetch, roomId: string, throughSeq: number) {
  return fetch('POST', `/rooms/${roomId}/ack`, { through_seq: throughSeq })
}
