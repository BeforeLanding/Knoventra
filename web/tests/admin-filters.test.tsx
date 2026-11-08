import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Admin from '../src/pages/Admin'
import { api, type AdminDocuments } from '../src/api'

vi.mock('../src/api', () => ({ api: { adminDocs: vi.fn(), upload: vi.fn(), publish: vi.fn() } }))
const docs: AdminDocuments = { kbs: ['ops', 'hr'], documents: [
  { kb: 'ops', title: 'VPN_100% 手册', lang: 'zh-Hans', revisions: [
    { id: 'old', version: 1, status: 'RETIRED' }, { id: 'live', version: 2, status: 'PUBLISHED' },
    { id: 'draft', version: 3, status: 'DRAFT' }] },
  { kb: 'hr', title: 'VPN 使用制度', lang: 'zh-Hans', revisions: [{ id: 'hr', version: 1, status: 'PUBLISHED' }] },
  { kb: 'ops', title: '待补版本', lang: 'zh-Hans', revisions: [] },
] }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.adminDocs).mockResolvedValue(structuredClone(docs)) })
afterEach(cleanup)
async function open() {
  render(<Admin />)
  await screen.findByText('显示 3 / 3 份文档')
  await waitFor(() => expect(screen.getByRole('button', { name: '刷新资料' }).disabled).toBe(false))
}
function title(value: string) { fireEvent.change(screen.getByRole('searchbox', { name: '查找标题' }), { target: { value } }) }
function kb(value: string) { fireEvent.change(screen.getByRole('combobox', { name: '筛选知识库' }), { target: { value } }) }
function status(value: string) { fireEvent.change(screen.getByRole('combobox', { name: '筛选修订状态' }), { target: { value } }) }
function rows() { return within(screen.getByRole('table')).getAllByRole('row').slice(1) }

it('matches literal titles case-insensitively and does not match document body or KB names', async () => {
  await open()
  title('  vpn_100%  ')
  expect(rows()).toHaveLength(1)
  expect(rows()[0].textContent).toContain('VPN_100% 手册')
  expect(screen.getByText('显示 1 / 3 份文档')).toBeTruthy()
  title('ops')
  expect(rows()).toHaveLength(0)
  expect(screen.getByText('没有匹配的文档，请调整或清除筛选。')).toBeTruthy()
  expect(api.adminDocs).toHaveBeenCalledOnce()
  expect(api.upload).not.toHaveBeenCalled()
  expect(api.publish).not.toHaveBeenCalled()
})

it.each([['DRAFT', 1], ['PUBLISHED', 2], ['RETIRED', 1]])('filters %s by document while keeping full revision history', async (value, count) => {
  await open()
  status(value as string)
  expect(rows()).toHaveLength(count as number)
  const vpn = rows().find(row => row.textContent?.includes('VPN_100%'))!
  expect(vpn.textContent).toContain('v1 RETIRED')
  expect(vpn.textContent).toContain('v2 PUBLISHED')
  expect(within(vpn).getByRole('button', { name: '发布 VPN_100% 手册 v3' }).disabled).toBe(false)
  expect(screen.queryByText('待补版本')).toBeNull()
})

it('combines filters and resets them without touching upload fields or making requests', async () => {
  await open()
  fireEvent.change(screen.getByRole('textbox', { name: '文档标题' }), { target: { value: '上传草稿' } })
  title('vpn'); kb('hr'); status('DRAFT')
  expect(rows()).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: '清除筛选' }))
  expect(rows()).toHaveLength(3)
  expect(screen.getByRole('textbox', { name: '文档标题' }).value).toBe('上传草稿')
  expect(screen.getByRole('combobox', { name: '知识库' }).value).toBe('ops')
  expect(screen.getByRole('button', { name: '清除筛选' }).disabled).toBe(true)
  expect(api.adminDocs).toHaveBeenCalledOnce()
  expect(api.upload).not.toHaveBeenCalled()
  expect(api.publish).not.toHaveBeenCalled()
})

it('retains filters across explicit refresh and applies them to the new catalog', async () => {
  await open()
  title('VPN'); kb('ops'); status('DRAFT')
  const next = structuredClone(docs)
  next.documents[0].revisions[2].status = 'PUBLISHED'
  next.documents[0].revisions[1].status = 'RETIRED'
  vi.mocked(api.adminDocs).mockResolvedValueOnce(next)
  fireEvent.click(screen.getByRole('button', { name: '刷新资料' }))
  await screen.findByText('显示 0 / 3 份文档')
  expect(screen.getByRole('searchbox', { name: '查找标题' }).value).toBe('VPN')
  expect(screen.getByRole('combobox', { name: '筛选知识库' }).value).toBe('ops')
  expect(screen.getByRole('combobox', { name: '筛选修订状态' }).value).toBe('DRAFT')
  expect(api.publish).not.toHaveBeenCalled()
})

it('filtering stale data after a failed refresh cannot unlock publishing', async () => {
  await open()
  vi.mocked(api.adminDocs).mockRejectedValueOnce(new Error('offline'))
  fireEvent.click(screen.getByRole('button', { name: '刷新资料' }))
  await screen.findByRole('alert')
  title('vpn'); kb('ops')
  const publish = screen.getByRole('button', { name: '发布 VPN_100% 手册 v3' })
  expect(publish.disabled).toBe(true)
  fireEvent.click(publish)
  expect(api.publish).not.toHaveBeenCalled()
  expect(screen.getByText('列表可能尚未更新，请刷新核对后再操作。')).toBeTruthy()
})

it('retains an unavailable selected KB until the user explicitly clears it', async () => {
  await open()
  kb('ops')
  vi.mocked(api.adminDocs).mockResolvedValueOnce({ ...docs, kbs: ['hr'], documents: docs.documents.filter(d => d.kb === 'hr') })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '刷新资料' })) })
  expect(screen.getByRole('combobox', { name: '筛选知识库' }).value).toBe('ops')
  expect(screen.getByRole('option', { name: 'ops（当前不可用）' })).toBeTruthy()
  expect(screen.getByText('显示 0 / 1 份文档')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '清除筛选' }))
  expect(rows()).toHaveLength(1)
})
