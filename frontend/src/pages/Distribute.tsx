import { useCallback, useEffect, useState } from 'react'
import clsx from 'clsx'
import api from '../lib/api'
import { Banner, Empty, Field, PageHeader, Section, Spinner } from '../components/ui'

/**
 * 成片分发：把已交付项目的成片按渠道规则复制到本地数字资产目录，并按规范命名。
 * 只复制、不改动项目；出片自检没通过的版本不分发；缺的版本可一键补出（本地合成，不花钱）。
 */
export default function DistributePage() {
  const [projects, setProjects] = useState<any[]>([])
  const [pid, setPid] = useState('')
  const [plan, setPlan] = useState<any>(null)
  const [cfg, setCfg] = useState<any>(null)
  const [picked, setPicked] = useState<string[]>([])
  const [root, setRoot] = useState('')
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState('')
  const [waiting, setWaiting] = useState(false)

  const loadBase = useCallback(async () => {
    try {
      const [p, c] = await Promise.all([api.distProjects(), api.distConfig()])
      setProjects(p)
      setCfg(c)
      setRoot(c.root)
      if (!pid && p.length) setPid(p[0].id)
    } catch (e: any) {
      setErr(e?.message || String(e))
    }
  }, [pid])

  const loadPlan = useCallback(async (id: string) => {
    if (!id) return setPlan(null)
    try {
      const d = await api.distPlan(id)
      setPlan(d)
      return d
    } catch (e: any) {
      setErr(e?.message || String(e))
    }
  }, [])

  useEffect(() => {
    loadBase()
  }, [loadBase])
  useEffect(() => {
    loadPlan(pid).then((d) => d && setPicked(d.channels.filter((c: any) => c.files.length).map((c: any) => c.key)))
  }, [pid, loadPlan])

  // 补出缺少的版本后轮询，直到不再缺
  useEffect(() => {
    if (!waiting || !pid) return
    let alive = true
    let t: number | undefined
    const tick = async () => {
      const d = await loadPlan(pid)
      if (!alive) return
      if (d && d.missing_variants.length === 0) {
        setWaiting(false)
        setMsg('缺少的版本已补出，可以分发了')
        setPicked(d.channels.filter((c: any) => c.files.length).map((c: any) => c.key))
      } else t = window.setTimeout(tick, 5000)
    }
    t = window.setTimeout(tick, 5000)
    return () => {
      alive = false
      if (t) clearTimeout(t)
    }
  }, [waiting, pid, loadPlan])

  const act = async (key: string, fn: () => Promise<any>) => {
    setBusy(key)
    setErr('')
    setMsg('')
    try {
      await fn()
    } catch (e: any) {
      setErr(e?.message || String(e))
    } finally {
      setBusy('')
    }
  }

  if (!cfg) return <div className="p-6">{err ? <Banner tone="error">{err}</Banner> : <Spinner label="加载分发设置" />}</div>

  return (
    <>
      <PageHeader title="成片分发" subtitle="把已交付项目的成片按渠道规则复制到本地数字资产目录，并按「目的地_渠道_版本_时长_日期」命名" />
      <div className="p-6 flex flex-col gap-4">
        {err && <Banner tone="error" onClose={() => setErr('')}>{err}</Banner>}
        {msg && <Banner tone="ok" onClose={() => setMsg('')}>{msg}</Banner>}

        <Section title="分发根目录" bodyClass="p-4">
          <div className="flex items-end gap-2">
            <div className="flex-1">
              <Field label="本地数字资产目录" hint="各渠道的文件夹建在它下面；渠道文件夹不存在会自动创建，但根目录需要已经存在">
                <input className="input" value={root} onChange={(e) => setRoot(e.target.value)} />
              </Field>
            </div>
            <button
              className="btn"
              disabled={!!busy || root === cfg.root}
              onClick={() =>
                act('cfg', async () => {
                  const c = await api.distSaveConfig({ root })
                  setCfg(c)
                  setMsg('已保存分发根目录')
                  loadPlan(pid)
                })
              }
            >
              保存
            </button>
          </div>
          {!cfg.root_exists && <div className="mt-2 text-[12px] text-human">这个目录现在不存在：请先在电脑上创建它，或改成正确的路径。</div>}
          <div className="mt-3 flex flex-wrap gap-2">
            {cfg.channels.map((c: any) => (
              <span key={c.key} className="tag" title={c.note}>
                {c.label} → {c.folder}\：{c.variants.map((v: string) => cfg.variant_names[v] || v).join('、')}
              </span>
            ))}
          </div>
        </Section>

        <Section
          title="选择项目"
          right={
            <select className="input h-8 py-0 text-[12px] w-[320px]" value={pid} onChange={(e) => setPid(e.target.value)}>
              {projects.length === 0 && <option value="">还没有已交付的项目</option>}
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          }
          bodyClass="p-4"
        >
          {!plan ? (
            <Empty text="只有已交付的项目可以分发（成片要先通过出片自检）" />
          ) : (
            <div className="flex flex-col gap-3">
              {plan.missing_variants.length > 0 && (
                <Banner tone="warn">
                  <div className="flex items-center gap-3">
                    <span className="flex-1">
                      这支片还缺 {plan.missing_variants.length} 个成片版本（
                      {plan.missing_variants.map((v: string) => cfg.variant_names[v] || v).join('、')}
                      ），相关渠道会少文件。补出是本地合成，不花钱。
                    </span>
                    <button
                      className="btn btn-sm shrink-0"
                      disabled={!!busy || waiting}
                      onClick={() =>
                        act('render', async () => {
                          await api.render(pid, { variants: plan.missing_variants })
                          setWaiting(true)
                          setMsg('已开始补出缺少的版本，完成后本页自动更新（每个版本约 1–2 分钟）')
                        })
                      }
                    >
                      {waiting ? '补出中…' : '补出缺少的版本'}
                    </button>
                  </div>
                </Banner>
              )}

              <div className="grid grid-cols-2 gap-3">
                {plan.channels.map((c: any) => {
                  const usable = c.files.filter((f: any) => f.qc_ok)
                  return (
                    <label
                      key={c.key}
                      className={clsx(
                        'border rounded-card p-3 flex flex-col gap-2',
                        usable.length === 0 ? 'opacity-60 border-line' : picked.includes(c.key) ? 'border-brand bg-brand-soft/30 cursor-pointer' : 'border-line cursor-pointer',
                      )}
                    >
                      <div className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          className="accent-[#0F5E57]"
                          disabled={usable.length === 0}
                          checked={picked.includes(c.key)}
                          onChange={(e) => setPicked(e.target.checked ? [...picked, c.key] : picked.filter((k) => k !== c.key))}
                        />
                        <span className="text-[13px] font-medium">{c.label}</span>
                        <span className="text-[11px] text-ink-faint">{usable.length} 个文件</span>
                        {c.last && <span className="tag-brand ml-auto" title={c.last.folder}>已分发过 {c.last.count} 个</span>}
                      </div>
                      <div className="text-[11px] text-ink-faint break-all">{c.folder}\</div>
                      {c.files.map((f: any) => (
                        <div key={f.variant} className={clsx('text-[12px] flex gap-2', !f.qc_ok && 'line-through text-ink-faint')}>
                          <span className="flex-1 break-all">{f.name}</span>
                          <span className="text-ink-faint shrink-0">{f.size_mb} MB</span>
                        </div>
                      ))}
                      {c.blocked.length > 0 && <div className="text-[11px] text-human">出片自检未通过、不分发：{c.blocked.join('、')}</div>}
                      {c.missing.length > 0 && (
                        <div className="text-[11px] text-human">缺：{c.missing.map((m: any) => m.variant_name).join('、')}</div>
                      )}
                      {c.note && <div className="text-[10px] text-ink-faint">{c.note}</div>}
                    </label>
                  )
                })}
              </div>

              <div className="flex items-center gap-3">
                <span className="text-[11px] text-ink-faint flex-1">
                  每个文件夹会附带「说明.md」（歌词、所用模型、费用、是否含 AI 画面）和「分发清单.json」。同名文件会被覆盖。
                </span>
                <button
                  className="btn-primary"
                  disabled={!!busy || picked.length === 0 || !plan.root_exists}
                  title={!plan.root_exists ? '分发根目录不存在' : ''}
                  onClick={() =>
                    act('run', async () => {
                      const r = await api.distRun(pid, picked)
                      setMsg(`已分发 ${r.total_files} 个文件到 ${r.channels.filter((x: any) => x.copied).length} 个渠道：` + r.channels.filter((x: any) => x.copied).map((x: any) => `${x.label} ${x.copied} 个`).join('，'))
                      loadPlan(pid)
                    })
                  }
                >
                  {busy === 'run' ? '分发中…' : `分发到所选 ${picked.length} 个渠道`}
                </button>
              </div>
            </div>
          )}
        </Section>
      </div>
    </>
  )
}
