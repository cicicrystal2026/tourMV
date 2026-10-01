import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import clsx from 'clsx'
import api from '../lib/api'
import { Banner, Empty, Field, Modal, PageHeader, Section, Spinner, StateDot } from '../components/ui'
import Stepper from '../components/Stepper'
import CandidateCard from '../components/CandidateCard'
import { CHECK_TYPE_LABEL, flatLineCount, fmtDuration, relative } from '../lib/format'

export default function LyricsPage() {
  const { projectId } = useParams()
  const nav = useNavigate()
  const [detail, setDetail] = useState<any>(null)
  const [lyric, setLyric] = useState<any>(null)
  const [checks, setChecks] = useState<any[]>([])
  const [summary, setSummary] = useState<any>(null)
  const [versions, setVersions] = useState<any[]>([])
  const [models, setModels] = useState<any[]>([])
  const [cands, setCands] = useState<any[]>([])
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
          api.candidates(id),
        ])
        setDetail(d)
        setLyric(l.current)
        setChecks(l.checks || [])
        setSummary(l.summary)
        setVersions(l.versions || [])
        if (!dirty) setSections(l.current?.sections || [])
        setModels(m)
        setCands(c.items || [])
        setErr('')
        setPicked((prev) => {
          if (Object.keys(prev).length) return prev
          // 默认只勾选第一个已就绪的模型：一轮只出一首，试听满意再选下一个，
          // 避免一次并排生成多首（省 token）。想对比时用户可手动多勾。
          const first =
            m.find((x: any) => x.enabled && x.ready) || m.find((x: any) => x.enabled)
          const init: Record<string, boolean> = {}
          m.forEach((x: any) => (init[x.key] = !!first && x.key === first.key))
          return init
        })
      } catch (e: any) {
        setErr(e?.message || String(e))
      }
    },
    [dirty],
  )

  useEffect(() => {
    if (projectId) loadAll(projectId)
    else api.projects().then(setProjects)
  }, [projectId, loadAll])

  // 生成中轮询
  const status = detail?.project?.status
  useEffect(() => {
    if (!projectId) return
    if (!['lyric_generating', 'music_generating'].includes(status)) return
    const t = setInterval(() => loadAll(projectId), 4000)
    return () => clearInterval(t)
  }, [projectId, status, loadAll])

  if (!projectId) {
    return (
      <>
        <PageHeader title="词曲工作台" subtitle="选择一个项目进入" />
        <div className="p-6">
          {projects.length === 0 ? (
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

  const act = async (key: string, fn: () => Promise<any>, after?: () => void) => {
    setBusy(key)
    setErr('')
    setMsg('')
    try {
      await fn()
      after?.()
    } catch (e: any) {
      setErr(e.message)
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
            {p.reject_targets?.length > 0 && (
              <button className="btn" onClick={() => setRejectOpen(true)} disabled={!!busy}>
                驳回到上游
              </button>
            )}
            {p.status === 'briefing' && (
              <button
                className="btn-primary"
                disabled={!!busy}
                onClick={() => act('advance', () => api.advance(p.id), () => loadAll(p.id))}
              >
                确认简报，开始写词
              </button>
            )}
            {p.status === 'lyric_review' && (
              <button
                className="btn-human"
                disabled={!!busy}
                onClick={() =>
                  act(
                    'advance',
                    () =>
                      api.advance(p.id, {
                        models: Object.entries(picked).filter(([, v]) => v).map(([k]) => k),
                        variants,
                      }),
                    () => loadAll(p.id),
                  )
                }
              >
                确认歌词，进入生曲
              </button>
            )}
            {p.status === 'music_review' && (
              <button
                className="btn-human"
                disabled={!!busy || !p.selected_candidate_id}
                title={p.selected_candidate_id ? '' : '请先选定一版候选'}
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
                api.trendCards().then((r) => setTrendCards(r.items || []))
              }}>
                查看
              </button>
            }
          />
          <BriefField label="知识库引用" value="本期不接入（按需求直调 LLM）" />
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
                  {advanced && (
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
                  <button
                    className="btn btn-sm"
                    disabled={!!busy || !['briefing', 'lyric_generating', 'lyric_review'].includes(p.status)}
                    onClick={() =>
                      act('gen', () => api.generateLyrics(p.id, true), () => {
                        setDirty(false)
                        loadAll(p.id)
                      })
                    }
                  >
                    重新生成
                  </button>
                  <button
                    className="btn-primary btn-sm"
                    disabled={!dirty || !!busy}
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
                <Empty
                  text="还没有歌词"
                  action={
                    <button
                      className="btn-primary"
                      disabled={!!busy}
                      onClick={() => act('gen', () => api.generateLyrics(p.id, true), () => loadAll(p.id))}
                    >
                      生成歌词
                    </button>
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
                                className="flex-1 bg-transparent border-b border-transparent hover:border-line focus:border-brand outline-none py-1 text-[13px]"
                                value={ln}
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
                  {summary.by_type &&
                    Object.entries(summary.by_type).map(([k, v]: any) => (
                      <span key={k} className={v.pending ? 'tag-human' : 'tag'}>
                        {CHECK_TYPE_LABEL[k]} {v.pending ? `${v.pending} 条待确认` : '通过'}
                      </span>
                    ))}
                  <button
                    className="btn btn-sm"
                    disabled={!!busy || !lyric}
                    onClick={() => act('check', () => api.recheck(p.id), () => loadAll(p.id))}
                  >
                    重新检查
                  </button>
                </div>
              }
              bodyClass="p-4"
            >
              {checks.length === 0 ? (
                <Empty text="三项检查全部通过" />
              ) : (
                <div className="flex flex-col gap-2.5">
                  {checks.map((c) => (
                    <div
                      key={c.id}
                      className={clsx(
                        'border rounded-btn p-3',
                        c.status === 'pending' ? 'border-human/40 bg-human-soft/35' : 'border-line bg-white',
                      )}
                    >
                      <div className="flex items-center gap-2">
                        <span className={c.status === 'pending' ? 'tag-human' : 'tag'}>{CHECK_TYPE_LABEL[c.type]}</span>
                        <span className="text-[11px] text-ink-soft">
                          {c.section} 第 {c.line_no} 行
                        </span>
                        <span className="text-[11px] text-ink-faint truncate flex-1">{c.text}</span>
                        {c.status !== 'pending' && (
                          <span className="tag-brand">已处理</span>
                        )}
                      </div>
                      {c.evidence && <div className="mt-1.5 text-[11px] text-ink-soft">依据：{c.evidence}</div>}
                      {c.suggestions?.length > 0 && (
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                          {c.suggestions.map((s: string, i: number) => (
                            <button
                              key={i}
                              className="btn btn-sm"
                              disabled={!!busy}
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
                          <button
                            className="btn btn-sm"
                            disabled={!!busy}
                            onClick={() => act('resolve', () => api.resolveCheck(c.id), () => loadAll(p.id))}
                          >
                            保留原文
                          </button>
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
                      <span className="text-ink-faint">{v.source === 'ai' ? 'AI 生成' : '人工编辑'}</span>
                      <span className="text-ink-faint">
                        {flatLineCount(v.sections || [])} 行 · {relative(v.created_at)}
                      </span>
                      {v.id !== lyric?.id && (
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
            <Section title="模型配置" bodyClass="p-3">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-ink-faint">
                    <th className="text-left font-normal pb-1 w-6"></th>
                    <th className="text-left font-normal pb-1">模型</th>
                    <th className="text-right font-normal pb-1">本轮均分</th>
                    <th className="text-right font-normal pb-1">累计胜出</th>
                  </tr>
                </thead>
                <tbody>
                  {models.map((m) => (
                    <tr key={m.key} className="border-t border-line">
                      <td className="py-2">
                        <input
                          type="checkbox"
                          className="accent-[#0F5E57]"
                          checked={!!picked[m.key]}
                          onChange={(e) => setPicked({ ...picked, [m.key]: e.target.checked })}
                        />
                      </td>
                      <td className="py-2">
                        <div className="flex items-center gap-1.5">
                          <StateDot status={m.ready ? 'done' : 'queued'} />
                          <span className="truncate">{m.name}</span>
                        </div>
                        <div className="text-[10px] text-ink-faint">
                          {m.run_mode === 'local' ? '本地 4090' : 'API'}
                          {m.model ? ` · ${m.model}` : ''} · {m.ready ? '已就绪' : `降级 ${m.effective}`}
                        </div>
                      </td>
                      <td className="text-right">{m.avg_score || '—'}</td>
                      <td className="text-right font-medium text-brand">{m.wins}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="mt-3 border-t border-line pt-3 flex flex-col gap-2">
                <Field label="生成版数">
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      min={1}
                      max={4}
                      className="input w-20"
                      value={variants}
                      onChange={(e) => setVariants(Math.max(1, Math.min(4, Number(e.target.value))))}
                    />
                    <span className="text-[11px] text-ink-faint">
                      共 {Object.values(picked).filter(Boolean).length * variants} 个候选
                      （默认 1 首，试听后不满意再换模型）
                    </span>
                  </div>
                </Field>
                <button
                  className="btn-primary w-full"
                  disabled={
                    !!busy ||
                    !lyric ||
                    !['lyric_review', 'music_generating', 'music_review'].includes(p.status) ||
                    Object.values(picked).filter(Boolean).length === 0
                  }
                  title={
                    !lyric
                      ? '先生成并确认歌词'
                      : p.status === 'lyric_review'
                        ? '先生成？点此会先确认歌词再生成'
                        : ''
                  }
                  onClick={() =>
                    act(
                      'music',
                      () => {
                        const payload = {
                          models: Object.entries(picked).filter(([, v]) => v).map(([k]) => k),
                          variants,
                        }
                        // 还在"待确认歌词"时，一次点击完成"确认 + 按所选模型生成"，避免重复触发
                        if (p.status === 'lyric_review') return api.advance(p.id, payload)
                        return api.generateMusic(p.id, { ...payload, style_prompt: b.style_prompt })
                      },
                      () => {
                        setMsg(`已按所选模型派发生成任务（每模型 ${variants} 版）`)
                        loadAll(p.id)
                      },
                    )
                  }
                >
                  {p.status === 'lyric_review' ? '确认歌词并生成一首' : '用所选模型生成一首'}
                </button>
              </div>
            </Section>

            <Section
              title={`候选曲目 ${cands.length ? `(${cands.length})` : ''}`}
              right={<span className="h-sub">模型与版本直接可见</span>}
              bodyClass="p-3"
            >
              {cands.length === 0 ? (
                <Empty text="还没有候选曲目" />
              ) : (
                <div className="flex flex-col gap-2.5 max-h-[620px] overflow-y-auto pr-0.5">
                  {cands.map((c) => (
                    <CandidateCard
                      key={c.id}
                      c={c}
                      selected={p.selected_candidate_id === c.id}
                      playing={playing === c.id}
                      onPlay={() => setPlaying(c.id)}
                      onPause={() => setPlaying((v) => (v === c.id ? '' : v))}
                      busy={busy === c.id}
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
                        act(c.id, () => api.selectCandidate(p.id, c.id), () => {
                          setMsg(`已选定 ${c.model_name} 第 ${c.variant} 版`)
                          loadAll(p.id)
                        })
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
                  if (!sel) return <Empty text="—" />
                  return (
                    <div className="flex flex-col gap-1.5 text-[12px]">
                      <div className="flex items-center gap-2">
                        <StateDot status="done" />
                        <span className="font-medium">{sel.model_name}</span>
                        <span className="tag">第 {sel.variant} 版</span>
                      </div>
                      <div className="text-ink-soft">
                        BPM {Math.round(sel.bpm || 0)} · {fmtDuration(sel.duration_s)} · 匹配分 {sel.fit_score}
                      </div>
                      {sel.rating && (
                        <div className="text-ink-soft">
                          人工评分 咬字 {sel.rating.diction} / 旋律 {sel.rating.melody} / 编曲 {sel.rating.arrangement}
                          （合计 {sel.rating.total}）
                        </div>
                      )}
                      <div className="mt-2 border-t border-line pt-2 flex flex-col gap-1.5">
                        <div className="text-[11px] text-ink-faint">
                          试听不满意？换一个模型再生成一首（每次只出一首）
                        </div>
                        <div className="flex flex-wrap gap-2">
                          {models
                            .filter(
                              (m) => m.enabled && m.ready && m.key !== sel.model_key,
                            )
                            .map((m) => (
                              <button
                                key={m.key}
                                className="btn btn-sm"
                                disabled={!!busy}
                                onClick={() =>
                                  act(
                                    'regen',
                                    () =>
                                      api.generateMusic(p.id, {
                                        models: [m.key],
                                        variants: 1,
                                      }),
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
              驳回到：{t}
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

const GUIDE_STEPS = ['创作简报', '审核歌词', '试听选曲', '配图审核', '精剪交付']

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
    desc: '先看下面「自动检查」里的黄色卡片 —— 那是 AI 查出的史实/字数/韵脚疑点，逐条点「采纳」或「保留原文」。都没有问题了，再点右上角「确认歌词，进入生曲」。',
  },
  music_generating: {
    step: 2,
    title: 'AI 正在生曲',
    desc: '默认只生成 1 首（省钱）。Mureka 一轮约 2–4 分钟，完成后自动进入选曲。',
    auto: true,
  },
  music_review: {
    step: 3,
    title: '试听并选定一版',
    desc: '在右侧「候选曲目」点播放键试听，满意的那版点「选定这一版」，再点右上角「确认选曲，进入配图」。想比较可以点「换 Mureka 7.6 生成 1 首」再出一版。',
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
