import { useEffect, useRef, useState } from 'react'
import { api, type EscalationRecord } from '../api'

const STATUS: Record<string, string> = { OPEN: '待处理', IN_PROGRESS: '处理中', CLOSED: '已关闭' }

export default function Escalations() {
  const [items, setItems] = useState<EscalationRecord[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const active = useRef<AbortController | null>(null)

  const fetchPage = async (before: string | undefined, append: boolean, controller: AbortController) => {
    try {
      const page = await api.escalations(before, controller.signal)
      if (controller.signal.aborted) return
      setItems(previous => append ? [...new Map([...previous, ...page.items].map(item => [item.id, item])).values()] : page.items)
      setCursor(page.next_cursor)
    } catch {
      if (!controller.signal.aborted) setError('转交记录加载失败，请重试。')
    } finally {
      if (!controller.signal.aborted) setBusy(false)
      if (active.current === controller) active.current = null
    }
  }

  useEffect(() => {
    const controller = new AbortController()
    active.current = controller
    api.escalations(undefined, controller.signal).then(page => {
      if (controller.signal.aborted) return
      setItems(page.items)
      setCursor(page.next_cursor)
    }).catch(() => {
      if (!controller.signal.aborted) setError('转交记录加载失败，请重试。')
    }).finally(() => {
      if (!controller.signal.aborted) setBusy(false)
      if (active.current === controller) active.current = null
    })
    return () => { controller.abort(); active.current?.abort() }
  }, [])

  const load = (append: boolean) => {
    if (active.current) return
    const controller = new AbortController()
    active.current = controller
    setBusy(true)
    setError('')
    void fetchPage(append ? cursor ?? undefined : undefined, append, controller)
  }

  return (
    <main className="escalations">
      <div className="escalation-heading">
        <h2>我的转交记录</h2>
        <button disabled={busy} onClick={() => load(false)}>刷新记录</button>
      </div>
      <p className="hint">查看已确认提交的转交及当前状态。新记录按创建时间倒序显示，历史时间未知的记录列在后面。</p>
      {error && <p role="alert" className="err">{error}</p>}
      {busy && <p role="status">加载中…</p>}
      {!busy && !error && items.length === 0 && <p>暂无转交记录。请在问答中发起转交并确认提交。</p>}
      <ul className="escalation-list">
        {items.map(item => (
          <li key={item.id}>
            <div className="escalation-heading">
              <strong>{STATUS[item.status] ?? item.status}</strong>
              <time dateTime={item.created_at ?? undefined}>{item.created_at ? new Date(item.created_at).toLocaleString() : '历史时间未知'}</time>
            </div>
            <p className="text">{item.summary}</p>
          </li>
        ))}
      </ul>
      {cursor && <button disabled={busy} onClick={() => load(true)}>加载更多</button>}
    </main>
  )
}
