import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Chat from '../src/pages/Chat'
import { api, stream, type ConversationState } from '../src/api'

vi.mock('../src/api', () => ({
  api: { conversationPage: vi.fn(), conversationState: vi.fn(), evidence: vi.fn() },
  stream: vi.fn(),
}))

const pending: ConversationState = {
  conversation_id: 'c1', blocked: true,
  messages: [{ role: 'user', content: '转人工', sources: [], error_code: null }],
  pending_confirmation: {
    confirmation_id: 'confirm1', summary: 'E17 复位后仍报警', status: 'PENDING',
    expires_at: '2099-01-01T00:00:00Z', expired: false,
  },
}

beforeEach(() => {
  vi.resetAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  vi.mocked(api.conversationPage).mockResolvedValue({ items: [{ id: 'c1', title: '已有会话' }], next_cursor: null })
  vi.mocked(api.conversationState).mockResolvedValue(structuredClone(pending))
})
afterEach(cleanup)

async function open() {
  render(<Chat />)
  fireEvent.click(await screen.findByRole('button', { name: '已有会话' }))
  await screen.findByText(pending.pending_confirmation!.summary)
  await waitFor(() => expect(screen.getByRole('button', { name: '取消' }).disabled).toBe(false))
}

describe('conversation recovery', () => {
  it('preserves oversized input and blocks submission until shortened to the advertised character limit', async () => {
    render(<Chat maxMessageChars={4} />)
    await screen.findByRole('button', { name: '已有会话' })
    const input = screen.getByRole('textbox', { name: '问题' }) as HTMLInputElement
    fireEvent.change(input, { target: { value: '🙂🙂🙂🙂🙂' } })
    expect(input.value).toBe('🙂🙂🙂🙂🙂')
    expect(input.getAttribute('aria-invalid')).toBe('true')
    expect(screen.getByText(/5\/4 字符/)).toBeTruthy()
    expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(true)
    fireEvent.submit(input.closest('form')!)
    expect(stream).not.toHaveBeenCalled()
    fireEvent.change(input, { target: { value: '🙂🙂🙂🙂' } })
    expect(input.getAttribute('aria-invalid')).toBe('false')
    expect(screen.getByText('4/4 字符')).toBeTruthy()
    expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(false)
  })

  it('counts trimmed outgoing text rather than silently truncating it', async () => {
    render(<Chat maxMessageChars={4} />)
    await screen.findByRole('button', { name: '已有会话' })
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '  四个汉字  ' } })
    expect(screen.getByText('4/4 字符')).toBeTruthy()
    expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(false)
  })

  it.each(['', '下一题草稿'])('restores a server-rejected question without overwriting a newer draft (%s)', async draft => {
    let reject!: () => void
    const waiting = new Promise<void>(resolve => { reject = resolve })
    vi.mocked(stream).mockImplementation(async function* () {
      yield* []
      await waiting
      throw Object.assign(new Error('validation'), { status: 422 })
    })
    render(<Chat />)
    await screen.findByRole('button', { name: '已有会话' })
    const input = screen.getByRole('textbox', { name: '问题' }) as HTMLInputElement
    fireEvent.change(input, { target: { value: '原始问题' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(stream).toHaveBeenCalledOnce())
    if (draft) fireEvent.change(input, { target: { value: draft } })
    reject()
    await waitFor(() => expect(input.value).toBe(draft || '原始问题'))
    expect(screen.getByRole('alert').textContent).toBe('问题未通过服务端校验，请检查长度与内容后重试。')
    expect(stream).toHaveBeenCalledOnce()
  })
  it('blocks a new interrupted request with no metadata instead of inventing a conversation ID', async () => {
    vi.mocked(stream).mockImplementation(async function* () {
      yield { event: 'token', data: { text: '预览' } }
      throw Object.assign(new Error('STREAM_INCOMPLETE'), { code: 'STREAM_INCOMPLETE' })
    })
    render(<Chat />)
    await screen.findByRole('button', { name: '已有会话' })
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '新问题' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await screen.findByRole('alert')
    expect(screen.getByRole('alert').textContent).toBe('连接异常且未收到会话标识，请从会话列表重新打开核对，避免重复提交。')
    await waitFor(() => expect(screen.getByRole('button', { name: '已有会话' }).disabled).toBe(false))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '重复问题' } })
    expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(true)
    expect(api.conversationState).not.toHaveBeenCalled()
    expect(stream).toHaveBeenCalledOnce()
  })
  it.each([
    ['STREAM_INCOMPLETE', '连接在收到完成状态前中断，已尝试同步会话。请先检查当前结果。'],
    ['STREAM_INVALID', '响应格式异常，已尝试同步会话。请先检查当前结果。'],
  ])('syncs checkpoint after %s without automatically resubmitting', async (code, text) => {
    const idle: ConversationState = { ...pending, blocked: false, pending_confirmation: null, messages: [] }
    vi.mocked(api.conversationState).mockResolvedValueOnce(idle).mockResolvedValueOnce({ ...idle,
      blocked: true, retry_checkpoint_id: 'cp-interrupted',
      messages: [{ role: 'user', content: '原问题', sources: [], error_code: null }],
    })
    vi.mocked(stream).mockImplementation(async function* () {
      yield { event: 'meta', data: { conversation_id: 'c1' } }
      yield { event: 'token', data: { text: '不能当作最终回答的片段' } }
      throw Object.assign(new Error(code), { code })
    })
    render(<Chat />)
    fireEvent.click(await screen.findByRole('button', { name: '已有会话' }))
    await waitFor(() => expect(api.conversationState).toHaveBeenCalledOnce())
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '原问题' } })
    await waitFor(() => expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await screen.findByRole('button', { name: '继续未完成请求' })
    expect(screen.getByRole('alert').textContent).toBe(text)
    expect(screen.queryByText('不能当作最终回答的片段')).toBeNull()
    expect(stream).toHaveBeenCalledOnce()
    expect(api.conversationState).toHaveBeenCalledTimes(2)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '另一问题' } })
    expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(true)
  })
  it.each([
    ['MODEL_OUTPUT_TRUNCATED', '模型输出达到长度上限，本轮回答不完整。若已确认转交，请先查看转交记录。'],
    ['MODEL_TOOL_CALL_INVALID', '模型返回的工具调用不符合要求，本轮已停止。若已确认转交，请先查看转交记录。'],
    ['MODEL_CITATION_MISSING', '检索回答缺少有效引用，本轮未提供可核验的答案。'],
  ])('restores %s as a model error without automatic execution', async (code, text) => {
    vi.mocked(api.conversationState).mockResolvedValue({ ...pending, blocked: false, pending_confirmation: null,
      messages: [{ role: 'assistant', content: '本轮已停止', sources: [], error_code: code }] })
    render(<Chat />)
    fireEvent.click(await screen.findByRole('button', { name: '已有会话' }))
    await screen.findByText(text)
    expect(screen.queryByRole('button', { name: '继续未完成请求' })).toBeNull()
    expect(screen.queryByRole('button', { name: '确认转交' })).toBeNull()
    expect(stream).not.toHaveBeenCalled()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '更具体的问题' } })
    await waitFor(() => expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(false))
  })
  it('keeps a repeated save failure visible and leaves completed-answer recovery available', async () => {
    vi.mocked(api.conversationState).mockResolvedValue({ ...pending, pending_confirmation: null, recover_checkpoint_id: 'cp1' })
    vi.mocked(stream).mockImplementation(async function* () {
      yield { event: 'error', data: { code: 'ANSWER_SAVE_FAILED' } }
      yield { event: 'done', data: { answer: '' } }
    })
    render(<Chat />)
    fireEvent.click(await screen.findByRole('button', { name: '已有会话' }))
    const recover = await screen.findByRole('button', { name: '恢复已完成回答' })
    await waitFor(() => expect(recover.disabled).toBe(false))
    fireEvent.click(recover)
    await waitFor(() => expect(api.conversationState).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('alert').textContent).toBe('回答保存失败，可稍后再次恢复。')
    expect(screen.getByRole('button', { name: '恢复已完成回答' }).disabled).toBe(false)
  })
  it('restores a completed answer through the save-only endpoint', async () => {
    const failedState: ConversationState = { ...pending, pending_confirmation: null, recover_checkpoint_id: 'complete1',
      messages: [{ role: 'user', content: '原问题', sources: [], error_code: null }] }
    vi.mocked(api.conversationState).mockResolvedValueOnce(failedState).mockResolvedValueOnce({
      ...failedState, blocked: false, recover_checkpoint_id: null,
      messages: [...failedState.messages, { role: 'assistant', content: '已恢复原回答', sources: [], error_code: null }],
    })
    vi.mocked(stream).mockImplementation(async function* () {
      yield { event: 'done', data: { answer: '已恢复原回答' } }
    })
    render(<Chat />)
    fireEvent.click(await screen.findByRole('button', { name: '已有会话' }))
    const recover = await screen.findByRole('button', { name: '恢复已完成回答' })
    await waitFor(() => expect(recover.disabled).toBe(false))
    expect(stream).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: '继续未完成请求' })).toBeNull()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '新问题' } })
    expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(true)
    fireEvent.click(recover)
    await screen.findByText('已恢复原回答')
    expect(stream).toHaveBeenCalledWith('/chat/recover', { conversation_id: 'c1', checkpoint_id: 'complete1' }, expect.any(AbortSignal))
    expect(screen.queryByRole('button', { name: '恢复已完成回答' })).toBeNull()
    expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(false)
  })
  it('explicitly resumes a failed checkpoint without sending the question again', async () => {
    const failedState: ConversationState = { ...pending, pending_confirmation: null, retry_checkpoint_id: 'cp1',
      messages: [{ role: 'user', content: '原问题', sources: [], error_code: null },
        { role: 'assistant', content: '', sources: [], error_code: 'RETRIEVAL_UNAVAILABLE' }] }
    vi.mocked(api.conversationState).mockResolvedValueOnce(failedState).mockResolvedValueOnce({
      ...failedState, blocked: false, retry_checkpoint_id: null,
      messages: [...failedState.messages, { role: 'assistant', content: '恢复完成', sources: [], error_code: null }],
    })
    vi.mocked(stream).mockImplementation(async function* () {
      yield { event: 'done', data: { answer: '恢复完成' } }
    })
    render(<Chat />)
    fireEvent.click(await screen.findByRole('button', { name: '已有会话' }))
    const retry = await screen.findByRole('button', { name: '继续未完成请求' })
    await waitFor(() => expect(retry.disabled).toBe(false))
    expect(stream).not.toHaveBeenCalled()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '新问题' } })
    expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(true)
    fireEvent.click(retry)
    await screen.findByText('恢复完成')
    expect(stream).toHaveBeenCalledWith('/chat/retry', { conversation_id: 'c1', checkpoint_id: 'cp1' }, expect.any(AbortSignal))
    expect(screen.queryByRole('button', { name: '继续未完成请求' })).toBeNull()
    expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(false)
    expect(screen.getAllByText('原问题')).toHaveLength(1)
  })

  it('refreshes a stale retry checkpoint without automatically executing the newer task', async () => {
    vi.mocked(api.conversationState).mockResolvedValueOnce({ ...pending, pending_confirmation: null, retry_checkpoint_id: 'cp1' })
      .mockResolvedValue({ ...pending, pending_confirmation: null, retry_checkpoint_id: 'cp2' })
    vi.mocked(stream).mockImplementation(async function* () {
      yield { event: 'status', data: { tool: 'search_knowledge' } }
      throw Object.assign(new Error('{"detail":"CHECKPOINT_CHANGED"}'), { status: 409 })
    })
    render(<Chat />)
    fireEvent.click(await screen.findByRole('button', { name: '已有会话' }))
    const retry = await screen.findByRole('button', { name: '继续未完成请求' })
    await waitFor(() => expect(retry.disabled).toBe(false))
    fireEvent.click(retry)
    await waitFor(() => expect(api.conversationState).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('alert').textContent).toBe('会话状态已更新，请重新打开后再继续。')
    expect(stream).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: '继续未完成请求' }))
    await waitFor(() => expect(stream).toHaveBeenCalledTimes(2))
    expect(vi.mocked(stream).mock.calls[1][1]).toEqual({ conversation_id: 'c1', checkpoint_id: 'cp2' })
    await waitFor(() => expect(api.conversationState).toHaveBeenCalledTimes(3))
  })

  it('shows a stale confirmation conflict and restores the currently active card', async () => {
    await open()
    vi.mocked(api.conversationState).mockResolvedValue({
      ...pending, pending_confirmation: { ...pending.pending_confirmation!, confirmation_id: 'confirm2', summary: '新一轮转交摘要' },
    })
    vi.mocked(stream).mockImplementation(async function* () {
      yield { event: 'status', data: { tool: 'escalate_to_expert' } }
      throw Object.assign(new Error('{"detail":"CONFIRMATION_NOT_ACTIVE"}'), { status: 409 })
    })
    fireEvent.click(screen.getByRole('button', { name: '确认转交' }))
    await screen.findByText('新一轮转交摘要')
    expect(screen.queryByText(pending.pending_confirmation!.summary)).toBeNull()
    expect(screen.getByRole('alert').textContent).toBe('此确认已不再等待处理，请重新打开会话。')
    expect(screen.getByRole('button', { name: '确认转交' }).disabled).toBe(false)
  })
  it('keeps a server busy response visible after restoring the checkpoint', async () => {
    await open()
    vi.mocked(stream).mockImplementation(async function* () {
      yield { event: 'status', data: { tool: 'escalate_to_expert' } }
      throw Object.assign(new Error('{"detail":"CONVERSATION_BUSY"}'), { status: 409 })
    })
    fireEvent.click(screen.getByRole('button', { name: '确认转交' }))
    await waitFor(() => expect(api.conversationState).toHaveBeenCalledTimes(2))
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '此会话正在处理其他请求，请稍后重新打开。')
    expect(screen.queryByText('已确认')).toBeNull()
  })
  it('restores a pending confirmation and blocks new messages', async () => {
    await open()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '下一问' } })
    expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(true)
    expect(screen.getByText(/有效期至/)).toBeTruthy()
    expect(stream).not.toHaveBeenCalled()
  })

  it('keeps a failed confirmation retryable instead of showing an optimistic decision', async () => {
    await open()
    vi.mocked(stream).mockImplementation(async function* () {
      yield { event: 'status', data: { tool: 'escalate_to_expert' } }
      throw new Error('network lost')
    })
    fireEvent.click(screen.getByRole('button', { name: '确认转交' }))
    await waitFor(() => expect(api.conversationState).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByRole('button', { name: '确认转交' }).disabled).toBe(false))
    expect(screen.queryByText('已确认')).toBeNull()
  })

  it('uses stored decisions on recovery and clears the card after completion', async () => {
    vi.mocked(stream).mockImplementation(async function* () {
      yield { event: 'done', data: { answer: '已创建转交记录' } }
    })
    vi.mocked(api.conversationState).mockResolvedValueOnce({
      ...pending, pending_confirmation: { ...pending.pending_confirmation!, status: 'APPROVED' },
    }).mockResolvedValueOnce({
      conversation_id: 'c1', blocked: false, pending_confirmation: null,
      messages: [{ role: 'assistant', content: '已创建转交记录', sources: [], error_code: null }],
    })
    render(<Chat />)
    fireEvent.click(await screen.findByRole('button', { name: '已有会话' }))
    fireEvent.click(await screen.findByRole('button', { name: '恢复处理' }))
    await screen.findByText('已创建转交记录')
    expect(stream).toHaveBeenCalledWith('/chat/confirm', { confirmation_id: 'confirm1', approve: false }, expect.any(AbortSignal))
    expect(screen.queryByText('转交专家确认')).toBeNull()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '下一问' } })
    expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(false)
  })

  it('restores refusal styling and its saved error code', async () => {
    vi.mocked(api.conversationState).mockResolvedValue({
      conversation_id: 'c1', blocked: false, pending_confirmation: null,
      messages: [{ role: 'assistant', content: '没有相关资料。', sources: [], error_code: 'INSUFFICIENT_EVIDENCE' }],
    })
    render(<Chat />)
    fireEvent.click(await screen.findByRole('button', { name: '已有会话' }))
    const answer = await screen.findByText('没有相关资料。')
    expect(answer.closest('.msg')?.classList.contains('refusal')).toBe(true)
    expect(screen.getByText('资料中没有可靠依据，已拒答。')).toBeTruthy()
  })

  it('offers closing an expired confirmation without allowing approval', async () => {
    vi.mocked(api.conversationState).mockResolvedValue({
      ...pending, pending_confirmation: { ...pending.pending_confirmation!, expired: true },
    })
    render(<Chat />)
    fireEvent.click(await screen.findByRole('button', { name: '已有会话' }))
    expect((await screen.findByRole('button', { name: '确认转交' })).disabled).toBe(true)
    await waitFor(() => expect(screen.getByRole('button', { name: '关闭过期确认' }).disabled).toBe(false))
  })

  it('locks conversation switching during streaming and clears rejected token text on done', async () => {
    let finish!: () => void
    const waiting = new Promise<void>(resolve => { finish = resolve })
    vi.mocked(stream).mockImplementation(async function* () {
      yield { event: 'meta', data: { conversation_id: 'c1' } }
      yield { event: 'token', data: { text: '尚未校验的内容' } }
      await waiting
      yield { event: 'done', data: { answer: '' } }
    })
    vi.mocked(api.conversationState).mockRejectedValue(new Error('offline'))
    render(<Chat />)
    await screen.findByRole('button', { name: '已有会话' })
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '提问' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await screen.findByText('尚未校验的内容')
    expect(screen.getByRole('button', { name: '已有会话' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: '＋ 新会话' }).disabled).toBe(true)
    finish()
    await screen.findByText('会话状态同步失败，请重新打开会话后继续。')
    expect(screen.queryByText('尚未校验的内容')).toBeNull()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '下一问' } })
    expect(screen.getByRole('button', { name: '发送' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: '已有会话' }).disabled).toBe(false)
  })
})
