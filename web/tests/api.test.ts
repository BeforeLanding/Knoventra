import { afterEach, expect, it, vi } from 'vitest'
import { api, stream, getToken, setToken, subscribeToken, ApiError } from '../src/api'

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear() })

it.each([
  ['{"detail":"ENCODER_OUTPUT_INVALID"}', 'ENCODER_OUTPUT_INVALID'],
  ['{"detail":{"code":"UPLOAD_TOO_LARGE","max_bytes":20}}', 'UPLOAD_TOO_LARGE'],
  ['<html>unavailable</html>', undefined], ['{"detail":"human readable message"}', undefined],
])('preserves error text and exposes only a structured machine code: %s', (body, code) => {
  const error = new ApiError(503, body)
  expect(error.code).toBe(code)
  expect(error.message).toBe(body)
  expect(error.status).toBe(503)
})

it('encodes conversation cursors and forwards cancellation', async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({ items: [], next_cursor: null }))
  vi.stubGlobal('fetch', fetcher)
  const controller = new AbortController()
  await api.conversationPage('cursor&limit=100', controller.signal)
  expect(fetcher.mock.calls[0][0]).toBe('/api/v1/conversations/page?before=cursor%26limit%3D100')
  expect(fetcher.mock.calls[0][1].signal).toBe(controller.signal)
})

it('encodes literal title searches alongside a conversation cursor', async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({ items: [], next_cursor: null }))
  vi.stubGlobal('fetch', fetcher)
  await api.conversationPage('anchor&limit=100', undefined, 'E17 % /_ &q=other')
  const url = new URL(fetcher.mock.calls[0][0], 'https://example.test')
  expect(url.searchParams.get('q')).toBe('E17 % /_ &q=other')
  expect(url.searchParams.get('before')).toBe('anchor&limit=100')
  expect(url.searchParams.get('limit')).toBeNull()
})

it.each([{}, null, { token: 123 }, { token: '' }, { token: ' padded ' }])('rejects malformed login payload %j without changing credentials', async payload => {
  setToken('existing')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(payload)))
  await expect(api.login('account', 'password')).rejects.toMatchObject({ status: 502 })
  expect(getToken()).toBe('existing')
})

it.each(['valid', 'aborted', 'changed'])('applies a %s login result only to its active credential snapshot', async mode => {
  let resolve!: (response: Response) => void
  const pending = new Promise<Response>(yes => { resolve = yes })
  const fetcher = vi.fn().mockReturnValue(pending)
  vi.stubGlobal('fetch', fetcher)
  setToken('old')
  const controller = new AbortController()
  const request = api.login('account', 'password', controller.signal)
  expect(fetcher.mock.calls[0][1].signal).toBe(controller.signal)
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ email: 'account', password: 'password' })
  if (mode === 'aborted') controller.abort()
  if (mode === 'changed') setToken('newer-session')
  resolve(Response.json({ token: 'login-token' }))
  if (mode === 'valid') await request
  else await expect(request).rejects.toMatchObject(mode === 'aborted' ? { name: 'AbortError' } : { status: 409 })
  expect(getToken()).toBe(mode === 'valid' ? 'login-token' : mode === 'changed' ? 'newer-session' : 'old')
})

it('does not send an already canceled login request', async () => {
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  const controller = new AbortController()
  controller.abort()
  await expect(api.login('account', 'password', controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  expect(fetcher).not.toHaveBeenCalled()
})

it.each(['current', 'replaced', 'aborted'])('handles a %s credential 401 without clearing a newer session', async mode => {
  let resolve!: (response: Response) => void
  const pending = new Promise<Response>(yes => { resolve = yes })
  const fetcher = vi.fn().mockReturnValue(pending)
  vi.stubGlobal('fetch', fetcher)
  history.replaceState(null, '', '#/chat')
  setToken('old-session')
  const controller = new AbortController()
  const request = api.me(controller.signal)
  expect(fetcher.mock.calls[0][1].headers.get('Authorization')).toBe('Bearer old-session')
  expect(fetcher.mock.calls[0][1].signal).toBe(controller.signal)
  if (mode === 'replaced') setToken('new-session')
  if (mode === 'aborted') controller.abort()
  resolve(new Response('{"detail":"expired"}', { status: 401 }))
  await expect(request).rejects.toMatchObject({ status: 401 })
  expect(getToken()).toBe(mode === 'current' ? null : mode === 'replaced' ? 'new-session' : 'old-session')
  expect(location.hash).toBe(mode === 'current' ? '#/login' : '#/chat')
})

it('subscribes to same-page and relevant cross-tab credentials and cleans up listeners', () => {
  const listener = vi.fn()
  const stop = subscribeToken(listener)
  setToken('session')
  expect(listener).toHaveBeenCalledTimes(1)
  dispatchEvent(new StorageEvent('storage', { key: 'unrelated', storageArea: localStorage }))
  expect(listener).toHaveBeenCalledTimes(1)
  dispatchEvent(new StorageEvent('storage', { key: 'knv_token', storageArea: localStorage }))
  dispatchEvent(new StorageEvent('storage', { key: null, storageArea: localStorage }))
  expect(listener).toHaveBeenCalledTimes(3)
  stop()
  setToken(null)
  dispatchEvent(new StorageEvent('storage', { key: 'knv_token', storageArea: localStorage }))
  expect(listener).toHaveBeenCalledTimes(3)
})

it('forwards admin cancellation and keeps multipart headers browser-managed', async () => {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(Response.json({ kbs: [], documents: [], version: 1 })))
  vi.stubGlobal('fetch', fetcher)
  const controller = new AbortController()
  const form = new FormData()
  form.set('title', '手册')
  await api.adminDocs(controller.signal)
  await api.upload(form, controller.signal)
  await api.publish('r/1?', controller.signal)
  expect(fetcher.mock.calls.map(call => call[1].signal)).toEqual([controller.signal, controller.signal, controller.signal])
  expect(fetcher.mock.calls[1][1].body).toBe(form)
  expect(fetcher.mock.calls[1][1].headers.has('Content-Type')).toBe(false)
  expect(fetcher.mock.calls[2][0]).toBe('/api/v1/admin/revisions/r%2F1%3F/publish')
  expect(fetcher.mock.calls[2][1].method).toBe('POST')
})

it('encodes source IDs and forwards preview cancellation', async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({ chunk_id: 'a/b?' }))
  vi.stubGlobal('fetch', fetcher)
  const controller = new AbortController()
  await api.evidence('a/b?', controller.signal)
  expect(fetcher.mock.calls[0][0]).toBe('/api/v1/evidence/a%2Fb%3F')
  expect(fetcher.mock.calls[0][1].signal).toBe(controller.signal)
})

function mockChunks(text: string, keepOpen = false) {
  const cancel = vi.fn()
  const body = new ReadableStream({ start(c) {
    for (const byte of new TextEncoder().encode(text)) c.enqueue(new Uint8Array([byte]))
    if (!keepOpen) c.close()
  }, cancel })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)))
  return { body, cancel }
}

async function collect() {
  const result = []
  for await (const item of stream('/chat', {})) result.push(item)
  return result
}

it.each(['\n', '\r\n', '\r'])('accepts fragmented %j lines, comments and multiline JSON', async newline => {
  const text = [': heartbeat', 'event: token', 'data: {', 'data: "text":"繁體"}', '',
    'event:done', 'data:{"answer":"完整"}', '', ''].join(newline)
  const { body } = mockChunks(text)
  expect(await collect()).toEqual([{ event: 'token', data: { text: '繁體' } }, { event: 'done', data: { answer: '完整' } }])
  expect(body.locked).toBe(false)
})

it.each(['done', 'confirm'])('stops and releases a still-open connection after %s', async terminal => {
  const payload = terminal === 'done' ? { answer: '完整' } : { confirmation_id: 'c1', summary: '确认',
    status: 'PENDING', expired: false, expires_at: '2099-01-01T00:00:00Z' }
  const { body, cancel } = mockChunks(`event: ${terminal}\ndata: ${JSON.stringify(payload)}\n\n`, true)
  expect(await collect()).toEqual([{ event: terminal, data: payload }])
  expect(cancel).toHaveBeenCalledOnce()
  expect(body.locked).toBe(false)
})

it.each(['', 'event: token\ndata: {"text":"一半"}\n\n', 'event: done\ndata: {"answer":"未分隔"}\n',
  'event: error\ndata: {"code":"FAIL"}\n\n'])('rejects EOF without a complete terminal event: %j', async text => {
  const { body } = mockChunks(text)
  await expect(collect()).rejects.toMatchObject({ code: 'STREAM_INCOMPLETE' })
  expect(body.locked).toBe(false)
})

it.each(['event: done\ndata: {bad}\n\n', 'event: done\ndata: {}\n\n',
  'event: confirm\ndata: {"confirmation_id":"c"}\n\n', 'event: token\ndata: []\n\n',
  'event: sources\ndata: {}\n\n', 'event: sources\ndata: [null]\n\n'])('rejects malformed event data: %j', async text => {
  const { body } = mockChunks(text)
  await expect(collect()).rejects.toMatchObject({ code: 'STREAM_INVALID' })
  expect(body.locked).toBe(false)
})

it('rejects truncated UTF-8 and releases the reader', async () => {
  const body = new ReadableStream({ start(c) { c.enqueue(new Uint8Array([0xe4, 0xb8])); c.close() } })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)))
  await expect(collect()).rejects.toMatchObject({ code: 'STREAM_INVALID' })
  expect(body.locked).toBe(false)
})

it('reports a response with no stream body', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null)))
  await expect(collect()).rejects.toMatchObject({ code: 'STREAM_INCOMPLETE' })
})

it('reports a reader connection failure and releases its lock', async () => {
  const body = new ReadableStream({ pull(c) { c.error(new TypeError('connection closed')) } })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)))
  await expect(collect()).rejects.toMatchObject({ code: 'STREAM_INCOMPLETE' })
  expect(body.locked).toBe(false)
})

it('accepts the normal chat event sequence including source cards', async () => {
  const source = { alias: 'S1', chunk_id: 'chunk', document_title: '手册', revision: 1,
    page: 2, section_path: '排查', lang: 'zh-CN', snippet: '资料片段' }
  const expected = [
    { event: 'meta', data: { conversation_id: 'c1', run_id: 'run' } },
    { event: 'status', data: { tool: 'search_knowledge' } },
    { event: 'token', data: { text: '回答' } },
    { event: 'sources', data: [source] },
    { event: 'done', data: { answer: '完整回答 [S1]' } },
  ]
  mockChunks(expected.map(e => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`).join(''))
  expect(await collect()).toEqual(expected)
})

it('encodes escalation pagination cursors and forwards cancellation', async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({ items: [], next_cursor: null }))
  vi.stubGlobal('fetch', fetcher)
  const controller = new AbortController()
  expect(await api.escalations('cursor&limit=100', controller.signal)).toEqual({ items: [], next_cursor: null })
  expect(fetcher.mock.calls[0][0]).toBe('/api/v1/escalations?before=cursor%26limit%3D100')
  expect(fetcher.mock.calls[0][1].signal).toBe(controller.signal)
})

it('loads the owned checkpoint with the caller cancellation signal', async () => {
  const state = { conversation_id: 'c1', messages: [], blocked: false, pending_confirmation: null }
  const fetcher = vi.fn().mockResolvedValue(Response.json(state))
  vi.stubGlobal('fetch', fetcher)
  localStorage.setItem('knv_token', 'demo-token')
  const controller = new AbortController()
  expect(await api.conversationState('c1', controller.signal)).toEqual(state)
  const [url, init] = fetcher.mock.calls[0]
  expect(url).toBe('/api/v1/conversations/c1/state')
  expect(init.signal).toBe(controller.signal)
  expect(init.headers.get('Authorization')).toBe('Bearer demo-token')
})

it('decodes fragmented UTF-8 SSE tokens and releases the stream reader', async () => {
  const bytes = new TextEncoder().encode('event: token\ndata: {"text":"繁體"}\n\nevent: done\ndata: {"answer":""}\n\n')
  const body = new ReadableStream({ start(c) {
    for (const byte of bytes) c.enqueue(new Uint8Array([byte]))
    c.close()
  } })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)))
  const events = []
  for await (const event of stream('/chat', { message: '问题' })) events.push(event)
  expect(events).toEqual([{ event: 'token', data: { text: '繁體' } }, { event: 'done', data: { answer: '' } }])
  expect(body.locked).toBe(false)
})

it('cancels the reader when a consumer stops and passes the abort signal to fetch', async () => {
  const cancel = vi.fn()
  const body = new ReadableStream({
    start(c) { c.enqueue(new TextEncoder().encode('event: token\ndata: {"text":"片段"}\n\n')) }, cancel,
  })
  const fetcher = vi.fn().mockResolvedValue(new Response(body))
  vi.stubGlobal('fetch', fetcher)
  const controller = new AbortController()
  for await (const event of stream('/chat', {}, controller.signal)) {
    expect(event.event).toBe('token')
    break
  }
  expect(fetcher.mock.calls[0][1].signal).toBe(controller.signal)
  expect(cancel).toHaveBeenCalledOnce()
  expect(body.locked).toBe(false)
})
