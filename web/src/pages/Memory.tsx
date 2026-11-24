import { useEffect, useRef, useState } from 'react'
import { api, type MemoryState } from '../api'

const KIND: Record<string, string> = { answer: '以往问答', help_request: '已解决的求助单' }
const SCRIPT: Record<string, string> = { 'zh-Hans': '简体', 'zh-Hant': '繁体' }

/** The user's own long-term memory: what the assistant may recall, and a way to delete it. */
export default function Memory() {
  const [state, setState] = useState<MemoryState | null>(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [confirmClear, setConfirmClear] = useState(false)
  const active = useRef<AbortController | null>(null)

  const run = (work: (signal: AbortSignal) => Promise<void>, failure: string) => {
    if (active.current) return
    const controller = new AbortController()
    active.current = controller
    setBusy(true)
    setError('')
    work(controller.signal)
      .then(() => api.memory(controller.signal))
      .then(next => { if (!controller.signal.aborted) setState(next) })
      .catch(() => { if (!controller.signal.aborted) setError(failure) })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false)
        if (active.current === controller) active.current = null
      })
  }

  useEffect(() => {
    run(async () => {}, '记忆加载失败，请重试。')
    return () => active.current?.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const profile = state?.profile
  return (
    <main className="memory">
      <div className="escalation-heading">
        <h2>我的记忆</h2>
        <button disabled={busy} onClick={() => run(async () => {}, '记忆加载失败，请重试。')}>刷新</button>
      </div>
      <p className="hint">助手会在新会话里参考这些记录。每次使用前都会按你当前的权限重新核对：引用的资料已撤回或你已无权查看时，这条记录不会被使用。</p>
      {error && <p role="alert" className="err">{error}</p>}
      {busy && <p role="status">加载中…</p>}
      {profile && <section aria-label="用户画像">
        <h3>画像</h3>
        <p>部门 {profile.department} · 区域 {profile.region ?? '—'}（来自账号资料，这里不能修改）</p>
        <p>回复字形：{profile.preferred_script ? SCRIPT[profile.preferred_script] : '按每次提问判断'}</p>
        <p>常问机型：{profile.models.length ? profile.models.map(([m, n]) => `${m}（${n} 次）`).join('、') : '暂无'}</p>
        <p>提过求助的机台：{profile.machines.length ? profile.machines.map(([m, n]) => `${m}（${n} 次）`).join('、') : '暂无'}</p>
      </section>}
      {state && <section aria-label="情景记忆">
        <h3>情景记忆（{state.episodes.length}）</h3>
        {state.episodes.length === 0 && <p>暂无记录。带引用的回答和已解决的求助单会记在这里。</p>}
        <ul className="escalation-list">
          {state.episodes.map(e => (
            <li key={e.id}>
              <div className="escalation-heading">
                <strong>{KIND[e.kind] ?? e.kind}</strong>
                <time dateTime={e.updated_at}>{new Date(e.updated_at).toLocaleString()}</time>
              </div>
              <p className="text">{e.text}</p>
              <button disabled={busy} onClick={() => run(signal => api.forgetEpisode(e.id, signal), '删除失败，请重试。')}>
                删除这条
              </button>
            </li>
          ))}
        </ul>
        {!confirmClear
          ? <button disabled={busy} onClick={() => setConfirmClear(true)}>清空全部记忆</button>
          : <p className="confirm">
            将删除全部情景记忆和画像统计，无法恢复。
            <button disabled={busy} onClick={() => {
              setConfirmClear(false)
              run(signal => api.forgetAll(signal), '清空失败，请重试。')
            }}>确认清空</button>
            <button disabled={busy} onClick={() => setConfirmClear(false)}>取消</button>
          </p>}
      </section>}
    </main>
  )
}
