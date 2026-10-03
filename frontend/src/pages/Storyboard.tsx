import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import clsx from 'clsx'
import api from '../lib/api'
import { useWorkHeartbeat } from '../lib/heartbeat'
import { Banner, Empty, Modal, PageHeader, ProgressBar, Section, Spinner } from '../components/ui'
import ReviewPager, { AiVerdict } from '../components/ReviewPager'

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
  // F1-11 有效工时：页面在前台且有操作时每 30 秒报一次
  useWorkHeartbeat(projectId)
  const [data, setData] = useState<any>(null)
  const [detail, setDetail] = useState<any>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [provider, setProvider] = useState('')
  const [projects, setProjects] = useState<any[] | null>(null)
  const [editShot, setEditShot] = useState<any>(null)
  const [draftPrompt, setDraftPrompt] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  // 默认翻页审片：一句歌词 + 一张大图，比 24 张小卡片好判断
  const [view, setView] = useState<'pager' | 'grid'>('pager')
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
  // 副歌复唱句跟随母句，只审母句
  const masters = useMemo(() => items.filter((x) => !x.dup_of), [items])
  const prog = data?.progress || { total: 0, approved: 0, pending: 0, rejected: 0 }
  const providers: any[] = data?.providers || []

  // 有句子还在排队/出图中就自动轮询 —— 出图是长任务，用户不该手动刷新
  // 只有真有出图任务在跑才算「出图中」（V1.0 把「待生成」也算进来，没配生图模型时永远显示出图中）
  const working = useMemo(
    () => !!data?.job_active || items.some((x) => x.status === 'generating'),
    [data, items],
  )
  useEffect(() => {
    if (!projectId || !working) return
    let alive = true
    let t: number | undefined
    const tick = async () => {
      await load(projectId).catch(() => {})
      if (alive) t = window.setTimeout(tick, 6000)
    }
    t = window.setTimeout(tick, 6000)
    return () => {
      alive = false
      if (t) clearTimeout(t)
    }
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

  // detail 是 {project, gate, ...}；V1.0 直接取 detail.name，面包屑永远显示「—」
  const p = detail?.project || {}
  const isHuman = !!p.waiting_human
  const locked = !!p.locked
  const gate = detail?.gate || { ok: true, reasons: [] }

  if (!projectId) {
    return (
      <>
        <PageHeader title="配图审核" subtitle="选择一个项目进入" />
        <div className="p-6">
          {err && <Banner tone="error" onClose={() => setErr('')}>{err}</Banner>}
          {projects === null ? (
            // 加载中就显示加载中：V1.0 在这里直接显示「还没有项目」，实际有 5 个
            !err && <Spinner />
          ) : !projects.length ? (
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
            {/* 当前画面由文生图生成，不是实拍；不能写成「真实景区图」（PRD V1.1 D1） */}
            <span>每句歌词配一个画面（当前为 AI 生成，交付时需标注），审过才进卡点合成</span>
          </span>
        }
        actions={
          locked ? (
            <span className="text-[11px] text-ink-faint">已交付 · 只读</span>
          ) : (
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
              disabled={!!busy || working}
              title={
                // 不再要求生图模型就绪：先在素材库找实拍镜头，找不到才用 AI 补位（PRD V1.1 D1）
                providers.some((x) => x.key === provider && x.ready)
                  ? 'AI 逐句出分镜（副歌复唱只出一次），出完自动预审；素材库有实拍时会提示可替换'
                  : data?.allow_mock
                    ? '生图模型未配置：先从素材库匹配，匹配不到的句子生成占位画面（演示模式）'
                    : '生图模型未配置：只从素材库匹配实拍镜头，匹配不到的句子会标为缺镜头'
              }
              onClick={() =>
                act('enqueue', () => api.storyboardEnqueue(projectId!, provider), () => {
                  setMsg('已开始：AI 逐句出分镜并自动预审打分，完成后你只需看标红的几张。页面会自动刷新')
                  load(projectId!)
                })
              }
            >
              {busy === 'enqueue' ? '提交中…' : working ? '出图中…' : '开始出图'}
            </button>
            <button
              className="btn-primary"
              disabled={!!busy || !prog.suggest_pass}
              title="通过机器预审打分较高的句子；低分的留给你在「翻页审片」里看"
              onClick={() =>
                act('suggested', () => api.storyboardApproveSuggested(projectId!), (r) => {
                  setMsg(`已通过 ${r.approved} 句机器建议通过的画面`)
                  load(projectId!)
                })
              }
            >
              通过机器建议（{prog.suggest_pass || 0}）
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
              disabled={!!busy || p.status !== 'storyboard_review' || !gate.ok}
              title={p.status !== 'storyboard_review' ? '当前不在「待审配图」阶段' : gate.reasons.join('；')}
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
          )
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
          {/* 按「需要审的句数」说话：副歌复唱已合并，机器预审高分的建议直接通过 */}
          <div className="text-[12px]">
            已通过 <span className="font-medium">{prog.unique_approved ?? prog.approved}</span> / {prog.unique ?? prog.total} 句
            {!!prog.unique && prog.unique < prog.total && (
              <span className="text-ink-faint">（共 {prog.total} 句，副歌复唱已合并）</span>
            )}
          </div>
          {!!prog.need_review && <span className="tag-human">需要你看 {prog.need_review}</span>}
          {!!prog.suggest_pass && <span className="tag-brand">机器建议通过 {prog.suggest_pass}</span>}
          {!!prog.rejected && <span className="tag-human">需重做 {prog.rejected}</span>}
          {!!prog.by_status?.draft && <span className="tag">待生成 {prog.by_status.draft}</span>}
          <div className="flex-1 min-w-[120px]">
            <ProgressBar value={prog.unique ? (prog.unique_approved / prog.unique) * 100 : 0} />
          </div>
          {prog.ai_ratio > 0 && (
            <span
              className={prog.ai_ratio > prog.ai_ratio_limit ? 'tag-human' : 'tag'}
              title="产品约定：AI 画面占比建议不超过 30%，超出只提醒、不拦截。素材库有实拍时可一键替换"
            >
              AI 画面 {Math.round(prog.ai_ratio * 100)}%
              {prog.ai_ratio > prog.ai_ratio_limit &&
                (prog.replaceable ? `（建议 ≤30%，可替换实拍 ${prog.replaceable} 句）` : '（建议 ≤30%）')}
            </span>
          )}
          <div className="flex rounded-btn border border-line overflow-hidden text-[11px]">
            {(
              [
                ['pager', '翻页审片'],
                ['grid', '全部卡片'],
              ] as const
            ).map(([k, label]) => (
              <button
                key={k}
                className={clsx('px-2.5 py-1', view === k ? 'bg-brand text-white' : 'bg-white text-ink-soft')}
                onClick={() => setView(k)}
              >
                {label}
              </button>
            ))}
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

        {locked && (
          <Banner tone="warn">该项目已交付并锁定，只读。需要调整请回词曲工作台「新建修订版」。</Banner>
        )}
        {!locked && data?.stale && (
          <Banner tone="warn">
            歌词已经更新，下面的画面对应的是旧歌词。点右上角「开始出图」会按新歌词重新出分镜。
          </Banner>
        )}
        {!data ? (
          !err && <Spinner label="加载配图" />
        ) : !items.length ? (
          <Empty
            text={
              locked
                ? '该项目没有逐句配图记录（V1.0 时期未经配图审核即交付）。'
                : '还没有分镜。先「重新提炼景区」，再点「开始/继续出图」。'
            }
          />
        ) : view === 'pager' ? (
          <ReviewPager
            shots={masters}
            threshold={prog.suggest_pass_threshold ?? 75}
            busy={busy}
            readOnly={locked}
            onApprove={(s) => act(`ap-${s.id}`, () => api.storyboardApprove(projectId!, s.id), () => load(projectId!))}
            onReject={(s) => act(`rj-${s.id}`, () => api.storyboardReject(projectId!, s.id), () => load(projectId!))}
            onRegen={(s) =>
              act(`rg-${s.id}`, () => api.storyboardRegenerate(projectId!, s.id, { provider }), () => {
                setMsg('已开始重出这一张，稍等自动刷新')
                load(projectId!)
              })
            }
            onEditPrompt={(s) => {
              setEditShot(s)
              setDraftPrompt(s.prompt || '')
            }}
            onUpload={(s) => {
              uploadFor.current = s.id
              fileRef.current?.click()
            }}
            onUseLibrary={(s) =>
              act(`lib-${s.id}`, () => api.storyboardUseLibrary(projectId!, s.id), () => {
                setMsg(`第 ${s.order} 句已换成素材库镜头`)
                load(projectId!)
              })
            }
          />
        ) : (
          <div className="grid grid-cols-3 gap-4">
            {masters.map((s) => (
              <ShotCard
                key={s.id}
                shot={s}
                threshold={prog.suggest_pass_threshold ?? 75}
                onUseLibrary={() =>
                  act(`lib-${s.id}`, () => api.storyboardUseLibrary(projectId!, s.id), () => {
                    setMsg(`第 ${s.order} 句已换成素材库镜头`)
                    load(projectId!)
                  })
                }
                // 已交付只读：用一个非空 busy 让卡片上的操作按钮统一置灰
                busy={locked ? 'locked' : busy}
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
  threshold,
  onUseLibrary,
  busy,
  onApprove,
  onReject,
  onRegen,
  onEditPrompt,
  onUpload,
}: {
  shot: any
  threshold: number
  onUseLibrary: () => void
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
        {/* 镜头来源：实拍 / AI / 占位一眼可辨（PRD V1.1 §10.3 来源标签） */}
        {shot.source_label && (
          <span
            className={clsx(
              'ml-auto shrink-0 text-[10px] px-1.5 py-0.5 rounded',
              shot.source === 'library' || shot.source === 'upload'
                ? 'bg-brand text-white'
                : shot.source === 'placeholder' || shot.source === 'library_seed'
                  ? 'bg-page text-ink-faint border border-line'
                  : 'border border-human text-human-dark',
            )}
          >
            {shot.source_label}
          </span>
        )}
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
          {shot.repeats > 0 && <span className="ml-1 tag">副歌复唱 ×{shot.repeats + 1}</span>}
        </div>
        {shot.image_url && shot.status === 'pending' && <AiVerdict shot={shot} threshold={threshold} compact />}
        {shot.suggest && (
          <button
            className="text-left text-[11px] text-brand underline disabled:opacity-50"
            disabled={!!busy}
            onClick={onUseLibrary}
          >
            有{shot.suggest.is_seed ? '演示素材' : '实拍'}可替换：{shot.suggest.name} → 换成它
          </button>
        )}
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
