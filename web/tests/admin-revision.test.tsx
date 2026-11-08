import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Admin from '../src/pages/Admin'
import { api, type AdminDocuments } from '../src/api'

vi.mock('../src/api', () => ({ api: { adminDocs: vi.fn(), upload: vi.fn(), publish: vi.fn() } }))
const title = '  原始🙂標題  '
const docs: AdminDocuments = { kbs: ['ops', 'hr'], documents: [
  { kb: 'ops', title, lang: 'zh-Hant', revisions: [{ id: 'live', version: 1, status: 'PUBLISHED' }] },
  { kb: 'hr', title, lang: 'en', revisions: [] },
] }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.adminDocs).mockResolvedValue(structuredClone(docs)) })
afterEach(cleanup)
async function open() {
  render(<Admin />)
  await screen.findByText('显示 2 / 2 份文档')
  await waitFor(() => expect(screen.getByRole('button', { name: '刷新资料' }).disabled).toBe(false))
}
function prepare(kb = 'ops') {
  return screen.getByRole('button', { name: `填入新修订 ${kb} / ${title.trim()}` })
}
function form() { return screen.getByRole('button', { name: '上传' }).closest('form')! }

it('copies exact document identity and language without uploading or publishing, then focuses file selection', async () => {
  await open()
  fireEvent.change(screen.getByRole('textbox', { name: '文档标题' }), { target: { value: '先前草稿' } })
  fireEvent.click(prepare())
  expect(screen.getByRole('textbox', { name: '文档标题' }).value).toBe(title)
  expect(screen.getByRole('combobox', { name: '知识库' }).value).toBe('ops')
  expect(screen.getByRole('combobox', { name: '语言' }).value).toBe('zh-Hant')
  expect(document.activeElement).toBe(screen.getByLabelText('资料文件'))
  expect(screen.getByText(/并清空原文件选择/)).toBeTruthy()
  expect(screen.getByLabelText('资料文件').value).toBe('')
  expect(api.upload).not.toHaveBeenCalled()
  expect(api.publish).not.toHaveBeenCalled()
  expect(api.adminDocs).toHaveBeenCalledOnce()
})

it('distinguishes same-title documents in different KBs and submits the chosen exact metadata', async () => {
  await open()
  vi.mocked(api.upload).mockResolvedValue({ version: 2 })
  fireEvent.click(prepare('hr'))
  fireEvent.submit(form())
  await screen.findByText(/已上传为草稿：修订 2/)
  const body = vi.mocked(api.upload).mock.calls[0][0]
  expect(body.get('kb')).toBe('hr')
  expect(body.get('title')).toBe(title)
  expect(body.get('lang')).toBe('en')
  expect(api.publish).not.toHaveBeenCalled()
})

it('leaves filled metadata editable for creating a separate document', async () => {
  await open()
  vi.mocked(api.upload).mockResolvedValue({ version: 1 })
  fireEvent.click(prepare())
  fireEvent.change(screen.getByRole('textbox', { name: '文档标题' }), { target: { value: '另一份资料' } })
  fireEvent.submit(form())
  await screen.findByText(/已上传为草稿：修订 1/)
  expect(vi.mocked(api.upload).mock.calls[0][0].get('title')).toBe('另一份资料')
})

it('cannot replace the form while an upload is active', async () => {
  await open()
  let resolve!: (value: { version: number }) => void
  vi.mocked(api.upload).mockReturnValue(new Promise(yes => { resolve = yes }))
  fireEvent.change(screen.getByRole('textbox', { name: '文档标题' }), { target: { value: '执行中的上传' } })
  fireEvent.submit(form())
  expect(prepare().disabled).toBe(true)
  fireEvent.click(prepare())
  expect(screen.getByRole('textbox', { name: '文档标题' }).value).toBe('执行中的上传')
  await act(async () => { resolve({ version: 2 }) })
  expect(api.upload).toHaveBeenCalledOnce()
})

it('requires verified catalog data before filling a new revision', async () => {
  await open()
  vi.mocked(api.adminDocs).mockRejectedValueOnce(new Error('offline'))
  fireEvent.click(screen.getByRole('button', { name: '刷新资料' }))
  await screen.findByRole('alert')
  expect(prepare().disabled).toBe(true)
  fireEvent.click(prepare())
  expect(screen.getByRole('textbox', { name: '文档标题' }).value).toBe('')
  expect(api.upload).not.toHaveBeenCalled()
})

it.each(['language', 'kb'])('disables filling if the catalog metadata has an unsupported %s', async field => {
  vi.mocked(api.adminDocs).mockResolvedValue({ ...docs,
    kbs: field === 'kb' ? ['hr'] : docs.kbs,
    upload_rules: { title_max_chars: 255, languages: field === 'language' ? ['en'] : ['en', 'zh-Hant'] } })
  await open()
  expect(prepare().disabled).toBe(true)
  fireEvent.click(prepare())
  expect(screen.getByRole('textbox', { name: '文档标题' }).value).toBe('')
})
