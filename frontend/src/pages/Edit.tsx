import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import clsx from 'clsx'
import api from '../lib/api'
import ProjectReview from '../components/ProjectReview'
import { useWorkHeartbeat } from '../lib/heartbeat'
import { Banner, Empty, Modal, PageHeader, ProgressBar, Section, Spinner, StateDot } from '../components/ui'
import Stepper from '../components/Stepper'
import Timeline, { usePeaks } from '../components/Timeline'
import { VARIANT_LABEL, fmtTime, relative } from '../lib/format'

export default function EditPage() {
  const { projectId } = useParams()
  // F1-11 有效工时：页面在前台且有操作时每 30 秒报一次
  useWorkHeartbeat(projectId)
  const nav = useNavigate()
  const [detail, setDetail] = useState<any>(null)
  const [tl, setTl] = useState<any>(null)
  const [clips, setClips] = useState<any[]>([])
  const [selected, setSelected] = useState<string>('')
  const [cands, setCands] = useState<any[]>([])
  const [renders, setRenders] = useState<any[]>([])
  // F3-7 内部评审预览（720p，不算交付）
  const [preview, setPreview] = useState<any>(null)
  // 默认全选「带字幕」的版本 —— 文旅 MV 主交付物必须有歌词。
  // （无字幕的 clean 版留给二剪，默认不勾。）
  const [variants, setVariants] = useState<string[]>(
    ['landscape', 'portrait', 'sub_zh_en', 'short', 'highlight30', 'essence60'],
  )
  // 可选版本列表以接口为准（后端是单一来源），接口没回来时用本地兜底
  // （兜底里刻意不含历史 key sub_zh —— 它和现在的 landscape 等价，不该出现在勾选项里）
  const [variantOpts, setVariantOpts] = useState<any[]>(
    [
      { key: 'landscape', label: VARIANT_LABEL.landscape, subs: 'zh' },
      { key: 'portrait', label: VARIANT_LABEL.portrait, subs: 'zh' },
      { key: 'sub_zh_en', label: VARIANT_LABEL.sub_zh_en, subs: 'zh_en' },
      { key: 'short', label: VARIANT_LABEL.short, subs: 'zh_en' },
      { key: 'highlight30', label: VARIANT_LABEL.highlight30, subs: 'zh' },
      { key: 'essence60', label: VARIANT_LABEL.essence60, subs: 'zh' },
      { key: 'clean', label: VARIANT_LABEL.clean, subs: null },
    ],
  )
  const [playing, setPlaying] = useState(false)
  const [time, setTime] = useState(0)
  const [aspect, setAspect] = useState<'16:9' | '9:16'>('16:9')
  const [subtitles, setSubtitles] = useState(true)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [projects, setProjects] = useState<any[] | null>(null)
  const [showSource, setShowSource] = useState(false)
  const [showReview, setShowReview] = useState(false)

  const audio = useRef<HTMLAudioElement>(null)
  const video = useRef<HTMLVideoElement>(null)
  const activeRef = useRef<string>('')

  const peaks = usePeaks(tl?.audio_url || '')

  // 同样必须自己兜住异常：在 useEffect 里直接调用，不 catch 会变成
  // unhandled rejection，页面上什么都不提示（旧链接失效时就是这个表现）。
  const load = useCallback(async (id: string) => {
    try {
      const [d, t, r, v, pv] = await Promise.all([
        api.project(id), api.timeline(id), api.renders(id), api.renderVariants(),
        api.preview(id).catch(() => null),
      ])
      setPreview(pv)
      setDetail(d)
      setTl(t.timeline)
      setClips(t.clips || [])
      setRenders(r || [])
      if (Array.isArray(v) && v.length) setVariantOpts(v)
      setErr('')
    } catch (e: any) {
      setErr(e?.message || String(e))
    }
  }, [])

  useEffect(() => {
    if (projectId) load(projectId)
    else
      api
        .projects()
        .then(setProjects)
        .catch((e: any) => setErr(e?.message || String(e)))
  }, [projectId, load])

  // 生成/合成中轮询
  const status = detail?.project?.status
  const previewRunning = ['queued', 'running'].includes(preview?.job?.status)
  useEffect(() => {
    if (!projectId) return
    if (!['audio_analyzing', 'rough_cutting', 'rendering'].includes(status) && !previewRunning) return
    let alive = true
    let t: number | undefined
    const tick = async () => {
      await load(projectId)
      if (alive) t = window.setTimeout(tick, 3000)
    }
    t = window.setTimeout(tick, 3000)
    return () => {
      alive = false
      if (t) clearTimeout(t)
    }
  }, [projectId, status, previewRunning, load])

  const activeClip = useMemo(() => {
    return clips.find((c) => time >= c.start_s && time < c.end_s) || clips[Math.floor(clips.length * (time / Math.max(1, tl?.duration_s))) ]
  }, [clips, time, tl])

  // 预览：音频驱动时间轴，视频按当前片段切换并同步到对应入点
  useEffect(() => {
    const v = video.current
    if (!v || !activeClip) return
    if (activeRef.current !== activeClip.id) {
      activeRef.current = activeClip.id
      v.src = activeClip.video_url
      v.load()
    }
  }, [activeClip])

  useEffect(() => {
    const v = video.current
    if (!v || !activeClip) return
    const target = (activeClip.in_s || 0) + Math.max(0, time - activeClip.start_s)
    if (Math.abs(v.currentTime - target) > 0.35) v.currentTime = Math.min(target, (v.duration || target) - 0.05)
    if (playing) v.play().catch(() => undefined)
    else v.pause()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [time, playing, activeClip?.id])

  const toggle = () => {
    const a = audio.current
    if (!a) return
    if (playing) {
      a.pause()
      video.current?.pause()
      setPlaying(false)
    } else {
      a.play().catch(() => undefined)
      setPlaying(true)
    }
  }

  const seek = (t: number) => {
    setTime(t)
    if (audio.current) audio.current.currentTime = t
  }

  const loadCandidates = useCallback(
    async (clipId: string) => {
      if (!projectId || !clipId) return
      try {
        const r = await api.clipCandidates(projectId, clipId)
        setCands(r.items || [])
      } catch {
        setCands([])
      }
    },
    [projectId],
  )

  useEffect(() => {
    if (selected) loadCandidates(selected)
  }, [selected, loadCandidates])

  const act = async (key: string, fn: () => Promise<any>, after?: (r: any) => void) => {
    setBusy(key)
    setErr('')
    try {
      const r = await fn()
      after?.(r)
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy('')
    }
  }

  if (!projectId) {
    return (
      <>
        <PageHeader title="剪辑工作台" subtitle="选择一个项目进入" />
        <div className="p-6 grid grid-cols-3 gap-4">
          {projects === null && !err && <Spinner />}
          {err && <Banner tone="error">{err}</Banner>}
          {(projects || []).map((p) => (
            <button key={p.id} className="card p-4 text-left hover:shadow-sm" onClick={() => nav(`/projects/${p.id}/edit`)}>
              <div className="text-[13px] font-medium">{p.name}</div>
              <div className="mt-1 text-[11px] text-ink-faint">{p.state_label} · {p.destination}</div>
            </button>
          ))}
          {projects !== null && projects.length === 0 && <Empty text="还没有项目" />}
        </div>
      </>
    )
  }

  if (!detail) {
    // 同 Lyrics：加载失败必须给错误和出路，不能只转圈
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
    return <div className="p-6"><Spinner /></div>
  }
  const p = detail.project
  const selectedClip = clips.find((c) => c.id === selected)
  const rendering = renders.some((r) => r.status === 'running' || r.status === 'queued')
  const previewBusy = previewRunning

  return (
    <>
      {projectId && <ProjectReview projectId={projectId} open={showReview} onClose={() => setShowReview(false)} />}
      <PageHeader
        crumbs={
          <>
            <Link to="/dashboard" className="hover:underline">生产看板</Link>
            <span className="mx-1">/</span>
            <span>{p.name}</span>
          </>
        }
        title="剪辑工作台"
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            <span className={p.waiting_human ? 'tag-human' : 'tag-brand'}>{p.state_label}</span>
            <span>{p.status_note}</span>
            {tl && <span className="text-ink-faint">BPM {Math.round(tl.bpm)} · {fmtTime(tl.duration_s)} · {clips.length} 个片段</span>}
            {tl?.align_engine && (
              <span
                className={tl.align_engine === 'whisper' ? 'tag-brand' : 'tag-human'}
                title={tl.align_note || ''}
              >
                {tl.align_engine === 'whisper' ? '歌词对齐：来自歌声' : '歌词对齐：按字数估算'}
              </span>
            )}
          </span>
        }
        actions={
          p.locked ? (
            <span className="flex items-center gap-2">
              <span className="text-[11px] text-ink-faint">已交付 · 只读（成片可下载，可回填投放数据）</span>
              <button className="btn" onClick={() => setShowReview(true)}>
                项目复盘
              </button>
            </span>
          ) : (
          <>
            {p.reject_targets?.length > 0 && (
              <button className="btn" disabled={!!busy} onClick={() => act('reject', () => api.reject(p.id, p.reject_targets[0]), () => load(p.id))}>
                驳回到「{p.reject_target_labels?.[p.reject_targets[0]] || p.reject_targets[0]}」
              </button>
            )}
            <button
              className="btn"
              disabled={!!busy || !tl || p.status === 'rendering'}
              title="重跑一次语音对齐（ASR），让歌词时间轴与歌声严格同步，然后按新时间轴重新卡点"
              onClick={() => act('align', () => api.realignTimeline(p.id), () => { setMsg('已派发对齐任务（约 1 分钟）'); load(p.id) })}
            >
              重做歌词对齐
            </button>
            <button
              className="btn"
              disabled={!!busy || !tl || p.status === 'rendering'}
              onClick={() => act('resnap', () => api.resnap(p.id), () => { setMsg('已重新自动卡点（保留人工锁定）'); load(p.id) })}
            >
              重新自动卡点
            </button>
            {p.status === 'edit_review' && (
              <button
                className="btn-human"
                disabled={!!busy || clips.length === 0 || !(detail.gate?.ok ?? true)}
                title={(detail.gate?.reasons || []).join('；')}
                onClick={() => act('advance', () => api.advance(p.id, { variants }), () => { setMsg('已派发合成任务'); load(p.id) })}
              >
                确认精剪，生成多版本
              </button>
            )}
            {p.status === 'rendering' && (
              // 已确认过精剪：不再有确认按钮，明确告诉用户在合成、合成完自动交付（E2E 反馈「找不到确认入口」）
              <span className="tag-brand" title="多版本合成完成并通过出片自检后，项目自动进入「已交付」">
                已确认精剪 · 正在合成 {renders.filter((r) => r.status === 'done').length}/{renders.length} 个版本，完成后自动交付
              </span>
            )}
          </>
          )
        }
      />

      <div className="px-6 py-4 border-b border-line bg-page">
        <Stepper column={p.column} status={p.status} />
      </div>

      <div className="p-6 flex flex-col gap-4">
        {detail.music_is_mock && (
          <Banner tone="warn">
            本片的音乐是<b>占位音频</b>（模型未配置或测试环境禁用了真实生曲），听起来是单调的嗡嗡声，不是真实歌曲，不能用于投放。
            需要真实音乐请回到词曲工作台，用已配置的音乐模型重新生成并选定。
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
        {detail.analyze_error && (
          // F3-1：对齐失败不静默降级，由人决定重试还是接受估算（估算的字幕可能与歌声不同步）
          <Banner tone="error">
            <div className="flex flex-col gap-2">
              <div>
                <span className="font-medium">歌词与歌声对齐失败，项目暂停在「音频分析」。</span>
                {String(detail.analyze_error).replace(/^AlignError:\s*/, '')}
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  className="btn btn-sm"
                  disabled={!!busy}
                  onClick={() => act('retry-align', () => api.retryAnalyze(p.id, false), () => { setMsg('已重新对齐（约 1–2 分钟）'); load(p.id) })}
                >
                  重试对齐
                </button>
                <button
                  className="btn btn-sm"
                  disabled={!!busy}
                  title="不跟歌声对齐，按每句字数平均分配时间。字幕可能明显早于或晚于演唱，只建议内部预览用"
                  onClick={() => act('estimate', () => api.retryAnalyze(p.id, true), () => { setMsg('已按字数估算时间轴'); load(p.id) })}
                >
                  按字数估算继续（字幕可能不同步）
                </button>
              </div>
            </div>
          </Banner>
        )}
        {!tl && (
          <Banner tone="warn">
            还没有时间线。{p.status === 'edit_review' ? '' : '需要先选定曲目并完成音频分析。'}
            <button
              className="ml-2 underline"
              disabled={!!busy}
              onClick={() => act('auto', () => api.autoTimeline(p.id, true), () => load(p.id))}
            >
              立即执行音频分析与自动粗剪
            </button>
          </Banner>
        )}

        <div className="grid grid-cols-[minmax(0,1fr)_340px] gap-5 items-start">
          <div className="flex flex-col gap-4">
            <Section
              title="预览"
              right={
                <div className="flex items-center gap-2">
                  <button className={clsx('btn btn-sm', aspect === '16:9' && 'border-brand text-brand')} onClick={() => setAspect('16:9')}>
                    横版 16:9
                  </button>
                  <button className={clsx('btn btn-sm', aspect === '9:16' && 'border-brand text-brand')} onClick={() => setAspect('9:16')}>
                    竖版 9:16
                  </button>
                  <button className={clsx('btn btn-sm', subtitles && 'border-brand text-brand')} onClick={() => setSubtitles((v) => !v)}>
                    字幕 {subtitles ? '开' : '关'}
                  </button>
                </div>
              }
              bodyClass="p-4"
            >
              <div className="flex gap-4">
                <div
                  className={clsx(
                    'bg-black rounded-card overflow-hidden relative shrink-0',
                    aspect === '16:9' ? 'w-[460px] aspect-video' : 'w-[212px] aspect-[9/16]',
                  )}
                >
                  <video ref={video} className="w-full h-full object-cover" muted playsInline />
                  {subtitles && activeClip && (
                    <div className="absolute inset-x-3 bottom-8 text-center">
                      <div className="inline-block bg-black/55 text-white text-[12px] px-2 py-1 rounded-btn">
                        {activeClip.lyric_line}
                      </div>
                    </div>
                  )}
                  {!activeClip && (
                    <div className="absolute inset-0 flex items-center justify-center text-white/60 text-[12px]">
                      无预览片段
                    </div>
                  )}
                </div>

                <div className="flex-1 min-w-0 flex flex-col gap-2">
                  <div className="flex items-center gap-2">
                    <button className="btn btn-sm w-16" onClick={toggle} disabled={!tl}>
                      {playing ? '暂停' : '播放'}
                    </button>
                    <span className="text-[12px] text-ink-soft font-mono">
                      {fmtTime(time, true)} / {fmtTime(tl?.duration_s || 0)}
                    </span>
                    <button className="btn btn-sm ml-auto" onClick={() => seek(0)} disabled={!tl}>
                      回到开头
                    </button>
                  </div>
                  <div className="text-[11px] text-ink-soft">当前片段</div>
                  <div className="text-[12px] text-ink break-all leading-relaxed">
                    {activeClip ? activeClip.lyric_line : '—'}
                  </div>
                  <div className="text-[11px] text-ink-faint">
                    画面描述：{activeClip?.scene_prompt || '—'}
                  </div>
                  <div className="text-[11px] text-ink-faint">
                    素材：{activeClip?.asset_name || '—'}（{fmtTime(activeClip?.in_s)}–{fmtTime(activeClip?.out_s)}）
                  </div>
                  <button
                    className="btn btn-sm self-start mt-1"
                    onClick={() => setShowSource(true)}
                    disabled={!activeClip}
                  >
                    查看原素材
                  </button>
                </div>
              </div>

              {tl?.audio_url && (
                <audio
                  ref={audio}
                  src={tl.audio_url}
                  onTimeUpdate={(e) => setTime((e.target as HTMLAudioElement).currentTime)}
                  onEnded={() => setPlaying(false)}
                  className="hidden"
                />
              )}
            </Section>

            <Section
              title="时间线"
              right={<span className="h-sub">点击片段选中 · 拖动两端改时长 · 橙色刻度为强拍</span>}
              bodyClass="p-4 overflow-x-auto"
            >
              {!tl || clips.length === 0 ? (
                <Empty text="暂无时间线" />
              ) : (
                <div className="min-w-[720px]">
                  <Timeline
                    duration={tl.duration_s}
                    clips={clips}
                    lyricLines={tl.lyric_lines || []}
                    beats={tl.beats || []}
                    strongBeats={tl.strong_beats || []}
                    selectedId={selected}
                    currentTime={time}
                    onSelect={(id) => {
                      const c = clips.find((x) => x.id === id)
                      if (c) seek(c.start_s)
                      setSelected(id)
                    }}
                    onSeek={seek}
                    onChange={(clipId, patch) => {
                      setClips((prev) => prev.map((c) => (c.id === clipId ? { ...c, ...patch } : c)))
                      api.updateClip(p.id, clipId, patch).catch(() => undefined)
                    }}
                    peaks={peaks}
                  />
                </div>
              )}
            </Section>

            {tl && (
              <Section
                title="内部评审预览"
                right={
                  <button
                    className="btn btn-sm"
                    disabled={!!busy || p.locked || previewBusy}
                    title="720p 低清、快速导出，用于内部评审；不是交付成片，不做出片自检"
                    onClick={() => act('preview', () => api.makePreview(p.id), () => { setMsg('已开始导出 720p 预览'); load(p.id) })}
                  >
                    {previewBusy ? `导出中 ${preview?.job?.progress || 0}%` : preview?.render ? '重新导出 720p 预览' : '导出 720p 预览'}
                  </button>
                }
                bodyClass="p-3"
              >
                {preview?.render?.status === 'done' && preview.render.url ? (
                  <div className="flex flex-col gap-1.5">
                    <video className="w-full max-w-[520px] rounded-btn bg-black" controls preload="metadata" src={preview.render.url} />
                    <div className="text-[11px] text-ink-faint">
                      720p · {preview.render.size_mb} MB
                      {preview.job?.result?.elapsed_s ? ` · 导出用时 ${preview.job.result.elapsed_s} 秒` : ''}
                      {' · '}{relative(preview.render.updated_at)}
                      {' · 粗剪改动后需重新导出'}
                    </div>
                  </div>
                ) : preview?.job?.status === 'failed' ? (
                  <div className="text-[12px] text-human">导出失败：{preview.job.error}</div>
                ) : (
                  <div className="text-[12px] text-ink-faint">
                    {previewBusy ? '正在导出（3 分钟的片约 1–2 分钟）…' : '精剪过程中可随时导出低清预览，发给同事评审；不影响项目状态。'}
                  </div>
                )}
              </Section>
            )}

            <Section
              title="成片"
              right={
                <div className="flex items-center gap-2">
                  {rendering && <span className="h-sub">合成中…</span>}
                  <button
                    className="btn btn-sm"
                    disabled={!!busy || !tl || clips.length === 0}
                    onClick={() => act('render', () => api.render(p.id, { variants }), () => { setMsg('已派发合成任务'); load(p.id) })}
                  >
                    生成所选版本
                  </button>
                </div>
              }
              bodyClass="p-4"
            >
              <div className="flex flex-wrap gap-2 mb-1">
                {variantOpts.map((o: any) => (
                  <label
                    key={o.key}
                    title={o.subs ? '带歌词字幕' : '无字幕，供二次剪辑用'}
                    className={clsx('flex items-center gap-1.5 px-2.5 h-8 rounded-btn border text-[12px] cursor-pointer', variants.includes(o.key) ? 'border-brand bg-brand-soft/50' : 'border-line')}
                  >
                    <input
                      type="checkbox"
                      className="accent-[#0F5E57]"
                      checked={variants.includes(o.key)}
                      onChange={(e) => setVariants(e.target.checked ? [...variants, o.key] : variants.filter((x: string) => x !== o.key))}
                    />
                    {o.label}
                  </label>
                ))}
              </div>

              {/* 主交付物必须带歌词 —— 用户曾下载完横版才发现没字幕 */}
              {variants.length > 0 && !variants.some((k: string) => variantOpts.find((o: any) => o.key === k)?.subs) && (
                <div className="mb-3 text-[11px] text-human">
                  所选版本都不带歌词字幕，建议至少勾一个带字幕的版本（文旅 MV 的字幕是主内容）。
                </div>
              )}

              {renders.length === 0 ? (
                <Empty text="还没有成片" />
              ) : (
                <div className="grid grid-cols-2 gap-3">
                  {renders.map((r) => (
                    <div key={r.id} className="border border-line rounded-btn p-3">
                      <div className="flex items-center gap-2">
                        <StateDot status={r.status} />
                        <span className="text-[12px] font-medium">{VARIANT_LABEL[r.variant] || r.variant}</span>
                        <span className="ml-auto text-[10px] text-ink-faint">{r.size_mb ? `${r.size_mb} MB` : ''}</span>
                      </div>
                      {r.status === 'running' && <div className="mt-2"><ProgressBar value={r.progress} /></div>}
                      {r.status === 'done' && r.url && (
                        <>
                          <video className="mt-2 w-full rounded-btn bg-black" controls preload="metadata" src={r.url} />
                          <div className="mt-1.5 flex items-center gap-2">
                            <a className="btn btn-sm" href={r.url} download>
                              下载
                            </a>
                            <a className="btn btn-sm" href={r.url} target="_blank" rel="noreferrer">
                              新窗口打开
                            </a>
                            <span className="text-[10px] text-ink-faint ml-auto">{relative(r.updated_at)}</span>
                          </div>
                          {/* 出片自检（PRD V1.1 F3-10）：不通过就不交付 */}
                          {r.qc?.checks?.length > 0 && (
                            <div className="mt-2 flex flex-wrap gap-1">
                              {r.qc.checks.map((c: any) => (
                                <span
                                  key={c.name}
                                  className={c.ok ? 'tag' : 'tag-human'}
                                  title={c.detail}
                                >
                                  {c.ok ? '✓' : '✗'} {c.name}
                                </span>
                              ))}
                              {!r.qc.ok && (
                                <div className="w-full text-[10px] text-human-dark mt-1">
                                  {r.qc.checks.filter((c: any) => !c.ok).map((c: any) => `${c.name}：${c.detail}`).join('；')}
                                </div>
                              )}
                            </div>
                          )}
                        </>
                      )}
                      {r.status === 'failed' && (
                        <div className="mt-1.5 text-[10px] text-human-dark break-all">{r.error}</div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </Section>
          </div>

          {/* 右栏：镜头候选 */}
          <div className="flex flex-col gap-4">
            <Section
              title="镜头候选"
              right={<span className="h-sub">Top4</span>}
              bodyClass="p-3"
            >
              {!selectedClip ? (
                <Empty text="在时间线上点选一个片段" />
              ) : (
                <div className="flex flex-col gap-3">
                  <div className="text-[11px] text-ink-soft leading-relaxed">
                    <div className="text-ink font-medium">{selectedClip.lyric_line}</div>
                    <div className="text-ink-faint mt-0.5">{selectedClip.scene_prompt}</div>
                  </div>

                  {selectedClip.video_url && (
                    <video
                      key={selectedClip.id}
                      className="w-full rounded-btn bg-black"
                      controls
                      preload="metadata"
                      src={selectedClip.video_url}
                    />
                  )}

                  <div className="flex items-center gap-2">
                    <button
                      className={clsx('btn btn-sm', selectedClip.locked && 'border-human text-human')}
                      disabled={!!busy}
                      onClick={() =>
                        act('lock', () => api.updateClip(p.id, selectedClip.id, { locked: !selectedClip.locked }), () => load(p.id))
                      }
                    >
                      {selectedClip.locked ? '已锁定（重新卡点不改动）' : '锁定该片段'}
                    </button>
                  </div>

                  <div className="flex flex-col gap-2 max-h-[420px] overflow-y-auto">
                    {cands.map((c) => (
                      <div
                        key={c.asset_id}
                        className={clsx(
                          'border rounded-btn p-2 flex gap-2',
                          c.in_use ? 'border-brand bg-brand-soft/40' : 'border-line',
                        )}
                      >
                        {c.thumb_url ? (
                          <img src={c.thumb_url} alt="" className="w-[74px] h-[42px] object-cover rounded-[6px] shrink-0" />
                        ) : (
                          <div className="w-[74px] h-[42px] bg-page rounded-[6px] shrink-0" />
                        )}
                        <div className="min-w-0 flex-1">
                          <div className="text-[11px] font-medium truncate">{c.name}</div>
                          <div className="text-[10px] text-ink-faint">
                            匹配 {c.score} · {c.duration_s?.toFixed(1)}s · {c.destination || '无目的地'}
                          </div>
                          <div className="mt-1 flex items-center gap-1.5">
                            <span className={c.license_status === 'licensed' ? 'tag' : 'tag-human'}>
                              {c.license_status === 'licensed' ? '已授权' : c.license_status === 'expiring' ? '即将到期' : '缺授权'}
                            </span>
                            <button
                              className="btn btn-sm ml-auto"
                              disabled={!!busy || c.in_use}
                              onClick={() =>
                                act('swap', () => api.updateClip(p.id, selectedClip.id, { asset_id: c.asset_id }), () => {
                                  setMsg('已替换镜头')
                                  load(p.id)
                                  loadCandidates(selectedClip.id)
                                })
                              }
                            >
                              {c.in_use ? '使用中' : '替换'}
                            </button>
                          </div>
                        </div>
                      </div>
                    ))}
                    {cands.length === 0 && <Empty text="没有可用候选（缺授权或不可用的素材已排除）" />}
                  </div>
                </div>
              )}
            </Section>

            <Section title="投放数据回填" bodyClass="p-3">
              <MetricForm projectId={p.id} onDone={() => load(p.id)} metrics={detail.metrics || []} />
            </Section>

            <Section title="成本与耗时" bodyClass="p-3">
              <ProjectCost projectId={p.id} total={p.total_cost} />
            </Section>
          </div>
        </div>
      </div>

      <Modal open={showSource} title={`原素材：${activeClip?.asset_name || ''}`} onClose={() => setShowSource(false)} width="max-w-[820px]">
        {activeClip?.video_url && (
          <video className="w-full rounded-btn bg-black" controls autoPlay src={activeClip.video_url} />
        )}
      </Modal>
    </>
  )
}

function MetricForm({ projectId, metrics, onDone }: { projectId: string; metrics: any[]; onDone: () => void }) {
  const [platform, setPlatform] = useState('YouTube')
  const [url, setUrl] = useState('')
  const [views, setViews] = useState(0)
  const [completion, setCompletion] = useState(0)
  const [engagement, setEngagement] = useState(0)
  const [busy, setBusy] = useState(false)

  return (
    <div className="flex flex-col gap-2">
      <div className="grid grid-cols-2 gap-2">
        <select className="input" value={platform} onChange={(e) => setPlatform(e.target.value)}>
          <option>YouTube</option>
          <option>TikTok</option>
          <option>Facebook</option>
          <option>X</option>
        </select>
        <input className="input" placeholder="链接" value={url} onChange={(e) => setUrl(e.target.value)} />
        <input type="number" className="input" placeholder="7 日播放" value={views} onChange={(e) => setViews(Number(e.target.value))} />
        <input type="number" step="0.01" className="input" placeholder="完播率 0-1" value={completion} onChange={(e) => setCompletion(Number(e.target.value))} />
      </div>
      <input type="number" step="0.01" className="input" placeholder="互动率 0-1" value={engagement} onChange={(e) => setEngagement(Number(e.target.value))} />
      <button
        className="btn btn-sm"
        disabled={busy || !url}
        onClick={async () => {
          setBusy(true)
          try {
            await api.addMetric(projectId, { platform, url, views_7d: views, completion, engagement })
            setUrl('')
            onDone()
          } finally {
            setBusy(false)
          }
        }}
      >
        保存一条
      </button>
      {metrics.length > 0 && (
        <div className="mt-1 flex flex-col gap-1">
          {metrics.map((m) => (
            <div key={m.id} className="text-[11px] text-ink-soft flex items-center gap-2">
              <span className="tag">{m.platform}</span>
              <span>{m.views_7d} 播放</span>
              <span className="truncate">{m.url}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function ProjectCost({ projectId, total }: { projectId: string; total: number }) {
  const [data, setData] = useState<any>(null)
  useEffect(() => {
    api.projectCalls(projectId).then(setData).catch(() => setData(null))
  }, [projectId])
  if (!data) return <Spinner />
  return (
    <div className="flex flex-col gap-2 text-[11px]">
      <div className="flex items-center justify-between">
        <span className="text-ink-soft">累计 API 费用</span>
        <span className="font-medium text-ink">¥{(total || 0).toFixed(2)}</span>
      </div>
      <table className="w-full">
        <thead>
          <tr className="text-ink-faint">
            <th className="text-left font-normal pb-1">类型</th>
            <th className="text-right font-normal pb-1">次数</th>
            <th className="text-right font-normal pb-1">耗时</th>
            <th className="text-right font-normal pb-1">费用</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(data.by_kind || {}).map(([k, v]: any) => (
            <tr key={k} className="border-t border-line">
              <td className="py-1">{k}</td>
              <td className="text-right">{v.calls}{v.errors ? `（失败 ${v.errors}）` : ''}</td>
              <td className="text-right">{v.elapsed_s}s</td>
              <td className="text-right">¥{v.cost.toFixed(2)}</td>
            </tr>
          ))}
          {Object.keys(data.by_kind || {}).length === 0 && (
            <tr>
              <td colSpan={4} className="py-2 text-ink-faint">暂无调用记录</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
