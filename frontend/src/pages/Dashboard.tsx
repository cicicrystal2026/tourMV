import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../lib/api'
import ProjectReview from '../components/ProjectReview'
import { fmtDate, relative, JOB_STATUS_LABEL, DEMO_MARK } from '../lib/format'
import {
  Banner,
  Empty,
  Field,
  Modal,
  PageHeader,
  ProgressBar,
  Section,
  Spinner,
  StateDot,
  ScoreDots,
} from '../components/ui'
import clsx from 'clsx'

type Col = { name: string; count: number; items: any[] }

export default function Dashboard() {
  const nav = useNavigate()
  const [data, setData] = useState<any>(null)
  const [err, setErr] = useState('')
  const [showNew, setShowNew] = useState(false)
  // F1-9 本周热歌抽屉；从抽屉「用此卡新建项目」时预选该卡
  const [showTrends, setShowTrends] = useState(false)
  const [presetCard, setPresetCard] = useState('')
  // F1-11 项目复盘
  const [reviewId, setReviewId] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [showModels, setShowModels] = useState(false)
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      setData(await api.dashboard())
      setErr('')
    } catch (e: any) {
      setErr(e.message)
    }
  }, [])

  // setTimeout 链：上一轮没回来不发下一轮（V1.0 用 setInterval，后端慢时请求层层堆积）
  useEffect(() => {
    let alive = true
    let t: number | undefined
    const tick = async () => {
      await load()
      if (alive) t = window.setTimeout(tick, 6000)
    }
    tick()
    return () => {
      alive = false
      if (t) clearTimeout(t)
    }
  }, [load])

  if (err && !data) return <div className="p-6"><Banner tone="error">{err}</Banner></div>
  if (!data)
    return (
      <div className="p-6">
        <Spinner label="加载看板" />
      </div>
    )

  const cards = data.cards
  // 「一屏全是完成」对决策没有帮助：进行中 / 排队 / 失败排前面，24 小时前完成的不显示
  const DAY = 24 * 3600 * 1000
  const rank: Record<string, number> = { running: 0, queued: 1, failed: 2, done: 3 }
  const queue = [...data.gpu_queue]
    .filter((j: any) => j.status !== 'done' || Date.now() - new Date(j.finished_at || j.created_at).getTime() < DAY)
    .sort((a: any, b: any) => (rank[a.status] ?? 9) - (rank[b.status] ?? 9))
  const selModels = data.models.filter((m: any) => m.in_selection)

  return (
    <>
      <PageHeader
        title="生产看板"
        subtitle={
          <>
            {cards.running_projects.total} 个项目在跑 · {cards.pending_human.total} 项等人处理 · 素材{' '}
            {cards.assets.total} 条
          </>
        }
        actions={
          <>
            <button className="btn" onClick={() => setShowImport(true)}>
              导入创作简报
            </button>
            <button className="btn" onClick={() => setShowTrends(true)}>
              本周热歌
            </button>
            <button
              className="btn-primary"
              onClick={() => {
                setPresetCard('')
                setShowNew(true)
              }}
            >
              新建样片项目
            </button>
          </>
        }
      />

      <div className="p-6 flex flex-col gap-5 min-h-0">
        <div className="grid grid-cols-4 gap-4">
          <StatCard
            title="进行中项目"
            main={cards.running_projects.total}
            parts={[
              ['样片', cards.running_projects.sample],
              ['演示分项', cards.running_projects.demo],
            ]}
            onClick={() => nav('/lyrics')}
          />
          <StatCard
            title="待人工处理"
            main={cards.pending_human.total}
            parts={Object.entries(cards.pending_human.by_type) as [string, number][]}
            tone="human"
            onClick={() => nav('/dashboard')}
          />
          <StatCard
            title="本周产出成片"
            main={cards.week_renders.total}
            parts={[
              ['横版', cards.week_renders.landscape],
              ['竖版', cards.week_renders.portrait],
            ]}
          />
          <StatCard
            title="素材库镜头"
            main={cards.assets.total}
            parts={[
              ['已授权', cards.assets.licensed],
              ['即将到期', cards.assets.expiring],
              ['缺授权', cards.assets.missing],
            ]}
            tone={cards.assets.expiring ? 'human' : undefined}
            onClick={() => nav('/assets')}
          />
        </div>

        <div className="grid grid-cols-[1fr_320px] gap-5 items-start">
          <Section
            title={`流水线（${cards.pending_human.total ? '橙色为等待人工' : '无阻塞'}）`}
            right={<span className="h-sub">按更新时间倒序 · 每列超过 5 张滚动</span>}
            bodyClass="p-3"
          >
            <div className="grid grid-cols-5 gap-3">
              {(data.columns as Col[]).map((c) => (
                <div key={c.name} className="flex flex-col min-h-[220px]">
                  <div className="flex items-center justify-between px-1 pb-2">
                    <span className="text-[12px] font-medium text-ink">{c.name}</span>
                    <span className="text-[11px] text-ink-faint">{c.count}</span>
                  </div>
                  <div className="flex flex-col gap-2 overflow-y-auto max-h-[420px] pr-0.5">
                    {c.items.length === 0 && (
                      <div className="text-[11px] text-ink-faint px-1 py-3 text-center border border-dashed border-line rounded-btn">
                        空
                      </div>
                    )}
                    {c.items.map((p) => (
                      <button
                        key={p.id}
                        onClick={() =>
                          nav(
                            ['创作简报', '词曲生成'].includes(p.column ?? c.name)
                              ? `/projects/${p.id}/lyrics`
                              : `/projects/${p.id}/edit`,
                          )
                        }
                        className={clsx(
                          'text-left bg-white rounded-btn border p-2.5 hover:shadow-sm transition-shadow',
                          p.waiting_human ? 'border-human/60 ring-1 ring-human/20' : 'border-line',
                        )}
                      >
                        <div className="flex items-center gap-1.5">
                          <StateDot status={p.waiting_human ? 'failed' : p.state_label === '已交付' ? 'done' : 'running'} />
                          <span className="text-[12px] font-medium truncate">{p.name}</span>
                        </div>
                        <div className="mt-1.5 flex flex-wrap gap-1">
                          <span className="tag">{p.type === 'demo' ? '演示' : '样片'}</span>
                          <span className="tag">{p.language === 'zh' ? '中文' : p.language}</span>
                        </div>
                        <div className="mt-1.5 text-[11px] text-ink-faint truncate">{p.destination}</div>
                        <div className="mt-1 text-[11px] text-ink-soft">{p.state_label}</div>
                        {p.status === 'delivered' && (
                          <span
                            role="button"
                            className="mt-1 inline-block text-[11px] text-brand underline"
                            onClick={(e) => {
                              e.stopPropagation()
                              setReviewId(p.id)
                            }}
                          >
                            项目复盘
                          </span>
                        )}
                        {p.status_note && (
                          <div className="mt-0.5 text-[10px] text-ink-faint truncate">{p.status_note}</div>
                        )}
                        <div className="mt-1 text-[10px] text-ink-faint">{relative(p.updated_at)}</div>
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </Section>

          <div className="flex flex-col gap-5">
            <Section title={`我的待办 ${data.todos.length ? `(${data.todos.length})` : ''}`} bodyClass="p-3">
              {data.todos.length === 0 ? (
                <Empty text="没有等待人工的任务" />
              ) : (
                <div className="flex flex-col gap-2 max-h-[280px] overflow-y-auto">
                  {data.todos.map((t: any) => (
                    <div key={t.project_id} className="border border-human/35 bg-human-soft/45 rounded-btn p-2.5">
                      <div className="flex items-center gap-2">
                        <span className="tag-human">{t.type}</span>
                        <span className="text-[12px] font-medium truncate">{t.title}</span>
                      </div>
                      <div className="mt-1 text-[11px] text-ink-soft truncate">
                        {t.state_label} · {t.hint}
                      </div>
                      <div className="mt-1 flex items-center justify-between">
                        <span className="text-[10px] text-ink-faint">{relative(t.updated_at)}</span>
                        <button
                          className="btn-sm btn-primary"
                          onClick={() =>
                            nav(
                              t.type === '精剪'
                                ? `/projects/${t.project_id}/edit`
                                : t.type === '配图'
                                  ? `/projects/${t.project_id}/storyboard`
                                  : `/projects/${t.project_id}/lyrics`,
                            )
                          }
                        >
                          去处理
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Section>

            <Section
              title="任务队列"
              right={<span className="h-sub">进行中与失败优先 · 隐藏 24 小时前已完成</span>}
              bodyClass="p-3"
            >
              <div className="mb-2 flex items-center gap-2 text-[11px] text-ink-soft">
                <span className={clsx('w-1.5 h-1.5 rounded-full', data.device.online ? 'bg-brand' : 'bg-human')} />
                {data.device.name} · {data.device.online ? '在线' : '离线'} · api {data.device.concurrency.api}
                {' / gpu '}
                {data.device.concurrency.gpu}
              </div>
              {queue.length === 0 ? (
                <Empty text="暂无进行中的任务" />
              ) : (
                <div className="flex flex-col gap-2 max-h-[300px] overflow-y-auto">
                  {queue.slice(0, 12).map((j: any) => (
                    <div key={j.id} className="border border-line rounded-btn p-2.5">
                      <div className="flex items-center gap-1.5">
                        <StateDot status={j.status} />
                        <span className="text-[12px] truncate flex-1">{j.name}</span>
                        <span className="text-[10px] text-ink-faint shrink-0">
                          {JOB_STATUS_LABEL[j.status] || j.status}
                        </span>
                      </div>
                      {j.status === 'running' && (
                        <div className="mt-1.5">
                          <ProgressBar value={j.progress} />
                        </div>
                      )}
                      <div className="mt-1 flex items-center gap-2 text-[10px] text-ink-faint">
                        <span className="tag">{j.queue}</span>
                        {j.vram_mb > 0 && <span>{j.vram_mb} MB</span>}
                        {j.retries > 0 && <span>重试 {j.retries}</span>}
                        {j.status === 'failed' && (
                          <button
                            className="ml-auto text-brand underline"
                            onClick={async () => {
                              setBusy(j.id)
                              await api.retryJob(j.id)
                              setBusy('')
                              load()
                            }}
                            disabled={busy === j.id}
                          >
                            重试
                          </button>
                        )}
                      </div>
                      {j.error && <div className="mt-1 text-[10px] text-human-dark truncate">{j.error}</div>}
                    </div>
                  ))}
                </div>
              )}
            </Section>

            <Section
              title="选型战绩"
              right={
                <span className="flex gap-2">
                  <a className="text-[11px] text-brand underline" href={api.selectionReportUrl('md')} download>
                    导出选型结论
                  </a>
                  <button className="text-[11px] text-brand underline" onClick={() => setShowModels(true)}>
                    详情
                  </button>
                </span>
              }
              bodyClass="p-3"
            >
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-ink-faint">
                    <th className="text-left font-normal pb-1">模型</th>
                    <th className="text-right font-normal pb-1">胜出</th>
                    <th className="text-right font-normal pb-1">轮次</th>
                    <th className="text-right font-normal pb-1" title="1–5 分">均分</th>
                  </tr>
                </thead>
                <tbody>
                  {selModels.map((m: any) => (
                    <tr key={m.key} className="border-t border-line">
                      <td className="py-1.5 truncate max-w-[130px]">
                        <span className="flex items-center gap-1.5">
                          <StateDot status={m.ready ? 'done' : 'queued'} />
                          <span className="truncate">{m.name}</span>
                        </span>
                      </td>
                      <td className="text-right font-medium text-brand">{m.wins}</td>
                      <td className="text-right text-ink-soft">{m.rounds}</td>
                      <td className="text-right">{m.score_n ? m.avg_score.toFixed(2) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="mt-2 text-[10px] text-ink-faint">
                只统计已揭晓的选型轮（盲听）；生产模式不计入。
                {selModels.every((m: any) => m.rounds < 3) && ' 目前样本不足 3 轮，结论仅供参考。'}
              </div>
            </Section>
          </div>
        </div>
      </div>

      <ProjectReview projectId={reviewId} open={!!reviewId} onClose={() => setReviewId('')} />
      <TrendsDrawer
        open={showTrends}
        onClose={() => setShowTrends(false)}
        onUse={(id) => {
          setShowTrends(false)
          setPresetCard(id)
          setShowNew(true)
        }}
      />
      <NewProjectModal
        presetCard={presetCard}
        open={showNew}
        onClose={() => setShowNew(false)}
        onDone={(id) => {
          setShowNew(false)
          load()
          nav(`/projects/${id}/lyrics`)
        }}
      />
      <ImportBriefModal
        open={showImport}
        onClose={() => setShowImport(false)}
        onDone={(id) => {
          setShowImport(false)
          load()
          nav(`/projects/${id}/lyrics`)
        }}
      />
      <Modal open={showModels} title="音乐模型详情" onClose={() => setShowModels(false)} width="max-w-[720px]">
        <div className="flex flex-col gap-3">
          {data.models.map((m: any) => (
            <div key={m.key} className="border border-line rounded-btn p-3">
              <div className="flex items-center gap-2">
                <StateDot status={m.ready ? 'done' : 'queued'} />
                <span className="text-[13px] font-medium">{m.name}</span>
                <span className="tag">{m.run_mode === 'local' ? '本地' : 'API'}</span>
                <span className={m.ready ? 'tag-brand' : 'tag-human'}>
                  {m.ready ? '凭证就绪' : m.usable ? '未配置（允许占位）' : '未配置（不可用）'}
                </span>
                <span className="ml-auto text-[11px] text-ink-soft">
                  {m.in_selection ? `选型轮 胜出 ${m.wins} · 轮次 ${m.rounds} · 均分 ${m.score_n ? m.avg_score.toFixed(2) : '—'}` : '不参加选型轮'}
                </span>
              </div>
              <div className="mt-1.5 text-[11px] text-ink-soft">
                中文：{m.capabilities?.supports_chinese ? '支持' : '未确认'} · 最长{' '}
                {m.capabilities?.max_duration_s ?? '—'}s · 自定义歌词：
                {m.capabilities?.supports_custom_lyrics ? '支持' : '不支持'} · 并发{' '}
                {m.capabilities?.concurrency ?? '—'}
              </div>
              {m.note && <div className="mt-1 text-[11px] text-ink-faint">{m.note}</div>}
            </div>
          ))}
        </div>
      </Modal>
    </>
  )
}

function StatCard({
  title,
  main,
  parts,
  tone,
  onClick,
}: {
  title: string
  main: number
  parts: [string, number][]
  tone?: 'human'
  onClick?: () => void
}) {
  return (
    <button
      onClick={onClick}
      disabled={!onClick}
      className={clsx(
        'card p-4 text-left transition-shadow',
        onClick && 'hover:shadow-sm cursor-pointer',
      )}
    >
      <div className="text-[12px] text-ink-soft">{title}</div>
      <div className={clsx('mt-1.5 font-serif text-[28px] leading-none', tone === 'human' ? 'text-human' : 'text-ink')}>
        {main}
      </div>
      <div className="mt-2.5 flex flex-wrap gap-x-3 gap-y-1">
        {parts.map(([k, v]) => (
          <span key={k} className="text-[11px] text-ink-faint">
            {k} <span className="text-ink-soft font-medium">{v}</span>
          </span>
        ))}
      </div>
    </button>
  )
}

function NewProjectModal({
  open,
  onClose,
  onDone,
  presetCard = '',
}: {
  open: boolean
  onClose: () => void
  onDone: (id: string) => void
  presetCard?: string
}) {
  const [cards, setCards] = useState<any[]>([])
  const [picked, setPicked] = useState<string[]>([])
  useEffect(() => {
    if (open && presetCard) setPicked([presetCard])
  }, [open, presetCard])
  const [form, setForm] = useState<any>({
    name: '',
    destination: '',
    type: 'sample',
    language: 'zh',
    mood: '治愈、宁静',
    genre: '',
    bpm_min: 70,
    bpm_max: 100,
    duration_s: 180,
    vocal: 'female',
    style_prompt: '',
    forbidden: '',
  })
  const [err, setErr] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!open) return
    api.trendCards().then((r) => setCards(r.items || [])).catch(() => setCards([]))
  }, [open])

  // F1-3：引用素材卡后自动预填曲风 / 速度 / 风格提示词
  useEffect(() => {
    const first = cards.find((c) => c.id === picked[0])
    if (!first) return
    const el = first.elements || {}
    const bpm = String(el.bpm || '')
    const m = bpm.match(/(\d{2,3})\s*[-~–]\s*(\d{2,3})/)
    setForm((f: any) => ({
      ...f,
      genre: f.genre || el.genre || '',
      style_prompt: f.style_prompt || first.remix?.style_prompt || '',
      bpm_min: m ? Number(m[1]) : f.bpm_min,
      bpm_max: m ? Number(m[2]) : f.bpm_max,
      mood: f.mood || (el.mood || []).join('、'),
    }))
  }, [picked, cards])

  const submit = async () => {
    if (!form.name.trim()) return setErr('请填写项目名')
    setSaving(true)
    try {
      const p = await api.createProject({
        ...form,
        mood: form.mood.split(/[、,，\s]+/).filter(Boolean),
        trend_card_ids: picked,
      })
      onDone(p.id)
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      title="新建样片项目"
      onClose={onClose}
      width="max-w-[720px]"
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn-primary" onClick={submit} disabled={saving}>
            {saving ? '创建中…' : '创建并进入词曲工作台'}
          </button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {err && <Banner tone="error">{err}</Banner>}
        <div className="grid grid-cols-2 gap-3">
          <Field label="项目名">
            <input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="如 桂林漓江·晨雾" />
          </Field>
          <Field label="目的地">
            <input className="input" value={form.destination} onChange={(e) => setForm({ ...form, destination: e.target.value })} placeholder="如 桂林·漓江" />
          </Field>
          <Field label="类型">
            <select className="input" value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
              <option value="sample">样片</option>
              <option value="demo">演示分项</option>
            </select>
          </Field>
          <Field label="语种">
            <select className="input" value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })}>
              <option value="zh">中文演唱 + 英文字幕</option>
            </select>
          </Field>
          <Field label="情绪" hint="用「、」分隔">
            <input className="input" value={form.mood} onChange={(e) => setForm({ ...form, mood: e.target.value })} />
          </Field>
          <Field label="曲风">
            <input className="input" value={form.genre} onChange={(e) => setForm({ ...form, genre: e.target.value })} placeholder="如 ambient pop" />
          </Field>
          <Field label="速度区间（BPM）">
            <div className="flex items-center gap-2">
              <input type="number" className="input" value={form.bpm_min} onChange={(e) => setForm({ ...form, bpm_min: Number(e.target.value) })} />
              <span className="text-ink-faint">–</span>
              <input type="number" className="input" value={form.bpm_max} onChange={(e) => setForm({ ...form, bpm_max: Number(e.target.value) })} />
            </div>
          </Field>
          <Field label="目标时长（秒）">
            <input type="number" className="input" value={form.duration_s} onChange={(e) => setForm({ ...form, duration_s: Number(e.target.value) })} />
          </Field>
          <Field label="声线">
            <select className="input" value={form.vocal} onChange={(e) => setForm({ ...form, vocal: e.target.value })}>
              <option value="female">女声</option>
              <option value="male">男声</option>
              <option value="choir">合声</option>
            </select>
          </Field>
          <Field label="禁用内容">
            <input className="input" value={form.forbidden} onChange={(e) => setForm({ ...form, forbidden: e.target.value })} placeholder="如 不得出现具体年代" />
          </Field>
        </div>

        <Field label="风格提示词" hint="引用素材卡后自动预填；保存时系统会剔除原曲名与艺人名">
          <textarea
            className="input h-[68px] py-2 resize-none"
            value={form.style_prompt}
            onChange={(e) => setForm({ ...form, style_prompt: e.target.value })}
          />
        </Field>

        <div>
          <span className="label">引用二创素材卡（可多选）</span>
          <div className="flex flex-col gap-2 max-h-[180px] overflow-y-auto border border-line rounded-btn p-2">
            {cards.length === 0 && <Empty text="暂无素材卡，去热歌监控同步" />}
            {cards.map((c) => (
              <label
                key={c.id}
                className={clsx(
                  'flex items-start gap-2 p-2 rounded-btn border cursor-pointer',
                  picked.includes(c.id) ? 'border-brand bg-brand-soft/50' : 'border-line bg-white',
                )}
              >
                <input
                  type="checkbox"
                  className="mt-0.5 accent-[#0F5E57]"
                  checked={picked.includes(c.id)}
                  onChange={(e) =>
                    setPicked(e.target.checked ? [...picked, c.id] : picked.filter((x) => x !== c.id))
                  }
                />
                <span className="min-w-0">
                  <span className="block text-[12px] font-medium truncate">
                    {c.song} <span className="text-ink-faint font-normal">/ {c.artist}</span>
                  </span>
                  <span className="block text-[11px] text-ink-soft truncate">
                    {c.week} · {c.elements?.genre || '—'} · BPM {c.elements?.bpm || '—'}
                  </span>
                  <span className="block text-[11px] text-ink-faint line-clamp-2">
                    {c.remix?.borrowable?.join('；')}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </div>
      </div>
    </Modal>
  )
}

function ImportBriefModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean
  onClose: () => void
  onDone: (id: string) => void
}) {
  const [text, setText] = useState('')
  const [err, setErr] = useState('')
  const [preview, setPreview] = useState<any>(null)

  const parse = () => {
    try {
      const j = JSON.parse(text)
      const card = Array.isArray(j) ? j[0]?.analysis || j[0] : j.analysis || j
      const el = card?.elements || {}
      const remix = card?.remix_card || card?.remix || {}
      setPreview({
        name: `${card?.song || '未命名'} · 二创`,
        destination: card?.place_or_culture_elements || el.place_or_culture_elements || '',
        genre: el.genre || '',
        mood: el.mood || [],
        style_prompt: remix.style_prompt || '',
        duration_s: 180,
      })
      setErr('')
    } catch (e: any) {
      setErr(`JSON 解析失败：${e.message}`)
      setPreview(null)
    }
  }

  return (
    <Modal
      open={open}
      title="导入创作简报"
      onClose={onClose}
      width="max-w-[680px]"
      footer={
        <button className="btn" onClick={onClose}>
          取消
        </button>
      }
    >
      <div className="flex flex-col gap-3">
        <Banner tone="info">
          粘贴 monitor.py 输出的 <code className="font-mono">analysis_YYYY-MM-DD.json</code>（整个数组或单条
          analysis 对象均可）。字段映射：song→项目名、elements.genre→曲风、remix_card.style_prompt→风格提示词。缺失字段留空。
        </Banner>
        {err && <Banner tone="error">{err}</Banner>}
        <textarea
          className="input h-[160px] py-2 resize-none font-mono text-[12px]"
          placeholder='[{"rank":1,"item":{...},"analysis":{"song":"...","elements":{...},"remix_card":{...}}}]'
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <div className="flex items-center gap-2">
          <button className="btn" onClick={parse}>
            解析预览
          </button>
          {preview && <span className="text-[11px] text-brand">解析成功，可创建</span>}
        </div>
        {preview && (
          <div className="border border-line rounded-btn p-3 text-[12px] flex flex-col gap-1">
            <div>
              项目名：<span className="font-medium">{preview.name}</span>
            </div>
            <div>目的地：{preview.destination || <span className="text-ink-faint">（空）</span>}</div>
            <div>曲风：{preview.genre || <span className="text-ink-faint">（空）</span>}</div>
            <div>情绪：{preview.mood.join('、') || <span className="text-ink-faint">（空）</span>}</div>
            <div className="truncate">风格提示词：{preview.style_prompt || <span className="text-ink-faint">（空）</span>}</div>
            <button
              className="btn-primary btn-sm mt-1 self-start"
              onClick={async () => {
                const p = await api.createProject({
                  ...preview,
                  mood: preview.mood,
                  type: 'sample',
                  trend_card_ids: [],
                })
                onDone(p.id)
              }}
            >
              创建并进入
            </button>
          </div>
        )}
      </div>
    </Modal>
  )
}


/** F1-9 本周热歌：近 30 天热度排行与二创素材卡，可直接「用此卡新建项目」。 */
function TrendsDrawer({ open, onClose, onUse }: { open: boolean; onClose: () => void; onUse: (id: string) => void }) {
  const [data, setData] = useState<any>(null)
  const [week, setWeek] = useState('')
  useEffect(() => {
    if (!open) return
    api.trendCards(week || undefined).then(setData).catch(() => setData({ items: [], weeks: [] }))
  }, [open, week])
  const items: any[] = data?.items || []
  return (
    <Modal open={open} title="本周热歌" onClose={onClose} width="max-w-[760px]">
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-2 text-[12px]">
          <span className="text-ink-faint">周次</span>
          <select className="input h-8 w-[140px]" value={week || data?.week || ''} onChange={(e) => setWeek(e.target.value)}>
            {(data?.weeks || []).map((w: string) => (
              <option key={w} value={w}>
                {w}
              </option>
            ))}
          </select>
          {items.some((c) => c.is_seed) && (
            <span className="tag-human">含演示数据：热歌监控（monitor.py）尚未接入真实 YouTube 数据</span>
          )}
        </div>
        {!data ? (
          <Spinner />
        ) : items.length === 0 ? (
          <Empty text="本周周报未生成" />
        ) : (
          items.map((c) => (
            <div key={c.id} className="border border-line rounded-btn p-3">
              <div className="flex items-center gap-2">
                <span className="tag">#{c.rank}</span>
                <span className="text-[13px] font-medium">{c.song}</span>
                <span className="text-[11px] text-ink-faint">{c.artist}</span>
                {c.is_seed && <span className="tag-human">演示</span>}
                <button className="btn-primary btn-sm ml-auto" onClick={() => onUse(c.id)}>
                  用此卡新建项目
                </button>
              </div>
              {c.why_hot && <div className="mt-1.5 text-[11px] text-ink-soft">走红原因：{c.why_hot}</div>}
              <div className="mt-1.5 flex flex-wrap gap-1">
                {['genre', 'bpm', 'vocal_style', 'mood']
                  .filter((k) => c.elements?.[k])
                  .map((k) => (
                    <span key={k} className="tag">
                      {Array.isArray(c.elements[k]) ? c.elements[k].join('/') : String(c.elements[k])}
                    </span>
                  ))}
              </div>
            </div>
          ))
        )}
      </div>
    </Modal>
  )
}
