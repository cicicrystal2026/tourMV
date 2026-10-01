import { NavLink } from 'react-router-dom'
import clsx from 'clsx'

const NAV = [
  { to: '/dashboard', label: '生产看板', hint: '项目全景与待办' },
  { to: '/lyrics', label: '词曲工作台', hint: '写词 · 生曲 · 选曲' },
  { to: '/storyboard', label: '配图审核', hint: '逐句景区图 · 审核替换' },
  { to: '/edit', label: '剪辑工作台', hint: '粗剪 · 精剪 · 导出' },
  { to: '/assets', label: '素材库管理', hint: '上传 · 打标 · 检索' },
]

const PLACEHOLDER = [
  { label: '简报确认', version: 'V2.0' },
  { label: '合规审核台', version: 'V2.0' },
  { label: '发布排期', version: 'V3.0' },
]

export default function Sidebar({ info, online }: { info: any; online: boolean | null }) {
  const dev = info?.device
  return (
    <aside className="w-[224px] shrink-0 bg-sidebar text-white/85 flex flex-col h-screen sticky top-0">
      <div className="px-5 pt-6 pb-5">
        <div className="font-serif text-[16px] text-white leading-snug">文旅MV</div>
        <div className="text-[11px] text-white/40 mt-0.5">生产管线 V1.0 · 实验室</div>
      </div>

      <nav className="px-3 flex flex-col gap-0.5">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            className={({ isActive }) =>
              clsx(
                'px-3 py-2.5 rounded-btn transition-colors',
                isActive ? 'bg-white/[0.10] text-white' : 'text-white/60 hover:bg-white/[0.06] hover:text-white/90',
              )
            }
          >
            <div className="text-[13px]">{n.label}</div>
            <div className="text-[11px] text-white/35 mt-0.5">{n.hint}</div>
          </NavLink>
        ))}
      </nav>

      <div className="px-3 mt-5">
        <div className="px-3 text-[11px] text-white/30 mb-1.5">后续版本</div>
        {PLACEHOLDER.map((p) => (
          <div
            key={p.label}
            className="px-3 py-2 rounded-btn text-white/25 flex items-center justify-between cursor-not-allowed"
            title="V1.0 不包含"
          >
            <span className="text-[13px]">{p.label}</span>
            <span className="text-[10px] border border-white/15 rounded-full px-1.5 py-px">{p.version}</span>
          </div>
        ))}
      </div>

      <div className="mt-auto px-5 py-4 border-t border-white/10">
        <div className="flex items-center gap-2 text-[11px]">
          <span
            className={clsx(
              'w-1.5 h-1.5 rounded-full',
              online ? 'bg-[#5DCAA5]' : online === false ? 'bg-[#F0997B]' : 'bg-white/30',
            )}
          />
          <span className="text-white/55">
            {online === false ? '后端离线' : online ? '后端已连接' : '连接中'}
          </span>
        </div>
        {dev && (
          <>
            <div className="mt-2 text-[11px] text-white/40">
              本地 4090 · {dev.online ? '在线' : '离线'} · 运行 {dev.running} / 排队 {dev.queued}
            </div>
            {dev.gpu_vram_mb > 0 && (
              <div className="mt-0.5 text-[11px] text-white/30">显存 {dev.gpu_vram_mb} MB</div>
            )}
          </>
        )}
        {info && (
          <div className="mt-2 flex flex-wrap gap-1">
            <span className="text-[10px] px-1.5 py-px rounded-full border border-white/15 text-white/40">
              {info.force_mock ? '兜底模式' : '真实模型'}
            </span>
            <span className="text-[10px] px-1.5 py-px rounded-full border border-white/15 text-white/40">
              {info.clip_encoder === 'chinese-clip' ? 'CLIP' : '哈希向量'}
            </span>
            {!info.ffmpeg && (
              <span className="text-[10px] px-1.5 py-px rounded-full border border-[#F0997B]/40 text-[#F0997B]">
                无 ffmpeg
              </span>
            )}
          </div>
        )}
      </div>
    </aside>
  )
}
