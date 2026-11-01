export type Me = { id: string; email: string; region: string | null; department: string; role: string }
export type Source = {
  alias: string; chunk_id: string; document_title: string; revision: number
  page: number | null; section_path: string; lang: string; snippet: string
}
export type SseEvent = { event: string; data: any }

const TOKEN_KEY = 'knv_token'
export const getToken = () => localStorage.getItem(TOKEN_KEY)
export const setToken = (t: string | null) => (t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY))

export class ApiError extends Error {
  status: number
  constructor(status: number, msg: string) { super(msg); this.status = status }
}

async function req(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  const t = getToken()
  if (t) headers.set('Authorization', `Bearer ${t}`)
  const r = await fetch(`/api/v1${path}`, { ...init, headers })
  if (r.status === 401 && path !== '/auth/login') { setToken(null); location.hash = '#/login' }
  if (!r.ok) throw new ApiError(r.status, await r.text())
  return r
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }

export const api = {
  login: async (email: string, password: string) => {
    const r = await req('/auth/login', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ email, password }) })
    setToken((await r.json()).token)
  },
  me: async (): Promise<Me> => (await req('/me')).json(),
  conversations: async (): Promise<{ id: string; title: string }[]> => (await req('/conversations')).json(),
  messages: async (id: string): Promise<{ role: string; content: string; sources: Source[] }[]> =>
    (await req(`/conversations/${id}/messages`)).json(),
  evidence: async (chunkId: string) => (await req(`/evidence/${chunkId}`)).json(),
  adminDocs: async () => (await req('/admin/documents')).json(),
  upload: async (form: FormData) => (await req('/admin/documents', { method: 'POST', body: form })).json(),
  publish: async (id: string) => (await req(`/admin/revisions/${id}/publish`, { method: 'POST' })).json(),
}

/** POST + SSE (EventSource cannot POST). Yields parsed events as they arrive. */
export async function* stream(path: string, body: unknown): AsyncGenerator<SseEvent> {
  const r = await req(path, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) })
  const reader = r.body!.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i: number
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, i)
      buf = buf.slice(i + 2)
      const ev = /^event: (.*)$/m.exec(raw)?.[1]
      const data = /^data: (.*)$/m.exec(raw)?.[1]
      if (ev && data) yield { event: ev, data: JSON.parse(data) }
    }
  }
}
