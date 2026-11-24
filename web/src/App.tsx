import { useEffect, useState, useSyncExternalStore } from 'react'
import { api, getToken, setToken, subscribeToken, type Me } from './api'
import Admin from './pages/Admin'
import Chat from './pages/Chat'
import Login from './pages/Login'
import Escalations from './pages/Escalations'
import HelpRequests from './pages/HelpRequests'
import Memory from './pages/Memory'

function useHash() {
  const [h, setH] = useState({ hash: location.hash || '#/chat', revision: 0 })
  useEffect(() => {
    const f = () => setH(previous => ({ hash: location.hash || '#/chat', revision: previous.revision + 1 }))
    addEventListener('hashchange', f)
    return () => removeEventListener('hashchange', f)
  }, [])
  return h
}

function AuthenticatedApp({ hash }: { hash: string }) {
  const [attempt, setAttempt] = useState(0)
  const [identity, setIdentity] = useState<{ attempt: number; me: Me | null } | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    api.me(controller.signal).then(me => {
      if (!controller.signal.aborted) setIdentity({ attempt, me })
    }).catch(() => {
      if (!controller.signal.aborted) setIdentity({ attempt, me: null })
    })
    return () => controller.abort()
  }, [attempt])

  const logout = () => { setToken(null); location.hash = '#/login' }
  if (!identity || identity.attempt !== attempt) {
    return <div role="status" className="center">正在核对身份…</div>
  }
  if (!identity.me) return <div className="center">
    <p role="alert">身份加载失败，请重试或切换账号。</p>
    <button onClick={() => setAttempt(value => value + 1)}>重试身份加载</button>
    <button onClick={logout}>切换账号</button>
  </div>
  const me = identity.me
  return (
    <div className="app">
      <header>
        <b>Knoventra · 知域</b>
        <nav>
          <a href="#/chat" className={hash.startsWith('#/chat') ? 'on' : ''}>问答</a>
          <a href="#/escalations" className={hash.startsWith('#/escalations') ? 'on' : ''}>转交记录</a>
          <a href="#/help" className={hash.startsWith('#/help') ? 'on' : ''}>我的求助单</a>
          <a href="#/memory" className={hash.startsWith('#/memory') ? 'on' : ''}>我的记忆</a>
          {(me.engineer_name || me.role === 'admin') &&
            <a href="#/workbench" className={hash.startsWith('#/workbench') ? 'on' : ''}>专家工作台</a>}
          {me.role === 'admin' && <a href="#/admin" className={hash.startsWith('#/admin') ? 'on' : ''}>资料管理</a>}
        </nav>
        <span className="who" data-testid="identity">
          {me.email} · 区域 {me.region ?? '—'} · 部门 {me.department}
          <button onClick={logout}>切换账号</button>
        </span>
      </header>
      {hash.startsWith('#/admin') && me.role === 'admin' ? <Admin />
        : hash.startsWith('#/escalations') ? <Escalations key={me.id} />
        : hash.startsWith('#/memory') ? <Memory key={`${me.id}-memory`} />
        : hash.startsWith('#/help') ? <HelpRequests key={`${me.id}-mine`} mode="mine" isAdmin={me.role === 'admin'} />
        : hash.startsWith('#/workbench') && (me.engineer_name || me.role === 'admin')
          ? <HelpRequests key={`${me.id}-bench`} mode="workbench" isAdmin={me.role === 'admin'} />
        : <Chat key={me.id} maxMessageChars={me.chat_max_chars} />}
    </div>
  )
}

export default function App() {
  const route = useHash()
  const token = useSyncExternalStore(subscribeToken, getToken)
  if (route.hash.startsWith('#/login') || !token) return <Login />
  // A fresh keyed boundary prevents identity reuse when navigating A -> B -> A
  // or changing credentials while an earlier identity request is still pending.
  return <AuthenticatedApp key={JSON.stringify([token, route.revision])} hash={route.hash} />
}
