import clsx from 'clsx'
import Waveform from './Waveform'
import { ScoreDots, StateDot } from './ui'
import { fmtDuration } from '../lib/format'

export default function CandidateCard({
  c,
  selected,
  playing,
  onPlay,
  onPause,
  onRate,
  onSelect,
  busy,
}: {
  c: any
  selected: boolean
  playing: boolean
  onPlay: () => void
  onPause: () => void
  onRate: (patch: { diction?: number; melody?: number; arrangement?: number }) => void
  onSelect: () => void
  busy?: boolean
}) {
  const rating = c.rating || { diction: 0, melody: 0, arrangement: 0 }
  const done = c.status === 'done' && c.audio_url
  const avg = rating.diction && rating.melody && rating.arrangement ? (rating.total / 3).toFixed(1) : null

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
        {/* 已按要求去掉盲听：直接显示模型与版本 */}
        <span className="text-[12px] font-medium truncate">{c.model_name}</span>
        <span className="tag shrink-0">第 {c.variant} 版</span>
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
        <span>耗时 {c.elapsed_s ? `${c.elapsed_s}s` : '—'}</span>
        <span>费用 ¥{(c.cost || 0).toFixed(2)}</span>
      </div>

      <div className="flex items-center gap-2 text-[11px]">
        <span className="text-ink-faint">AI 简报匹配</span>
        <span className="font-medium text-brand">{c.fit_score || 0}</span>
        <div className="flex-1 h-1 rounded-full bg-line overflow-hidden">
          <div className="h-full bg-brand" style={{ width: `${c.fit_score || 0}%` }} />
        </div>
      </div>
      {c.fit_reason && <div className="text-[10px] text-ink-faint leading-snug">{c.fit_reason}</div>}

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
            <ScoreDots value={rating[k] || 0} onChange={(v) => onRate({ [k]: v })} readOnly={!done} />
          </div>
        ))}
      </div>

      <button
        className={clsx('btn btn-sm w-full mt-0.5', selected ? 'btn-human' : 'btn-primary')}
        disabled={!done || busy}
        onClick={onSelect}
      >
        {busy ? '处理中…' : selected ? `${c.model_name} 已选定` : '选定这一版'}
        {avg && !selected && <span className="opacity-70">（均分 {avg}）</span>}
      </button>
    </div>
  )
}
