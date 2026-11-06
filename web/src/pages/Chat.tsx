import { useEffect, useRef, useState } from 'react'
import { api, stream, type Source, type Confirmation, type ConversationState } from '../api'

type Msg = {
  role: 'user' | 'assistant'; content: string; sources?: Source[]
  refusal?: string; status?: string; confirm?: Confirmation
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
  const [blocked, setBlocked] = useState(false)
  const [notice, setNotice] = useState('')
  const [preview, setPreview] = useState<any>(null)
  const bottom = useRef<HTMLDivElement>(null)
  const active = useRef<AbortController | null>(null)

  const refresh = () => api.conversations().then(setConvs).catch(() => setNotice('会话列表加载失败，请重试。'))
  useEffect(() => {
    refresh()
    return () => { active.current?.abort() }
  }, [])
  useEffect(() => { bottom.current?.scrollIntoView({ block: 'end' }) }, [msgs])

  const restore = (state: ConversationState) => {
    const history: Msg[] = state.messages.map(m => ({
      role: m.role, content: m.content, sources: m.sources,
      refusal: m.error_code && REFUSAL_TEXT[m.error_code] ? m.error_code : undefined,
      error: m.error_code && !REFUSAL_TEXT[m.error_code] ? ERROR_TEXT[m.error_code] ?? m.error_code : undefined,
    }))
    if (state.pending_confirmation) history.push({ role: 'assistant', content: '', confirm: state.pending_confirmation })
    setMsgs(history)
    setBlocked(state.blocked)
  }

  const open = async (id: string | null) => {
    if (active.current) return
    setPreview(null)
    setNotice('')
    if (!id) { setCid(null); setMsgs([]); setBlocked(false); return }
    const controller = new AbortController()
    active.current = controller
    setBusy(true)
    try {
      const state = await api.conversationState(id, controller.signal)
      if (controller.signal.aborted) return
      setCid(id)
      restore(state)
    } catch {
      if (!controller.signal.aborted) setNotice('会话加载失败，请重新选择会话。')
    } finally {
      if (!controller.signal.aborted) setBusy(false)
      active.current = null
    }
  }

  const patchLast = (f: (m: Msg) => Msg) => setMsgs(ms => ms.map((m, i) => (i === ms.length - 1 ? f(m) : m)))

  const consume = async (path: string, body: unknown) => {
    if (active.current) return
    const controller = new AbortController()
    active.current = controller
    setBusy(true)
    setNotice('')
    let conversationId = cid
    try {
      for await (const { event, data } of stream(path, body, controller.signal)) {
        if (controller.signal.aborted) return
        if (event === 'meta' && data.conversation_id) { conversationId = data.conversation_id; setCid(conversationId) }
        else if (event === 'status') patchLast(m => ({ ...m, status: data.tool === 'search_knowledge' ? '检索中…' : '调用工具：' + data.tool }))
        else if (event === 'token') patchLast(m => ({ ...m, content: m.content + data.text, status: undefined }))
        else if (event === 'sources') patchLast(m => ({ ...m, sources: data }))
        else if (event === 'confirm') { setBlocked(true); patchLast(m => ({ ...m, status: undefined, confirm: data })) }
        else if (event === 'error') {
          const c = data.code as string
          patchLast(m => (REFUSAL_TEXT[c] ? { ...m, refusal: c } : { ...m, error: ERROR_TEXT[c] ?? c }))
        } else if (event === 'done') patchLast(m => ({ ...m, status: undefined, content: data.answer }))
      }
    } catch (e: any) {
      if (controller.signal.aborted) return
      const error = e.status === 409
        ? (e.message?.includes('CONVERSATION_BUSY') ? '此会话正在处理其他请求，请稍后重新打开。' : '请先处理待确认的转交。')
        : '请求失败'
      setNotice(error)
      patchLast(m => ({ ...m, status: undefined, error }))
    } finally {
      if (!controller.signal.aborted) {
        // The stored decision wins, including after a lost confirmation response.
        // Lock the composer until the server's checkpoint has been read successfully.
        if (conversationId) {
          setBlocked(true)
          try { restore(await api.conversationState(conversationId, controller.signal)) }
          catch { if (!controller.signal.aborted) setNotice('会话状态同步失败，请重新打开会话后继续。') }
        }
        if (!controller.signal.aborted) { setBusy(false); refresh() }
      }
      active.current = null
    }
  }

  const send = async () => {
    const text = input.trim()
    if (!text || busy || blocked || active.current) return
    setInput('')
    setMsgs(ms => [...ms, { role: 'user', content: text }, { role: 'assistant', content: '', status: '思考中…' }])
    await consume('/chat', { conversation_id: cid, message: text })
  }

  const decide = async (id: string, approve: boolean) => {
    if (active.current) return
    setMsgs(ms => [...ms, { role: 'assistant', content: '', status: '处理中…' }])
    await consume('/chat/confirm', { confirmation_id: id, approve })
  }

  const show = async (s: Source) => {
    try { setPreview(await api.evidence(s.chunk_id)) } catch { setPreview({ error: '无权预览或来源不存在' }) }
  }

  return (
    <div className="chat">
      <aside>
        <button disabled={busy} onClick={() => open(null)}>＋ 新会话</button>
        {convs.map(c => <button disabled={busy} key={c.id} className={c.id === cid ? 'conv on' : 'conv'} onClick={() => open(c.id)}>{c.title || '(无标题)'}</button>)}
      </aside>
      <main>
        <div className="msgs">
          {notice && <p role="alert" className="err">{notice}</p>}
          {blocked && <p className="hint">请先处理待确认的转交；若状态未同步，请重新打开会话。</p>}
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
                  <small>有效期至 {new Date(m.confirm.expires_at).toLocaleString()}</small>
                  {m.confirm.expired && <p className="err">确认已过期，请关闭此确认后继续。</p>}
                  {m.confirm.status !== 'PENDING' ? <span>已保存决定，点击恢复处理。</span> : null}
                  <div>
                    <button disabled={busy || m.confirm.expired || m.confirm.status !== 'PENDING'} onClick={() => decide(m.confirm!.confirmation_id, true)}>确认转交</button>
                    <button disabled={busy} onClick={() => decide(m.confirm!.confirmation_id, false)}>{m.confirm.expired ? '关闭过期确认' : m.confirm.status !== 'PENDING' ? '恢复处理' : '取消'}</button>
                  </div>
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
          <button disabled={busy || blocked || !input.trim()}>发送</button>
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
