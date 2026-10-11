import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import clsx from 'clsx'
import api from '../lib/api'
import { useWorkHeartbeat } from '../lib/heartbeat'
import { Banner, Empty, Field, Modal, PageHeader, Section, Spinner, StateDot } from '../components/ui'
import Stepper from '../components/Stepper'
import CandidateCard from '../components/CandidateCard'
import { CHECK_TYPE_LABEL, flatLineCount, fmtDuration, relative } from '../lib/format'

export default function LyricsPage() {
  const { projectId } = useParams()
  const nav = useNavigate()
  // F1-11 有效工时：页面在前台且有操作时每 30 秒报一次
  useWorkHeartbeat(projectId)
  const [detail, setDetail] = useState<any>(null)
  const [lyric, setLyric] = useState<any>(null)
  const [checks, setChecks] = useState<any[]>([])
  const [summary, setSummary] = useState<any>(null)
  const [versions, setVersions] = useState<any[]>([])
  const [models, setModels] = useState<any[]>([])
  const [cands, setCands] = useState<any[]>([])
  const [cardErr, setCardErr] = useState<Record<string, string>>({})
  const [sections, setSections] = useState<any[]>([])
  const [dirty, setDirty] = useState(false)
  const [picked, setPicked] = useState<Record<string, boolean>>({})
  const [variants, setVariants] = useState(1)
  const [playing, setPlaying] = useState('')
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [selectedLines, setSelectedLines] = useState<Set<string>>(new Set())
  const [projects, setProjects] = useState<any[]>([])
  const [showTrends, setShowTrends] = useState(false)
  const [trendCards, setTrendCards] = useState<any[]>([])
  const [rejectOpen, setRejectOpen] = useState(false)
  // 默认只给「看歌词、审核、选曲」这条主线；逐句勾选/重写/韵脚字数这些是精细操作，
  // 需要时才展开，避免一进页面就被十几个控件淹没。
  const [advanced, setAdvanced] = useState(false)
  // PRD V1.1 D2：生产模式（日常出片）/ 选型轮（盲听对比，确定主力模型）
  const [mode, setMode] = useState<'production' | 'selection'>('production')
  const [roundId, setRoundId] = useState<string>('')
  const [rounds, setRounds] = useState<any[]>([])
  const [roundMeta, setRoundMeta] = useState<any>(null)
  // 「保留原文」要填理由：记下正在处理的检查项
  const [keepFor, setKeepFor] = useState<any>(null)
  const [keepReason, setKeepReason] = useState('')
  // F7-4 疑点回流：查证结论顺手存进知识库
  const [keepToKb, setKeepToKb] = useState(false)
  const [keepSource, setKeepSource] = useState('')
  const [projectsLoading, setProjectsLoading] = useState(true)
  const [kbCov, setKbCov] = useState<any>(null)

  const loadAll = useCallback(
    async (id: string) => {
      // 必须自己兜住异常：这个函数在 useEffect 里被直接调用，
      // 不 catch 的话失败会变成 unhandled rejection —— 页面上什么都不显示，
      // 用户只会觉得「按钮点了没反应」（项目被重建后旧链接失效就是这种表现）。
      try {
        const [d, l, m, c] = await Promise.all([
          api.project(id),
          api.lyrics(id),
          api.musicModels(),
          api.candidates(id, roundId || undefined),
        ])
        setDetail(d)
        if (d.project?.destination) api.kbCoverage(d.project.destination).then(setKbCov).catch(() => {})
        setLyric(l.current)
        setChecks(l.checks || [])
        setSummary(l.summary)
        setVersions(l.versions || [])
        if (!dirty) setSections(l.current?.sections || [])
        setModels(m)
        setCands(c.items || [])
        setRounds(c.rounds || [])
        setRoundMeta(c.round_meta || null)
        setErr('')
        setPicked((prev) => {
          if (Object.keys(prev).length) return prev
          // 默认只勾选第一个可用模型：一轮只出一首，试听满意再选下一个，省钱。
          const first =
            m.find((x: any) => x.enabled && x.ready && x.in_production) ||
            m.find((x: any) => x.enabled && x.usable && x.in_production)
          const init: Record<string, boolean> = {}
          m.forEach((x: any) => (init[x.key] = !!first && x.key === first.key))
          return init
        })
      } catch (e: any) {
        setErr(e?.message || String(e))
      }
    },
    [dirty, roundId],
  )

  useEffect(() => {
    if (projectId) loadAll(projectId)
    else
      api
        .projects()
        .then(setProjects)
        .catch((e: any) => setErr(e?.message || String(e)))
        .finally(() => setProjectsLoading(false))
  }, [projectId, loadAll])

  // 机器在跑时轮询：写词 / 检查未完成 / 有候选还在生成。
  // 用 setTimeout 链而不是 setInterval —— 上一轮没回来就不发下一轮，慢网络下不会堆积请求。
  const status = detail?.project?.status
  const busyGen =
    ['lyric_generating', 'music_generating'].includes(status) ||
    summary?.state === 'checking' ||
    cands.some((c) => ['queued', 'running'].includes(c.status))
  useEffect(() => {
    if (!projectId || !busyGen) return
    let alive = true
    let t: number | undefined
    const tick = async () => {
      await loadAll(projectId)
      if (alive) t = window.setTimeout(tick, 4000)
    }
    t = window.setTimeout(tick, 4000)
    return () => {
      alive = false
      if (t) clearTimeout(t)
    }
  }, [projectId, busyGen, loadAll])

  if (!projectId) {
    return (
      <>
        <PageHeader title="词曲工作台" subtitle="选择一个项目进入" />
        <div className="p-6">
          {projectsLoading ? (
            <Spinner />
          ) : err ? (
            <Banner tone="error">{err}</Banner>
          ) : projects.length === 0 ? (
            <Empty text="还没有项目，先到生产看板新建" />
          ) : (
            <div className="grid grid-cols-3 gap-4">
              {projects.map((p) => (
                <button key={p.id} className="card p-4 text-left hover:shadow-sm" onClick={() => nav(`/projects/${p.id}/lyrics`)}>
                  <div className="text-[13px] font-medium">{p.name}</div>
                  <div className="mt-1 text-[11px] text-ink-faint">
                    {p.state_label} · {p.destination}
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </>
    )
  }

  if (!detail || !summary) {
    // 加载失败时不能只转圈：那样用户看到的是「页面空白、点什么都没反应」。
    // 必须把错误和出路摆出来（项目被重建后旧链接失效就是这个表现）。
    if (err) {
      return (
        <div className="p-6">
          <Banner tone="error">
            {err}
            {err.includes('项目不存在') && (
              <>
                {' '}
                <a href="/dashboard" className="underline font-medium">
                  ← 返回看板重新选择项目
                </a>
              </>
            )}
          </Banner>
        </div>
      )
    }
    return (
      <div className="p-6">
        <Spinner />
      </div>
    )
  }

  const p = detail.project
  const b = detail.brief || {}
  const isHuman = p.waiting_human
  const flat = sections.flatMap((s: any, si: number) =>
    (s.lines || []).map((ln: string, li: number) => ({ si, li, key: `${si}:${li}`, section: s.name, text: ln })),
  )
  const pendingChecks = checks.filter((c) => c.status === 'pending')
  const locked = !!p.locked
  const gate = detail.gate || { ok: true, reasons: [] }
  const canEditLyrics = p.status === 'lyric_review' && !locked
  const allowMock = !!detail.allow_mock
  const pickedKeys = Object.entries(picked)
    .filter(([k, v]) => v && models.find((m) => m.key === k && (mode === 'selection' ? m.in_selection : m.in_production) && m.usable))
    .map(([k]) => k)
  const genPayload = () => ({ models: pickedKeys, variants, mode })

  const act = async (key: string, fn: () => Promise<any>, after?: () => void) => {
    setBusy(key)
    setErr('')
    setMsg('')
    setCardErr({})
    try {
      await fn()
      after?.()
    } catch (e: any) {
      setErr(e.message)
      setCardErr({ [key]: e.message })
    } finally {
      setBusy('')
    }
  }

  return (
    <>
      <PageHeader
        crumbs={
          <>
            <Link to="/dashboard" className="hover:underline">
              生产看板
            </Link>
            <span className="mx-1">/</span>
            <span>{p.name}</span>
          </>
        }
        title="词曲工作台"
        subtitle={
          <span className="flex items-center gap-2">
            <span className={isHuman ? 'tag-human' : 'tag-brand'}>{p.state_label}</span>
            <span>{p.status_note}</span>
          </span>
        }
        actions={
          <>
            {locked && (
              <button
                className="btn-primary"
                disabled={!!busy}
                title="已交付项目只读；修订版会复制简报与歌词，重新走一遍闸门"
                onClick={() =>
                  act('revise', async () => {
                    const np = await api.revise(p.id)
                    nav(`/projects/${np.id}/lyrics`)
                  })
                }
              >
                新建修订版
              </button>
            )}
            {!locked && p.reject_targets?.length > 0 && (
              <button className="btn" onClick={() => setRejectOpen(true)} disabled={!!busy}>
                驳回到上游
              </button>
            )}
            {/* 全页只有一个主按钮，对应当前人工节点（PRD V1.1 §6.2）；是否可点由后端闸门决定 */}
            {p.status === 'briefing' && !locked && (
              <button
                className="btn-primary"
                disabled={!!busy || !gate.ok}
                title={gate.reasons.join('；')}
                onClick={() => act('advance', () => api.advance(p.id), () => loadAll(p.id))}
              >
                确认简报，开始写词
              </button>
            )}
            {p.status === 'lyric_review' && !locked && (
              <button
                className="btn-human"
                disabled={!!busy || !gate.ok || pickedKeys.length === 0 || dirty}
                title={
                  dirty
                    ? '歌词有未保存的修改，先保存版本'
                    : !gate.ok
                      ? gate.reasons.join('；')
                      : pickedKeys.length === 0
                        ? '在右侧「模型配置」里勾选生成用的模型'
                        : ''
                }
                onClick={() =>
                  act('advance', () => api.advance(p.id, genPayload()), () => loadAll(p.id))
                }
              >
                {mode === 'selection' ? '确认歌词，发起选型轮' : '确认歌词，开始生曲'}
              </button>
            )}
            {p.status === 'music_review' && !locked && (
              <button
                className="btn-human"
                disabled={!!busy || !gate.ok}
                title={gate.reasons.join('；')}
                onClick={() =>
                  act('advance', () => api.advance(p.id), () => nav(`/projects/${p.id}/storyboard`))
                }
              >
                确认选曲，进入配图
              </button>
            )}
          </>
        }
      />

      <div className="px-6 py-4 border-b border-line bg-page">
        <Stepper column={p.column} status={p.status} />
      </div>

      <div className="p-6 flex flex-col gap-4">
        {/* 上手引导：一句话说清「现在该做什么」，高级操作收在折叠里 */}
        <GuideCard status={p.status} />

        {locked && (
          <Banner tone="warn">
            该项目已交付并锁定，只读。需要调整请点右上角「新建修订版」——修订版会复制简报与歌词，并重新走一遍检查与闸门。
          </Banner>
        )}
        {lyric?.source === 'mock' && (
          // 写词模型调用失败时系统用了模板占位歌词：必须一眼看出来（E2E：欠费导致整版模板歌词，页面毫无提示）
          <Banner tone="error">
            <span className="font-medium">这版歌词是模板占位，不是 AI 写的。</span>
            写词模型调用失败，原因见下方提示（常见：账户欠费 / Key 失效 / 限流）。处理好后点歌词区的「重新生成」。
          </Banner>
        )}
        {/* 闸门原因逐条摆出来：V1.0 只把按钮置灰，用户不知道差什么 */}
        {!locked && isHuman && !gate.ok && gate.reasons.length > 0 && (
          <Banner tone="warn">
            <span className="font-medium">还不能进入下一步：</span>
            {gate.reasons.join('；')}
          </Banner>
        )}

        {err && (
          <Banner tone="error" onClose={() => setErr('')}>
            {err}
            {err.includes('项目不存在') && (
              <>
                {' '}
                {/* 项目被重建（例如重跑 seed）后旧链接会失效，直接给一条回看板的出路 */}
                <a href="/dashboard" className="underline font-medium">
                  ← 返回看板重新选择项目
                </a>
              </>
            )}
          </Banner>
        )}
        {msg && <Banner tone="ok" onClose={() => setMsg('')}>{msg}</Banner>}

        {/* F2-1 创作简报条 */}
        <div className="card px-4 py-3 flex flex-wrap items-center gap-x-6 gap-y-2">
          <BriefField label="目的地" value={p.destination || '—'} />
          <BriefField label="情绪" value={(b.mood || []).join('、') || '—'} />
          <BriefField label="曲风 / 速度" value={`${b.genre || '—'} · ${b.bpm_min}–${b.bpm_max} BPM`} />
          <BriefField label="时长 / 语种" value={`${b.duration_s}s · 中文演唱`} />
          <BriefField
            label="热歌要素参考"
            value={b.trend_card_ids?.length ? `已引用 ${b.trend_card_ids.length} 张素材卡` : '未引用'}
            action={
              <button className="text-[11px] text-brand underline ml-1" onClick={() => {
                setShowTrends(true)
                // 只显示本项目引用的素材卡（V1.0 显示的是全部卡片）
                api.trendCards().then((r) => {
                  const ids: string[] = b.trend_card_ids || []
                  const items = r.items || []
                  setTrendCards(ids.length ? items.filter((c: any) => ids.includes(c.id)) : items)
                })
              }}>
                查看
              </button>
            }
          />
          <BriefField
            label="知识库"
            value={
              kbCov
                ? `已核实 ${kbCov.verified} 条${kbCov.enough ? '' : `（建议 ≥${kbCov.min}）`}${b.kb_refs?.length ? ` · 本版写词引用 ${b.kb_refs.length} 条` : ''}`
                : '—'
            }
            action={
              <a className="text-[11px] text-brand underline ml-1" href="/kb">
                去补充
              </a>
            }
          />
        </div>

        <div className="grid grid-cols-[1fr_380px] gap-5 items-start">
          {/* 左栏 */}
          <div className="flex flex-col gap-4">
            <Section
              title={`歌词 v${lyric?.version ?? '—'}${dirty ? ' · 有未保存修改' : ''}`}
              right={
                <div className="flex items-center gap-2">
                  <label
                    className="flex items-center gap-1 text-[11px] text-ink-soft cursor-pointer select-none"
                    title="需要逐行核对/重写某一句时再打开"
                  >
                    <input
                      type="checkbox"
                      className="accent-[#0F5E57]"
                      checked={advanced}
                      onChange={(e) => setAdvanced(e.target.checked)}
                    />
                    逐句校订
                  </label>
                  {advanced && canEditLyrics && (
                  <button
                    className="btn btn-sm"
                    disabled={!pickedLineCount(selectedLines) || !!busy}
                    onClick={() =>
                      act(
                        'rewrite',
                        () =>
                          api.rewriteLyrics(
                            p.id,
                            [...selectedLines].map((k) => {
                              const [si, li] = k.split(':').map(Number)
                              return { section: sections[si].name, line_no: li + 1 }
                            }),
                            '',
                          ),
                        () => {
                          setSelectedLines(new Set())
                          setDirty(false)
                          loadAll(p.id)
                        },
                      )
                    }
                  >
                    重写选中行
                  </button>
                  )}
                  {canEditLyrics && (
                  <button
                    className="btn btn-sm"
                    disabled={!!busy}
                    title="整首重写（会生成新版本并重新检查）"
                    onClick={() =>
                      act('gen', () => api.generateLyrics(p.id, true), () => {
                        setDirty(false)
                        loadAll(p.id)
                      })
                    }
                  >
                    重新生成
                  </button>
                  )}
                  <button
                    className="btn-primary btn-sm"
                    disabled={!dirty || !!busy || !canEditLyrics}
                    onClick={() =>
                      act('save', () => api.saveLyrics(p.id, lyric?.version || 1, sections), () => {
                        setDirty(false)
                        setMsg('已保存为新版本')
                        loadAll(p.id)
                      })
                    }
                  >
                    保存版本
                  </button>
                </div>
              }
              bodyClass="p-4"
            >
              {sections.length === 0 ? (
                // 首次写词只能通过「确认简报」触发：简报没确认就写词，等于跳过了第一道闸门
                <Empty
                  text={
                    p.status === 'lyric_generating'
                      ? 'AI 正在写词，完成后自动出现在这里…'
                      : p.status === 'briefing'
                        ? '还没有歌词：核对简报后点右上角「确认简报，开始写词」'
                        : '还没有歌词'
                  }
                />
              ) : (
                <div className="flex flex-col gap-4">
                  {sections.map((s: any, si: number) => (
                    <div key={si}>
                      <div className="flex items-center gap-2 mb-1.5">
                        <span className={clsx('text-[12px] font-medium', s.name?.includes('副歌') ? 'text-brand' : 'text-ink')}>
                          {s.name}
                        </span>
                        <span className="text-[11px] text-ink-faint">{s.lines?.length || 0} 行</span>
                      </div>
                      <div className="flex flex-col gap-1">
                        {(s.lines || []).map((ln: string, li: number) => {
                          const key = `${si}:${li}`
                          const issue = pendingChecks.some((c) => c.section === s.name && c.line_no === li + 1)
                          const n = ln.replace(/[\s,，。、！？!?；;：:—…·-]/g, '').length
                          return (
                            <div
                              key={key}
                              className={clsx(
                                'flex items-center gap-2 rounded-btn px-1.5 py-0.5 border',
                                selectedLines.has(key) ? 'border-brand bg-brand-soft/40' : 'border-transparent',
                                issue && 'bg-human-soft/40',
                              )}
                            >
                              {advanced && (
                                <input
                                  type="checkbox"
                                  className="accent-[#0F5E57] shrink-0"
                                  checked={selectedLines.has(key)}
                                  onChange={(e) => {
                                    const next = new Set(selectedLines)
                                    e.target.checked ? next.add(key) : next.delete(key)
                                    setSelectedLines(next)
                                  }}
                                />
                              )}
                              <span className="text-[10px] text-ink-faint w-5 text-right shrink-0">{li + 1}</span>
                              <input
                                className="flex-1 bg-transparent border-b border-transparent hover:border-line focus:border-brand outline-none py-1 text-[13px] read-only:hover:border-transparent"
                                value={ln}
                                readOnly={!canEditLyrics}
                                title={canEditLyrics ? '' : '只有「待确认歌词」阶段可以改词；需要改请驳回到这一步'}
                                onChange={(e) => {
                                  const next = sections.map((x: any, i: number) =>
                                    i === si ? { ...x, lines: x.lines.map((y: string, j: number) => (j === li ? e.target.value : y)) } : x,
                                  )
                                  setSections(next)
                                  setDirty(true)
                                }}
                              />
                              {advanced && (
                                <>
                                  {/* F2-3：行尾显示字数 */}
                                  <span className={clsx('text-[10px] shrink-0', n >= 6 && n <= 12 ? 'text-ink-faint' : 'text-human')}>
                                    {n} 字
                                  </span>
                                  <span className="text-[10px] text-ink-faint shrink-0">韵 {ln.trim().slice(-1)}</span>
                                </>
                              )}
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Section>

            {/* F2-4 自动检查 */}
            <Section
              title="自动检查"
              right={
                <div className="flex items-center gap-2">
                  {/* 标签只取后端检查结果；检查没跑完时绝不显示「通过」（V1.0 无歌词也显示三项通过） */}
                  {summary.state === 'done' &&
                    summary.by_type &&
                    Object.entries(summary.by_type).map(([k, v]: any) => (
                      // 只有史实疑点必须处理（橙色）；押韵、字数是建议（灰色），可采纳可不理
                      <span
                        key={k}
                        className={v.pending && v.blocking ? 'tag-human' : 'tag'}
                        title={v.blocking ? '史实疑点必须处理后才能确认歌词' : '风格建议：可采纳，也可以不处理'}
                      >
                        {CHECK_TYPE_LABEL[k]} {v.pending ? (v.blocking ? `${v.pending} 条待确认` : `${v.pending} 条建议`) : '通过'}
                      </span>
                    ))}
                  {summary.state === 'checking' && <span className="tag-brand">检查中…</span>}
                  {canEditLyrics && (
                    <button
                      className="btn btn-sm"
                      disabled={!!busy || !lyric || summary.state === 'checking'}
                      onClick={() => act('check', () => api.recheck(p.id), () => loadAll(p.id))}
                    >
                      重新检查
                    </button>
                  )}
                </div>
              }
              bodyClass="p-4"
            >
              {summary.state === 'none' ? (
                <Empty text="还没有歌词，写完后自动检查押韵、字数与史实" />
              ) : summary.state === 'checking' ? (
                <div className="flex items-center gap-2 text-[12px] text-ink-soft">
                  <Spinner /> 正在检查押韵、字数与史实，完成前不能确认歌词…
                </div>
              ) : checks.length === 0 ? (
                <Empty text="三项检查均未发现疑点" />
              ) : (
                <div className="flex flex-col gap-2.5">
                  <div className="flex items-center gap-2 text-[11px] text-ink-soft">
                    <span className="flex-1">
                      只有<b>史实</b>疑点必须处理（事实错误会直接影响文旅内容的可信度）；押韵和字数是建议，可以采纳，也可以不管。
                    </span>
                    {canEditLyrics && checks.filter((c) => c.type === 'fact' && c.status === 'pending').length > 1 && (
                      <button
                        className="btn btn-sm shrink-0"
                        disabled={!!busy}
                        title="逐条看过都没问题时，用同一个理由一次保留全部史实疑点"
                        onClick={() => {
                          setKeepFor({ batch: true, text: `全部 ${checks.filter((c) => c.type === 'fact' && c.status === 'pending').length} 条史实疑点` })
                          setKeepReason('已逐条人工查证属实')
                        }}
                      >
                        全部保留史实疑点…
                      </button>
                    )}
                  </div>
                  {[...checks].sort((a, b) => Number(b.type === 'fact' && b.status === 'pending') - Number(a.type === 'fact' && a.status === 'pending')).map((c) => (
                    <div
                      key={c.id}
                      className={clsx(
                        'border rounded-btn p-3',
                        c.status === 'pending' && c.type === 'fact' ? 'border-human/40 bg-human-soft/35' : 'border-line bg-white',
                      )}
                    >
                      <div className="flex items-center gap-2">
                        <span className={c.status === 'pending' && c.type === 'fact' ? 'tag-human' : 'tag'}>{CHECK_TYPE_LABEL[c.type]}</span>
                        {c.status === 'pending' && c.type !== 'fact' && <span className="tag" title="风格建议，不处理也能确认歌词">建议 · 可不处理</span>}
                        {c.type === 'fact' && c.kb_entry_ids?.length > 0 && (
                          <span className="tag-brand" title="依据来自已核实的知识库条目">有出处</span>
                        )}
                        {c.needs_manual && (
                          <span className="tag-human" title="知识库尚未接入，这条疑点没有可追溯出处，需人工查证后再决定">
                            需人工核实
                          </span>
                        )}
                        <span className="text-[11px] text-ink-soft">
                          {c.section} 第 {c.line_no} 行
                        </span>
                        <span className="text-[11px] text-ink-faint truncate flex-1">{c.text}</span>
                        {c.status === 'kept' && <span className="tag-brand">已保留原文</span>}
                        {c.status === 'adopted' && <span className="tag-brand">已采纳</span>}
                      </div>
                      {c.evidence && <div className="mt-1.5 text-[11px] text-ink-soft">依据：{c.evidence}</div>}
                      {c.status === 'kept' && c.keep_reason && (
                        <div className="mt-1 text-[11px] text-ink-faint">保留理由：{c.keep_reason}</div>
                      )}
                      {c.status === 'pending' && canEditLyrics && (
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                          {(c.suggestions || []).map((s: string, i: number) => (
                            <button
                              key={i}
                              className="btn btn-sm"
                              disabled={!!busy || dirty}
                              title={dirty ? '先保存手动修改' : '用这句替换原行，生成新版本并重新检查'}
                              onClick={() =>
                                act('adopt', () => api.adoptCheck(c.id, i), () => {
                                  setDirty(false)
                                  setMsg('已采纳建议并重新检查')
                                  loadAll(p.id)
                                })
                              }
                            >
                              采纳：{s}
                            </button>
                          ))}
                          {/* 「保留原文」任何疑点都要能点（V1.0 没有建议的疑点连这个按钮都没有，只能卡住） */}
                          <button
                            className="btn btn-sm"
                            disabled={!!busy}
                            onClick={() => {
                              setKeepFor(c)
                              setKeepReason(c.needs_manual ? '已人工查证属实' : '')
                            }}
                          >
                            {c.type === 'fact' ? '保留原文…' : '不改，标记已看过…'}
                          </button>
                          {(c.suggestions || []).length === 0 && (
                            <span className="text-[10px] text-ink-faint">没有合格的改写建议，可直接在上方编辑该行后保存</span>
                          )}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </Section>

            {versions.length > 0 && (
              <Section title="歌词版本" bodyClass="p-3">
                <div className="flex flex-col gap-1.5 max-h-[180px] overflow-y-auto">
                  {versions.map((v: any) => (
                    <div
                      key={v.id}
                      className={clsx(
                        'flex items-center gap-2 rounded-btn px-2 py-1.5 border text-[11px]',
                        v.id === lyric?.id ? 'border-brand bg-brand-soft/40' : 'border-line',
                      )}
                    >
                      <span className="font-medium">v{v.version}</span>
                      <span className="text-ink-faint">{v.source === 'ai' ? 'AI 生成' : v.source === 'mock' ? '模板占位' : '人工编辑'}</span>
                      <span className="text-ink-faint">
                        {flatLineCount(v.sections || [])} 行 · {relative(v.created_at)}
                      </span>
                      {v.id !== lyric?.id && canEditLyrics && (
                        <button
                          className="ml-auto text-brand underline shrink-0"
                          disabled={!!busy}
                          onClick={() =>
                            act('rollback', () => api.rollbackLyrics(p.id, v.version), () => {
                              setDirty(false)
                              setMsg(`已回滚到 v${v.version}（生成新版本，不覆盖历史）`)
                              loadAll(p.id)
                            })
                          }
                        >
                          回滚
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </Section>
            )}
          </div>

          {/* 右栏 */}
          <div className="flex flex-col gap-4">
            <Section
              title="模型配置"
              right={
                // PRD V1.1 D2：生产模式（日常出片）/ 选型轮（盲听，确定主力模型）
                <div className="flex rounded-btn border border-line overflow-hidden text-[11px]">
                  {(
                    [
                      ['production', '生产模式'],
                      ['selection', '选型轮'],
                    ] as const
                  ).map(([k, label]) => (
                    <button
                      key={k}
                      className={clsx('px-2.5 py-1', mode === k ? 'bg-brand text-white' : 'bg-white text-ink-soft')}
                      onClick={() => {
                        setMode(k)
                        setVariants(k === 'selection' ? 2 : 1)
                        // 选型轮默认全选可用的参赛模型；生产模式默认只勾一个
                        // 默认只勾真实就绪的模型；都没就绪（演示模式）才退回可用（占位）模型
                        const usableIn = models.filter((m) => (k === 'selection' ? m.in_selection : m.in_production) && m.usable)
                        const readyIn = usableIn.filter((m) => m.ready)
                        const pool = readyIn.length ? readyIn : usableIn
                        const next: Record<string, boolean> = {}
                        models.forEach((m) => (next[m.key] = false))
                        ;(k === 'selection' ? pool : pool.slice(0, 1)).forEach((m) => (next[m.key] = true))
                        setPicked(next)
                      }}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              }
              bodyClass="p-3"
            >
              <div className="text-[11px] text-ink-faint mb-2 leading-relaxed">
                {mode === 'selection'
                  ? '选型轮：参赛模型并行各出几版，盲听打分后选定，选定才揭晓模型并计入战绩。用于确定主力模型。'
                  : '生产模式：用一个模型出一首，不满意再换模型或同模型再来。显示模型名，不计入战绩。'}
              </div>
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-ink-faint">
                    <th className="text-left font-normal pb-1 w-6"></th>
                    <th className="text-left font-normal pb-1">模型</th>
                    <th className="text-right font-normal pb-1" title="只统计已揭晓的选型轮，1–5 分">
                      选型均分
                    </th>
                    <th className="text-right font-normal pb-1">胜出 / 轮</th>
                  </tr>
                </thead>
                <tbody>
                  {models
                    .filter((m) => (mode === 'selection' ? m.in_selection : m.in_production))
                    .map((m) => (
                      <tr key={m.key} className={clsx('border-t border-line', !m.usable && 'opacity-50')}>
                        <td className="py-2">
                          <input
                            type="checkbox"
                            className="accent-[#0F5E57]"
                            checked={!!picked[m.key]}
                            disabled={!m.usable}
                            title={m.usable ? '' : '未配置凭证，生产环境禁用占位结果'}
                            onChange={(e) =>
                              setPicked(
                                mode === 'production' && e.target.checked
                                  ? // 生产模式一次只出一首：单选
                                    Object.fromEntries(models.map((x) => [x.key, x.key === m.key]))
                                  : { ...picked, [m.key]: e.target.checked },
                              )
                            }
                          />
                        </td>
                        <td className="py-2">
                          <div className="flex items-center gap-1.5">
                            <StateDot status={m.ready ? 'done' : 'queued'} />
                            <span className="truncate">{m.name}</span>
                          </div>
                          <div className="text-[10px] text-ink-faint">
                            {m.run_mode === 'local' ? '本地 4090' : 'API'}
                            {m.model ? ` · ${m.model}` : ''} ·{' '}
                            {m.ready ? '已就绪' : m.usable ? '未配置（占位）' : '未配置'}
                          </div>
                        </td>
                        <td className="text-right">{m.score_n ? m.avg_score.toFixed(2) : '—'}</td>
                        <td className="text-right">
                          <span className="font-medium text-brand">{m.wins}</span>
                          <span className="text-ink-faint"> / {m.rounds}</span>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
              {models.some((m) => m.in_selection && m.rounds > 0 && m.rounds < 3) && (
                <div className="mt-1 text-[10px] text-ink-faint">选型轮样本不足 3 轮，战绩仅供参考</div>
              )}
              <div className="mt-3 border-t border-line pt-3 flex flex-col gap-2">
                <Field label={mode === 'selection' ? '每个模型出几版' : '生成版数'}>
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={1}
                      max={4}
                      className="input w-20"
                      value={variants}
                      onChange={(e) => setVariants(Math.max(1, Math.min(4, Number(e.target.value))))}
                    />
                    <span className="text-[11px] text-ink-faint">共 {pickedKeys.length * variants} 个候选</span>
                  </div>
                </Field>
                {p.status === 'lyric_review' ? (
                  // 待确认歌词时不在这里放第二个主按钮：生成由右上角「确认歌词」触发（全页只有一个主按钮）
                  <div className="text-[11px] text-ink-faint">
                    选好模型后，点右上角「{mode === 'selection' ? '确认歌词，发起选型轮' : '确认歌词，开始生曲'}」。
                  </div>
                ) : (
                  <button
                    className="btn w-full"
                    disabled={
                      !!busy ||
                      locked ||
                      !['music_generating', 'music_review'].includes(p.status) ||
                      pickedKeys.length === 0 ||
                      (mode === 'selection' && pickedKeys.length < 2)
                    }
                    title={
                      !['music_generating', 'music_review'].includes(p.status)
                        ? '确认歌词后才能生曲'
                        : mode === 'selection' && pickedKeys.length < 2
                          ? '选型轮至少勾选 2 个模型'
                          : ''
                    }
                    onClick={() =>
                      act(
                        'music',
                        async () => {
                          const r = await api.generateMusic(p.id, { ...genPayload(), style_prompt: b.style_prompt })
                          setRoundId(r.round_id || '')
                        },
                        () => {
                          setMsg(mode === 'selection' ? '已发起选型轮（盲听）' : `已派发生成任务（${variants} 首）`)
                          loadAll(p.id)
                        },
                      )
                    }
                  >
                    {mode === 'selection' ? '发起新一轮选型（盲听）' : '用所选模型再生成'}
                  </button>
                )}
              </div>
            </Section>

            <Section
              title={`候选曲目 ${cands.length ? `(${cands.length})` : ''}`}
              right={
                roundMeta ? (
                  <span className={roundMeta.blind ? 'tag-human' : 'tag'}>
                    {roundMeta.kind === 'selection'
                      ? `选型轮 #${roundMeta.selection_no}${roundMeta.blind ? ' · 盲听中' : ' · 已揭晓'}`
                      : '生产模式'}
                  </span>
                ) : null
              }
              bodyClass="p-3"
            >
              {rounds.length > 1 && (
                <div className="flex flex-wrap gap-1.5 mb-2.5">
                  {rounds.map((r) => (
                    <button
                      key={r.id}
                      className={clsx(
                        'text-[10px] px-2 py-0.5 rounded border',
                        roundMeta?.id === r.id ? 'border-brand text-brand bg-brand-soft/40' : 'border-line text-ink-soft',
                      )}
                      onClick={() => setRoundId(r.id)}
                    >
                      第 {r.round_no} 轮 · {r.kind === 'selection' ? `选型${r.blind ? '（盲听）' : ''}` : '生产'}
                    </button>
                  ))}
                </div>
              )}
              {roundMeta?.blind && (
                <div className="mb-2.5 text-[11px] text-ink-soft bg-page border border-line rounded-btn px-2.5 py-2">
                  盲听中：本轮 {roundMeta.model_count} 个模型的候选已随机编号。逐个试听并给<b>所有候选</b>打分，再选定最好的一版——选定后揭晓模型并计入战绩。
                </div>
              )}
              {cands.length === 0 ? (
                <Empty text={p.status === 'music_generating' ? '正在生成，完成后出现在这里…' : '还没有候选曲目'} />
              ) : (
                <div className="flex flex-col gap-2.5 max-h-[620px] overflow-y-auto pr-0.5">
                  {cands.map((c) => (
                    <CandidateCard
                      key={c.id}
                      c={{ ...c, allow_mock: allowMock }}
                      selected={p.selected_candidate_id === c.id}
                      playing={playing === c.id}
                      onPlay={() => setPlaying(c.id)}
                      onPause={() => setPlaying((v) => (v === c.id ? '' : v))}
                      busy={busy === c.id}
                      readOnly={locked || !['music_generating', 'music_review'].includes(p.status)}
                      error={cardErr[c.id]}
                      selectBlock={(() => {
                        if (!roundMeta?.blind) return ''
                        const un = cands.filter((x) => x.status === 'done' && !(x.rating?.diction && x.rating?.melody && x.rating?.arrangement)).map((x) => x.label)
                        return un.length ? `盲听轮需先给全部候选打完三项分再选定，还差：候选 ${un.sort().join('、')}` : ''
                      })()}
                      onRate={(patch) =>
                        act(c.id, () =>
                          api.rate(c.id, {
                            diction: patch.diction ?? c.rating?.diction ?? 0,
                            melody: patch.melody ?? c.rating?.melody ?? 0,
                            arrangement: patch.arrangement ?? c.rating?.arrangement ?? 0,
                          }),
                        ).then(() => loadAll(p.id))
                      }
                      onSelect={() =>
                        act(c.id, async () => {
                          const r = await api.selectCandidate(p.id, c.id)
                          setMsg(
                            r.revealed
                              ? `已选定候选 ${c.label}，揭晓：${r.model_name}（已计入选型战绩）`
                              : `已选定 ${r.model_name} 第 ${r.variant} 版`,
                          )
                        }, () => loadAll(p.id))
                      }
                    />
                  ))}
                </div>
              )}
            </Section>

            {p.selected_candidate_id && (
              <Section title="已选结果" bodyClass="p-3">
                {(() => {
                  const sel = cands.find((c) => c.id === p.selected_candidate_id)
                  if (!sel) return <Empty text="选定的曲目在其他轮次，可在上方切换轮次查看" />
                  return (
                    <div className="flex flex-col gap-1.5 text-[12px]">
                      <div className="flex items-center gap-2">
                        <StateDot status="done" />
                        <span className="font-medium">{sel.model_name || `候选 ${sel.label}`}</span>
                        {sel.variant && <span className="tag">第 {sel.variant} 版</span>}
                        {sel.is_mock && <span className="tag-human">占位</span>}
                      </div>
                      <div className="text-ink-soft">
                        BPM {Math.round(sel.bpm || 0)} · {fmtDuration(sel.duration_s)}
                        {sel.fit_score !== null && sel.fit_score !== undefined && ` · 匹配分 ${sel.fit_score}`}
                      </div>
                      {sel.rating && (
                        <div className="text-ink-soft">
                          人工评分 咬字 {sel.rating.diction} / 旋律 {sel.rating.melody} / 编曲 {sel.rating.arrangement}
                          （均分 {(sel.rating.total / 3).toFixed(1)}）
                        </div>
                      )}
                      {!locked && p.status === 'music_review' && sel.round_kind !== 'selection' && (
                        <div className="mt-2 border-t border-line pt-2 flex flex-col gap-1.5">
                          <div className="text-[11px] text-ink-faint">
                            试听不满意？换一个模型再生成一首（每次只出一首）
                          </div>
                          <div className="flex flex-wrap gap-2">
                            {models
                              .filter((m) => m.enabled && m.in_production && m.usable && m.key !== sel.model_key)
                              .map((m) => (
                                <button
                                  key={m.key}
                                  className="btn btn-sm"
                                  disabled={!!busy}
                                  onClick={() =>
                                    act(
                                      'regen',
                                      () => api.generateMusic(p.id, { models: [m.key], variants: 1, mode: 'production' }),
                                      () => {
                                        setMsg(`已用 ${m.name} 生成 1 首`)
                                        loadAll(p.id)
                                      },
                                    )
                                  }
                                >
                                  换 {m.name} 生成 1 首
                                </button>
                              ))}
                            <button
                              className="btn btn-sm"
                              disabled={!!busy}
                              onClick={() =>
                                act('regen', () => api.regenerate(p.id, 1), () => {
                                  setMsg(`已用 ${sel.model_name} 再生成 1 版`)
                                  loadAll(p.id)
                                })
                              }
                            >
                              同模型再来 1 版
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })()}
              </Section>
            )}
          </div>
        </div>
      </div>

      <Modal open={showTrends} title="引用的热歌素材卡" onClose={() => setShowTrends(false)} width="max-w-[760px]">
        <div className="flex flex-col gap-3">
          {trendCards.length === 0 && <Empty text="暂无素材卡" />}
          {trendCards.map((c) => (
            <div key={c.id} className="border border-line rounded-btn p-3">
              <div className="flex items-center gap-2">
                <span className="text-[13px] font-medium">{c.song}</span>
                <span className="text-[11px] text-ink-faint">{c.artist}</span>
                <span className="tag ml-auto">{c.week}</span>
              </div>
              {c.why_hot && <div className="mt-1.5 text-[11px] text-ink-soft">走红原因：{c.why_hot}</div>}
              <div className="mt-1.5 flex flex-wrap gap-1">
                {Object.entries(c.elements || {})
                  .filter(([, v]) => v && v !== 'unknown')
                  .slice(0, 10)
                  .map(([k, v]: any) => (
                    <span key={k} className="tag">
                      {k}: {Array.isArray(v) ? v.join('/') : String(v)}
                    </span>
                  ))}
              </div>
              {c.remix?.style_prompt && (
                <div className="mt-2 text-[11px] bg-page border border-line rounded-btn p-2 font-mono">
                  {c.remix.style_prompt}
                </div>
              )}
              {c.remix?.avoid?.length > 0 && (
                <div className="mt-1.5 text-[11px] text-human-dark">禁用项：{c.remix.avoid.join('；')}</div>
              )}
            </div>
          ))}
        </div>
      </Modal>

      <Modal
        open={!!keepFor}
        title="保留原文"
        onClose={() => setKeepFor(null)}
        footer={
          <>
            <button className="btn" onClick={() => setKeepFor(null)}>
              取消
            </button>
            <button
              className="btn-primary"
              disabled={!!busy || keepReason.trim().length < 2 || (keepToKb && !keepSource.trim())}
              onClick={() =>
                act(
                  'keep',
                  async () => {
                    if (keepFor.batch) {
                      await api.keepAllChecks(p.id, keepReason.trim())
                      return
                    }
                    await api.keepCheck(keepFor.id, keepReason.trim())
                    if (keepToKb)
                      await api.kbFromCheck(keepFor.id, {
                        destination: p.destination,
                        content: keepReason.trim(),
                        source_name: keepSource.trim(),
                      })
                  },
                  () => {
                    setKeepFor(null)
                    setMsg(keepToKb ? '已保留原文，查证结论已存入知识库（下次核对会引用）' : '已保留原文（理由已留档）')
                    setKeepToKb(false)
                    setKeepSource('')
                    loadAll(p.id)
                  },
                )
              }
            >
              确认保留
            </button>
          </>
        }
      >
        {keepFor && (
          <div className="flex flex-col gap-2">
            <div className="text-[12px]">
              <span className="text-ink-faint">{keepFor.section} 第 {keepFor.line_no} 行：</span>
              {keepFor.text}
            </div>
            {keepFor.evidence && <div className="text-[11px] text-ink-soft">疑点：{keepFor.evidence}</div>}
            <Field label="保留理由（必填，会作为闸门放行依据留档）">
              <input
                className="input"
                autoFocus
                value={keepReason}
                placeholder="例如：已查证属实（附出处） / 诗意表达不做史实解读"
                onChange={(e) => setKeepReason(e.target.value)}
              />
            </Field>
            {keepFor.type === 'fact' && (
              <>
                <label className="flex items-center gap-2 text-[12px]">
                  <input
                    type="checkbox"
                    className="accent-[#0F5E57]"
                    checked={keepToKb}
                    onChange={(e) => setKeepToKb(e.target.checked)}
                  />
                  把查证结论存入知识库（以后同类疑点直接引用，不用再查）
                </label>
                {keepToKb && (
                  <Field label="出处（你查证用的来源）">
                    <input
                      className="input"
                      value={keepSource}
                      placeholder="例如：长白山管委会官网 / 《长白山志》"
                      onChange={(e) => setKeepSource(e.target.value)}
                    />
                  </Field>
                )}
              </>
            )}
          </div>
        )}
      </Modal>

      <Modal
        open={rejectOpen}
        title="驳回到上游状态"
        onClose={() => setRejectOpen(false)}
        footer={<button className="btn" onClick={() => setRejectOpen(false)}>取消</button>}
      >
        <div className="flex flex-col gap-2">
          <Banner tone="warn">驳回会把项目退回上游重新生成，已产生的下游产物会保留在库中以便对比。</Banner>
          {p.reject_targets?.map((t: string) => (
            <button
              key={t}
              className="btn justify-start"
              disabled={!!busy}
              onClick={() =>
                act('reject', () => api.reject(p.id, t), () => {
                  setRejectOpen(false)
                  loadAll(p.id)
                })
              }
            >
              驳回到：{p.reject_target_labels?.[t] || t}
            </button>
          ))}
        </div>
      </Modal>
    </>
  )
}

function pickedLineCount(s: Set<string>) {
  return s.size
}

function BriefField({ label, value, action }: { label: string; value: string; action?: ReactNode }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-[11px] text-ink-faint">{label}</span>
      <span className="text-[12px] text-ink">{value}</span>
      {action}
    </div>
  )
}

/* ------------------------------------------------------------------ 上手引导 */

// 引导卡显示的是「人工节点」（PRD V1.1 §4.5），顶部进度条显示的是「阶段」，两套名称各司其职、全站统一
const GUIDE_STEPS = ['确认简报', '确认歌词', '确认选曲', '确认配图', '确认精剪']

// 每个状态一句话说清「现在该做什么、按钮在哪」。auto=true 表示机器在跑，用户不用操作。
const GUIDE: Record<string, { step: number; title: string; desc: string; auto?: boolean }> = {
  briefing: {
    step: 1,
    title: '核对创作简报',
    desc: '确认目的地、情绪、曲风、时长没问题，然后点右上角「确认简报，开始写词」。',
  },
  lyric_generating: {
    step: 1,
    title: 'AI 正在写词',
    desc: '约 30 秒，不用操作。写完后会停在第 2 步等你确认。',
    auto: true,
  },
  lyric_review: {
    step: 2,
    title: '审核歌词',
    desc: '先看下面「自动检查」里的黄色卡片 —— 那是 AI 查出的史实/字数/韵脚疑点，逐条「采纳」或「保留原文」（保留需写理由）。疑点清零后，在右侧选生产模式或选型轮，再点右上角确认。',
  },
  music_generating: {
    step: 2,
    title: 'AI 正在生曲',
    desc: '生产模式默认只出 1 首（省钱），选型轮按勾选的模型并行出。一般 2–4 分钟，完成后自动进入选曲。',
    auto: true,
  },
  music_review: {
    step: 3,
    title: '试听并选定一版',
    desc: '在右侧「候选曲目」试听并打分，选定满意的一版，再点右上角「确认选曲，进入配图」。选型轮是盲听：所有候选都打完分才能选定，选定后揭晓模型。',
  },
  storyboard_generating: {
    step: 4,
    title: 'AI 正在逐句配图',
    desc: '按每句歌词提炼真实景区并出图，一句一张。页面会自动刷新，完成后停在「待审配图」。',
    auto: true,
  },
  storyboard_review: {
    step: 4,
    title: '审核逐句配图（这步最影响成片观感）',
    desc: '去「配图审核」页逐句看：图是否贴这句歌词的意境。可通过、改提示词重出、换模型重出，或上传你自己的图替换。全部通过后点「确认配图，进入卡点」。',
  },
  audio_analyzing: {
    step: 4,
    title: '正在分析音频',
    desc: '检测节拍、把歌词逐句对齐到时间轴上。自动进行。',
    auto: true,
  },
  rough_cutting: {
    step: 4,
    title: '正在自动卡点',
    desc: '按节拍切镜头，并按你审核过的配图逐句对号入座。自动进行。',
    auto: true,
  },
  edit_review: {
    step: 5,
    title: '精剪确认',
    desc: '去「剪辑工作台」查看时间轴和字幕，确认无误后点「确认精剪，开始合成」。',
  },
  rendering: {
    step: 5,
    title: '正在合成成片',
    desc: '输出横版 / 竖版 / 短版 / 中文字幕 / 中英字幕共 5 个版本，约 3 分钟。',
    auto: true,
  },
  delivered: {
    step: 5,
    title: '已完成，成片可下载',
    desc: '去「剪辑工作台」可以播放和下载各版本成片。',
  },
}

function GuideCard({ status }: { status: string }) {
  const [open, setOpen] = useState(false)
  const g = GUIDE[status] || { step: 1, title: '准备中', desc: '等待项目状态更新。' }
  return (
    <div className="card overflow-hidden">
      <div className="flex items-start gap-3 px-4 py-3">
        <div
          className={clsx(
            'shrink-0 w-6 h-6 rounded-full flex items-center justify-center text-[12px] font-medium',
            g.auto ? 'bg-page text-ink-faint border border-line' : 'bg-brand text-white',
          )}
        >
          {g.auto ? '·' : g.step}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[13px] font-medium">{g.title}</span>
            <span className="flex items-center gap-1 flex-wrap">
              {GUIDE_STEPS.map((s, i) => (
                <span
                  key={s}
                  className={clsx(
                    'text-[10px] px-1.5 py-0.5 rounded',
                    i + 1 === g.step
                      ? 'bg-brand-soft text-brand font-medium'
                      : i + 1 < g.step
                        ? 'text-ink-faint'
                        : 'text-ink-faint opacity-60',
                  )}
                >
                  {i + 1}.{s}
                </span>
              ))}
            </span>
          </div>
          <div className="text-[11px] text-ink-soft mt-1 leading-relaxed">{g.desc}</div>
        </div>
        <button className="text-[11px] text-brand underline shrink-0" onClick={() => setOpen(!open)}>
          {open ? '收起' : '完整流程'}
        </button>
      </div>
      {open && (
        <div className="px-4 py-3 border-t border-line bg-page text-[11px] text-ink-soft leading-relaxed">
          <div className="font-medium text-ink mb-1">五步走完一条文旅 MV</div>
          <div>
            <b>1. 创作简报</b> —— 定目的地与风格（几秒）
          </div>
          <div>
            <b>2. 审核歌词</b> —— AI 写词 + 史实自检，你逐条确认（约 30 秒）
          </div>
          <div>
            <b>3. 试听选曲</b> —— 出一首真曲（¥0.2–0.6），试听满意再定；不满意可换模型再出一首
          </div>
          <div>
            <b>4. 配图审核</b> —— 逐句提炼真实景区并出图，一句一张，你逐句过（去「配图审核」页）
          </div>
          <div>
            <b>5. 精剪交付</b> —— 自动卡点 + 合成，一次输出 5 个版本
          </div>
          <div className="mt-2 text-ink-faint">
            带数字的步骤是<b>人工闸门</b>，必须你点确认才会往下走；显示「正在…」的步骤机器在跑，页面会自动刷新。
          </div>
          <div className="mt-1 text-ink-faint">
            本页管前 3 步；第 4 步在「配图审核」，第 5 步在「剪辑工作台」。
          </div>
        </div>
      )}
    </div>
  )
}
