import { useEffect, useRef, useState } from 'react'
import { api, type AdminDocuments } from '../api'

export default function Admin() {
  const [data, setData] = useState<AdminDocuments | null>(null)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(true)
  const [verified, setVerified] = useState(false)
  const [titleQuery, setTitleQuery] = useState('')
  const [kbFilter, setKbFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const active = useRef<AbortController | null>(null)
  const uploadForm = useRef<HTMLFormElement | null>(null)
  const titleLimit = data?.upload_rules?.title_max_chars ?? 255
  const languages = data?.upload_rules?.languages ?? ['zh-Hans', 'zh-Hant', 'en']
  const matchingDocuments = data?.documents.filter(d =>
    (!kbFilter || d.kb === kbFilter)
    && d.title.toLowerCase().includes(titleQuery.trim().toLowerCase())
    && (!statusFilter || d.revisions.some(r => r.status === statusFilter))) ?? []
  const clearFilters = () => { setTitleQuery(''); setKbFilter(''); setStatusFilter('') }

  const read = async (controller: AbortController) => {
    try {
      const next = await api.adminDocs(controller.signal)
      if (controller.signal.aborted) return
      setData(next)
      setVerified(true)
    } catch {
      if (!controller.signal.aborted) setError('列表加载失败，请刷新资料核对后再操作。')
    }
  }
  const finish = (controller: AbortController) => {
    if (!controller.signal.aborted) setBusy(false)
    if (active.current === controller) active.current = null
  }
  useEffect(() => {
    const controller = new AbortController()
    active.current = controller
    void api.adminDocs(controller.signal).then(next => {
      if (controller.signal.aborted) return
      setData(next)
      setVerified(true)
    }).catch(() => {
      if (!controller.signal.aborted) setError('列表加载失败，请刷新资料核对后再操作。')
    }).finally(() => finish(controller))
    return () => { controller.abort(); active.current?.abort() }
  }, [])

  const begin = () => {
    if (active.current) return null
    const controller = new AbortController()
    active.current = controller
    setBusy(true)
    setVerified(false)
    setError('')
    return controller
  }
  const refresh = async () => {
    const controller = begin()
    if (!controller) return
    try { await read(controller) } finally { finish(controller) }
  }

  const prepareRevision = (doc: AdminDocuments['documents'][number]) => {
    const form = uploadForm.current
    if (!form || busy || active.current || !verified || !data?.kbs.includes(doc.kb) || !languages.includes(doc.lang)) return
    const kb = form.elements.namedItem('kb') as HTMLSelectElement
    const title = form.elements.namedItem('title') as HTMLInputElement
    const lang = form.elements.namedItem('lang') as HTMLSelectElement
    kb.value = doc.kb
    title.value = doc.title
    lang.value = doc.lang
    const file = form.elements.namedItem('file') as HTMLInputElement
    file.value = ''
    setError('')
    setMsg(`已填入 ${doc.kb} / ${doc.title} 的上传信息，并清空原文件选择。请选择新文件后上传为草稿。`)
    file.focus()
  }

  const upload = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    if (!verified || !data?.kbs.length) return
    const form = e.currentTarget
    const title = (form.elements.namedItem('title') as HTMLInputElement).value
    const lang = (form.elements.namedItem('lang') as HTMLSelectElement).value
    if (!title.trim() || [...title].length > titleLimit || title.includes('\0')) {
      setMsg('')
      setError(`标题须含非空白内容，不含空字符，且最多 ${titleLimit} 个字符。`)
      return
    }
    if (!languages.includes(lang)) {
      setMsg('')
      setError('请选择服务端支持的资料语言。')
      return
    }
    const file = (form.elements.namedItem('file') as HTMLInputElement).files?.[0]
    if (file && data.upload_max_bytes && file.size > data.upload_max_bytes) {
      setMsg('')
      setError('文件超过单文件大小上限，请选择更小的文件。')
      return
    }
    const body = new FormData(form)
    const controller = begin()
    if (!controller) return
    setMsg('')
    try {
      const r = await api.upload(body, controller.signal)
      if (controller.signal.aborted) return
      setMsg(`已上传为草稿：修订 ${r.version}（尚未生效，点击发布后才可被检索）`)
      form.reset()
      await read(controller)
    } catch (error: any) {
      if (!controller.signal.aborted) {
        setMsg('')
        if (error?.status === 503 && error?.code === 'ENCODER_OUTPUT_INVALID') {
          setVerified(true)
          setError('编码服务返回异常结果，上传未提交。请检查服务后再重试。')
        } else if (error?.status === 413) {
          setVerified(true)
          setError('文件超过服务端允许的大小，上传已拒绝。请选择更小的文件后重试。')
        } else if (error?.status === 422 && error?.code === 'DOCUMENT_PARSE_INVALID') {
          setVerified(true)
          setError('文件无法解析，上传未提交。请使用 UTF-8 Markdown 或有效且未加密的 PDF，并检查正文是否含空字符。')
        } else if (error?.status === 422) {
          setVerified(true)
          setError('资料未通过校验，上传未提交。请检查标题、语言和正文后重试。')
        } else setError('上传结果未确认，请先刷新资料核对，避免重复上传。')
      }
    } finally { finish(controller) }
  }
  const publish = async (id: string) => {
    if (!verified) return
    const controller = begin()
    if (!controller) return
    setMsg('')
    try {
      await api.publish(id, controller.signal)
      if (controller.signal.aborted) return
      setMsg('发布请求已成功，列表状态需以刷新结果为准。')
      await read(controller)
    } catch {
      if (!controller.signal.aborted) {
        setMsg('')
        setError('发布结果未确认，请先刷新资料核对，不要直接重复发布。')
      }
    } finally { finish(controller) }
  }

  return (
    <div className="admin">
      <button disabled={busy} onClick={refresh}>刷新资料</button>
      {busy && <p role="status">处理中…</p>}
      {error && <p role="alert" className="err">{error}</p>}
      {msg && <p role="status" className="hint">{msg}</p>}
      {data && !verified && <p className="hint">列表可能尚未更新，请刷新核对后再操作。</p>}
      <h3>上传资料</h3>
      <p className="hint" id="document-title-rule">标题最多 {titleLimit} 个字符，语言以当前支持列表为准。</p>
      {data?.upload_max_bytes && <p className="hint">单文件最大 {data.upload_max_bytes.toLocaleString()} 字节（以服务端校验为准）。</p>}
      <form ref={uploadForm} onSubmit={upload}>
        <fieldset disabled={busy || !verified || !data?.kbs.length}>
          <select aria-label="知识库" name="kb">{data?.kbs.map(k => <option key={k}>{k}</option>)}</select>
          <input aria-label="文档标题" aria-describedby="document-title-rule" name="title" placeholder="文档标题（与已有标题相同则作为新修订）" required />
          <select aria-label="语言" name="lang">{languages.map(lang => <option key={lang}>{lang}</option>)}</select>
          <input aria-label="资料文件" name="file" type="file" accept=".md,.pdf" required />
          <button>上传</button>
        </fieldset>
      </form>
      {verified && !data?.kbs.length && <p>暂无可用知识库，暂时无法上传资料。</p>}
      <h3>文档与修订</h3>
      <div className="catalog-filters">
        <label>查找标题 <input type="search" value={titleQuery} onChange={e => setTitleQuery(e.target.value)} /></label>
        <label>筛选知识库 <select value={kbFilter} onChange={e => setKbFilter(e.target.value)}>
          <option value="">全部知识库</option>
          {kbFilter && !data?.kbs.includes(kbFilter) && <option value={kbFilter}>{kbFilter}（当前不可用）</option>}
          {data?.kbs.map(k => <option key={k} value={k}>{k}</option>)}
        </select></label>
        <label>筛选修订状态 <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
          <option value="">全部状态</option>
          <option value="DRAFT">含草稿</option>
          <option value="PUBLISHED">含生效版本</option>
          <option value="RETIRED">含退役版本</option>
        </select></label>
        <button type="button" onClick={clearFilters} disabled={!titleQuery && !kbFilter && !statusFilter}>清除筛选</button>
      </div>
      <p className="hint">筛选已加载目录中的文档标题与修订状态；匹配文档仍显示全部修订。</p>
      {data && <p role="status">显示 {matchingDocuments.length} / {data.documents.length} 份文档</p>}
      {verified && data?.documents.length === 0 && <p>暂无文档，可上传资料创建草稿。</p>}
      {data && data.documents.length > 0 && matchingDocuments.length === 0 && <p>没有匹配的文档，请调整或清除筛选。</p>}
      <table>
        <thead><tr><th>知识库</th><th>标题</th><th>语言</th><th>修订</th><th>操作</th></tr></thead>
        <tbody>
          {matchingDocuments.map(d => (
            <tr key={JSON.stringify([d.kb, d.title])}>
              <td>{d.kb}</td><td>{d.title}</td><td>{d.lang}</td>
              <td>{d.revisions.map(r => (
                <span key={r.id} className={`rev ${r.status}`}>v{r.version} {r.status}
                  {r.status === 'DRAFT' && <button disabled={busy || !verified} aria-label={`发布 ${d.title} v${r.version}`} onClick={() => publish(r.id)}>发布</button>}</span>
              ))}</td>
              <td><button type="button" onClick={() => prepareRevision(d)}
                disabled={busy || !verified || !data?.kbs.includes(d.kb) || !languages.includes(d.lang)}
                aria-label={`填入新修订 ${d.kb} / ${d.title}`}
                title="将替换上传表单中的知识库、标题和语言，并清空文件选择">填入新修订</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
