import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Chat from '../src/pages/Chat'
import { api, stream, type ConversationState } from '../src/api'

vi.mock('../src/api', () => ({
  api: { conversations: vi.fn(), conversationState: vi.fn(), evidence: vi.fn() },
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
  vi.mocked(api.conversations).mockResolvedValue([{ id: 'c1', title: '已有会话' }])
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
