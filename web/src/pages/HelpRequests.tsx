import { useEffect, useState } from 'react'
import { api, ApiError, type HelpAction, type HelpCandidate, type HelpItem } from '../api'
import { allowedActions, HELP_STATUS } from '../help'

const ACTION_LABEL: Record<string, string> = {
  created: '创建', assign: '派单', unassign: '取消派单', start: '开始处理', progress: '进展', resolve: '提交解决方案',
  reopen: '重新打开', close: '确认关闭', cancel: '撤销',
}
type Scope = 'mine' | 'assigned' | 'queue'

function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 409) return '求助单状态已变化，请刷新后再操作。'
    if (e.status === 403 || e.status === 404) return '你无权进行这个操作。'
    if (e.status === 422) return '请填写必要的内容后再提交。'
  }
  return '操作失败，请重试。'
}

export default function HelpRequests({ mode, isAdmin }: { mode: 'mine' | 'workbench'; isAdmin: boolean }) {
  const scopes: Scope[] = mode === 'mine' ? ['mine'] : isAdmin ? ['assigned', 'queue'] : ['assigned']
  const [scope, setScope] = useState<Scope>(scopes[0])
  const [items, setItems] = useState<HelpItem[]>([])
  const [selected, setSelected] = useState<HelpItem | null>(null)
  const [candidates, setCandidates] = useState<HelpCandidate[]>([])
  const [assignee, setAssignee] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    api.helpRequests(scope, controller.signal).then(page => {
      if (!controller.signal.aborted) { setItems(page.items); setError('') }
    }).catch(() => {
      if (!controller.signal.aborted) setError('求助单加载失败，请重试。')
    }).finally(() => { if (!controller.signal.aborted) setBusy(false) })
    return () => controller.abort()
  }, [scope, reload])

  const open = async (id: string) => {
    setError(''); setText(''); setCandidates([]); setAssignee('')
    try {
      const detail = await api.helpRequest(id)
      setSelected(detail)
      if (detail.my_roles.includes('admin') && detail.status === 'OPEN') {
        const rec = await api.helpCandidates(id)
        setCandidates(rec.candidates)
        setAssignee(rec.candidates[0]?.user_id ?? '')
      }
    } catch (e) {
      setError(errorText(e))
    }
  }

  const act = async (action: string) => {
    if (!selected) return
    const body: HelpAction = { action }
    if (action === 'resolve') body.resolution = text
    else if (action === 'assign') body.assignee_id = assignee
    else body.note = text
    setBusy(true); setError('')
    try {
      setSelected(await api.helpAction(selected.id, body))
      setText('')
      setReload(value => value + 1)
    } catch (e) {
      setError(errorText(e))
      setBusy(false)
    }
  }

  const actions = selected ? allowedActions(selected) : []
  const needsText = actions.some(a => ['progress', 'resolve', 'reopen'].includes(a))
  return (
    <main className="help">
      <section>
        <div className="escalation-heading">
          <h2>{mode === 'mine' ? '我的求助单' : '专家工作台'}</h2>
          <button disabled={busy} onClick={() => { setBusy(true); setReload(value => value + 1) }}>刷新</button>
        </div>
        {scopes.length > 1 && <div role="tablist">
          {scopes.map(s => <button key={s} role="tab" aria-selected={scope === s}
            onClick={() => { if (s !== scope) { setBusy(true); setScope(s); setSelected(null) } }}>{s === 'queue' ? '待派单队列' : '派给我的'}</button>)}
        </div>}
        {busy && <p role="status">加载中…</p>}
        {!busy && items.length === 0 && <p>{mode === 'mine' ? '暂无求助单。可以在问答中发起求助。' : '暂无需要处理的求助单。'}</p>}
        <ul className="help-list">
          {items.map(item => <li key={item.id}>
            <button className={selected?.id === item.id ? 'on' : ''} onClick={() => void open(item.id)}>
              <strong>{HELP_STATUS[item.status] ?? item.status}</strong> · {item.machine_id ?? '—'}
              <br />{item.symptom || item.summary.split('\n')[0]}
            </button>
          </li>)}
        </ul>
      </section>
      <section className="help-detail" aria-live="polite">
        {error && <p role="alert" className="err">{error}</p>}
        {!selected ? <p>选择左侧的一条求助单查看详情。</p> : <>
          <h3>{HELP_STATUS[selected.status] ?? selected.status}</h3>
          <dl>
            <dt>机台</dt><dd>{selected.machine_id ?? '—'}（{selected.machine_model ?? '机型未知'}）</dd>
            <dt>告警</dt><dd>{selected.alarm_codes.join('、') || '—'}</dd>
            <dt>现象</dt><dd>{selected.symptom || selected.summary}</dd>
            <dt>已尝试</dt><dd>{selected.tried_steps || '—'}</dd>
            <dt>提问人</dt><dd>{selected.requester ?? '—'}</dd>
            <dt>负责人</dt><dd>{selected.assignee ?? '待派单'}</dd>
            {selected.resolution && <><dt>解决方案</dt><dd>{selected.resolution}</dd></>}
          </dl>
          {actions.length > 0 && <div className="help-actions">
            {needsText && <textarea aria-label="说明" value={text} onChange={e => setText(e.target.value)}
              placeholder="填写进展、解决方案或重新打开的原因" />}
            {actions.includes('assign') && (candidates.length > 0
              ? <select aria-label="派给" value={assignee} onChange={e => setAssignee(e.target.value)}>
                  {candidates.map(c => <option key={c.user_id} value={c.user_id}>{c.engineer}：{c.reason}</option>)}
                </select>
              : <span>没有可推荐的工程师</span>)}
            {actions.map(a => <button key={a} disabled={busy || (a === 'assign' && !assignee)
              || (['progress', 'resolve'].includes(a) && !text.trim())} onClick={() => void act(a)}>{ACTION_LABEL[a]}</button>)}
          </div>}
          <h4>处理记录</h4>
          <ol className="help-events">
            {(selected.events ?? []).map((e, i) => <li key={i}>
              <time dateTime={e.at}>{new Date(e.at).toLocaleString()}</time> {e.actor} · {ACTION_LABEL[e.action] ?? e.action}
              {e.note && `：${e.note}`}
            </li>)}
          </ol>
        </>}
      </section>
    </main>
  )
}
