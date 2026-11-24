import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Memory from '../src/pages/Memory'
import Chat from '../src/pages/Chat'
import { api, type MemoryState } from '../src/api'

vi.mock('../src/api', () => ({
  api: { memory: vi.fn(), forgetEpisode: vi.fn(), forgetAll: vi.fn(),
         conversationPage: vi.fn(), conversationState: vi.fn(), evidence: vi.fn() },
  stream: vi.fn(),
}))
beforeEach(() => { vi.resetAllMocks(); Element.prototype.scrollIntoView = vi.fn() })
afterEach(cleanup)

const state = (episodes = 2): MemoryState => ({
  profile: { department: 'Ops', region: 'SG', preferred_script: 'zh-Hant', models: [['WX-38', 3]],
             machines: [['610101', 2]] },
  episodes: [
    { id: 'e1', kind: 'answer', text: '问：E17-42 怎么排查\n答：检查冷却液循环泵', created_at: '2026-11-23T00:00:00Z',
      updated_at: '2026-11-23T00:00:00Z' },
    { id: 'e2', kind: 'help_request', text: '求助单 ab12，机台 610101。处理结果：更换 LS-2', created_at: '2026-11-23T00:00:00Z',
      updated_at: '2026-11-23T01:00:00Z' },
  ].slice(0, episodes) as MemoryState['episodes'],
})

it('shows the profile and episodes, and deletes one episode', async () => {
  vi.mocked(api.memory).mockResolvedValueOnce(state()).mockResolvedValueOnce(state(1))
  vi.mocked(api.forgetEpisode).mockResolvedValue()
  render(<Memory />)
  await screen.findByText(/检查冷却液循环泵/)
  expect(screen.getByText('回复字形：繁体')).toBeTruthy()
  expect(screen.getByText('常问机型：WX-38（3 次）')).toBeTruthy()
  expect(screen.getByText(/部门 Ops · 区域 SG/)).toBeTruthy()
  fireEvent.click(screen.getAllByRole('button', { name: '删除这条' })[1])
  await waitFor(() => expect(api.forgetEpisode).toHaveBeenCalledWith('e2', expect.anything()))
  await waitFor(() => expect(screen.queryByText(/更换 LS-2/)).toBeNull())
})

it('clearing everything needs a second confirmation', async () => {
  vi.mocked(api.memory).mockResolvedValueOnce(state())
    .mockResolvedValueOnce({ ...state(0), profile: { ...state().profile, preferred_script: null, models: [], machines: [] } })
  vi.mocked(api.forgetAll).mockResolvedValue()
  render(<Memory />)
  fireEvent.click(await screen.findByRole('button', { name: '清空全部记忆' }))
  expect(api.forgetAll).not.toHaveBeenCalled()
  expect(screen.getByText(/无法恢复/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '确认清空' }))
  await screen.findByText('暂无记录。带引用的回答和已解决的求助单会记在这里。')
  expect(api.forgetAll).toHaveBeenCalledOnce()
})

it('shows an error and keeps the page usable when loading fails', async () => {
  vi.mocked(api.memory).mockRejectedValueOnce(new Error('down')).mockResolvedValueOnce(state(0))
  render(<Memory />)
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: '刷新' }))
  await screen.findByText('暂无记录。带引用的回答和已解决的求助单会记在这里。')
})

it('returning to a conversation shows help request updates', async () => {
  vi.mocked(api.conversationPage).mockResolvedValue({ items: [{ id: 'c1', title: '现场求助' }], next_cursor: null })
  vi.mocked(api.conversationState).mockResolvedValue({
    conversation_id: 'c1', blocked: false, pending_confirmation: null,
    messages: [{ role: 'assistant', content: '已创建求助单。', sources: [], error_code: null }],
    help_updates: [{ id: 'r1', kind: 'field_help', status: 'IN_PROGRESS', summary: '现场求助单', machine_id: '610101',
                     symptom: 'pump 不转', action: 'progress', note: '已远程查看 alarm log', at: '2026-11-24T01:00:00Z' }],
  })
  render(<Chat />)
  fireEvent.click(await screen.findByRole('button', { name: '现场求助' }))
  await screen.findByText('你的求助单已更新')
  expect(screen.getByText(/机台 610101：pump 不转/).textContent).toContain('处理中：已远程查看 alarm log')
  expect(screen.getByRole('link', { name: '查看我的求助单' }).getAttribute('href')).toBe('#/help')
  fireEvent.click(screen.getByRole('button', { name: '＋ 新会话' }))
  await waitFor(() => expect(screen.queryByText('你的求助单已更新')).toBeNull())
})
