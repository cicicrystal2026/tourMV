/** 展示层格式化与共用常量。 */
import type { Json } from './api'

export const COLUMNS = ['创作简报', '词曲生成', '影像匹配', '多版本合成', '交付']

export const DEMO_MARK = '模拟'

export function fmtDuration(sec?: number | null): string {
  const s = Math.max(0, Math.round(sec || 0))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export function fmtTime(sec?: number | null, withMs = false): string {
  const v = Math.max(0, sec || 0)
  const m = Math.floor(v / 60)
  const s = v % 60
  return `${String(m).padStart(2, '0')}:${withMs ? s.toFixed(2).padStart(5, '0') : String(Math.floor(s)).padStart(2, '0')}`
}

export function fmtViews(n?: number | null): string {
  const v = n || 0
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `${(v / 1e3).toFixed(0)}K`
  return String(v)
}

export function fmtDate(v?: string | null): string {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return v.slice(0, 16).replace('T', ' ')
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function relative(v?: string | null): string {
  if (!v) return '—'
  const t = new Date(v).getTime()
  if (Number.isNaN(t)) return '—'
  const diff = Math.round((Date.now() - t) / 1000)
  if (diff < 60) return `${diff} 秒前`
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

export const LICENSE_LABEL: Record<string, string> = {
  licensed: '已授权',
  expiring: '即将到期',
  missing: '缺授权',
}

// 成片版本标签：**仅用于展示已生成记录**。可勾选的版本列表从
// /api/render/variants 取（后端是单一来源，改版本不用动前端）。
// 这里保留历史 key，避免老项目显示出裸的英文 key。
export const VARIANT_LABEL: Record<string, string> = {
  landscape: '横版 1920×1080 · 中文字幕',
  portrait: '竖版 1080×1920 · 中文字幕',
  sub_zh_en: '横版 1920×1080 · 中英字幕',
  short: '短版竖屏 2:30–2:50 · 中英字幕',
  clean: '横版 1920×1080 · 无字幕（供二次剪辑）',
  // 历史版本：sub_zh 当年就是「横版 + 中文字幕」，等价于现在的 landscape
  sub_zh: '横版 1920×1080 · 中文字幕（旧版）',
}

export const JOB_STATUS_LABEL: Record<string, string> = {
  queued: '排队',
  running: '运行中',
  done: '完成',
  failed: '失败',
}

export const CHECK_TYPE_LABEL: Record<string, string> = {
  rhyme: '押韵',
  meter: '字数节奏',
  fact: '史实',
}

export const TAG_DIM_LABEL: Record<string, string> = {
  place: '地点',
  season: '季节',
  time: '时段',
  weather: '天气',
  mood: '情绪',
  camera: '镜头运动',
  subject: '画面主体',
}

export function statusTone(status?: string): 'brand' | 'human' | 'muted' {
  if (!status) return 'muted'
  if (status === 'done' || status === 'delivered') return 'brand'
  if (status === 'failed') return 'human'
  if (status === 'running') return 'brand'
  return 'muted'
}

export function flatLineCount(sections: Json[]): number {
  return sections.reduce((n, s) => n + (s.lines?.length || 0), 0)
}
