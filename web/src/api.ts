export type Me = { id: string; email: string; region: string | null; department: string; role: string; chat_max_chars?: number }
export type Source = {
  alias: string; chunk_id: string; document_title: string; revision: number
  page: number | null; section_path: string; lang: string; snippet: string
}
export type EvidencePreview = { chunk_id: string; document_title: string; revision: number; revision_status: string
  page: number | null; section_path: string; text: string; lang: string }
export type SseEvent = { event: string; data: any }
export type Confirmation = {
  confirmation_id: string; summary: string; status: string; expires_at: string; expired: boolean
}
export type EscalationRecord = { id: string; summary: string; status: string; created_at: string | null; evidence_refs: string[] }
export type EscalationPage = { items: EscalationRecord[]; next_cursor: string | null }
export type ConversationSummary = { id: string; title: string; created_at?: string }
export type ConversationPage = { items: ConversationSummary[]; next_cursor: string | null }
export type AdminDocuments = { kbs: string[]; upload_max_bytes?: number
  upload_rules?: { title_max_chars: number; languages: string[] }; documents: { kb: string; title: string; lang: string
  revisions: { id: string; version: number; status: string }[] }[] }
export type ConversationState = {
  conversation_id: string
  messages: { role: 'user' | 'assistant'; content: string; sources: Source[]; error_code: string | null }[]
  blocked: boolean
  pending_confirmation: Confirmation | null
  retry_checkpoint_id?: string | null
  recover_checkpoint_id?: string | null
}

const TOKEN_KEY = 'knv_token'
export const getToken = () => localStorage.getItem(TOKEN_KEY)
const AUTH_EVENT = 'knv-auth-change'
export const setToken = (t: string | null) => {
  if (t) localStorage.setItem(TOKEN_KEY, t)
  else localStorage.removeItem(TOKEN_KEY)
  dispatchEvent(new Event(AUTH_EVENT))
}
export function subscribeToken(listener: () => void) {
  const storage = (event: StorageEvent) => {
    if (event.storageArea === localStorage && (event.key === TOKEN_KEY || event.key === null)) listener()
  }
  addEventListener(AUTH_EVENT, listener)
  addEventListener('storage', storage)
  return () => { removeEventListener(AUTH_EVENT, listener); removeEventListener('storage', storage) }
}

export class ApiError extends Error {
  status: number
  code?: string
  constructor(status: number, msg: string) {
    super(msg); this.status = status
    try {
      const detail = JSON.parse(msg).detail
      const code = typeof detail === 'string' ? detail : detail?.code
      if (typeof code === 'string' && /^[A-Z][A-Z0-9_]+$/.test(code)) this.code = code
    } catch { /* Non-JSON errors keep their original message and status. */ }
  }
}

async function req(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  const t = getToken()
  if (t) headers.set('Authorization', `Bearer ${t}`)
  const r = await fetch(`/api/v1${path}`, { ...init, headers })
  if (r.status === 401 && path !== '/auth/login' && !init.signal?.aborted && getToken() === t) {
    setToken(null); location.hash = '#/login'
  }
  if (!r.ok) throw new ApiError(r.status, await r.text())
  return r
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }
const validSource = (source: any): boolean => source && typeof source === 'object'
  && ['alias', 'chunk_id', 'document_title', 'section_path', 'lang', 'snippet'].every(k => typeof source[k] === 'string')
  && Number.isInteger(source.revision) && (source.page === null || Number.isInteger(source.page))

export class StreamError extends Error {
  code: 'STREAM_INCOMPLETE' | 'STREAM_INVALID'
  constructor(code: 'STREAM_INCOMPLETE' | 'STREAM_INVALID') { super(code); this.code = code }
}

export const api = {
  login: async (email: string, password: string, signal?: AbortSignal) => {
    if (signal?.aborted) throw new DOMException('Login canceled', 'AbortError')
    const originalToken = getToken()
    const r = await req('/auth/login', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ email, password }), signal })
    const payload = await r.json()
    if (signal?.aborted) throw new DOMException('Login canceled', 'AbortError')
    if (getToken() !== originalToken) throw new ApiError(409, 'LOGIN_SESSION_CHANGED')
    if (!payload || typeof payload.token !== 'string' || !payload.token || payload.token.trim() !== payload.token) {
      throw new ApiError(502, 'INVALID_LOGIN_RESPONSE')
    }
    setToken(payload.token)
    return payload.token as string
  },
  me: async (signal?: AbortSignal): Promise<Me> => (await req('/me', { signal })).json(),
  conversations: async (): Promise<{ id: string; title: string }[]> => (await req('/conversations')).json(),
  conversationPage: async (before?: string, signal?: AbortSignal, query?: string): Promise<ConversationPage> => {
    const params = new URLSearchParams()
    if (before) params.set('before', before)
    if (query) params.set('q', query)
    return (await req('/conversations/page' + (params.size ? `?${params}` : ''), { signal })).json()
  },
  messages: async (id: string): Promise<{ role: string; content: string; sources: Source[] }[]> =>
    (await req(`/conversations/${id}/messages`)).json(),
  conversationState: async (id: string, signal?: AbortSignal): Promise<ConversationState> =>
    (await req(`/conversations/${id}/state`, { signal })).json(),
  evidence: async (chunkId: string, signal?: AbortSignal): Promise<EvidencePreview> =>
    (await req(`/evidence/${encodeURIComponent(chunkId)}`, { signal })).json(),
  escalations: async (before?: string, signal?: AbortSignal): Promise<EscalationPage> =>
    (await req('/escalations' + (before ? `?before=${encodeURIComponent(before)}` : ''), { signal })).json(),
  adminDocs: async (signal?: AbortSignal): Promise<AdminDocuments> => (await req('/admin/documents', { signal })).json(),
  upload: async (form: FormData, signal?: AbortSignal): Promise<{ version: number }> =>
    (await req('/admin/documents', { method: 'POST', body: form, signal })).json(),
  publish: async (id: string, signal?: AbortSignal) =>
    (await req(`/admin/revisions/${encodeURIComponent(id)}/publish`, { method: 'POST', signal })).json(),
}

/** POST + SSE (EventSource cannot POST). Yields parsed events as they arrive. */
export async function* stream(path: string, body: unknown, signal?: AbortSignal): AsyncGenerator<SseEvent> {
  const r = await req(path, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body), signal })
  if (!r.body) throw new StreamError('STREAM_INCOMPLETE')
  const reader = r.body.getReader()
  const dec = new TextDecoder('utf-8', { fatal: true })
  let buf = ''
  let event = ''
  let data: string[] = []
  const parseLine = (line: string): SseEvent | null => {
    if (!line) {
      const name = event
      const payload = data
      event = ''; data = []
      if (!name || !payload.length) return null
      let parsed: any
      try { parsed = JSON.parse(payload.join('\n')) } catch { throw new StreamError('STREAM_INVALID') }
      const object = parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      if ((name === 'sources' ? !Array.isArray(parsed) || !parsed.every(validSource) : !object)
        || (name === 'token' && typeof parsed.text !== 'string')
        || (name === 'status' && typeof parsed.tool !== 'string')
        || (name === 'meta' && typeof parsed.conversation_id !== 'string')
        || (name === 'error' && typeof parsed.code !== 'string')
        || (name === 'done' && typeof parsed.answer !== 'string')
        || (name === 'confirm' && (typeof parsed.confirmation_id !== 'string' || !parsed.confirmation_id
          || typeof parsed.summary !== 'string' || typeof parsed.status !== 'string'
          || typeof parsed.expired !== 'boolean' || typeof parsed.expires_at !== 'string'
          || !Number.isFinite(Date.parse(parsed.expires_at))))) {
        throw new StreamError('STREAM_INVALID')
      }
      return { event: name, data: parsed }
    }
    if (line.startsWith(':')) return null
    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    let value = colon < 0 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') event = value
    if (field === 'data') data.push(value)
    return null
  }
  try {
    for (;;) {
      let part: ReadableStreamReadResult<Uint8Array>
      try { part = await reader.read() }
      catch (error) { if (signal?.aborted) throw error; throw new StreamError('STREAM_INCOMPLETE') }
      const { value, done } = part
      try { buf += done ? dec.decode() : dec.decode(value, { stream: true }) }
      catch { throw new StreamError('STREAM_INVALID') }
      for (;;) {
        const boundary = /[\r\n]/.exec(buf)
        if (!boundary) break
        const i = boundary.index
        if (!done && buf[i] === '\r' && i === buf.length - 1) break
        const line = buf.slice(0, i)
        const width = buf[i] === '\r' && buf[i + 1] === '\n' ? 2 : 1
        buf = buf.slice(i + width)
        const parsed = parseLine(line)
        if (parsed) {
          yield parsed
          if (parsed.event === 'done' || parsed.event === 'confirm') return
        }
      }
      if (done) throw new StreamError('STREAM_INCOMPLETE')
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
