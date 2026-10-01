import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import clsx from 'clsx'
import api from '../lib/api'
import { Banner, Empty, Modal, PageHeader, ProgressBar, Section, Spinner } from '../components/ui'

const STATUS_TONE: Record<string, string> = {
  draft: 'tag',
  generating: 'tag-brand',
  pending: 'tag-human',
  approved: 'tag-brand',
  rejected: 'tag-human',
}

export default function StoryboardPage() {
  const { projectId } = useParams()
  const nav = useNavigate()
  const [data, setData] = useState<any>(null)
  const [detail, setDetail] = useState<any>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [provider, setProvider] = useState('')
  const [projects, setProjects] = useState<any[]>([])
  const [editShot, setEditShot] = useState<any>(null)
  const [draftPrompt, setDraftPrompt] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  const uploadFor = useRef<string>('')

  const load = useCallback(
    async (id: string) => {
      const [d, sb] = await Promise.all([api.project(id), api.storyboard(id)])
      setDetail(d)
      setData(sb)
      if (!provider && sb.provider) setProvider(sb.provider)
    },
    [provider],
  )

  useEffect(() => {
    if (projectId) {
      load(projectId).catch((e) => setErr(e?.message || String(e)))
      return
    }
    // 侧栏直接点「配图审核」时没有项目 id —— 给一个项目入口，别让用户看到空页
    api
      .projects()
      .then(setProjects)
      .catch((e) => setErr(e?.message || String(e)))
  }, [projectId, load])

  const items: any[] = data?.items || []
  const prog = data?.progress || { total: 0, approved: 0, pending: 0, rejected: 0 }
  const providers: any[] = data?.providers || []

  // 有句子还在排队/出图中就自动轮询 —— 出图是长任务，用户不该手动刷新
  const working = useMemo(
    () => items.some((x) => x.status === 'draft' || x.status === 'generating'),
    [items],
  )
  useEffect(() => {
    if (!projectId || !working) return
    const t = setInterval(() => load(projectId).catch(() => {}), 6000)
    return () => clearInterval(t)
  }, [projectId, working, load])

  async function act(key: string, fn: () => Promise<any>, after?: (r: any) => void) {
    setBusy(key)
    setErr('')
    try {
      const r = await fn()
      after?.(r)
    } catch (e: any) {
      setErr(e?.message || String(e))
    } finally {
      setBusy('')
    }
  }

  const p = detail || {}
  const isHuman = (p.state_label || '').includes('待')

  if (!projectId) {
    return (
      <>
        <PageHeader title="配图审核" subtitle="选择一个项目进入" />
        <div className="p-6">
          {err && <Banner tone="error" onClose={() => setErr('')}>{err}</Banner>}
          {!projects.length ? (
            <Empty text="还没有项目" action={<Link className="btn-primary" to="/dashboard">去生产看板新建</Link>} />
          ) : (
            <div className="grid grid-cols-3 gap-4">
              {projects.map((x) => (
                <Link
                  key={x.id}
                  to={`/projects/${x.id}/storyboard`}
                  className="card p-4 hover:border-brand transition-colors"
                >
                  <div className="text-[13px] font-medium">{x.name}</div>
                  <div className="text-[11px] text-ink-faint mt-1">
                    {x.destination || '—'} · {x.state_label}
                  </div>
                </Link>
              ))}
            </div>
          )}
        </div>
      </>
    )
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
            <span>{p.name || '—'}</span>
          </>
        }
        title="逐句配图审核"
        subtitle={
          <span className="flex items-center gap-2">
            <span className={isHuman ? 'tag-human' : 'tag-brand'}>{p.state_label || '—'}</span>
            <span>每句歌词配一张真实景区图，审过才进卡点合成</span>
          </span>
        }
        actions={
          <>
            <button
              className="btn"
              disabled={!!busy}
              onClick={() =>
                act('plan', () => api.storyboardPlan(projectId!, true), (r) => {
                  setMsg(`已重新提炼：${r.note}`)
                  load(projectId!)
                })
              }
            >
              重新提炼景区
            </button>
            <button
              className="btn-primary"
              disabled={!!busy || !providers.some((x) => x.key === provider && x.ready)}
              title={
                providers.some((x) => x.key === provider && x.ready)
                  ? ''
                  : '所选生图模型未配置（缺 Key），请在 .env 里补上'
              }
              onClick={() =>
                act('enqueue', () => api.storyboardEnqueue(projectId!, provider), () => {
                  setMsg('已入队：逐句出图进行中，页面会自动刷新')
                  load(projectId!)
                })
              }
            >
              {busy === 'enqueue' ? '提交中…' : '开始/继续出图'}
            </button>
            <button
              className="btn"
              disabled={!!busy || !prog.pending}
              onClick={() =>
                act('all', () => api.storyboardApproveAll(projectId!), (r) => {
                  setMsg(`已通过 ${r.approved} 句`)
                  load(projectId!)
                })
              }
            >
              一键全部通过
            </button>
            <button
              className="btn-human"
              disabled={!!busy || !prog.total || prog.approved < prog.total}
              title={prog.approved < prog.total ? '还有未通过的句子' : ''}
              onClick={() =>
                act('advance', () => api.advance(projectId!), () => {
                  setMsg('配图已确认，进入卡点合成')
                  load(projectId!)
                })
              }
            >
              确认配图，进入卡点
            </button>
          </>
        }
      />

      <div className="p-6 flex flex-col gap-4">
        {err && (
          <Banner tone="error" onClose={() => setErr('')}>
            {err}
          </Banner>
        )}
        {msg && (
          <Banner tone="ok" onClose={() => setMsg('')}>
            {msg}
          </Banner>
        )}

        <div className="card px-4 py-3 flex flex-wrap items-center gap-x-5 gap-y-2">
          <div className="text-[12px]">
            进度 <span className="font-medium">{prog.approved}</span> / {prog.total} 句已通过
          </div>
          {!!prog.pending && <span className="tag-human">待审核 {prog.pending}</span>}
          {!!prog.rejected && <span className="tag-human">需重做 {prog.rejected}</span>}
          {!!prog.by_status?.draft && <span className="tag">待生成 {prog.by_status.draft}</span>}
          <div className="flex-1 min-w-[160px]">
            <ProgressBar value={prog.total ? (prog.approved / prog.total) * 100 : 0} />
          </div>
          <label className="flex items-center gap-2 text-[11px] text-ink-soft">
            生图模型
            <select
              className="input h-8 py-0 text-[12px]"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
            >
              {providers.map((x) => (
                <option key={x.key} value={x.key} disabled={!x.ready}>
                  {x.label}
                  {x.ready ? ` · ${x.size}` : '（未配置）'}
                </option>
              ))}
            </select>
          </label>
          {working && <span className="tag-brand">出图中，自动刷新…</span>}
        </div>

        {!items.length ? (
          <Empty
            text="还没有分镜。先「重新提炼景区」，再点「开始/继续出图」。"
          />
        ) : (
          <div className="grid grid-cols-3 gap-4">
            {items.map((s) => (
              <ShotCard
                key={s.id}
                shot={s}
                busy={busy}
                onApprove={() =>
                  act(`ap-${s.id}`, () => api.storyboardApprove(projectId!, s.id), () => load(projectId!))
                }
                onReject={() =>
                  act(`rj-${s.id}`, () => api.storyboardReject(projectId!, s.id), () => load(projectId!))
                }
                onRegen={() =>
                  act(
                    `rg-${s.id}`,
                    () => api.storyboardRegenerate(projectId!, s.id, { provider }),
                    () => {
                      setMsg('已入队重生成，稍等自动刷新')
                      load(projectId!)
                    },
                  )
                }
                onEditPrompt={() => {
                  setEditShot(s)
                  setDraftPrompt(s.prompt || '')
                }}
                onUpload={() => {
                  uploadFor.current = s.id
                  fileRef.current?.click()
                }}
              />
            ))}
          </div>
        )}
      </div>

      <input
        ref={fileRef}
        type="file"
        accept="image/png,image/jpeg,image/webp"
        className="hidden"
        onChange={async (e) => {
          const f = e.target.files?.[0]
          const shotId = uploadFor.current
          e.target.value = ''
          if (!f || !shotId || !projectId) return
          await act(
            `up-${shotId}`,
            () => api.storyboardUpload(projectId, shotId, f),
            () => {
              setMsg(`已用「${f.name}」替换这一句的配图`)
              load(projectId)
            },
          )
        }}
      />

      <Modal
        open={!!editShot}
        onClose={() => setEditShot(null)}
        title={`调整提示词 · 第 ${editShot?.order} 句`}
        width="560px"
      >
        <div className="flex flex-col gap-3">
          <div className="text-[12px] text-ink-soft">
            这句歌词：<span className="text-ink">{editShot?.line_text}</span>
          </div>
          <div className="text-[12px] text-ink-soft">
            景区：<span className="text-ink">{editShot?.place}</span>
          </div>
          <textarea
            className="input h-28 py-2 text-[12px] leading-relaxed"
            value={draftPrompt}
            onChange={(e) => setDraftPrompt(e.target.value)}
            placeholder="描述画面：主体 + 时间光线 + 天气 + 机位景别 + 质感"
          />
          <div className="text-[11px] text-ink-faint">
            写完点「保存并重出」，会把提示词改掉并用所选模型重生成一张。
          </div>
          <div className="flex gap-2">
            <button
              className="btn-primary"
              disabled={!!busy}
              onClick={() =>
                act(
                  'save-prompt',
                  () =>
                    api.storyboardRegenerate(projectId!, editShot.id, {
                      prompt: draftPrompt,
                      provider,
                    }),
                  () => {
                    setEditShot(null)
                    setMsg('已入队重生成')
                    load(projectId!)
                  },
                )
              }
            >
              保存并重出
            </button>
            <button className="btn" onClick={() => setEditShot(null)}>
              取消
            </button>
          </div>
        </div>
      </Modal>
    </>
  )
}

function ShotCard({
  shot,
  busy,
  onApprove,
  onReject,
  onRegen,
  onEditPrompt,
  onUpload,
}: {
  shot: any
  busy: string
  onApprove: () => void
  onReject: () => void
  onRegen: () => void
  onEditPrompt: () => void
  onUpload: () => void
}) {
  const done = shot.status === 'approved'
  return (
    <div
      className={clsx(
        'card overflow-hidden flex flex-col',
        done ? 'ring-1 ring-brand/40' : shot.status === 'rejected' ? 'ring-1 ring-human/50' : '',
      )}
    >
      <div className="flex items-center gap-2 px-3 py-2 border-b border-line">
        <span className="text-[11px] text-ink-faint w-8 shrink-0">#{shot.order}</span>
        <span className={clsx('shrink-0', STATUS_TONE[shot.status] || 'tag')}>
          {shot.status_label}
        </span>
        <span className="text-[11px] text-ink-faint truncate">{shot.section}</span>
        {shot.rev > 0 && <span className="text-[10px] text-ink-faint shrink-0">第{shot.rev + 1}版</span>}
      </div>

      <div className="relative bg-page aspect-video">
        {shot.image_url ? (
          <img src={shot.image_url} alt={shot.place} className="w-full h-full object-cover" />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            {shot.status === 'generating' ? (
              <Spinner label="出图中" />
            ) : (
              <span className="text-[12px] text-ink-faint">
                {shot.status === 'draft' ? '还没出图' : '无图'}
              </span>
            )}
          </div>
        )}
      </div>

      {shot.prev_image_url && (
        <div className="px-3 pt-2 flex items-center gap-2">
          <img
            src={shot.prev_image_url}
            alt="上一版"
            className="w-14 h-9 object-cover rounded border border-line"
          />
          <span className="text-[10px] text-ink-faint">← 上一版，可对比</span>
        </div>
      )}

      <div className="p-3 flex flex-col gap-2 flex-1">
        <div>
          <div className="text-[13px] font-medium leading-snug">{shot.place}</div>
          {shot.place_note && (
            <div className="text-[11px] text-ink-faint mt-0.5 leading-relaxed line-clamp-2">
              {shot.place_note}
            </div>
          )}
        </div>
        <div className="text-[12px] text-ink-soft leading-relaxed border-l-2 border-line pl-2">
          {shot.line_text}
        </div>
        {shot.error && <div className="text-[11px] text-human leading-relaxed">{shot.error}</div>}
        {shot.note && <div className="text-[11px] text-ink-faint">备注：{shot.note}</div>}

        <div className="mt-auto flex flex-wrap gap-1.5 pt-1">
          <button
            className={clsx('btn btn-sm', done && 'opacity-50')}
            disabled={!!busy || !shot.image_url || done}
            onClick={onApprove}
          >
            {done ? '已通过' : '通过'}
          </button>
          <button className="btn btn-sm" disabled={!!busy} onClick={onEditPrompt}>
            改提示词
          </button>
          <button className="btn btn-sm" disabled={!!busy} onClick={onRegen}>
            换模型重出
          </button>
          <button className="btn btn-sm" disabled={!!busy} onClick={onUpload}>
            上传我的图
          </button>
          {!done && !!shot.image_url && (
            <button className="btn btn-sm" disabled={!!busy} onClick={onReject}>
              驳回
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
