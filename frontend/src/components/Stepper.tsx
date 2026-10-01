import clsx from 'clsx'

const STEPS = ['创作简报', '词曲生成', '影像匹配', '多版本合成', '交付']

export default function Stepper({ column, status }: { column?: string; status?: string }) {
  const idx = Math.max(0, STEPS.indexOf(column || '创作简报'))
  const human = ['lyric_review', 'music_review', 'edit_review'].includes(status || '')
  return (
    <div className="flex items-center gap-0">
      {STEPS.map((s, i) => {
        const state = i < idx ? 'done' : i === idx ? 'current' : 'todo'
        return (
          <div key={s} className="flex items-center">
            <div className="flex items-center gap-2">
              <span
                className={clsx(
                  'flex items-center justify-center w-5 h-5 rounded-full text-[11px] border transition-colors',
                  state === 'done' && 'bg-brand border-brand text-white',
                  state === 'current' &&
                    (human ? 'bg-human-soft border-human text-human-dark' : 'bg-brand-soft border-brand text-brand-dark'),
                  state === 'todo' && 'bg-white border-line text-ink-faint',
                )}
              >
                {state === 'done' ? '✓' : i + 1}
              </span>
              <span
                className={clsx(
                  'text-[12px] whitespace-nowrap',
                  state === 'todo' ? 'text-ink-faint' : state === 'current' ? 'text-ink font-medium' : 'text-ink-soft',
                )}
              >
                {s}
              </span>
              {state === 'current' && human && <span className="tag-human">待人工</span>}
            </div>
            {i < STEPS.length - 1 && (
              <span className={clsx('mx-3 h-px w-6', i < idx ? 'bg-brand' : 'bg-line')} />
            )}
          </div>
        )
      })}
    </div>
  )
}
