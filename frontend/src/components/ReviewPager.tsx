import { useEffect, useMemo, useState } from 'react'
import clsx from 'clsx'

/**
 * 翻页审片（PRD V1.1 审核减负）。
 *
 * 一屏只做一件事：这张图配这句歌词、这个景点，行不行？
 * - 默认只翻「需要你看的」（机器预审低分 / 未预审 / 被驳回的），建议通过的交给「一键通过建议」；
 * - 键盘：Y 或 Enter 通过并翻到下一张，N 驳回，← → 翻页；
 * - 副歌复唱已合并，审一次即对所有复唱句生效。
 */
export default function ReviewPager({
  shots,
  threshold,
  busy,
  readOnly,
  onApprove,
  onReject,
  onRegen,
  onRevert,
  providers = [],
  onEditPrompt,
  onUpload,
  onUseLibrary,
}: {
  shots: any[]
  threshold: number
  busy: string
  readOnly?: boolean
  onApprove: (s: any) => Promise<void>
  onReject: (s: any) => Promise<void>
  /** 逐句选模型重出：provider 为空时用页面上方选中的默认模型 */
  onRegen: (s: any, provider?: string) => void
  onRevert?: (s: any) => void
  providers?: any[]
  onEditPrompt: (s: any) => void
  onUpload: (s: any) => void
  onUseLibrary: (s: any) => Promise<void>
}) {
  const needsMe = (s: any) =>
    s.status === 'rejected' || (s.status === 'pending' && (s.ai_score === null || s.ai_score < threshold))
  const [onlyMine, setOnlyMine] = useState(true)
  const list = useMemo(() => {
    const mine = shots.filter(needsMe)
    return onlyMine && mine.length ? mine : shots
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shots, onlyMine, threshold])
  const [idx, setIdx] = useState(0)
  const cur = list[Math.min(idx, Math.max(0, list.length - 1))]

  useEffect(() => {
    if (idx > list.length - 1) setIdx(Math.max(0, list.length - 1))
  }, [list.length, idx])

  const next = () => setIdx((i) => Math.min(list.length - 1, i + 1))
  const prev = () => setIdx((i) => Math.max(0, i - 1))

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || !cur || busy || readOnly) return
      if (e.key === 'ArrowRight') next()
      else if (e.key === 'ArrowLeft') prev()
      else if ((e.key === 'y' || e.key === 'Y' || e.key === 'Enter') && cur.image_url) {
        e.preventDefault()
        onApprove(cur).then(next)
      } else if (e.key === 'n' || e.key === 'N') onReject(cur)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  if (!cur) return null
  const mineLeft = shots.filter(needsMe).length

  return (
    <div className="card overflow-hidden">
      <div className="flex items-center gap-3 px-4 py-2.5 border-b border-line text-[12px]">
        <span className="font-medium">
          第 {idx + 1} / {list.length} 张
        </span>
        <label className="flex items-center gap-1.5 text-ink-soft cursor-pointer select-none">
          <input
            type="checkbox"
            className="accent-[#0F5E57]"
            checked={onlyMine}
            onChange={(e) => {
              setOnlyMine(e.target.checked)
              setIdx(0)
            }}
          />
          只看需要我看的（{mineLeft}）
        </label>
        <span className="ml-auto text-[11px] text-ink-faint">键盘：Y 通过 · N 驳回 · ← → 翻页</span>
      </div>

      <div className="grid grid-cols-[1fr_360px]">
        <div className="bg-ink/90 aspect-video flex items-center justify-center relative">
          {cur.image_url ? (
            <img src={cur.image_url} alt={cur.place} className="w-full h-full object-contain" />
          ) : (
            <span className="text-white/60 text-[13px]">{cur.status === 'generating' ? '出图中…' : '还没有画面'}</span>
          )}
          <button
            className="absolute left-2 top-1/2 -translate-y-1/2 w-9 h-9 rounded-full bg-white/80 text-ink disabled:opacity-30"
            disabled={idx === 0}
            onClick={prev}
            aria-label="上一张"
          >
            ←
          </button>
          <button
            className="absolute right-2 top-1/2 -translate-y-1/2 w-9 h-9 rounded-full bg-white/80 text-ink disabled:opacity-30"
            disabled={idx >= list.length - 1}
            onClick={next}
            aria-label="下一张"
          >
            →
          </button>
        </div>

        <div className="p-4 flex flex-col gap-3">
          <div>
            <div className="text-[11px] text-ink-faint">
              第 {cur.order} 句 · {cur.section}
              {cur.repeats > 0 && <span className="ml-1 tag">副歌复唱 ×{cur.repeats + 1}，审一次全部生效</span>}
            </div>
            {/* 歌词放最大：审的就是「这句话 ↔ 这张图」 */}
            <div className="mt-1.5 text-[18px] leading-snug font-medium">{cur.line_text}</div>
            <div className="mt-1 text-[12px] text-ink-soft">
              景点：{cur.place}
              {cur.source_label && <span className="ml-2 tag">{cur.source_label}</span>}
            </div>
          </div>

          <AiVerdict shot={cur} threshold={threshold} />

          {cur.suggest && (
            <div className="border border-brand/40 bg-brand-soft/30 rounded-btn p-2.5 flex items-center gap-2.5">
              {cur.suggest.thumb_url && (
                <img src={cur.suggest.thumb_url} alt="" className="w-20 h-12 object-cover rounded border border-line" />
              )}
              <div className="flex-1 min-w-0">
                <div className="text-[11px] font-medium text-brand">
                  有{cur.suggest.is_seed ? '演示素材' : '实拍'}可替换
                </div>
                <div className="text-[10px] text-ink-soft truncate">
                  {cur.suggest.name} · {cur.suggest.licensor || '未登记授权方'}
                </div>
              </div>
              <button
                className="btn btn-sm shrink-0"
                disabled={!!busy || readOnly}
                onClick={() => onUseLibrary(cur).then(next)}
              >
                换成它
              </button>
            </div>
          )}

          {/* 借用前句画面 / 保留上一版等兜底说明：告诉运营这张图是怎么来的、下一步能做什么 */}
          {cur.note && cur.source === 'borrowed' && (
            <div className="text-[11px] text-human-dark bg-human-soft/40 border border-human/30 rounded-btn px-2.5 py-2">
              {cur.note}
            </div>
          )}
          {cur.error && <div className="text-[11px] text-human">出图失败：{cur.error.slice(0, 120)}</div>}

          <div className="mt-auto flex flex-col gap-2">
            <div className="flex gap-2">
              <button
                className="btn-primary flex-1"
                disabled={!!busy || readOnly || !cur.image_url || cur.status === 'approved'}
                onClick={() => onApprove(cur).then(next)}
              >
                {cur.status === 'approved' ? '已通过' : '图配得上，通过（Y）'}
              </button>
              <button className="btn" disabled={!!busy || readOnly} onClick={() => onReject(cur)}>
                不行（N）
              </button>
            </div>
            {cur.prev_image_url && (
              <div className="flex items-center gap-2 border border-line rounded-btn p-1.5">
                <img src={cur.prev_image_url} alt="上一版" className="w-20 h-12 object-cover rounded border border-line" />
                <span className="text-[10px] text-ink-faint flex-1">上一版（可对比）</span>
                {onRevert && (
                  <button className="btn btn-sm" disabled={!!busy || readOnly} onClick={() => onRevert(cur)}>
                    用回上一版
                  </button>
                )}
              </div>
            )}
            {providers.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[11px] text-ink-faint">换模型重出：</span>
                {providers.map((x) => (
                  <button
                    key={x.key}
                    className={clsx('btn btn-sm', cur.provider === x.key && 'ring-1 ring-brand/40')}
                    disabled={!!busy || readOnly || !x.ready}
                    title={x.ready ? `${x.model} · ${x.size}` : x.note}
                    onClick={() => onRegen(cur, x.key)}
                  >
                    {x.label}
                    {!x.ready && '（未配置）'}
                  </button>
                ))}
              </div>
            )}
            <div className="flex flex-wrap gap-1.5">
              <button className="btn btn-sm" disabled={!!busy || readOnly} onClick={() => onEditPrompt(cur)}>
                改描述再出
              </button>
              <button className="btn btn-sm" disabled={!!busy || readOnly} onClick={() => onUpload(cur)}>
                上传我的图
              </button>
            </div>
            <div className="text-[10px] text-ink-faint leading-relaxed">
              只需要判断：画面是不是这个地方、和这句歌词的意思搭不搭。不需要懂音乐。
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export function AiVerdict({ shot, threshold, compact }: { shot: any; threshold: number; compact?: boolean }) {
  if (shot.ai_score === null || shot.ai_score === undefined) {
    return (
      <div className={clsx('text-[11px] text-ink-faint', !compact && 'border border-line rounded-btn px-2.5 py-2')}>
        {shot.source === 'borrowed'
          ? '这张是借用的画面，未做预审，请人工判断能不能先用'
          : !shot.image_url
            ? '还没有画面，无法预审'
            : '未预审（视觉模型未配置或调用失败），请人工判断'}
      </div>
    )
  }
  const pass = shot.ai_score >= threshold
  return (
    <div
      className={clsx(
        'rounded-btn text-[11px] leading-relaxed',
        compact ? '' : 'px-2.5 py-2 border',
        pass ? 'border-brand/40 bg-brand-soft/30' : 'border-human/40 bg-human-soft/40',
      )}
    >
      <span className={clsx('font-medium', pass ? 'text-brand' : 'text-human-dark')}>
        机器预审 {shot.ai_score} 分 · {pass ? '建议通过' : '建议你看一眼'}
      </span>
      {shot.ai_reason && <span className="text-ink-soft">：{shot.ai_reason}</span>}
    </div>
  )
}
