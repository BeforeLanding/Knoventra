import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Escalations from '../src/pages/Escalations'
import { api, type EscalationRecord } from '../src/api'

vi.mock('../src/api', () => ({ api: { escalations: vi.fn() } }))
beforeEach(() => vi.resetAllMocks())
afterEach(cleanup)

const record = (id: string, status = 'OPEN'): EscalationRecord => ({
  id, status, summary: `摘要 ${id}`, created_at: null, evidence_refs: [],
})

it('shows an explicit empty state after loading owned records', async () => {
  vi.mocked(api.escalations).mockResolvedValue({ items: [], next_cursor: null })
  render(<Escalations />)
  await screen.findByText('暂无转交记录。请在问答中发起转交并确认提交。')
  expect(screen.queryByRole('button', { name: '加载更多' })).toBeNull()
})

it('loads additional pages and refreshes from the start', async () => {
  vi.mocked(api.escalations).mockResolvedValueOnce({ items: [record('1')], next_cursor: 'cursor1' })
    .mockResolvedValueOnce({ items: [record('2', 'IN_PROGRESS')], next_cursor: null })
    .mockResolvedValueOnce({ items: [{ ...record('3', 'CLOSED'), created_at: '2026-11-07T01:00:00Z' }], next_cursor: null })
  render(<Escalations />)
  await screen.findByText('摘要 1')
  expect(screen.getByText('历史时间未知')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '加载更多' }))
  await screen.findByText('摘要 2')
  expect(screen.getByText('待处理')).toBeTruthy()
  expect(screen.getByText('处理中')).toBeTruthy()
  expect(vi.mocked(api.escalations).mock.calls[1][0]).toBe('cursor1')
  fireEvent.click(screen.getByRole('button', { name: '刷新记录' }))
  await screen.findByText('摘要 3')
  expect(screen.getByText('已关闭')).toBeTruthy()
  expect(screen.queryByText('摘要 1')).toBeNull()
  expect(vi.mocked(api.escalations).mock.calls[2][0]).toBeUndefined()
})

it('allows refresh after an initial request failure', async () => {
  vi.mocked(api.escalations).mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({ items: [record('1')], next_cursor: null })
  render(<Escalations />)
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: '刷新记录' }))
  await screen.findByText('摘要 1')
  expect(screen.queryByRole('alert')).toBeNull()
})

it('keeps previous records and cursor after a pagination failure, deduplicating overlapping results', async () => {
  vi.mocked(api.escalations).mockResolvedValueOnce({ items: [record('1')], next_cursor: 'cursor1' })
    .mockRejectedValueOnce(new Error('network'))
    .mockResolvedValueOnce({ items: [record('1', 'CLOSED'), record('2')], next_cursor: null })
  render(<Escalations />)
  await screen.findByText('摘要 1')
  fireEvent.click(screen.getByRole('button', { name: '加载更多' }))
  await screen.findByRole('alert')
  expect(screen.getByText('摘要 1')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '加载更多' }))
  await screen.findByText('摘要 2')
  expect(screen.getAllByText('摘要 1')).toHaveLength(1)
  expect(screen.getByText('已关闭')).toBeTruthy()
  expect(vi.mocked(api.escalations).mock.calls[2][0]).toBe('cursor1')
})

it('aborts the active request when leaving the page', async () => {
  vi.mocked(api.escalations).mockImplementation(() => new Promise(() => {}))
  const { unmount } = render(<Escalations />)
  await waitFor(() => expect(api.escalations).toHaveBeenCalledOnce())
  const signal = vi.mocked(api.escalations).mock.calls[0][1]!
  expect(signal.aborted).toBe(false)
  unmount()
  expect(signal.aborted).toBe(true)
})

it('also aborts an in-flight pagination request when leaving', async () => {
  vi.mocked(api.escalations).mockResolvedValueOnce({ items: [record('1')], next_cursor: 'cursor1' })
    .mockImplementationOnce(() => new Promise(() => {}))
  const { unmount } = render(<Escalations />)
  await screen.findByText('摘要 1')
  fireEvent.click(screen.getByRole('button', { name: '加载更多' }))
  await waitFor(() => expect(api.escalations).toHaveBeenCalledTimes(2))
  const signal = vi.mocked(api.escalations).mock.calls[1][1]!
  unmount()
  expect(signal.aborted).toBe(true)
})
