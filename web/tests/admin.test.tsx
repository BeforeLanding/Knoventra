import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Admin from '../src/pages/Admin'
import { api, type AdminDocuments } from '../src/api'

vi.mock('../src/api', () => ({ api: { adminDocs: vi.fn(), upload: vi.fn(), publish: vi.fn() } }))
const docs: AdminDocuments = { kbs: ['ops'], documents: [{ kb: 'ops', title: '手册', lang: 'zh-Hans',
  revisions: [{ id: 'r1', version: 1, status: 'DRAFT' }] }] }
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.adminDocs).mockResolvedValue(structuredClone(docs)) })
afterEach(cleanup)
async function open() {
  const view = render(<Admin />)
  const publish = await screen.findByRole('button', { name: '发布 手册 v1' })
  await waitFor(() => expect(publish.disabled).toBe(false))
  fireEvent.change(screen.getByRole('textbox', { name: '文档标题' }), { target: { value: '测试资料' } })
  return { ...view, publish }
}

it('shows a failed initial load and permits an explicit read retry', async () => {
  vi.mocked(api.adminDocs).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(docs)
  render(<Admin />)
  await screen.findByRole('alert')
  expect(screen.getByRole('button', { name: '上传' }).closest('fieldset')!.disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: '刷新资料' }))
  await screen.findByRole('button', { name: '发布 手册 v1' })
  await waitFor(() => expect(screen.getByRole('button', { name: '刷新资料' }).disabled).toBe(false))
  expect(screen.queryByRole('alert')).toBeNull()
  expect(api.upload).not.toHaveBeenCalled()
  expect(api.publish).not.toHaveBeenCalled()
})

it('blocks duplicate publish and upload while a mutation is pending', async () => {
  const pending = deferred<unknown>()
  vi.mocked(api.publish).mockReturnValue(pending.promise)
  const { publish } = await open()
  fireEvent.click(publish)
  fireEvent.click(publish)
  fireEvent.submit(screen.getByRole('button', { name: '上传' }).closest('form')!)
  expect(api.publish).toHaveBeenCalledOnce()
  expect(api.upload).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: '刷新资料' }).disabled).toBe(true)
  await act(async () => { pending.resolve({ status: 'PUBLISHED' }) })
  await waitFor(() => expect(api.adminDocs).toHaveBeenCalledTimes(2))
})

it('preserves success when the following list refresh fails and requires a read before another write', async () => {
  vi.mocked(api.publish).mockResolvedValue({ status: 'PUBLISHED' })
  vi.mocked(api.adminDocs).mockResolvedValueOnce(docs).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(docs)
  const { publish } = await open()
  fireEvent.click(publish)
  await screen.findByRole('alert')
  expect(screen.getByText('发布请求已成功，列表状态需以刷新结果为准。')).toBeTruthy()
  expect(publish.disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: '刷新资料' }))
  await waitFor(() => expect(publish.disabled).toBe(false))
  expect(api.publish).toHaveBeenCalledOnce()
})

it('does not claim an uncertain publish was rolled back and does not automatically retry', async () => {
  vi.mocked(api.publish).mockRejectedValue(new Error('response lost'))
  const { publish } = await open()
  fireEvent.click(publish)
  await screen.findByRole('alert')
  expect(screen.getByRole('alert').textContent).toContain('发布结果未确认')
  expect(screen.queryByText('发布失败，旧版本仍然有效')).toBeNull()
  expect(publish.disabled).toBe(true)
  expect(api.adminDocs).toHaveBeenCalledOnce()
  expect(api.publish).toHaveBeenCalledOnce()
})

it('preserves upload fields on failure and blocks repeat submission until list verification', async () => {
  vi.mocked(api.upload).mockRejectedValue(new Error('lost'))
  await open()
  fireEvent.change(screen.getByRole('textbox', { name: '文档标题' }), { target: { value: '保留的标题' } })
  const form = screen.getByRole('button', { name: '上传' }).closest('form')!
  fireEvent.submit(form)
  await screen.findByRole('alert')
  expect((screen.getByRole('textbox', { name: '文档标题' }) as HTMLInputElement).value).toBe('保留的标题')
  fireEvent.submit(form)
  expect(api.upload).toHaveBeenCalledOnce()
  expect(api.adminDocs).toHaveBeenCalledOnce()
})

it('clears fields after confirmed upload and refreshes without publishing', async () => {
  vi.mocked(api.upload).mockResolvedValue({ version: 2 })
  await open()
  fireEvent.change(screen.getByRole('textbox', { name: '文档标题' }), { target: { value: '新手册' } })
  fireEvent.submit(screen.getByRole('button', { name: '上传' }).closest('form')!)
  await screen.findByText(/已上传为草稿：修订 2/)
  expect(vi.mocked(api.upload).mock.calls[0][0].get('title')).toBe('新手册')
  expect((screen.getByRole('textbox', { name: '文档标题' }) as HTMLInputElement).value).toBe('')
  await waitFor(() => expect(api.adminDocs).toHaveBeenCalledTimes(2))
  expect(api.publish).not.toHaveBeenCalled()
})

it.each(['read', 'publish', 'upload'])('cancels %s when leaving and ignores a late response', async kind => {
  const pending = deferred<any>()
  let unmount: () => void
  let signal: AbortSignal
  if (kind === 'read') {
    vi.mocked(api.adminDocs).mockReturnValue(pending.promise)
    unmount = render(<Admin />).unmount
    signal = vi.mocked(api.adminDocs).mock.calls[0][0]!
  } else {
    if (kind === 'publish') vi.mocked(api.publish).mockReturnValue(pending.promise)
    else vi.mocked(api.upload).mockReturnValue(pending.promise)
    const view = await open()
    unmount = view.unmount
    if (kind === 'publish') fireEvent.click(view.publish)
    else fireEvent.submit(screen.getByRole('button', { name: '上传' }).closest('form')!)
    signal = kind === 'publish' ? vi.mocked(api.publish).mock.calls[0][1]! : vi.mocked(api.upload).mock.calls[0][1]!
  }
  unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => { pending.resolve(kind === 'read' ? docs : { version: 2 }) })
  expect(api.adminDocs).toHaveBeenCalledOnce()
})

it('handles an empty catalog without enabling upload', async () => {
  vi.mocked(api.adminDocs).mockResolvedValue({ kbs: [], documents: [] })
  render(<Admin />)
  await screen.findByText('暂无可用知识库，暂时无法上传资料。')
  expect(screen.getByRole('button', { name: '上传' }).closest('fieldset')!.disabled).toBe(true)
})

it('keeps writes locked until the confirmed publish is reflected by a refreshed list', async () => {
  const pending = deferred<AdminDocuments>()
  vi.mocked(api.adminDocs).mockResolvedValueOnce(docs).mockReturnValueOnce(pending.promise)
  vi.mocked(api.publish).mockResolvedValue({ status: 'PUBLISHED' })
  const { publish } = await open()
  fireEvent.click(publish)
  await screen.findByText('发布请求已成功，列表状态需以刷新结果为准。')
  expect(publish.disabled).toBe(true)
  expect(screen.getByRole('button', { name: '刷新资料' }).disabled).toBe(true)
  const next = structuredClone(docs)
  next.documents[0].revisions[0].status = 'PUBLISHED'
  await act(async () => { pending.resolve(next) })
  await screen.findByText('v1 PUBLISHED')
  expect(screen.queryByRole('button', { name: '发布 手册 v1' })).toBeNull()
  expect(api.publish).toHaveBeenCalledOnce()
  expect(screen.getByRole('button', { name: '上传' }).closest('fieldset')!.disabled).toBe(false)
})

it('does not submit a second upload while the first is pending', async () => {
  const pending = deferred<{ version: number }>()
  vi.mocked(api.upload).mockReturnValue(pending.promise)
  await open()
  const form = screen.getByRole('button', { name: '上传' }).closest('form')!
  fireEvent.change(screen.getByRole('textbox', { name: '文档标题' }), { target: { value: '处理中标题' } })
  fireEvent.submit(form)
  fireEvent.submit(form)
  expect(api.upload).toHaveBeenCalledOnce()
  expect((screen.getByRole('textbox', { name: '文档标题' }) as HTMLInputElement).value).toBe('处理中标题')
  await act(async () => { pending.resolve({ version: 2 }) })
  await screen.findByText(/已上传为草稿：修订 2/)
})

it('uses the server-advertised file limit before sending and preserves the selected file', async () => {
  vi.mocked(api.adminDocs).mockResolvedValue({ ...docs, upload_max_bytes: 4 })
  await open()
  const input = screen.getByLabelText('资料文件') as HTMLInputElement
  const large = new File(['12345'], 'large.md', { type: 'text/markdown' })
  fireEvent.change(input, { target: { files: [large] } })
  fireEvent.submit(screen.getByRole('button', { name: '上传' }).closest('form')!)
  expect(screen.getByRole('alert').textContent).toContain('超过单文件大小上限')
  expect(api.upload).not.toHaveBeenCalled()
  expect(input.files?.[0]).toBe(large)
  expect(input.closest('fieldset')!.disabled).toBe(false)
  vi.mocked(api.upload).mockResolvedValue({ version: 2 })
  fireEvent.change(input, { target: { files: [new File(['1234'], 'exact.md')] } })
  fireEvent.submit(screen.getByRole('button', { name: '上传' }).closest('form')!)
  await screen.findByText(/已上传为草稿：修订 2/)
  expect(api.upload).toHaveBeenCalledOnce()
})

it('treats server 413 as a rejected upload and permits selecting a smaller file without replaying', async () => {
  vi.mocked(api.upload).mockRejectedValue(Object.assign(new Error('too large'), { status: 413 }))
  await open()
  fireEvent.change(screen.getByRole('textbox', { name: '文档标题' }), { target: { value: '保留标题' } })
  fireEvent.submit(screen.getByRole('button', { name: '上传' }).closest('form')!)
  await screen.findByRole('alert')
  expect(screen.getByRole('alert').textContent).toContain('上传已拒绝')
  expect(screen.getByRole('button', { name: '上传' }).closest('fieldset')!.disabled).toBe(false)
  expect((screen.getByRole('textbox', { name: '文档标题' }) as HTMLInputElement).value).toBe('保留标题')
  expect(api.upload).toHaveBeenCalledOnce()
  expect(api.adminDocs).toHaveBeenCalledOnce()
})

it.each(['   ', '字'.repeat(256), '🙂'.repeat(256), '含\0字符'])('rejects invalid title locally and preserves the form', async title => {
  await open()
  const input = screen.getByRole('textbox', { name: '文档标题' }) as HTMLInputElement
  fireEvent.change(input, { target: { value: title } })
  fireEvent.submit(screen.getByRole('button', { name: '上传' }).closest('form')!)
  expect(screen.getByRole('alert').textContent).toContain('标题须含非空白内容')
  expect(input.value).toBe(title)
  expect(api.upload).not.toHaveBeenCalled()
})

it('applies advertised title/language rules and counts Unicode code points', async () => {
  vi.mocked(api.adminDocs).mockResolvedValue({ ...docs, upload_rules: { title_max_chars: 4, languages: ['en'] } })
  vi.mocked(api.upload).mockResolvedValue({ version: 2 })
  await open()
  const input = screen.getByRole('textbox', { name: '文档标题' })
  expect((screen.getByRole('combobox', { name: '语言' }) as HTMLSelectElement).value).toBe('en')
  fireEvent.change(input, { target: { value: '🙂🙂🙂🙂🙂' } })
  fireEvent.submit(screen.getByRole('button', { name: '上传' }).closest('form')!)
  expect(api.upload).not.toHaveBeenCalled()
  fireEvent.change(input, { target: { value: '🙂🙂🙂🙂' } })
  fireEvent.submit(screen.getByRole('button', { name: '上传' }).closest('form')!)
  await screen.findByText(/已上传为草稿：修订 2/)
  expect(vi.mocked(api.upload).mock.calls[0][0].get('title')).toBe('🙂🙂🙂🙂')
  expect(vi.mocked(api.upload).mock.calls[0][0].get('lang')).toBe('en')
})

it('preserves a server validation rejection for correction instead of claiming an uncertain commit', async () => {
  vi.mocked(api.upload).mockRejectedValue(Object.assign(new Error('metadata'), { status: 422 }))
  await open()
  fireEvent.submit(screen.getByRole('button', { name: '上传' }).closest('form')!)
  await screen.findByRole('alert')
  expect(screen.getByRole('alert').textContent).toContain('上传未提交')
  expect((screen.getByRole('textbox', { name: '文档标题' }) as HTMLInputElement).value).toBe('测试资料')
  expect(screen.getByRole('button', { name: '上传' }).closest('fieldset')!.disabled).toBe(false)
  expect(api.upload).toHaveBeenCalledOnce()
  expect(api.adminDocs).toHaveBeenCalledOnce()
})

it('reports a recognized encoder service failure without blaming document metadata or replaying', async () => {
  vi.mocked(api.upload).mockRejectedValue(Object.assign(new Error('encoder'), { status: 503, code: 'ENCODER_OUTPUT_INVALID' }))
  await open()
  fireEvent.submit(screen.getByRole('button', { name: '上传' }).closest('form')!)
  await screen.findByRole('alert')
  expect(screen.getByRole('alert').textContent).toBe('编码服务返回异常结果，上传未提交。请检查服务后再重试。')
  expect(screen.getByRole('button', { name: '上传' }).closest('fieldset')!.disabled).toBe(false)
  expect(api.upload).toHaveBeenCalledOnce()
  expect(api.adminDocs).toHaveBeenCalledOnce()
})

it('explains a recognized parse rejection and preserves editable fields without retrying', async () => {
  vi.mocked(api.upload).mockRejectedValue(Object.assign(new Error('parse'), { status: 422, code: 'DOCUMENT_PARSE_INVALID' }))
  await open()
  fireEvent.submit(screen.getByRole('button', { name: '上传' }).closest('form')!)
  await screen.findByRole('alert')
  expect(screen.getByRole('alert').textContent).toContain('UTF-8 Markdown 或有效且未加密的 PDF')
  expect(screen.getByRole('textbox', { name: '文档标题' }).value).toBe('测试资料')
  expect(screen.getByRole('button', { name: '上传' }).closest('fieldset')!.disabled).toBe(false)
  expect(api.upload).toHaveBeenCalledOnce()
  expect(api.adminDocs).toHaveBeenCalledOnce()
})
