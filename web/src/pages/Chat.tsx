import { useEffect, useRef, useState } from 'react'
import { api, stream, type Source } from '../api'

type Msg = {
  role: 'user' | 'assistant'; content: string; sources?: Source[]
  refusal?: string; status?: string; confirm?: { id: string; summary: string; done?: string }
  error?: string
}

const REFUSAL_TEXT: Record<string, string> = {
  NO_ACCESS: '你当前没有可检索的资料权限。',
  INSUFFICIENT_EVIDENCE: '资料中没有可靠依据，已拒答。',
}
const ERROR_TEXT: Record<string, string> = {
  RUN_LIMIT: '本轮工具调用已达上限。', RETRIEVAL_UNAVAILABLE: '检索服务暂不可用。',
  CONFIRMATION_EXPIRED: '确认已过期，未创建转交记录。',
}

export default function Chat() {
  const [convs, setConvs] = useState<{ id: string; title: string }[]>([])
  const [cid, setCid] = useState<string | null>(null)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<any>(null)
  const bottom = useRef<HTMLDivElement>(null)

  const refresh = () => api.conversations().then(setConvs)
  useEffect(() => { refresh() }, [])
  useEffect(() => { bottom.current?.scrollIntoView({ block: 'end' }) }, [msgs])

  const open = async (id: string | null) => {
    setCid(id)
    if (!id) return setMsgs([])
    setMsgs((await api.messages(id)).map(m => ({ role: m.role as Msg['role'], content: m.content, sources: m.sources })))
  }

  const patchLast = (f: (m: Msg) => Msg) => setMsgs(ms => ms.map((m, i) => (i === ms.length - 1 ? f(m) : m)))

  const consume = async (it: AsyncGenerator<{ event: string; data: any }>) => {
    try {
      for await (const { event, data } of it) {
        if (event === 'meta' && data.conversation_id) setCid(data.conversation_id)
        else if (event === 'status') patchLast(m => ({ ...m, status: data.tool === 'search_knowledge' ? '检索中…' : '调用工具：' + data.tool }))
        else if (event === 'token') patchLast(m => ({ ...m, content: m.content + data.text, status: undefined }))
        else if (event === 'sources') patchLast(m => ({ ...m, sources: data }))
        else if (event === 'confirm') patchLast(m => ({ ...m, status: undefined, confirm: { id: data.confirmation_id, summary: data.summary } }))
        else if (event === 'error') {
          const c = data.code as string
          patchLast(m => (REFUSAL_TEXT[c] ? { ...m, refusal: c } : { ...m, error: ERROR_TEXT[c] ?? c }))
        } else if (event === 'done') patchLast(m => ({ ...m, status: undefined, content: data.answer || m.content }))
      }
    } catch (e: any) {
      patchLast(m => ({ ...m, status: undefined, error: e.status === 409 ? '请先处理待确认的转交。' : '请求失败' }))
    }
    refresh()
  }

  const send = async () => {
    const text = input.trim()
    if (!text || busy) return
    setInput('')
    setBusy(true)
    setMsgs(ms => [...ms, { role: 'user', content: text }, { role: 'assistant', content: '', status: '思考中…' }])
    await consume(stream('/chat', { conversation_id: cid, message: text }))
    setBusy(false)
  }

  const decide = async (i: number, id: string, approve: boolean) => {
    setBusy(true)
    setMsgs(ms => ms.map((m, j) => (j === i ? { ...m, confirm: { ...m.confirm!, done: approve ? '已确认' : '已取消' } } : m)))
    setMsgs(ms => [...ms, { role: 'assistant', content: '', status: '处理中…' }])
    await consume(stream('/chat/confirm', { confirmation_id: id, approve }))
    setBusy(false)
  }

  const show = async (s: Source) => {
    try { setPreview(await api.evidence(s.chunk_id)) } catch { setPreview({ error: '无权预览或来源不存在' }) }
  }

  return (
    <div className="chat">
      <aside>
        <button onClick={() => open(null)}>＋ 新会话</button>
        {convs.map(c => <div key={c.id} className={c.id === cid ? 'conv on' : 'conv'} onClick={() => open(c.id)}>{c.title || '(无标题)'}</div>)}
      </aside>
      <main>
        <div className="msgs">
          {msgs.length === 0 && <p className="hint">试试：「E17 设备报代码 42 先查什么」</p>}
          {msgs.map((m, i) => (
            <div key={i} className={`msg ${m.role}${m.refusal ? ' refusal' : ''}`}>
              {m.refusal && <div className="tag">{REFUSAL_TEXT[m.refusal]}</div>}
              {m.error && <div className="tag err">{m.error}</div>}
              <div className="text">{m.content}{m.status && <em className="status"> {m.status}</em>}</div>
              {m.confirm && (
                <div className="confirm">
                  <b>转交专家确认</b>
                  <p>{m.confirm.summary}</p>
                  {m.confirm.done ? <span>{m.confirm.done}</span> : (
                    <>
                      <button disabled={busy} onClick={() => decide(i, m.confirm!.id, true)}>确认转交</button>
                      <button disabled={busy} onClick={() => decide(i, m.confirm!.id, false)}>取消</button>
                    </>
                  )}
                </div>
              )}
              {!!m.sources?.length && (
                <div className="sources">
                  {m.sources.map(s => (
                    <div key={s.chunk_id} className="card" onClick={() => show(s)}>
                      <b>[{s.alias}] {s.document_title}</b>
                      <small>修订 {s.revision} · 第 {s.page ?? '-'} 页{s.section_path && ` · ${s.section_path}`}</small>
                      <p>{s.snippet}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
          <div ref={bottom} />
        </div>
        <form onSubmit={e => { e.preventDefault(); send() }}>
          <input value={input} onChange={e => setInput(e.target.value)} placeholder="向你有权限的资料提问…" />
          <button disabled={busy || !input.trim()}>发送</button>
        </form>
      </main>
      {preview && (
        <div className="modal" onClick={() => setPreview(null)}>
          <div className="panel" onClick={e => e.stopPropagation()}>
            {preview.error ? <p className="err">{preview.error}</p> : (
              <>
                <h3>{preview.document_title}</h3>
                <small>修订 {preview.revision}（{preview.revision_status}）· 第 {preview.page ?? '-'} 页 · {preview.section_path}</small>
                <pre>{preview.text}</pre>
              </>
            )}
            <button onClick={() => setPreview(null)}>关闭</button>
          </div>
        </div>
      )}
    </div>
  )
}
