import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Chat from '../src/pages/Chat'
import { api, type EvidencePreview, type Source } from '../src/api'

vi.mock('../src/api', () => ({ api: { conversationPage: vi.fn(), conversationState: vi.fn(), evidence: vi.fn() }, stream: vi.fn() }))

const source = (id: string): Source => ({ alias: 'S1', chunk_id: id, document_title: `手册${id}`, revision: 1,
  page: 2, section_path: '排查', snippet: `片段${id}`, lang: 'zh-CN' })
const evidence = (id: string): EvidencePreview => ({ chunk_id: id, document_title: `手册${id}`, revision: 1,
  revision_status: 'PUBLISHED', page: 2, section_path: '排查', text: `完整正文${id}`, lang: 'zh-CN' })
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

beforeEach(() => {
  vi.resetAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  vi.mocked(api.conversationPage).mockResolvedValue({ items: [{ id: 'c1', title: '已有会话' }], next_cursor: null })
  vi.mocked(api.conversationState).mockResolvedValue({ conversation_id: 'c1', blocked: false,
    pending_confirmation: null, messages: [{ role: 'assistant', content: '回答 [S1]',
      sources: [source('A'), { ...source('B'), alias: 'S2' }], error_code: null }] })
})
afterEach(cleanup)

async function openChat() {
  const view = render(<Chat />)
  fireEvent.click(await screen.findByRole('button', { name: '已有会话' }))
  const a = await screen.findByRole('button', { name: /手册A/ })
  const b = screen.getByRole('button', { name: /手册B/ })
  return { ...view, a, b }
}

it('shows loading, traps keyboard focus and closes with Escape back to the source', async () => {
  const pending = deferred<EvidencePreview>()
  vi.mocked(api.evidence).mockReturnValue(pending.promise)
  const { a } = await openChat()
  fireEvent.click(a)
  const dialog = screen.getByRole('dialog', { name: '来源预览' })
  expect(within(dialog).getByRole('status').textContent).toBe('正在加载来源…')
  const close = within(dialog).getByRole('button', { name: '关闭' })
  expect(document.activeElement).toBe(close)
  fireEvent.keyDown(close, { key: 'Tab', shiftKey: true })
  expect(document.activeElement).toBe(close)
  const signal = vi.mocked(api.evidence).mock.calls[0][1]!
  fireEvent.keyDown(close, { key: 'Escape' })
  expect(signal.aborted).toBe(true)
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(document.activeElement).toBe(a)
  await act(async () => { pending.resolve(evidence('A')) })
  expect(screen.queryByRole('dialog')).toBeNull()
})

it.each([false, true])('ignores an older response after closing and opening another source (reject=%s)', async reject => {
  const first = deferred<EvidencePreview>(), second = deferred<EvidencePreview>()
  vi.mocked(api.evidence).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  const { a, b } = await openChat()
  fireEvent.click(a)
  const firstSignal = vi.mocked(api.evidence).mock.calls[0][1]!
  fireEvent.click(screen.getByRole('button', { name: '关闭' }))
  fireEvent.click(b)
  expect(firstSignal.aborted).toBe(true)
  await act(async () => { second.resolve(evidence('B')) })
  await screen.findByText('完整正文B')
  await act(async () => { if (reject) first.reject(new Error('late')); else first.resolve(evidence('A')) })
  expect(screen.getByText('完整正文B')).toBeTruthy()
  expect(screen.queryByText('完整正文A')).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
})

it('aborts pending preview on leaving the page and ignores a late response', async () => {
  const pending = deferred<EvidencePreview>()
  vi.mocked(api.evidence).mockReturnValue(pending.promise)
  const { a, unmount } = await openChat()
  fireEvent.click(a)
  const signal = vi.mocked(api.evidence).mock.calls[0][1]!
  unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => { pending.resolve(evidence('A')) })
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('clears failure on reopening and loads the source afresh', async () => {
  vi.mocked(api.evidence).mockRejectedValueOnce(new Error('denied')).mockResolvedValueOnce(evidence('A'))
  const { a } = await openChat()
  fireEvent.click(a)
  await screen.findByRole('alert')
  expect(screen.queryByText('完整正文A')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '关闭' }))
  fireEvent.click(a)
  await screen.findByText('完整正文A')
  expect(api.evidence).toHaveBeenCalledTimes(2)
  expect(screen.queryByRole('alert')).toBeNull()
})

it('rejects a successful response for the wrong source identity', async () => {
  vi.mocked(api.evidence).mockResolvedValue(evidence('B'))
  const { a } = await openChat()
  fireEvent.click(a)
  await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('来源暂不可用'))
  expect(screen.queryByText('完整正文B')).toBeNull()
})

it('keeps inner clicks open but cancels the request when the backdrop is clicked', async () => {
  const pending = deferred<EvidencePreview>()
  vi.mocked(api.evidence).mockReturnValue(pending.promise)
  const { a } = await openChat()
  fireEvent.click(a)
  const dialog = screen.getByRole('dialog')
  fireEvent.click(within(dialog).getByRole('status'))
  expect(screen.getByRole('dialog')).toBe(dialog)
  fireEvent.click(dialog.parentElement!)
  expect(vi.mocked(api.evidence).mock.calls[0][1]!.aborted).toBe(true)
  expect(screen.queryByRole('dialog')).toBeNull()
  await act(async () => { pending.resolve(evidence('A')) })
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('cannot reopen the old preview after closing it and starting a new conversation', async () => {
  const pending = deferred<EvidencePreview>()
  vi.mocked(api.evidence).mockReturnValue(pending.promise)
  const { a } = await openChat()
  fireEvent.click(a)
  fireEvent.click(screen.getByRole('button', { name: '关闭' }))
  fireEvent.click(screen.getByRole('button', { name: '＋ 新会话' }))
  await act(async () => { pending.resolve(evidence('A')) })
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.queryByRole('button', { name: /手册A/ })).toBeNull()
})
