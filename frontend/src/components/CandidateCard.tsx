import clsx from 'clsx'
import { useEffect, useState } from 'react'
import Waveform from './Waveform'
import { ScoreDots, StateDot } from './ui'
import { fmtDuration } from '../lib/format'

/**
 * 候选曲目卡片。
 * - 盲听（选型轮未揭晓，c.blind）：只显示「候选 X」，不显示模型、版本、费用、耗时 —— 接口本身也不返回；
 * - 占位结果（c.is_mock）：醒目标注「占位」，不显示匹配分（PRD V1.1 F2-17）；
 * - 未评分就显示「未评分」，不给默认分（V1.0 显示「均分 4.0」）。
 */
export default function CandidateCard({
  c,
  selected,
  playing,
  onPlay,
  onPause,
  onRate,
  onSelect,
  busy,
  readOnly,
  selectBlock,
  error,
}: {
  c: any
  selected: boolean
  playing: boolean
  onPlay: () => void
  onPause: () => void
  onRate: (patch: { diction?: number; melody?: number; arrangement?: number }) => void
  onSelect: () => void
  busy?: boolean
  readOnly?: boolean
  /** 不能选定的原因（如选型轮还有候选未评分）；有值时按钮置灰并直接展示原因 */
  selectBlock?: string
  /** 本卡片上一次操作的错误，就近展示（页面顶部的横幅滚动后看不到） */
  error?: string
}) {
  // 评分草稿：三项都打完才提交。V1.0 每点一项就提交、其余两项按 0 送，
  // 被后端以「评分必须在 1-5 之间」拒绝 —— 结果永远存不上分，选型轮也就永远选不定。
  const saved = c.rating || { diction: 0, melody: 0, arrangement: 0 }
  const [draft, setDraft] = useState({ diction: saved.diction || 0, melody: saved.melody || 0, arrangement: saved.arrangement || 0 })
  useEffect(() => {
    setDraft({ diction: saved.diction || 0, melody: saved.melody || 0, arrangement: saved.arrangement || 0 })
  }, [saved.diction, saved.melody, saved.arrangement])
  const [pending, setPending] = useState(false)
  const rating = { ...draft, total: draft.diction + draft.melody + draft.arrangement }
  const missing = (['diction', 'melody', 'arrangement'] as const).filter((k) => !draft[k]).length
  const setScore = (k: 'diction' | 'melody' | 'arrangement', v: number) => {
    // 函数式更新：连续快速点击时不会拿到旧草稿而丢分；提交放到 effect 里用最新草稿
    setDraft((d) => ({ ...d, [k]: v }))
    setPending(true)
  }
  useEffect(() => {
    if (!pending) return
    setPending(false)
    if (draft.diction && draft.melody && draft.arrangement) onRate(draft)
  }, [pending, draft]) // eslint-disable-line react-hooks/exhaustive-deps
  const done = c.status === 'done' && c.audio_url
  const rated = !!(rating.diction && rating.melody && rating.arrangement)
  const avg = rated ? (rating.total / 3).toFixed(1) : null
  const title = c.blind ? `候选 ${c.label}` : c.model_name

  return (
    <div
      className={clsx(
        'border rounded-card p-3 flex flex-col gap-2 transition-colors',
        selected ? 'border-brand ring-1 ring-brand/25 bg-brand-soft/30' : 'border-line bg-white',
      )}
    >
      <div className="flex items-center gap-2">
        <span className="w-6 h-6 rounded-full bg-page border border-line flex items-center justify-center text-[11px] font-medium shrink-0">
          {c.label}
        </span>
        <span className="text-[12px] font-medium truncate">{title}</span>
        {c.blind ? (
          <span className="tag shrink-0" title="选型轮：评分并选定后才揭晓模型">盲听</span>
        ) : (
          c.variant && <span className="tag shrink-0">第 {c.variant} 版</span>
        )}
        {c.is_mock && (
          <span className="tag-human shrink-0" title="模型未配置时的占位音频，不能选定为交付曲目，也不计入统计">
            占位
          </span>
        )}
        <span className="ml-auto flex items-center gap-1 shrink-0">
          <StateDot status={c.status} />
          <span className="text-[10px] text-ink-faint">
            {c.status === 'done' ? (selected ? '已选定' : '可试听') : c.status === 'failed' ? '失败' : '生成中'}
          </span>
        </span>
      </div>

      {done ? (
        <Waveform
          url={c.audio_url}
          playing={playing}
          onPlayRequest={playing ? onPause : onPlay}
          onFinish={onPause}
        />
      ) : (
        <div className="h-[44px] rounded-btn border border-dashed border-line flex items-center justify-center text-[11px] text-ink-faint">
          {c.status === 'failed' ? c.error || '生成失败' : '等待音频…'}
        </div>
      )}

      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-soft">
        <span>BPM {c.bpm ? Math.round(c.bpm) : '—'}</span>
        <span>{fmtDuration(c.duration_s)}</span>
        <span>声线 {c.vocal || '—'}</span>
        {!c.blind && <span>耗时 {c.elapsed_s ? `${c.elapsed_s}s` : '—'}</span>}
        {!c.blind && <span>费用 ¥{(c.cost || 0).toFixed(2)}</span>}
      </div>

      {c.fit_score === null || c.fit_score === undefined ? (
        <div className="text-[10px] text-ink-faint">{c.fit_reason || '未评估简报匹配度'}</div>
      ) : (
        <>
          <div className="flex items-center gap-2 text-[11px]">
            <span className="text-ink-faint">AI 简报匹配</span>
            <span className="font-medium text-brand">{c.fit_score}</span>
            <div className="flex-1 h-1 rounded-full bg-line overflow-hidden">
              <div className="h-full bg-brand" style={{ width: `${c.fit_score}%` }} />
            </div>
          </div>
          {c.fit_reason && <div className="text-[10px] text-ink-faint leading-snug">{c.fit_reason}</div>}
        </>
      )}

      <div className="border-t border-line pt-2 flex flex-col gap-1.5">
        {(
          [
            ['diction', '咬字'],
            ['melody', '旋律'],
            ['arrangement', '编曲'],
          ] as const
        ).map(([k, label]) => (
          <div key={k} className="flex items-center justify-between">
            <span className="text-[11px] text-ink-soft">{label}</span>
            <ScoreDots value={rating[k] || 0} onChange={(v) => setScore(k, v)} readOnly={!done || readOnly} />
          </div>
        ))}
        <div className="text-[10px] text-ink-faint text-right">
          {avg ? (c.rating ? `均分 ${avg} / 5 · 已保存` : `均分 ${avg} / 5 · 保存中…`) : missing < 3 ? `还差 ${missing} 项，三项打完自动保存` : '未评分'}
        </div>
      </div>

      {error && <div className="text-[11px] text-red-600 bg-red-50 border border-red-200 rounded-btn px-2 py-1.5">{error}</div>}
      {!readOnly && !selected && selectBlock && (
        <div className="text-[11px] text-ink-soft">{selectBlock}</div>
      )}
      {!readOnly && (
        <button
          className={clsx('btn btn-sm w-full mt-0.5', selected ? 'btn-human' : 'btn-primary')}
          disabled={!done || busy || (c.is_mock && !c.allow_mock) || (!selected && !!selectBlock)}
          title={c.is_mock && !c.allow_mock ? '占位结果不能选定为交付曲目' : ''}
          onClick={onSelect}
        >
          {busy ? '处理中…' : selected ? `${title} 已选定` : c.blind ? `选定候选 ${c.label}（选定后揭晓）` : '选定这一版'}
        </button>
      )}
    </div>
  )
}
