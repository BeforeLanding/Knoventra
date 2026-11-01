import { useEffect, useState } from 'react'
import { api, getToken, setToken, type Me } from './api'
import Admin from './pages/Admin'
import Chat from './pages/Chat'
import Login from './pages/Login'

function useHash() {
  const [h, setH] = useState(location.hash || '#/chat')
  useEffect(() => {
    const f = () => setH(location.hash || '#/chat')
    addEventListener('hashchange', f)
    return () => removeEventListener('hashchange', f)
  }, [])
  return h
}

export default function App() {
  const hash = useHash()
  const [me, setMe] = useState<Me | null>(null)
  const authed = !!getToken()

  useEffect(() => {
    if (!authed) { setMe(null); return }
    api.me().then(setMe).catch(() => setMe(null))
  }, [authed, hash])

  if (hash.startsWith('#/login') || !authed) return <Login />
  if (!me) return <div className="center">加载中…</div>
  return (
    <div className="app">
      <header>
        <b>Knoventra · 知域</b>
        <nav>
          <a href="#/chat" className={hash.startsWith('#/admin') ? '' : 'on'}>问答</a>
          {me.role === 'admin' && <a href="#/admin" className={hash.startsWith('#/admin') ? 'on' : ''}>资料管理</a>}
        </nav>
        <span className="who" data-testid="identity">
          {me.email} · 区域 {me.region ?? '—'} · 部门 {me.department}
          <button onClick={() => { setToken(null); location.hash = '#/login' }}>切换账号</button>
        </span>
      </header>
      {hash.startsWith('#/admin') && me.role === 'admin' ? <Admin /> : <Chat key={me.id} />}
    </div>
  )
}
