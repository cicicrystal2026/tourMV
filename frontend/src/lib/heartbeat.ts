import { useEffect } from 'react'
import api from './api'

/**
 * 有效工时心跳（PRD V1.1 F1-11）。
 *
 * 只在「页面在前台 + 最近 60 秒内有过键鼠操作」时每 30 秒报一次，
 * 后端只在项目处于人工节点时计入。这样挂着页面过夜不会被算成 8 小时工时。
 */
export function useWorkHeartbeat(projectId?: string, enabled = true) {
  useEffect(() => {
    if (!projectId || !enabled) return
    let last = Date.now()
    const touch = () => (last = Date.now())
    const evs = ['mousemove', 'keydown', 'click', 'scroll', 'touchstart'] as const
    evs.forEach((e) => window.addEventListener(e, touch, { passive: true }))
    const t = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return
      if (Date.now() - last > 60_000) return
      api.workHeartbeat(projectId).catch(() => {})
    }, 30_000)
    return () => {
      clearInterval(t)
      evs.forEach((e) => window.removeEventListener(e, touch))
    }
  }, [projectId, enabled])
}
