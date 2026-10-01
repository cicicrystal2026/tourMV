import { useEffect, useImperativeHandle, useRef, useState, forwardRef } from 'react'
import WaveSurfer from 'wavesurfer.js'
import clsx from 'clsx'

export type WaveHandle = {
  toggle: () => void
  seek: (ratio: number) => void
}

/** 候选曲目波形。同一时刻只允许一首播放（由父组件通过 activeKey 控制）。 */
const Waveform = forwardRef<
  WaveHandle,
  {
    url: string
    height?: number
    playing: boolean
    onPlayRequest?: () => void
    onFinish?: () => void
    barWidth?: number
  }
>(function Waveform({ url, height = 44, playing, onPlayRequest, onFinish, barWidth = 2 }, ref) {
  const box = useRef<HTMLDivElement>(null)
  const ws = useRef<WaveSurfer | null>(null)
  const [ready, setReady] = useState(false)
  const [progress, setProgress] = useState(0)

  useEffect(() => {
    if (!box.current || !url) return
    const inst = WaveSurfer.create({
      container: box.current,
      url,
      height,
      waveColor: '#E3DFD6',
      progressColor: '#0F5E57',
      cursorWidth: 0,
      barWidth,
      barGap: 1,
      barRadius: 1,
      normalize: true,
      interact: true,
    })
    ws.current = inst
    inst.on('ready', () => setReady(true))
    inst.on('audioprocess', () => {
      const d = inst.getDuration()
      if (d > 0) setProgress(inst.getCurrentTime() / d)
    })
    inst.on('finish', () => {
      setProgress(1)
      onFinish?.()
    })
    inst.on('error', () => setReady(false))
    return () => {
      inst.destroy()
      ws.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, height])

  useEffect(() => {
    const inst = ws.current
    if (!inst || !ready) return
    if (playing) {
      inst.play().catch(() => undefined)
    } else {
      inst.pause()
    }
  }, [playing, ready])

  useImperativeHandle(ref, () => ({
    toggle: () => {
      if (onPlayRequest) onPlayRequest()
      else ws.current?.playPause()
    },
    seek: (ratio: number) => ws.current?.seekTo(Math.max(0, Math.min(1, ratio))),
  }))

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        onClick={() => (onPlayRequest ? onPlayRequest() : ws.current?.playPause())}
        className={clsx(
          'w-7 h-7 rounded-full flex items-center justify-center shrink-0 border transition-colors',
          playing ? 'bg-brand border-brand text-white' : 'bg-white border-line text-ink-soft hover:border-brand',
        )}
        title={playing ? '暂停' : '试听'}
      >
        <span className="text-[10px] leading-none">{playing ? '❚❚' : '▶'}</span>
      </button>
      <div className="relative flex-1 min-w-0" ref={box} />
      <span className="text-[10px] text-ink-faint w-8 text-right shrink-0">{Math.round(progress * 100)}%</span>
    </div>
  )
})

export default Waveform
