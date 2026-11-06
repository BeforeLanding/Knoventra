import { afterEach, expect, it, vi } from 'vitest'
import { api, stream } from '../src/api'

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear() })

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
