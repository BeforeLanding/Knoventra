import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Login from '../src/pages/Login'
import { api, getToken } from '../src/api'
import { openChat } from '../src/navigation'

vi.mock('../src/api', () => ({ api: { login: vi.fn() }, getToken: vi.fn() }))
vi.mock('../src/navigation', () => ({ openChat: vi.fn() }))
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
beforeEach(() => vi.resetAllMocks())
afterEach(cleanup)

it('locks all account choices until the first login completes and navigates only once', async () => {
  const pending = deferred<string>()
  vi.mocked(api.login).mockReturnValue(pending.promise)
  render(<Login />)
  const first = screen.getByRole('button', { name: /sg-ops@example.test/ })
  const other = screen.getByRole('button', { name: /admin@example.test/ })
  fireEvent.click(first)
  fireEvent.click(first)
  fireEvent.click(other)
  expect(api.login).toHaveBeenCalledOnce()
  expect(api.login).toHaveBeenCalledWith('sg-ops@example.test', 'demo-pass-1', expect.any(AbortSignal))
  expect(screen.getByRole('status').textContent).toContain('sg-ops@example.test')
  expect(screen.getAllByRole('button').every(button => button.disabled)).toBe(true)
  vi.mocked(getToken).mockReturnValue('logged-in')
  await act(async () => { pending.resolve('logged-in') })
  expect(openChat).toHaveBeenCalledOnce()
  fireEvent.click(other)
  expect(api.login).toHaveBeenCalledOnce()
})

it('permits an explicit account selection after failure and clears the old error', async () => {
  const retry = deferred<string>()
  vi.mocked(api.login).mockRejectedValueOnce(new Error('offline')).mockReturnValueOnce(retry.promise)
  render(<Login />)
  fireEvent.click(screen.getByRole('button', { name: /sg-ops@example.test/ }))
  await screen.findByRole('alert')
  expect(screen.getAllByRole('button').every(button => !button.disabled)).toBe(true)
  expect(openChat).not.toHaveBeenCalled()
  expect(api.login).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: /tw-ops@example.test/ }))
  expect(screen.queryByRole('alert')).toBeNull()
  vi.mocked(getToken).mockReturnValue('retry-token')
  await act(async () => { retry.resolve('retry-token') })
  expect(api.login).toHaveBeenCalledTimes(2)
  expect(openChat).toHaveBeenCalledOnce()
})

it.each([false, true])('aborts on leaving and ignores a late login result (reject=%s)', async reject => {
  const pending = deferred<string>()
  vi.mocked(api.login).mockReturnValue(pending.promise)
  const view = render(<Login />)
  fireEvent.click(screen.getByRole('button', { name: /sg-ops@example.test/ }))
  const signal = vi.mocked(api.login).mock.calls[0][2]!
  view.unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => { if (reject) pending.reject(new Error('late')); else pending.resolve('late-token') })
  expect(openChat).not.toHaveBeenCalled()
  expect(screen.queryByRole('alert')).toBeNull()
})

it('explains a changed session without automatically overwriting or retrying it', async () => {
  vi.mocked(api.login).mockRejectedValue(new Error('LOGIN_SESSION_CHANGED'))
  render(<Login />)
  fireEvent.click(screen.getByRole('button', { name: /admin@example.test/ }))
  await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('账号状态已变化，请核对当前账号后重新选择。'))
  expect(api.login).toHaveBeenCalledOnce()
  expect(openChat).not.toHaveBeenCalled()
})

it('does not navigate for a login result superseded by newer credentials', async () => {
  vi.mocked(api.login).mockResolvedValue('old-result')
  vi.mocked(getToken).mockReturnValue('newer-result')
  render(<Login />)
  fireEvent.click(screen.getByRole('button', { name: /sg-ops@example.test/ }))
  await screen.findByRole('alert')
  expect(openChat).not.toHaveBeenCalled()
  expect(screen.getAllByRole('button').every(button => !button.disabled)).toBe(true)
  expect(api.login).toHaveBeenCalledOnce()
})
