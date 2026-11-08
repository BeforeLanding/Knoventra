import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Chat from '../src/pages/Chat'
import { api, stream, type ConversationPage } from '../src/api'

vi.mock('../src/api', () => ({ api: { conversationPage: vi.fn(), conversationState: vi.fn(), evidence: vi.fn() }, stream: vi.fn() }))
const first: ConversationPage = { items: [{ id: 'c1', title: '会话一' }], next_cursor: 'cursor-1' }
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
beforeEach(() => {
  vi.resetAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  vi.mocked(api.conversationPage).mockResolvedValue(first)
  vi.mocked(api.conversationState).mockResolvedValue({ conversation_id: 'c1', messages: [], blocked: false, pending_confirmation: null })
})
afterEach(cleanup)

it('shows a list read failure and supports explicit refresh without starting a model', async () => {
  vi.mocked(api.conversationPage).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ items: [], next_cursor: null })
  render(<Chat />)
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: '刷新会话' }))
  await screen.findByText('暂无会话。')
  expect(screen.queryByRole('alert')).toBeNull()
  expect(stream).not.toHaveBeenCalled()
})

it('appends pages with deduplication and refreshes from the first page', async () => {
  vi.mocked(api.conversationPage).mockResolvedValueOnce(first).mockResolvedValueOnce({
    items: [{ id: 'c1', title: '会话一更新' }, { id: 'c2', title: '会话二' }], next_cursor: null,
  }).mockResolvedValueOnce({ items: [{ id: 'new', title: '新会话' }], next_cursor: 'new-cursor' })
  render(<Chat />)
  await screen.findByRole('button', { name: '会话一' })
  fireEvent.click(screen.getByRole('button', { name: '加载更多会话' }))
  await screen.findByRole('button', { name: '会话二' })
  expect(screen.getAllByRole('button', { name: '会话一更新' })).toHaveLength(1)
  expect(api.conversationPage).toHaveBeenNthCalledWith(2, 'cursor-1', expect.any(AbortSignal))
  expect(screen.queryByRole('button', { name: '加载更多会话' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '刷新会话' }))
  await screen.findByRole('button', { name: '新会话', exact: true })
  expect(screen.queryByRole('button', { name: '会话二' })).toBeNull()
  expect(api.conversationPage).toHaveBeenNthCalledWith(3, undefined, expect.any(AbortSignal))
})

it('preserves existing rows and the cursor after pagination failure', async () => {
  vi.mocked(api.conversationPage).mockResolvedValueOnce(first).mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce({ items: [{ id: 'c2', title: '重试取得' }], next_cursor: null })
  render(<Chat />)
  await screen.findByRole('button', { name: '会话一' })
  fireEvent.click(screen.getByRole('button', { name: '加载更多会话' }))
  await screen.findByRole('alert')
  expect(screen.getByRole('button', { name: '会话一' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '加载更多会话' }))
  await screen.findByRole('button', { name: '重试取得' })
  expect(api.conversationPage).toHaveBeenNthCalledWith(3, 'cursor-1', expect.any(AbortSignal))
})

it.each(['initial', 'append'])('cancels %s list requests on leaving chat', async mode => {
  const pending = deferred<ConversationPage>()
  if (mode === 'initial') vi.mocked(api.conversationPage).mockReturnValue(pending.promise)
  else vi.mocked(api.conversationPage).mockResolvedValueOnce(first).mockReturnValueOnce(pending.promise)
  const view = render(<Chat />)
  if (mode === 'append') {
    await screen.findByRole('button', { name: '会话一' })
    fireEvent.click(screen.getByRole('button', { name: '加载更多会话' }))
  }
  const signal = vi.mocked(api.conversationPage).mock.calls.at(-1)![1]!
  view.unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => { pending.resolve({ items: [], next_cursor: null }) })
  expect(screen.queryByText('暂无会话。')).toBeNull()
})

it('ignores an older pending page after the post-run list refresh supersedes it', async () => {
  const oldPage = deferred<ConversationPage>()
  vi.mocked(api.conversationPage).mockResolvedValueOnce(first).mockReturnValueOnce(oldPage.promise)
    .mockResolvedValueOnce({ items: [{ id: 'new', title: '最新列表' }], next_cursor: null })
  vi.mocked(stream).mockImplementation(async function* () {
    yield { event: 'meta', data: { conversation_id: 'c1' } }
    yield { event: 'done', data: { answer: '完成' } }
  })
  render(<Chat />)
  fireEvent.click(await screen.findByRole('button', { name: '会话一' }))
  await waitFor(() => expect(api.conversationState).toHaveBeenCalledOnce())
  fireEvent.click(screen.getByRole('button', { name: '加载更多会话' }))
  const oldSignal = vi.mocked(api.conversationPage).mock.calls[1][1]!
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '原问题' } })
  await waitFor(() => expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  await screen.findByRole('button', { name: '最新列表' })
  expect(oldSignal.aborted).toBe(true)
  await act(async () => { oldPage.resolve({ items: [{ id: 'late', title: '过期分页' }], next_cursor: null }) })
  expect(screen.queryByRole('button', { name: '过期分页' })).toBeNull()
  expect(stream).toHaveBeenCalledOnce()
})

it('refreshes the sidebar without clearing the active conversation or executing its checkpoint', async () => {
  vi.mocked(api.conversationState).mockResolvedValue({ conversation_id: 'c1', blocked: false, pending_confirmation: null,
    messages: [{ role: 'assistant', content: '当前会话的已存回答', sources: [], error_code: null }] })
  vi.mocked(api.conversationPage).mockResolvedValueOnce(first)
    .mockResolvedValueOnce({ items: [{ id: 'new', title: '新首页' }], next_cursor: 'older' })
  render(<Chat />)
  fireEvent.click(await screen.findByRole('button', { name: '会话一' }))
  await screen.findByText('当前会话的已存回答')
  fireEvent.click(screen.getByRole('button', { name: '刷新会话' }))
  await screen.findByRole('button', { name: '新首页' })
  expect(screen.getByText('当前会话的已存回答')).toBeTruthy()
  expect(api.conversationState).toHaveBeenCalledOnce()
  expect(stream).not.toHaveBeenCalled()
})

it('searches only on submission and preserves the submitted filter across paging and refresh', async () => {
  vi.mocked(api.conversationPage).mockResolvedValueOnce(first)
    .mockResolvedValueOnce({ items: [{ id: 'a', title: 'E17 一' }], next_cursor: 'e17-cursor' })
    .mockResolvedValueOnce({ items: [{ id: 'b', title: 'E17 二' }], next_cursor: null })
    .mockResolvedValueOnce({ items: [{ id: 'a', title: 'E17 一' }], next_cursor: 'again' })
    .mockResolvedValueOnce(first)
  render(<Chat />)
  await screen.findByRole('button', { name: '会话一' })
  const input = screen.getByRole('searchbox', { name: '会话标题关键词' })
  fireEvent.change(input, { target: { value: '  E17  ' } })
  expect(api.conversationPage).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: '搜索会话' }))
  await screen.findByRole('button', { name: 'E17 一' })
  expect(api.conversationPage).toHaveBeenNthCalledWith(2, undefined, expect.any(AbortSignal), 'E17')
  expect(screen.queryByRole('button', { name: '会话一' })).toBeNull()
  fireEvent.change(input, { target: { value: '尚未提交' } })
  fireEvent.click(screen.getByRole('button', { name: '加载更多会话' }))
  await screen.findByRole('button', { name: 'E17 二' })
  expect(api.conversationPage).toHaveBeenNthCalledWith(3, 'e17-cursor', expect.any(AbortSignal), 'E17')
  fireEvent.click(screen.getByRole('button', { name: '刷新会话' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: 'E17 二' })).toBeNull())
  expect(api.conversationPage).toHaveBeenNthCalledWith(4, undefined, expect.any(AbortSignal), 'E17')
  fireEvent.change(input, { target: { value: ' ' } })
  fireEvent.click(screen.getByRole('button', { name: '搜索会话' }))
  await screen.findByRole('button', { name: '会话一' })
  expect(api.conversationPage).toHaveBeenNthCalledWith(5, undefined, expect.any(AbortSignal))
  expect(stream).not.toHaveBeenCalled()
})

it.each([false, true])('ignores a superseded title search (reject=%s)', async reject => {
  const old = deferred<ConversationPage>()
  vi.mocked(api.conversationPage).mockResolvedValueOnce(first).mockReturnValueOnce(old.promise)
    .mockResolvedValueOnce({ items: [{ id: 'b', title: '新查询结果' }], next_cursor: null })
  render(<Chat />)
  await screen.findByRole('button', { name: '会话一' })
  const input = screen.getByRole('searchbox')
  fireEvent.change(input, { target: { value: '旧查询' } })
  fireEvent.click(screen.getByRole('button', { name: '搜索会话' }))
  const oldSignal = vi.mocked(api.conversationPage).mock.calls[1][1]!
  fireEvent.change(input, { target: { value: '新查询' } })
  fireEvent.click(screen.getByRole('button', { name: '搜索会话' }))
  await screen.findByRole('button', { name: '新查询结果' })
  expect(oldSignal.aborted).toBe(true)
  await act(async () => {
    if (reject) old.reject(new Error('late'))
    else old.resolve({ items: [{ id: 'old', title: '过期查询结果' }], next_cursor: 'old-cursor' })
  })
  expect(screen.queryByRole('button', { name: '过期查询结果' })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByRole('button', { name: '加载更多会话' })).toBeNull()
})

it('clears old filter results and cursor on a failed new search, then retries that filter', async () => {
  vi.mocked(api.conversationPage).mockResolvedValueOnce(first).mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce({ items: [], next_cursor: null })
  render(<Chat />)
  await screen.findByRole('button', { name: '会话一' })
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: '关键词' } })
  fireEvent.click(screen.getByRole('button', { name: '搜索会话' }))
  await screen.findByRole('alert')
  expect(screen.queryByRole('button', { name: '会话一' })).toBeNull()
  expect(screen.queryByRole('button', { name: '加载更多会话' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '刷新会话' }))
  await screen.findByText('没有匹配的会话标题。')
  expect(api.conversationPage).toHaveBeenNthCalledWith(3, undefined, expect.any(AbortSignal), '关键词')
})

it('does not clear the currently open messages or run a model when searching titles', async () => {
  vi.mocked(api.conversationState).mockResolvedValue({ conversation_id: 'c1', messages: [
    { role: 'assistant', content: '已存的当前消息', sources: [], error_code: null }], blocked: false, pending_confirmation: null })
  vi.mocked(api.conversationPage).mockResolvedValueOnce(first).mockResolvedValueOnce({ items: [], next_cursor: null })
  render(<Chat />)
  fireEvent.click(await screen.findByRole('button', { name: '会话一' }))
  await screen.findByText('已存的当前消息')
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'E17' } })
  fireEvent.click(screen.getByRole('button', { name: '搜索会话' }))
  await screen.findByText('没有匹配的会话标题。')
  expect(screen.getByText('已存的当前消息')).toBeTruthy()
  expect(api.conversationState).toHaveBeenCalledOnce()
  expect(stream).not.toHaveBeenCalled()
})
