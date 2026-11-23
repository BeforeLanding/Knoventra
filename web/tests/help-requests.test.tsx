import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import HelpRequests from '../src/pages/HelpRequests'
import { allowedActions } from '../src/help'
import { api, ApiError, type HelpItem } from '../src/api'

vi.mock('../src/api', async () => {
  const actual = await vi.importActual<typeof import('../src/api')>('../src/api')
  return { ApiError: actual.ApiError, api: { helpRequests: vi.fn(), helpRequest: vi.fn(), helpAction: vi.fn(), helpCandidates: vi.fn() } }
})
beforeEach(() => vi.resetAllMocks())
afterEach(cleanup)

const item = (over: Partial<HelpItem> = {}): HelpItem => ({
  id: 'r1', kind: 'field_help', status: 'OPEN', summary: '现场求助单', machine_id: '610101', machine_model: 'WX-38',
  alarm_codes: ['A-1301'], symptom: 'pump 不转', tried_steps: '重插 LS-2', resolution: '', requester: 'sg-ops@example.test',
  assignee: null, assignee_id: null, created_at: null, updated_at: '2026-11-23T00:00:00Z', my_roles: ['requester'],
  events: [{ action: 'created', from_status: null, to_status: 'OPEN', note: '', actor: 'sg-ops@example.test', at: '2026-11-23T00:00:00Z' }],
  ...over,
})

it('offers only the actions the viewer may take', () => {
  expect(allowedActions(item())).toEqual(['cancel'])
  expect(allowedActions(item({ status: 'RESOLVED' }))).toEqual(['close', 'reopen'])
  expect(allowedActions(item({ status: 'ASSIGNED', my_roles: ['assignee'] }))).toEqual(['start'])
  expect(allowedActions(item({ status: 'IN_PROGRESS', my_roles: ['assignee'] }))).toEqual(['progress', 'resolve'])
  expect(allowedActions(item({ my_roles: ['admin'] }))).toEqual(['assign', 'cancel'])
  expect(allowedActions(item({ status: 'CLOSED' }))).toEqual([])
})

it('requester sees details and the event trail, and can withdraw', async () => {
  vi.mocked(api.helpRequests).mockResolvedValue({ items: [item()] })
  vi.mocked(api.helpRequest).mockResolvedValue(item())
  vi.mocked(api.helpAction).mockResolvedValue(item({ status: 'CANCELLED' }))
  render(<HelpRequests mode="mine" isAdmin={false} />)
  fireEvent.click(await screen.findByText(/pump 不转/))
  await screen.findByText('A-1301')
  expect(screen.getByText(/创建/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '撤销' }))
  await screen.findByRole('heading', { name: '已撤销' })
  expect(vi.mocked(api.helpAction).mock.calls[0]).toEqual(['r1', { action: 'cancel', note: '' }])
  expect(vi.mocked(api.helpRequests)).toHaveBeenCalledTimes(2)
})

it('expert must write a resolution before submitting it', async () => {
  const working = item({ status: 'IN_PROGRESS', my_roles: ['assignee'] })
  vi.mocked(api.helpRequests).mockResolvedValue({ items: [working] })
  vi.mocked(api.helpRequest).mockResolvedValue(working)
  vi.mocked(api.helpAction).mockResolvedValue(item({ status: 'RESOLVED', my_roles: ['assignee'], resolution: '更换传感器' }))
  render(<HelpRequests mode="workbench" isAdmin={false} />)
  fireEvent.click(await screen.findByText(/pump 不转/))
  const submit = await screen.findByRole('button', { name: '提交解决方案' })
  expect((submit as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('说明'), { target: { value: '更换传感器' } })
  fireEvent.click(submit)
  await screen.findByText('更换传感器')
  expect(vi.mocked(api.helpAction).mock.calls[0]).toEqual(['r1', { action: 'resolve', resolution: '更换传感器' }])
})

it('admin dispatches from the queue with the recommended engineer preselected', async () => {
  const queued = item({ my_roles: ['admin'] })
  vi.mocked(api.helpRequests).mockResolvedValue({ items: [queued] })
  vi.mocked(api.helpRequest).mockResolvedValue(queued)
  vi.mocked(api.helpCandidates).mockResolvedValue({ reason: 'r', candidates: [
    { user_id: 'u2', engineer: 'Zhou Lan', reason: '同区域' }, { user_id: 'u3', engineer: 'Tan Wei Ming', reason: '跨区域' }] })
  vi.mocked(api.helpAction).mockResolvedValue(item({ status: 'ASSIGNED', my_roles: ['admin'], assignee: 'Zhou Lan' }))
  render(<HelpRequests mode="workbench" isAdmin />)
  fireEvent.click(screen.getByRole('tab', { name: '待派单队列' }))
  fireEvent.click(await screen.findByText(/pump 不转/))
  await waitFor(() => expect((screen.getByLabelText('派给') as HTMLSelectElement).value).toBe('u2'))
  fireEvent.click(screen.getByRole('button', { name: '派单' }))
  await screen.findByRole('heading', { name: '已派单' })
  expect(vi.mocked(api.helpAction).mock.calls[0]).toEqual(['r1', { action: 'assign', assignee_id: 'u2' }])
  expect(vi.mocked(api.helpRequests).mock.calls.some(call => call[0] === 'queue')).toBe(true)
})

it('explains a conflict instead of failing silently', async () => {
  vi.mocked(api.helpRequests).mockResolvedValue({ items: [item()] })
  vi.mocked(api.helpRequest).mockResolvedValue(item())
  vi.mocked(api.helpAction).mockRejectedValue(new ApiError(409, '{"detail":{"code":"INVALID_TRANSITION"}}'))
  render(<HelpRequests mode="mine" isAdmin={false} />)
  fireEvent.click(await screen.findByText(/pump 不转/))
  fireEvent.click(await screen.findByRole('button', { name: '撤销' }))
  expect((await screen.findByRole('alert')).textContent).toContain('状态已变化')
})
