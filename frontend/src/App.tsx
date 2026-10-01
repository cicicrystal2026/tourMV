import { useCallback, useEffect, useRef, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import Sidebar from './components/Sidebar'
import Dashboard from './pages/Dashboard'
import LyricsPage from './pages/Lyrics'
import EditPage from './pages/Edit'
import AssetsPage from './pages/Assets'
import StoryboardPage from './pages/Storyboard'
import api from './lib/api'

/** 本地开发（没启后端基本就是自己忘了）与线上（多为网络抖动）要给出完全不同的提示 */
const IS_LOCAL = /^(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)$/.test(location.hostname)

/**
 * 失败后的静默重探节奏。线上链路是「边缘函数 → 跨境后端」，
 * 单次请求偶发要 8~12 秒，挂载时探一次就下结论会误报「后端未连接」。
 */
const RETRY_DELAYS = [2000, 5000, 10000]

export default function App() {
  const [info, setInfo] = useState<any>(null)
  const [online, setOnline] = useState<boolean | null>(null)
  const [exhausted, setExhausted] = useState(false)
  const attemptRef = useRef(0)
  const timerRef = useRef<number | undefined>(undefined)

  const probe = useCallback(async (): Promise<void> => {
    try {
      // 双探针：system/info 失败时退回 health，任一成功即视为在线。
      // 单个接口的偶发抖动不应该判定整个后端不可用。
      let detail: any = null
      try {
        detail = await api.systemInfo()
      } catch {
        await api.health()
      }
      setOnline(true)
      setExhausted(false)
      attemptRef.current = 0
      // health 兜底时结构不含 device/ffmpeg 等字段，别覆盖已有信息
      if (detail) setInfo(detail)
    } catch {
      const attempt = attemptRef.current + 1
      attemptRef.current = attempt
      if (attempt <= RETRY_DELAYS.length) {
        timerRef.current = window.setTimeout(probe, RETRY_DELAYS[attempt - 1])
      } else {
        setOnline(false)
        setExhausted(true)
      }
    }
  }, [])

  useEffect(() => {
    probe()
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [probe])

  const retry = () => {
    if (timerRef.current) clearTimeout(timerRef.current)
    attemptRef.current = 0
    setOnline(null)
    setExhausted(false)
    probe()
  }

  return (
    <div className="flex min-h-screen bg-page">
      <Sidebar info={info} online={online} />
      <main className="flex-1 min-w-0 max-w-screen flex flex-col">
        {exhausted && (
          <div className="m-4 px-4 py-3 rounded-card border border-human/40 bg-human-soft text-human-dark text-[13px] flex items-center gap-3">
            {IS_LOCAL ? (
              <span>
                后端未连接。请先在 <code className="font-mono">backend/</code> 下启动
                <code className="font-mono"> uvicorn app.main:app --reload</code>。
              </span>
            ) : (
              <span>后端暂时不可达（可能正在冷启动或网络抖动）。已自动重试 3 次。</span>
            )}
            <button
              onClick={retry}
              className="ml-auto shrink-0 px-3 py-1 rounded-card border border-human/50 hover:bg-human/10"
            >
              重新探测
            </button>
          </div>
        )}
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/projects/:projectId/lyrics" element={<LyricsPage />} />
          <Route path="/projects/:projectId/storyboard" element={<StoryboardPage />} />
          <Route path="/projects/:projectId/edit" element={<EditPage />} />
          <Route path="/lyrics" element={<LyricsPage />} />
          <Route path="/storyboard" element={<StoryboardPage />} />
          <Route path="/edit" element={<EditPage />} />
          <Route path="/assets" element={<AssetsPage />} />
        </Routes>
      </main>
    </div>
  )
}
