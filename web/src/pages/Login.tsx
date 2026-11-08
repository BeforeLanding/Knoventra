import { useEffect, useRef, useState } from 'react'
import { api, getToken } from '../api'
import { openChat } from '../navigation'

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
  const [pending, setPending] = useState<string | null>(null)
  const active = useRef<AbortController | null>(null)
  useEffect(() => () => { active.current?.abort() }, [])
  const go = async (email: string) => {
    if (active.current) return
    const controller = new AbortController()
    active.current = controller
    setPending(email)
    setErr('')
    let navigated = false
    try {
      const authenticated = await api.login(email, 'demo-pass-1', controller.signal)
      // Committing credentials can itself unmount Login on a protected URL.
      // Only the still-current committed result may complete its navigation.
      if (getToken() === authenticated) { openChat(); navigated = true }
      else if (!controller.signal.aborted) setErr('账号状态已变化，请核对当前账号后重新选择。')
    } catch (error: any) {
      if (!controller.signal.aborted) setErr(error?.message === 'LOGIN_SESSION_CHANGED'
        ? '账号状态已变化，请核对当前账号后重新选择。' : '登录失败，请重新选择账号重试。')
    } finally {
      if (!controller.signal.aborted && !navigated) setPending(null)
      if (!navigated && active.current === controller) active.current = null
    }
  }
  return (
    <div className="center login">
      <h2>Knoventra · 知域</h2>
      <p>选择演示账号（虚构数据）</p>
      {DEMO.map(([e, label]) => <button disabled={pending !== null} key={e} onClick={() => go(e)}>{label}<small>{e}</small></button>)}
      {pending && <p role="status">正在登录 {pending}…</p>}
      {err && <p role="alert" className="err">{err}</p>}
    </div>
  )
}
