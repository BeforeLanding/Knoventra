import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import App from '../src/App'
import { getToken } from '../src/api'

vi.mock('../src/pages/Chat', () => ({ default: () => <p>已进入问答</p> }))
vi.mock('../src/pages/Admin', () => ({ default: () => <p>资料管理页面</p> }))
vi.mock('../src/pages/Escalations', () => ({ default: () => <p>转交页面</p> }))
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear() })

it.each(['#/login', '#/admin'])('completes real Login -> credentials -> App identity without reload from %s', async route => {
  localStorage.clear()
  history.replaceState(null, '', route)
  const fetcher = vi.fn().mockImplementation((url: string) => {
    if (url === '/api/v1/auth/login') return Promise.resolve(Response.json({ token: 'fresh-login' }))
    if (url === '/api/v1/me') return Promise.resolve(Response.json({ id: 'sg', email: 'sg-ops@example.test',
      role: 'user', department: 'Ops', region: 'SG' }))
    throw new Error('Unexpected request')
  })
  vi.stubGlobal('fetch', fetcher)
  render(<App />)
  fireEvent.click(screen.getByRole('button', { name: /sg-ops@example.test/ }))
  await screen.findByText('已进入问答')
  await waitFor(() => expect(location.hash).toBe('#/chat'))
  expect(getToken()).toBe('fresh-login')
  expect(screen.getByTestId('identity').textContent).toContain('sg-ops@example.test')
  const identityCalls = fetcher.mock.calls.filter(call => call[0] === '/api/v1/me')
  expect(identityCalls.length).toBeGreaterThan(0)
  expect(identityCalls.every(call => call[1].headers.get('Authorization') === 'Bearer fresh-login')).toBe(true)
  expect(fetcher.mock.calls.filter(call => call[0] === '/api/v1/auth/login')).toHaveLength(1)
  expect(screen.queryByText('资料管理页面')).toBeNull()
})
