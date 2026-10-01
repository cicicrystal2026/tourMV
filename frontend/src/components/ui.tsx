import { useEffect } from 'react'
import type { ReactNode } from 'react'
import clsx from 'clsx'

export function PageHeader({
  title,
  subtitle,
  actions,
  crumbs,
}: {
  title: string
  subtitle?: ReactNode
  actions?: ReactNode
  crumbs?: ReactNode
}) {
  return (
    <header className="px-6 pt-5 pb-4 border-b border-line bg-page sticky top-0 z-10">
      {crumbs && <div className="text-[12px] text-ink-faint mb-1.5">{crumbs}</div>}
      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <h1 className="font-serif text-[22px] font-medium text-ink leading-tight">{title}</h1>
          {subtitle && <div className="mt-1 text-[12px] text-ink-soft">{subtitle}</div>}
        </div>
        {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
      </div>
    </header>
  )
}

export function Section({
  title,
  right,
  children,
  className,
  bodyClass,
}: {
  title?: ReactNode
  right?: ReactNode
  children: ReactNode
  className?: string
  bodyClass?: string
}) {
  return (
    <section className={clsx('card', className)}>
      {title && (
        <div className="flex items-center justify-between gap-3 px-4 h-11 border-b border-line">
          <h2 className="text-[13px] font-medium text-ink">{title}</h2>
          {right}
        </div>
      )}
      <div className={clsx('p-4', bodyClass)}>{children}</div>
    </section>
  )
}

export function Banner({
  tone = 'info',
  children,
  onClose,
}: {
  tone?: 'info' | 'warn' | 'error' | 'ok'
  children: ReactNode
  onClose?: () => void
}) {
  const tones = {
    info: 'bg-white border-line text-ink-soft',
    warn: 'bg-human-soft border-human/35 text-human-dark',
    error: 'bg-[#FCEBEB] border-[#F09595] text-[#A32D2D]',
    ok: 'bg-brand-soft border-brand-line text-brand-dark',
  }[tone]
  return (
    <div className={clsx('flex items-start gap-3 px-3 py-2 rounded-btn border text-[12px]', tones)}>
      <div className="flex-1 leading-relaxed">{children}</div>
      {onClose && (
        <button className="text-[12px] opacity-60 hover:opacity-100 shrink-0" onClick={onClose}>
          关闭
        </button>
      )}
    </div>
  )
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-[12px] text-ink-faint">
      <span className="inline-block w-3.5 h-3.5 border-2 border-line border-t-brand rounded-full animate-spin" />
      {label || '加载中'}
    </div>
  )
}

export function Empty({ text, action }: { text: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-10 text-ink-faint text-[13px]">
      <span>{text}</span>
      {action}
    </div>
  )
}

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
  width = 'max-w-[560px]',
}: {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  width?: string
}) {
  // Esc 关闭（V1.0 只能点 ×）
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])
  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-ink/35 pt-[8vh] px-4"
      onMouseDown={(e) => {
        // 点遮罩关闭；点弹窗内部不关
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={clsx('w-full bg-white rounded-card border border-line flex flex-col max-h-[82vh]', width)}
      >
        <div className="flex items-center justify-between px-4 h-12 border-b border-line shrink-0">
          <h3 className="text-[14px] font-medium">{title}</h3>
          <button className="text-ink-faint hover:text-ink text-[16px] leading-none" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>
        <div className="p-4 overflow-y-auto flex-1">{children}</div>
        {footer && (
          <div className="flex items-center justify-end gap-2 px-4 h-14 border-t border-line shrink-0">
            {footer}
          </div>
        )}
      </div>
    </div>
  )
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
      {hint && <span className="block mt-1 text-[11px] text-ink-faint">{hint}</span>}
    </label>
  )
}

export function Chip({
  active,
  children,
  onClick,
  tone = 'brand',
}: {
  active?: boolean
  children: ReactNode
  onClick?: () => void
  tone?: 'brand' | 'human'
}) {
  const on = tone === 'brand' ? 'bg-brand text-white border-brand' : 'bg-human text-white border-human'
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        'h-7 px-2.5 rounded-full border text-[12px] transition-colors whitespace-nowrap',
        active ? on : 'bg-white border-line text-ink-soft hover:border-ink-faint',
      )}
    >
      {children}
    </button>
  )
}

export function ScoreDots({
  value,
  onChange,
  max = 5,
  readOnly,
}: {
  value: number
  onChange?: (v: number) => void
  max?: number
  readOnly?: boolean
}) {
  return (
    <div className="flex items-center gap-1">
      {Array.from({ length: max }, (_, i) => i + 1).map((n) => (
        <button
          key={n}
          type="button"
          disabled={readOnly}
          onClick={() => onChange?.(n)}
          title={`${n} 分`}
          className={clsx(
            'w-5 h-5 rounded-[5px] border text-[10px] leading-none transition-colors',
            n <= value
              ? 'bg-brand border-brand text-white'
              : 'bg-white border-line text-ink-faint hover:border-ink-faint',
            readOnly && 'cursor-default',
          )}
        >
          {n}
        </button>
      ))}
    </div>
  )
}

export function StateDot({ status }: { status: string }) {
  const map: Record<string, string> = {
    queued: 'bg-[#B4B2A9]',
    running: 'bg-brand animate-pulse',
    done: 'bg-brand',
    failed: 'bg-human',
    delivered: 'bg-brand',
  }
  return <span className={clsx('inline-block w-2 h-2 rounded-full shrink-0', map[status] || 'bg-[#B4B2A9]')} />
}

export function ProgressBar({ value, tone = 'brand' }: { value: number; tone?: 'brand' | 'human' }) {
  return (
    <div className="h-1.5 w-full rounded-full bg-line overflow-hidden">
      <div
        className={clsx('h-full rounded-full transition-all', tone === 'brand' ? 'bg-brand' : 'bg-human')}
        style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
      />
    </div>
  )
}
