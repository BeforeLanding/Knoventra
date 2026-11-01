import { useEffect, useState } from 'react'
import { api } from '../api'

type Doc = { kb: string; title: string; lang: string; revisions: { id: string; version: number; status: string }[] }

export default function Admin() {
  const [data, setData] = useState<{ kbs: string[]; documents: Doc[] } | null>(null)
  const [msg, setMsg] = useState('')
  const load = () => api.adminDocs().then(setData)
  useEffect(() => { load() }, [])

  const upload = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const form = e.currentTarget
    try {
      const r = await api.upload(new FormData(form))
      setMsg(`已上传为草稿：修订 ${r.version}（尚未生效，点击发布后才可被检索）`)
      form.reset()
      load()
    } catch (x: any) { setMsg('上传失败：' + (x.message ?? '')) }
  }
  const publish = async (id: string) => {
    try { await api.publish(id); setMsg('已发布，旧版本已退役'); load() } catch { setMsg('发布失败，旧版本仍然有效') }
  }

  return (
    <div className="admin">
      <h3>上传资料</h3>
      <form onSubmit={upload}>
        <select name="kb">{data?.kbs.map(k => <option key={k}>{k}</option>)}</select>
        <input name="title" placeholder="文档标题（与已有标题相同则作为新修订）" required />
        <select name="lang"><option>zh-Hans</option><option>zh-Hant</option><option>en</option></select>
        <input name="file" type="file" accept=".md,.pdf" required />
        <button>上传</button>
      </form>
      {msg && <p className="hint">{msg}</p>}
      <h3>文档与修订</h3>
      <table>
        <thead><tr><th>知识库</th><th>标题</th><th>语言</th><th>修订</th></tr></thead>
        <tbody>
          {data?.documents.map(d => (
            <tr key={d.kb + d.title}>
              <td>{d.kb}</td><td>{d.title}</td><td>{d.lang}</td>
              <td>{d.revisions.map(r => (
                <span key={r.id} className={`rev ${r.status}`}>v{r.version} {r.status}
                  {r.status === 'DRAFT' && <button onClick={() => publish(r.id)}>发布</button>}</span>
              ))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
