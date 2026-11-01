import { useState } from 'react'
import { api } from '../api'

// Fictional demo accounts; the shared password is a demo value from data/demo/manifest.json.
const DEMO = [
  ['sg-ops@example.test', 'SG · Ops（场景 A）'],
  ['tw-ops@example.test', 'TW · Ops（繁体提问）'],
  ['cn-ops@example.test', 'CN · Ops（区域限定）'],
  ['hr@example.test', 'SG · HR'],
  ['fin@example.test', 'CN · FIN'],
  ['admin@example.test', 'IT · 管理员'],
]

export default function Login() {
  const [err, setErr] = useState('')
  const go = async (email: string) => {
    try {
      await api.login(email, 'demo-pass-1')
      location.hash = '#/chat'
      location.reload()
    } catch { setErr('登录失败') }
  }
  return (
    <div className="center login">
      <h2>Knoventra · 知域</h2>
      <p>选择演示账号（虚构数据）</p>
      {DEMO.map(([e, label]) => <button key={e} onClick={() => go(e)}>{label}<small>{e}</small></button>)}
      {err && <p className="err">{err}</p>}
    </div>
  )
}
