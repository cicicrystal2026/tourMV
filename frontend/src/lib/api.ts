/** 统一 API 客户端。所有接口路径与 PRD §9 一致。 */

export type Json = Record<string, any>

/**
 * 线上是「EdgeOne 边缘函数 → 跨境单进程后端」的两跳链路，偶发 502/504 或连接中断。
 * 这里对幂等请求（GET/HEAD）做自动重试来吸收抖动；
 * 写请求（POST/PUT/PATCH/DELETE）**绝不重试**——否则会重复建项目、重复入队生成、重复扣费。
 */
const RETRYABLE_STATUS = new Set([502, 503, 504])
const MAX_ATTEMPTS = 3
const REQUEST_TIMEOUT = 20000

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

async function fetchWithTimeout(path: string, init?: RequestInit): Promise<Response> {
  // 调用方自带 signal 时交给调用方控制，避免互相覆盖
  if (init?.signal) return fetch(path, init)
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT)
  try {
    return await fetch(path, { ...init, signal: ctl.signal })
  } finally {
    clearTimeout(timer)
  }
}

async function readErrorDetail(res: Response): Promise<string> {
  let detail = `${res.status} ${res.statusText}`
  try {
    const body = await res.json()
    if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
  } catch {
    /* 保留默认信息 */
  }
  return detail
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase()
  const canRetry = method === 'GET' || method === 'HEAD'
  const attempts = canRetry ? MAX_ATTEMPTS : 1
  let lastError: Error | null = null

  for (let attempt = 1; attempt <= attempts; attempt++) {
    if (attempt > 1) await sleep(300 * (attempt - 1) + Math.random() * 150)

    let res: Response
    try {
      res = await fetchWithTimeout(path, {
        headers: { 'Content-Type': 'application/json' },
        ...init,
      })
    } catch (e) {
      // 网络层错误：连接重置 / 超时 / 边缘层没吐出响应。
      // 浏览器原文是「Failed to fetch」，用户看不懂，换成能指导下一步的说法
      const aborted = e instanceof DOMException && e.name === 'AbortError'
      lastError = new Error(
        aborted
          ? '请求超时：后端响应太慢，页面会自动重试，也可以稍后刷新'
          : '连不上后端服务：可能服务未启动、正在重启或网络中断。恢复后页面会自动刷新',
      )
      continue
    }

    if (!res.ok) {
      if (res.status === 401) {
        // 访问口令未登录 / 已过期：通知 App 切到登录页（PRD V1.1 §11）
        window.dispatchEvent(new CustomEvent('wenlv:auth-required'))
      }
      lastError = new ApiError(await readErrorDetail(res), res.status)
      // 4xx 是业务错误，重试无意义；只重试网关类错误
      if (canRetry && attempt < attempts && RETRYABLE_STATUS.has(res.status)) continue
      throw lastError
    }

    const ct = res.headers.get('content-type') || ''
    return (ct.includes('application/json') ? res.json() : (res.text() as unknown)) as Promise<T>
  }

  throw lastError ?? new Error('请求失败')
}

/** 带 HTTP 状态码的错误：409 = 闸门不满足（detail 里是逐条原因，用「；」分隔） */
export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

/**
 * 同一路径的 GET 在途时复用同一个 Promise。
 * V1.0 的页面轮询不看上一轮是否返回，后端一慢请求就层层堆积（实测几秒内 84 个在途请求），
 * 越堆越慢、最后整页卡在「加载中」。合并在途请求后，慢的时候最多只有一个在排队。
 */
const inflight = new Map<string, Promise<any>>()
const get = <T = any,>(p: string): Promise<T> => {
  const hit = inflight.get(p)
  if (hit) return hit as Promise<T>
  const pr = req<T>(p).finally(() => inflight.delete(p))
  inflight.set(p, pr)
  return pr
}
const post = <T = any,>(p: string, body?: unknown) =>
  req<T>(p, { method: 'POST', body: JSON.stringify(body ?? {}) })
const put = <T = any,>(p: string, body?: unknown) =>
  req<T>(p, { method: 'PUT', body: JSON.stringify(body ?? {}) })
const del = <T = any,>(p: string) => req<T>(p, { method: 'DELETE' })

export const api = {
  // 访问控制
  authStatus: () => get('/api/auth/status'),
  login: (password: string) => post('/api/auth/login', { password }),
  logout: () => post('/api/auth/logout'),

  // 系统
  health: () => get('/api/health'),
  systemInfo: () => get('/api/system/info'),
  stateMachine: () => get('/api/state-machine'),

  // 看板
  dashboard: () => get('/api/dashboard/summary'),

  // 项目
  projects: (status?: string) => get(`/api/projects${status ? `?status=${status}` : ''}`),
  createProject: (body: Json) => post('/api/projects', body),
  project: (id: string) => get(`/api/projects/${id}`),
  advance: (id: string, body?: Json) => post(`/api/projects/${id}/advance`, body ?? {}),
  reject: (id: string, target: string) => post(`/api/projects/${id}/reject`, { target }),
  /** 已交付（锁定）项目新建修订版 */
  revise: (id: string) => post(`/api/projects/${id}/revise`),
  projectCalls: (id: string) => get(`/api/projects/${id}/calls`),
  addMetric: (id: string, body: Json) => post(`/api/projects/${id}/metrics`, body),

  // 歌词
  lyrics: (id: string) => get(`/api/projects/${id}/lyrics`),
  generateLyrics: (id: string, sync = false) =>
    post(`/api/projects/${id}/lyrics/generate${sync ? '?sync=true' : ''}`),
  recheck: (id: string) => post(`/api/projects/${id}/lyrics/check`),
  saveLyrics: (id: string, version: number, sections: Json[], source = 'manual') =>
    put(`/api/projects/${id}/lyrics/${version}`, { sections, source }),
  rewriteLyrics: (id: string, targets: Json[], instruction = '') =>
    post(`/api/projects/${id}/lyrics/rewrite`, { targets, instruction }),
  rollbackLyrics: (id: string, version: number) => post(`/api/projects/${id}/lyrics/rollback/${version}`),
  adoptCheck: (checkId: string, index = 0) => post(`/api/lyrics/checks/${checkId}/adopt`, { index }),
  /** 保留原文：必须写理由（PRD V1.1 F2-5） */
  keepCheck: (checkId: string, reason: string) => post(`/api/lyrics/checks/${checkId}/keep`, { reason }),

  // 音乐
  musicModels: () => get('/api/music/models'),
  updateMusicModel: (key: string, body: Json) => put(`/api/music/models/${key}`, body),
  generateMusic: (id: string, body: Json) => post(`/api/projects/${id}/music/generate`, body),
  /** 是否盲听由轮次决定（选型轮未揭晓即盲听），前端无法关闭 */
  candidates: (id: string, roundId?: string) =>
    get(`/api/projects/${id}/music/candidates${roundId ? `?round_id=${roundId}` : ''}`),
  rate: (cid: string, body: Json) => put(`/api/music/candidates/${cid}/rating`, body),
  selectCandidate: (id: string, cid: string) => post(`/api/projects/${id}/music/select`, { candidate_id: cid }),
  regenerate: (id: string, variants = 2) => post(`/api/projects/${id}/music/regenerate`, { variants }),

  // 剪辑
  timeline: (id: string) => get(`/api/projects/${id}/timeline`),
  autoTimeline: (id: string, sync = true) => post(`/api/projects/${id}/timeline/auto`, { sync }),
  resnap: (id: string) => post(`/api/projects/${id}/timeline/resnap`),
  clipCandidates: (id: string, clipId: string) =>
    get(`/api/projects/${id}/timeline/clips/${clipId}/candidates`),
  updateClip: (id: string, clipId: string, body: Json) =>
    put(`/api/projects/${id}/timeline/clips/${clipId}`, body),
  render: (id: string, body: Json = {}) => post(`/api/projects/${id}/render`, body),
  renders: (id: string) => get(`/api/projects/${id}/renders`),
  renderVariants: () => get('/api/render/variants'),
  // 重做歌词强制对齐（音画同步）→ 自动按新时间轴重新卡点
  realignTimeline: (id: string) => post(`/api/projects/${id}/timeline/align`),
  alignStatus: () => get('/api/system/align'),

  // 素材
  assets: (params: { q?: string; filters?: Json; onlyUsable?: boolean } = {}) => {
    const sp = new URLSearchParams()
    if (params.q) sp.set('q', params.q)
    if (params.filters && Object.keys(params.filters).length)
      sp.set('filters', JSON.stringify(params.filters))
    if (params.onlyUsable) sp.set('only_usable', 'true')
    const qs = sp.toString()
    return get(`/api/assets${qs ? `?${qs}` : ''}`)
  },
  assetFacets: () => get('/api/assets/facets'),
  asset: (id: string) => get(`/api/assets/${id}`),
  updateAsset: (id: string, body: Json) => put(`/api/assets/${id}`, body),
  retagAsset: (id: string, hint = '') => post(`/api/assets/${id}/retag`, { hint }),
  deleteAsset: (id: string) => del(`/api/assets/${id}`),
  seedAssets: () => post('/api/assets/seed'),
  /**
   * 用文生图按目的地生成真实景点素材，替换占位色块。
   * 这是长任务（6 张约 1 分钟），必须绕开默认的 20 秒超时，否则必然被 abort。
   */
  generateAiAssets: async (body: {
    destination: string
    theme?: string
    count?: number
    overwrite?: boolean
  }) => {
    const ctl = new AbortController()
    // 文生图每张实测 60–90 秒，上限 12 张 → 最坏 18 分钟。
    // 这里给到 25 分钟，否则张数调大时会中途 abort，前功尽弃。
    const timer = setTimeout(() => ctl.abort(), 25 * 60 * 1000)
    try {
      const res = await fetch('/api/assets/generate-ai', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: ctl.signal,
      })
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`
        try {
          const j = await res.json()
          if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
        } catch {
          /* 保留默认信息 */
        }
        throw new Error(detail)
      }
      return res.json()
    } finally {
      clearTimeout(timer)
    }
  },
  assetExportUrl: '/api/assets/export',
  uploadAssets: async (files: File[], fields: Record<string, string>) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    Object.entries(fields).forEach(([k, v]) => fd.append(k, v))
    const res = await fetch('/api/assets/upload', { method: 'POST', body: fd })
    if (!res.ok) throw new Error(`${res.status} 上传失败`)
    return res.json()
  },

  // 逐句配图（分镜审核）
  storyboard: (id: string) => get(`/api/projects/${id}/storyboard`),
  storyboardPlan: (id: string, force = false) =>
    post(`/api/projects/${id}/storyboard/plan?force=${force ? 'true' : 'false'}`, {}),
  /** 整批出图走队列（18 句要跑十几分钟，不该占住请求） */
  storyboardEnqueue: (id: string, provider = '') =>
    post(`/api/projects/${id}/storyboard/enqueue?provider=${encodeURIComponent(provider)}`, {}),
  storyboardApprove: (id: string, shotId: string) =>
    post(`/api/projects/${id}/storyboard/shots/${shotId}/approve`, {}),
  storyboardReject: (id: string, shotId: string, note = '') =>
    post(`/api/projects/${id}/storyboard/shots/${shotId}/reject?note=${encodeURIComponent(note)}`, {}),
  storyboardRegenerate: (id: string, shotId: string, body: { prompt?: string; provider?: string } = {}) => {
    const q = new URLSearchParams()
    if (body.prompt) q.set('prompt', body.prompt)
    if (body.provider) q.set('provider', body.provider)
    return post(`/api/projects/${id}/storyboard/shots/${shotId}/regenerate?${q}`, {})
  },
  storyboardApproveAll: (id: string) => post(`/api/projects/${id}/storyboard/approve-all`, {}),
  storyboardUpload: async (id: string, shotId: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    const res = await fetch(`/api/projects/${id}/storyboard/shots/${shotId}/upload`, {
      method: 'POST',
      body: fd,
    })
    if (!res.ok) {
      let detail = `${res.status} 上传失败`
      try {
        const j = await res.json()
        if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
      } catch {
        /* 保留默认信息 */
      }
      throw new Error(detail)
    }
    return res.json()
  },

  // 热歌
  trendCards: (week?: string) => get(`/api/trends/cards${week ? `?week=${week}` : ''}`),
  trendsStatus: () => get('/api/trends/status'),
  syncTrends: (sync = true, days = 30, top = 10) => post('/api/trends/sync', { sync, days, top }),

  // 任务
  jobs: (status?: string) => get(`/api/jobs${status ? `?status=${status}` : ''}`),
  retryJob: (id: string) => post(`/api/jobs/${id}/retry`),
}

export default api
