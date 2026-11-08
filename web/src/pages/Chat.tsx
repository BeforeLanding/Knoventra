import { useEffect, useRef, useState } from 'react'
import { api, stream, type Source, type Confirmation, type ConversationState, type EvidencePreview } from '../api'

type Preview = { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; source: EvidencePreview }

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
  ANSWER_SAVE_FAILED: '回答保存失败，可稍后再次恢复。',
  MODEL_OUTPUT_TRUNCATED: '模型输出达到长度上限，本轮回答不完整。若已确认转交，请先查看转交记录。',
  MODEL_TOOL_CALL_INVALID: '模型返回的工具调用不符合要求，本轮已停止。若已确认转交，请先查看转交记录。',
  MODEL_CITATION_MISSING: '检索回答缺少有效引用，本轮未提供可核验的答案。',
  STREAM_INCOMPLETE: '连接在收到完成状态前中断，已尝试同步会话。请先检查当前结果。',
  STREAM_INVALID: '响应格式异常，已尝试同步会话。请先检查当前结果。',
}
const CONFLICT_TEXT: Record<string, string> = {
  CONVERSATION_BUSY: '此会话正在处理其他请求，请稍后重新打开。',
  CONFIRMATION_NOT_ACTIVE: '此确认已不再等待处理，请重新打开会话。',
  RUN_INCOMPLETE: '上次请求尚未完成，请先继续处理。',
  RUN_NOT_RETRYABLE: '当前请求无需重试或正在等待确认，请重新打开会话。',
  CHECKPOINT_CHANGED: '会话状态已更新，请重新打开后再继续。',
  ANSWER_NOT_SAVED: '上次回答尚未保存，请先恢复已完成回答。',
  RUN_NOT_RECOVERABLE: '当前没有需要恢复的已完成回答，请重新打开会话。',
}

export default function Chat({ maxMessageChars = 4000 }: { maxMessageChars?: number }) {
  const [convs, setConvs] = useState<{ id: string; title: string }[]>([])
  const [listCursor, setListCursor] = useState<string | null>(null)
  const [listBusy, setListBusy] = useState(true)
  const [listError, setListError] = useState('')
  const [searchTerm, setSearchTerm] = useState('')
  const [listQuery, setListQuery] = useState('')
  const queryRef = useRef('')
  const listRequest = useRef<AbortController | null>(null)
  const [cid, setCid] = useState<string | null>(null)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const messageLimit = Number.isSafeInteger(maxMessageChars) && maxMessageChars > 0 ? maxMessageChars : 4000
  const messageLength = [...input.trim()].length
  const messageTooLong = messageLength > messageLimit
  const [busy, setBusy] = useState(false)
  const [blocked, setBlocked] = useState(false)
  const [retryCheckpoint, setRetryCheckpoint] = useState<string | null>(null)
  const [recoverCheckpoint, setRecoverCheckpoint] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const bottom = useRef<HTMLDivElement>(null)
  const active = useRef<AbortController | null>(null)
  const previewRequest = useRef<AbortController | null>(null)
  const previewOrigin = useRef<HTMLElement | null>(null)
  const returnPreviewFocus = useRef(false)
  const previewClose = useRef<HTMLButtonElement | null>(null)
  const previewOpen = preview !== null

  const closePreview = (restoreFocus = true) => {
    previewRequest.current?.abort()
    previewRequest.current = null
    setPreview(null)
    returnPreviewFocus.current = restoreFocus
    if (!restoreFocus) previewOrigin.current = null
  }

  const readList = (controller: AbortController, before: string | undefined, append: boolean) =>
    (queryRef.current ? api.conversationPage(before, controller.signal, queryRef.current)
      : api.conversationPage(before, controller.signal)).then(page => {
      if (controller.signal.aborted || listRequest.current !== controller) return
      setConvs(previous => append ? [...new Map([...previous, ...page.items].map(item => [item.id, item])).values()] : page.items)
      setListCursor(page.next_cursor)
    }).catch(() => {
      if (!controller.signal.aborted && listRequest.current === controller) setListError('会话列表加载失败，请刷新或重试加载。')
    }).finally(() => {
      if (!controller.signal.aborted && listRequest.current === controller) {
        setListBusy(false)
        listRequest.current = null
      }
    })
  const refresh = (append = false) => {
    if (append && listRequest.current) return
    listRequest.current?.abort()
    const controller = new AbortController()
    listRequest.current = controller
    setListBusy(true)
    setListError('')
    void readList(controller, append ? listCursor ?? undefined : undefined, append)
  }
  const searchConversations = () => {
    if (busy) return
    const term = searchTerm.trim()
    if (term !== queryRef.current) {
      setConvs([])
      setListCursor(null)
      queryRef.current = term
      setListQuery(term)
    }
    refresh()
  }
  useEffect(() => {
    const controller = new AbortController()
    listRequest.current = controller
    void readList(controller, undefined, false)
    return () => { active.current?.abort(); previewRequest.current?.abort(); listRequest.current?.abort() }
  }, [])
  useEffect(() => {
    if (previewOpen) previewClose.current?.focus()
    else if (returnPreviewFocus.current) {
      if (previewOrigin.current?.isConnected) previewOrigin.current.focus()
      returnPreviewFocus.current = false
      previewOrigin.current = null
    }
  }, [previewOpen])
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
    setRetryCheckpoint(state.retry_checkpoint_id ?? null)
    setRecoverCheckpoint(state.recover_checkpoint_id ?? null)
  }

  const open = async (id: string | null) => {
    if (active.current) return
    closePreview(false)
    setNotice('')
    if (!id) { setCid(null); setMsgs([]); setBlocked(false); setRetryCheckpoint(null); setRecoverCheckpoint(null); return }
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
    let rejectedInput = false
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
          if (!REFUSAL_TEXT[c]) setNotice(ERROR_TEXT[c] ?? c)
          patchLast(m => (REFUSAL_TEXT[c] ? { ...m, refusal: c } : { ...m, error: ERROR_TEXT[c] ?? c }))
        } else if (event === 'done') patchLast(m => ({ ...m, status: undefined, content: data.answer }))
      }
    } catch (e: any) {
      if (controller.signal.aborted) return
      const error = e.status === 409
        ? (Object.entries(CONFLICT_TEXT).find(([code]) => e.message?.includes(code))?.[1] ?? '请先处理待确认的转交。')
        : e.status === 422 && path === '/chat' ? '问题未通过服务端校验，请检查长度与内容后重试。'
        : !conversationId && (e.code === 'STREAM_INCOMPLETE' || e.code === 'STREAM_INVALID')
          ? '连接异常且未收到会话标识，请从会话列表重新打开核对，避免重复提交。'
          : ERROR_TEXT[e.code] ?? '请求失败'
      if (!conversationId && (e.code === 'STREAM_INCOMPLETE' || e.code === 'STREAM_INVALID')) setBlocked(true)
      rejectedInput = e.status === 422 && path === '/chat'
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
    return rejectedInput && !controller.signal.aborted
  }

  const send = async () => {
    const text = input.trim()
    if (!text || messageTooLong || busy || blocked || active.current) return
    const originalInput = input
    setInput('')
    setMsgs(ms => [...ms, { role: 'user', content: text }, { role: 'assistant', content: '', status: '思考中…' }])
    if (await consume('/chat', { conversation_id: cid, message: text })) {
      setInput(current => current || originalInput)
    }
  }

  const decide = async (id: string, approve: boolean) => {
    if (active.current) return
    setMsgs(ms => [...ms, { role: 'assistant', content: '', status: '处理中…' }])
    await consume('/chat/confirm', { confirmation_id: id, approve })
  }

  const retry = async () => {
    if (active.current || !cid || !retryCheckpoint) return
    setMsgs(ms => [...ms, { role: 'assistant', content: '', status: '继续处理中…' }])
    await consume('/chat/retry', { conversation_id: cid, checkpoint_id: retryCheckpoint })
  }

  const recover = async () => {
    if (active.current || !cid || !recoverCheckpoint) return
    setMsgs(ms => [...ms, { role: 'assistant', content: '', status: '恢复回答中…' }])
    await consume('/chat/recover', { conversation_id: cid, checkpoint_id: recoverCheckpoint })
  }

  const show = async (s: Source, origin: HTMLElement) => {
    previewRequest.current?.abort()
    const controller = new AbortController()
    previewRequest.current = controller
    returnPreviewFocus.current = false
    previewOrigin.current = origin
    setPreview({ status: 'loading' })
    try {
      const source = await api.evidence(s.chunk_id, controller.signal)
      if (controller.signal.aborted || previewRequest.current !== controller) return
      if (source.chunk_id !== s.chunk_id) throw new Error('source identity mismatch')
      setPreview({ status: 'ready', source })
    } catch {
      if (!controller.signal.aborted && previewRequest.current === controller) {
        setPreview({ status: 'error', message: '来源暂不可用、无权预览或已不存在。请关闭后重试。' })
      }
    } finally {
      if (previewRequest.current === controller) previewRequest.current = null
    }
  }

  return (
    <div className="chat">
      <aside inert={previewOpen}>
        <button disabled={busy} onClick={() => open(null)}>＋ 新会话</button>
        <form onSubmit={e => { e.preventDefault(); searchConversations() }}>
          <input type="search" aria-label="会话标题关键词" placeholder="按会话标题搜索" maxLength={128} disabled={busy}
            value={searchTerm} onChange={e => setSearchTerm(e.target.value)} />
          <button disabled={busy || (listBusy && searchTerm.trim() === listQuery)}>搜索会话</button>
        </form>
        {listQuery && <p className="hint">当前标题筛选：{listQuery}</p>}
        <button disabled={busy || listBusy} onClick={() => refresh()}>刷新会话</button>
        {listBusy && <p role="status">正在加载会话…</p>}
        {listError && <p role="alert" className="err">{listError}</p>}
        {!listBusy && !listError && convs.length === 0 && <p>{listQuery ? '没有匹配的会话标题。' : '暂无会话。'}</p>}
        {convs.map(c => <button disabled={busy} key={c.id} className={c.id === cid ? 'conv on' : 'conv'} onClick={() => open(c.id)}>{c.title || '(无标题)'}</button>)}
        {listCursor && <button disabled={busy || listBusy} onClick={() => refresh(true)}>加载更多会话</button>}
      </aside>
      <main inert={previewOpen}>
        <div className="msgs">
          {notice && <p role="alert" className="err">{notice}</p>}
          {blocked && <p className="hint">{recoverCheckpoint ? '上次回答已完成但尚未保存，可直接恢复，无需重新生成。'
            : retryCheckpoint ? '上次请求未完成，可继续处理。' : '请先处理待确认的转交；若状态未同步，请重新打开会话。'}</p>}
          {retryCheckpoint && <button disabled={busy} onClick={retry}>继续未完成请求</button>}
          {recoverCheckpoint && <button disabled={busy} onClick={recover}>恢复已完成回答</button>}
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
                    <button type="button" key={s.chunk_id} className="card" onClick={e => show(s, e.currentTarget)}>
                      <b>[{s.alias}] {s.document_title}</b>
                      <small>修订 {s.revision} · 第 {s.page ?? '-'} 页{s.section_path && ` · ${s.section_path}`}</small>
                      <span className="snippet">{s.snippet}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
          <div ref={bottom} />
        </div>
        <form onSubmit={e => { e.preventDefault(); send() }}>
          <input aria-label="问题" aria-describedby="chat-message-limit" aria-invalid={messageTooLong}
            value={input} onChange={e => setInput(e.target.value)} placeholder="向你有权限的资料提问…" />
          <button disabled={busy || blocked || !input.trim() || messageTooLong}>发送</button>
        </form>
        <p id="chat-message-limit" className={messageTooLong ? 'err' : 'hint'}>
          {messageLength}/{messageLimit} 字符{messageTooLong && '，请缩短问题后再发送。'}
        </p>
      </main>
      {preview && (
        <div className="modal" onClick={() => closePreview()}>
          <div className="panel" role="dialog" aria-modal="true" aria-label="来源预览" onClick={e => e.stopPropagation()}
            onKeyDown={e => {
              if (e.key === 'Escape') { e.preventDefault(); closePreview() }
              if (e.key === 'Tab') { e.preventDefault(); previewClose.current?.focus() }
            }}>
            {preview.status === 'loading' ? <p role="status">正在加载来源…</p>
              : preview.status === 'error' ? <p role="alert" className="err">{preview.message}</p> : (
              <>
                <h3>{preview.source.document_title}</h3>
                <small>修订 {preview.source.revision}（{preview.source.revision_status}）· 第 {preview.source.page ?? '-'} 页 · {preview.source.section_path}</small>
                <pre>{preview.source.text}</pre>
              </>
            )}
            <button ref={previewClose} onClick={() => closePreview()}>关闭</button>
          </div>
        </div>
      )}
    </div>
  )
}
