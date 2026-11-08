import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from '../src/App'
import { api, setToken, type Me } from '../src/api'

vi.mock('../src/api', async original => {
  const real = await original<typeof import('../src/api')>()
  return { ...real, api: { ...real.api, me: vi.fn() } }
})
vi.mock('../src/pages/Chat', () => ({ default: () => <p>聊天页面</p> }))
vi.mock('../src/pages/Admin', () => ({ default: () => <p>资料管理页面</p> }))
vi.mock('../src/pages/Escalations', () => ({ default: () => <p>转交列表页面</p> }))
vi.mock('../src/pages/Login', () => ({ default: () => <p>登录页面</p> }))
const user = (id: string, role = 'user'): Me => ({ id, email: `${id}@example.test`, role, region: 'SG', department: 'Ops' })
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function navigate(hash: string) {
  await act(async () => { history.replaceState(null, '', hash); dispatchEvent(new HashChangeEvent('hashchange')) })
}
beforeEach(() => {
  vi.resetAllMocks()
  history.replaceState(null, '', '#/chat')
  localStorage.clear()
  setToken('token-a')
})
afterEach(() => { cleanup(); localStorage.clear() })

it('offers explicit identity retry and does not mount protected pages while unverified', async () => {
  vi.mocked(api.me).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(user('alice'))
  render(<App />)
  await screen.findByRole('alert')
  expect(screen.queryByText('聊天页面')).toBeNull()
  expect(api.me).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: '重试身份加载' }))
  await screen.findByText('聊天页面')
  expect(screen.getByTestId('identity').textContent).toContain('alice@example.test')
  expect(api.me).toHaveBeenCalledTimes(2)
})

it('ignores late identity from the previous token and aborts that request', async () => {
  const first = deferred<Me>(), second = deferred<Me>()
  vi.mocked(api.me).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  render(<App />)
  const signal = vi.mocked(api.me).mock.calls[0][0]!
  await act(async () => setToken('token-b'))
  expect(signal.aborted).toBe(true)
  await act(async () => { second.resolve(user('bob')) })
  await screen.findByText('聊天页面')
  await act(async () => { first.resolve(user('old-admin', 'admin')) })
  expect(screen.getByTestId('identity').textContent).toContain('bob@example.test')
  expect(screen.queryByRole('link', { name: '资料管理' })).toBeNull()
})

it('rechecks route identity and cannot reuse an old snapshot on returning to the same route', async () => {
  const pending = deferred<Me>(), returned = deferred<Me>()
  vi.mocked(api.me).mockResolvedValueOnce(user('admin', 'admin')).mockReturnValueOnce(pending.promise).mockReturnValue(returned.promise)
  render(<App />)
  await screen.findByText('聊天页面')
  await navigate('#/admin')
  expect(screen.queryByText('资料管理页面')).toBeNull()
  await navigate('#/chat')
  expect(screen.queryByText('聊天页面')).toBeNull()
  await act(async () => { pending.resolve(user('stale-admin', 'admin')) })
  expect(screen.queryByText('聊天页面')).toBeNull()
  await act(async () => { returned.resolve(user('now-user')) })
  await screen.findByText('聊天页面')
  expect(screen.queryByRole('link', { name: '资料管理' })).toBeNull()
})

it('reacts to a credential change from another tab', async () => {
  vi.mocked(api.me).mockResolvedValueOnce(user('alice')).mockResolvedValueOnce(user('bob'))
  render(<App />)
  await screen.findByText('聊天页面')
  await act(async () => {
    localStorage.setItem('knv_token', 'other-tab-token')
    dispatchEvent(new StorageEvent('storage', { key: 'knv_token', storageArea: localStorage }))
  })
  await waitFor(() => expect(screen.getByTestId('identity').textContent).toContain('bob@example.test'))
})

it('cancels identity on logout and ignores a late successful response', async () => {
  const pending = deferred<Me>()
  vi.mocked(api.me).mockReturnValue(pending.promise)
  render(<App />)
  const signal = vi.mocked(api.me).mock.calls[0][0]!
  await act(async () => setToken(null))
  expect(signal.aborted).toBe(true)
  await act(async () => { pending.resolve(user('old-admin', 'admin')) })
  expect(screen.getByText('登录页面')).toBeTruthy()
  expect(screen.queryByTestId('identity')).toBeNull()
})

it('does not request identity on the login route and cancels requests on unmount', async () => {
  history.replaceState(null, '', '#/login')
  const view = render(<App />)
  expect(api.me).not.toHaveBeenCalled()
  const pending = deferred<Me>()
  vi.mocked(api.me).mockReturnValue(pending.promise)
  await navigate('#/chat')
  const signal = vi.mocked(api.me).mock.calls[0][0]!
  view.unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => { pending.reject(new Error('late failure')) })
  expect(screen.queryByTestId('identity')).toBeNull()
})

it.each(['admin', 'user'])('checks %s before mounting a directly requested admin route', async role => {
  history.replaceState(null, '', '#/admin')
  const pending = deferred<Me>()
  vi.mocked(api.me).mockReturnValue(pending.promise)
  render(<App />)
  expect(screen.queryByText('资料管理页面')).toBeNull()
  expect(screen.queryByText('聊天页面')).toBeNull()
  await act(async () => { pending.resolve(user('current', role)) })
  await screen.findByText(role === 'admin' ? '资料管理页面' : '聊天页面')
  if (role !== 'admin') expect(screen.queryByRole('link', { name: '资料管理' })).toBeNull()
})
