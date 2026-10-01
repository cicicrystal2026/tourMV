import { useEffect, useMemo, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent } from 'react'
import clsx from 'clsx'
import { fmtTime } from '../lib/format'

/** 用 Web Audio 在浏览器端解码音频并抽取波形峰值（不额外增加后端接口）。 */
export function usePeaks(url: string, buckets = 900) {
  const [peaks, setPeaks] = useState<number[]>([])
  useEffect(() => {
    if (!url) return
    let dead = false
    ;(async () => {
      try {
        const buf = await (await fetch(url)).arrayBuffer()
        const Ctx = (window.AudioContext || (window as any).webkitAudioContext) as typeof AudioContext
        const ac = new Ctx()
        const audio = await ac.decodeAudioData(buf)
        const ch = audio.getChannelData(0)
        const size = Math.floor(ch.length / buckets) || 1
        const out: number[] = []
        for (let i = 0; i < buckets; i++) {
          let max = 0
          const start = i * size
          for (let j = 0; j < size; j += 16) {
            const v = Math.abs(ch[start + j] || 0)
            if (v > max) max = v
          }
          out.push(max)
        }
        const norm = Math.max(...out) || 1
        if (!dead) setPeaks(out.map((v) => v / norm))
        ac.close()
      } catch {
        if (!dead) setPeaks([])
      }
    })()
    return () => {
      dead = true
    }
  }, [url, buckets])
  return peaks
}

type Clip = any

export default function Timeline({
  duration,
  clips,
  lyricLines,
  beats,
  strongBeats,
  selectedId,
  currentTime,
  onSelect,
  onSeek,
  onChange,
  peaks,
  trackHeight = 46,
}: {
  duration: number
  clips: Clip[]
  lyricLines: any[]
  beats: number[]
  strongBeats: number[]
  selectedId?: string
  currentTime: number
  onSelect: (id: string) => void
  onSeek: (t: number) => void
  onChange: (clipId: string, patch: { start_s?: number; end_s?: number }) => void
  peaks: number[]
  trackHeight?: number
}) {
  const wrap = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<{ id: string; edge: 'start' | 'end'; t0: number; x0: number } | null>(null)
  const dur = Math.max(duration, 1)

  const pct = (t: number) => `${(t / dur) * 100}%`

  useEffect(() => {
    if (!drag) return
    const move = (e: PointerEvent) => {
      const w = wrap.current?.clientWidth || 1
      const dt = ((e.clientX - drag.x0) / w) * dur
      if (drag.edge === 'end') onChange(drag.id, { end_s: Math.max(drag.t0 + 0.4, drag.t0 + dt) })
      else onChange(drag.id, { start_s: Math.max(0, Math.min(drag.t0 + 0.4, drag.t0 + dt)) })
    }
    const up = () => setDrag(null)
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
  }, [drag, dur, onChange])

  const seconds = useMemo(() => {
    const step = dur > 180 ? 30 : dur > 90 ? 15 : 10
    const out: number[] = []
    for (let t = 0; t <= dur; t += step) out.push(t)
    return out
  }, [dur])

  const sectionSpans = useMemo(() => {
    const out: { name: string; start: number; end: number }[] = []
    for (const l of lyricLines || []) {
      const name = l.section || '—'
      const last = out[out.length - 1]
      if (last && last.name === name) last.end = l.end_s
      else out.push({ name, start: l.start_s, end: l.end_s })
    }
    return out
  }, [lyricLines])

  const seek = (e: ReactMouseEvent) => {
    const rect = wrap.current!.getBoundingClientRect()
    onSeek(Math.max(0, Math.min(dur, ((e.clientX - rect.left) / rect.width) * dur)))
  }

  return (
    <div className="select-none">
      <div className="flex items-stretch gap-2">
        <div className="w-[52px] shrink-0 flex flex-col text-[10px] text-ink-faint">
          <div style={{ height: 22 }} className="flex items-center">段落</div>
          <div style={{ height: trackHeight + 6 }} className="flex items-center">画面</div>
          <div style={{ height: 34 }} className="flex items-center">音乐</div>
          <div style={{ height: 16 }} className="flex items-center">节拍</div>
          <div style={{ height: 26 }} className="flex items-center">字幕</div>
        </div>

        <div className="flex-1 min-w-0 relative" ref={wrap}>
          {/* 播放头 */}
          <div
            className="absolute top-0 bottom-0 w-px bg-human z-20 pointer-events-none"
            style={{ left: pct(currentTime) }}
          >
            <div className="w-2 h-2 -ml-[3.5px] rounded-full bg-human" />
          </div>

          {/* 时间刻度 + 点击定位 */}
          <div className="h-[22px] relative border-b border-line cursor-pointer" onClick={seek}>
            {seconds.map((t) => (
              <span key={t} className="absolute top-0 text-[9px] text-ink-faint" style={{ left: pct(t) }}>
                {fmtTime(t)}
              </span>
            ))}
          </div>

          {/* 段落 */}
          <div className="h-[22px] relative mt-0">
            {sectionSpans.map((s, i) => (
              <div
                key={i}
                className={clsx(
                  'absolute top-0 h-[18px] rounded-[4px] text-[9px] flex items-center justify-center overflow-hidden border',
                  s.name.includes('副歌') ? 'bg-brand-soft border-brand-line text-brand-dark' : 'bg-page border-line text-ink-faint',
                )}
                style={{ left: pct(s.start), width: pct(Math.max(0.5, s.end - s.start)) }}
                title={`${s.name} ${fmtTime(s.start)}–${fmtTime(s.end)}`}
              >
                {s.name}
              </div>
            ))}
          </div>

          {/* 画面片段 */}
          <div className="relative mt-1.5" style={{ height: trackHeight }}>
            {clips.map((c) => (
              <div
                key={c.id}
                onClick={(e) => {
                  e.stopPropagation()
                  onSelect(c.id)
                }}
                className={clsx(
                  'absolute top-0 h-full rounded-[6px] border overflow-hidden cursor-pointer group',
                  selectedId === c.id ? 'border-brand ring-2 ring-brand/30' : 'border-line',
                  c.locked ? 'ring-1 ring-human/50' : '',
                )}
                style={{ left: pct(c.start_s), width: pct(Math.max(0.3, c.end_s - c.start_s)) }}
                title={`${c.asset_name}\n${fmtTime(c.start_s)}–${fmtTime(c.end_s)}\n${c.scene_prompt}`}
              >
                {c.thumb_url ? (
                  <img src={c.thumb_url} alt="" className="w-full h-full object-cover" draggable={false} />
                ) : (
                  <div className="w-full h-full bg-page" />
                )}
                <span className="absolute inset-x-0 bottom-0 bg-ink/55 text-white text-[9px] px-1 truncate">
                  {c.asset_name}
                </span>
                {c.manual && <span className="absolute top-0.5 left-0.5 tag-human !h-[14px] !text-[8px] !px-1">人工</span>}
                {c.locked && <span className="absolute top-0.5 right-0.5 text-[10px]">🔒</span>}
                {/* 拖拽改时长 */}
                <span
                  onPointerDown={(e) => {
                    e.stopPropagation()
                    setDrag({ id: c.id, edge: 'end', t0: c.end_s, x0: e.clientX })
                  }}
                  className="absolute right-0 top-0 bottom-0 w-1.5 cursor-ew-resize bg-brand/0 group-hover:bg-brand/60"
                />
                <span
                  onPointerDown={(e) => {
                    e.stopPropagation()
                    setDrag({ id: c.id, edge: 'start', t0: c.start_s, x0: e.clientX })
                  }}
                  className="absolute left-0 top-0 bottom-0 w-1.5 cursor-ew-resize bg-brand/0 group-hover:bg-brand/60"
                />
              </div>
            ))}
          </div>

          {/* 音乐波形 */}
          <div className="h-[34px] mt-1.5 relative flex items-center gap-px overflow-hidden rounded-[4px] bg-page border border-line">
            {peaks.length === 0 ? (
              <span className="text-[10px] text-ink-faint px-2">波形（解码中或不可用）</span>
            ) : (
              peaks.map((v, i) => (
                <span
                  key={i}
                  className="flex-1 bg-brand/70 rounded-[1px]"
                  style={{ height: `${Math.max(6, v * 100)}%`, opacity: i / peaks.length <= currentTime / dur ? 1 : 0.35 }}
                />
              ))
            )}
          </div>

          {/* 节拍 */}
          <div className="h-[16px] mt-0.5 relative">
            {beats.map((b, i) => {
              const strong = strongBeats?.includes(b)
              return (
                <span
                  key={i}
                  className={clsx('absolute bottom-0 w-px', strong ? 'h-[12px] bg-human/80' : 'h-[7px] bg-line')}
                  style={{ left: pct(b) }}
                />
              )
            })}
          </div>

          {/* 字幕 */}
          <div className="h-[26px] mt-0.5 relative">
            {lyricLines.map((l, i) => (
              <div
                key={i}
                className="absolute top-1 h-[18px] text-[9px] text-ink-soft truncate border-l border-brand/40 pl-1"
                style={{ left: pct(l.start_s), width: pct(Math.max(0.2, l.end_s - l.start_s)) }}
                title={`${fmtTime(l.start_s)}–${fmtTime(l.end_s)} ${l.text}`}
              >
                {l.text}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
