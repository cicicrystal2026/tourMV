import { useCallback, useEffect, useState } from 'react'
import clsx from 'clsx'
import api from '../lib/api'
import { relative } from '../lib/format'
import { Banner, Empty, Field, PageHeader, ScoreDots, Section, Spinner } from '../components/ui'

/**
 * 模型对比台：同一个输入交给多个模型，并排比较效果、费用与用时，挑「效果最好且最划算」的组合。
 * - 生图：同一句歌词 → 多个生图模型各出一张（费用占单支成本约 88%，是选型重点）；
 * - 写词：同一份简报 → 多个文字模型各写一首（费用不到 1%，只看效果）；
 * - 生曲：用词曲工作台的「选型轮」盲听，这里只汇总战绩。
 */
export default function BenchPage() {
  const [tab, setTab] = useState<'image' | 'lyric' | 'summary'>('image')
  const [opts, setOpts] = useState<any>(null)
  const [runs, setRuns] = useState<any[]>([])
  const [sum, setSum] = useState<any>(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      const [o, r, s] = await Promise.all([api.benchOptions(), api.benchRuns(), api.benchSummary()])
      setOpts(o)
      setRuns(r)
      setSum(s)
    } catch (e: any) {
      setErr(e?.message || String(e))
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // 有对比在跑时轮询（setTimeout 链，上一轮没回来不发下一轮）
  const anyBusy = runs.some((r) => r.busy)
  useEffect(() => {
    if (!anyBusy) return
    let alive = true
    let t: number | undefined
    const tick = async () => {
      await load()
      if (alive) t = window.setTimeout(tick, 3000)
    }
    t = window.setTimeout(tick, 3000)
    return () => {
      alive = false
      if (t) clearTimeout(t)
    }
  }, [anyBusy, load])

  const act = async (key: string, fn: () => Promise<any>, ok?: string) => {
    setBusy(key)
    setErr('')
    setMsg('')
    try {
      await fn()
      if (ok) setMsg(ok)
      await load()
    } catch (e: any) {
      setErr(e?.message || String(e))
    } finally {
      setBusy('')
    }
  }

  if (!opts) return <div className="p-6">{err ? <Banner tone="error">{err}</Banner> : <Spinner label="加载对比台" />}</div>

  return (
    <>
      <PageHeader
        title="模型对比台"
        subtitle="同一个输入交给多个模型，并排比较效果、费用与用时。每个结果的费用是按真实用量记的"
        actions={
          <a className="btn" href={api.benchExportUrl} download>
            导出对比明细（CSV）
          </a>
        }
      />
      <div className="p-6 flex flex-col gap-4">
        {err && <Banner tone="error" onClose={() => setErr('')}>{err}</Banner>}
        {msg && <Banner tone="ok" onClose={() => setMsg('')}>{msg}</Banner>}

        <div className="flex items-center gap-2">
          {(
            [
              ['image', '生图对比'],
              ['lyric', '写词对比'],
              ['summary', '汇总与性价比'],
            ] as const
          ).map(([k, label]) => (
            <button key={k} className={clsx('btn', tab === k && 'border-brand text-brand bg-brand-soft/40')} onClick={() => setTab(k)}>
              {label}
            </button>
          ))}
          <span className="text-[11px] text-ink-faint ml-2">
            生曲对比在词曲工作台用「选型轮」盲听；战绩汇总在这里的「汇总与性价比」。
          </span>
        </div>

        {tab === 'image' && <ImageForm opts={opts} busy={busy} act={act} />}
        {tab === 'lyric' && <LyricForm opts={opts} busy={busy} act={act} />}
        {tab === 'summary' && <Summary sum={sum} opts={opts} />}

        {tab !== 'summary' && (
          <div className="flex flex-col gap-4">
            {runs.filter((r) => r.kind === tab).length === 0 ? (
              <Empty text={tab === 'image' ? '还没有生图对比。在上面填一句歌词，勾选模型，点「开始对比」' : '还没有写词对比。在上面填目的地和曲风，勾选模型，点「开始对比」'} />
            ) : (
              runs
                .filter((r) => r.kind === tab)
                .map((r) => <RunCard key={r.id} run={r} busy={busy} act={act} />)
            )}
          </div>
        )}
      </div>
    </>
  )
}

function ImageForm({ opts, busy, act }: { opts: any; busy: string; act: any }) {
  const [form, setForm] = useState({ destination: '', place: '', line: '', prompt: '' })
  const ready = opts.image.filter((x: any) => x.ready)
  const [picked, setPicked] = useState<string[]>(ready.map((x: any) => x.key))
  const [projects, setProjects] = useState<any[]>([])
  const [shots, setShots] = useState<any[]>([])
  const [pid, setPid] = useState('')

  useEffect(() => {
    api.projects().then((p: any[]) => setProjects(p || [])).catch(() => {})
  }, [])
  useEffect(() => {
    if (!pid) return setShots([])
    api.benchShots(pid).then(setShots).catch(() => setShots([]))
  }, [pid])

  const est = picked.reduce((a, k) => a + (opts.image.find((x: any) => x.key === k)?.price || 0) + 0.004, 0)

  return (
    <Section title="发起生图对比" bodyClass="p-4">
      <div className="grid grid-cols-2 gap-3">
        <Field label="从已有项目挑一句（可选）" hint="沿用那一句的景点和画面描述，省得自己写">
          <div className="flex gap-2">
            <select className="input flex-1" value={pid} onChange={(e) => setPid(e.target.value)}>
              <option value="">不沿用，手动填写</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
            <select
              className="input flex-1"
              disabled={!shots.length}
              value=""
              onChange={(e) => {
                const s = shots.find((x) => x.id === e.target.value)
                const proj = projects.find((p) => p.id === pid)
                if (s) setForm({ destination: proj?.destination || '', place: s.place || '', line: s.line || '', prompt: s.prompt || '' })
              }}
            >
              <option value="">{shots.length ? '选择歌词句…' : '先选项目'}</option>
              {shots.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.order}. {s.line}
                </option>
              ))}
            </select>
          </div>
        </Field>
        <Field label="目的地">
          <input className="input" placeholder="如 杭州·西湖" value={form.destination} onChange={(e) => setForm({ ...form, destination: e.target.value })} />
        </Field>
        <Field label="歌词句" hint="机器预审会按这句判断图对不对题">
          <input className="input" placeholder="如 断桥柳影斜照苏堤" value={form.line} onChange={(e) => setForm({ ...form, line: e.target.value })} />
        </Field>
        <Field label="景点">
          <input className="input" placeholder="如 断桥" value={form.place} onChange={(e) => setForm({ ...form, place: e.target.value })} />
        </Field>
        <div className="col-span-2">
          <Field label="画面描述（提示词）" hint="留空则用「景点 + 目的地 + 歌词」；所有模型用同一段描述，才是公平对比">
            <textarea className="input h-[68px] py-2" value={form.prompt} onChange={(e) => setForm({ ...form, prompt: e.target.value })} />
          </Field>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-[12px] text-ink-soft">参加对比的模型：</span>
        {opts.image.map((x: any) => (
          <label
            key={x.key}
            title={x.ready ? `${x.model} · ${x.size}` : x.note}
            className={clsx(
              'flex items-center gap-1.5 px-2.5 h-8 rounded-btn border text-[12px]',
              !x.ready ? 'opacity-50 border-line' : picked.includes(x.key) ? 'border-brand bg-brand-soft/50 cursor-pointer' : 'border-line cursor-pointer',
            )}
          >
            <input
              type="checkbox"
              className="accent-[#0F5E57]"
              disabled={!x.ready}
              checked={picked.includes(x.key)}
              onChange={(e) => setPicked(e.target.checked ? [...picked, x.key] : picked.filter((k) => k !== x.key))}
            />
            {x.label}
            <span className="text-ink-faint">{x.ready ? `¥${x.price.toFixed(2)}/张 · ${x.size}` : '未配置'}</span>
          </label>
        ))}
        <button
          className="btn-primary ml-auto"
          disabled={!!busy || picked.length === 0 || !(form.prompt || form.line || form.place)}
          onClick={() => act('image', () => api.benchImage({ ...form, providers: picked }), '已开始出图，完成后自动刷新')}
        >
          {busy === 'image' ? '提交中…' : `开始对比（预计 ¥${est.toFixed(2)}）`}
        </button>
      </div>
    </Section>
  )
}

function LyricForm({ opts, busy, act }: { opts: any; busy: string; act: any }) {
  const [form, setForm] = useState({ destination: '', genre: '', mood: '', duration_s: 120, style_prompt: '' })
  const [picked, setPicked] = useState<string[]>(opts.lyric.filter((x: any) => x.current).map((x: any) => x.model))
  const [extra, setExtra] = useState('')
  const all: any[] = [...opts.lyric, ...picked.filter((m) => !opts.lyric.some((x: any) => x.model === m)).map((m) => ({ model: m, price: null }))]

  return (
    <Section title="发起写词对比" bodyClass="p-4">
      {!opts.llm_ready && <Banner tone="warn">文字模型未配置，无法对比。</Banner>}
      <div className="grid grid-cols-4 gap-3">
        <Field label="目的地">
          <input className="input" placeholder="如 杭州·西湖" value={form.destination} onChange={(e) => setForm({ ...form, destination: e.target.value })} />
        </Field>
        <Field label="曲风">
          <input className="input" placeholder="如 city pop" value={form.genre} onChange={(e) => setForm({ ...form, genre: e.target.value })} />
        </Field>
        <Field label="情绪（顿号分隔）">
          <input className="input" placeholder="如 怀旧、治愈" value={form.mood} onChange={(e) => setForm({ ...form, mood: e.target.value })} />
        </Field>
        <Field label="时长（秒）" hint="决定写几段几行">
          <input type="number" className="input" value={form.duration_s} onChange={(e) => setForm({ ...form, duration_s: Number(e.target.value) })} />
        </Field>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-[12px] text-ink-soft">参加对比的模型：</span>
        {all.map((x) => (
          <label
            key={x.model}
            className={clsx('flex items-center gap-1.5 px-2.5 h-8 rounded-btn border text-[12px] cursor-pointer', picked.includes(x.model) ? 'border-brand bg-brand-soft/50' : 'border-line')}
            title={x.price ? `输入 ¥${x.price[0]} / 输出 ¥${x.price[1]}（每百万 token）` : '价格表里没有这个模型，费用会显示为「未配置单价」'}
          >
            <input
              type="checkbox"
              className="accent-[#0F5E57]"
              checked={picked.includes(x.model)}
              onChange={(e) => setPicked(e.target.checked ? [...picked, x.model] : picked.filter((k) => k !== x.model))}
            />
            {x.model}
            {x.current && <span className="tag">当前在用</span>}
            {!x.price && <span className="text-ink-faint">未配置单价</span>}
          </label>
        ))}
        <input
          className="input h-8 w-[170px]"
          placeholder="加一个模型名，回车"
          value={extra}
          onChange={(e) => setExtra(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && extra.trim()) {
              setPicked([...new Set([...picked, extra.trim()])])
              setExtra('')
            }
          }}
        />
        <button
          className="btn-primary ml-auto"
          disabled={!!busy || picked.length === 0 || !form.destination.trim() || !opts.llm_ready}
          onClick={() =>
            act(
              'lyric',
              () => api.benchLyric({ ...form, mood: form.mood.split(/[、,，\s]+/).filter(Boolean), models: picked }),
              '已开始写词，完成后自动刷新',
            )
          }
        >
          {busy === 'lyric' ? '提交中…' : '开始对比（每个模型约几分钱）'}
        </button>
      </div>
      <div className="mt-2 text-[11px] text-ink-faint">
        模型须是当前接口地址（{`LLM_BASE_URL`}）下可用的模型名；不可用的会在结果里显示失败原因。
      </div>
    </Section>
  )
}

function RunCard({ run, busy, act }: { run: any; busy: string; act: any }) {
  const isImage = run.kind === 'image'
  return (
    <Section
      title={
        <span className="flex items-center gap-2">
          <span>{run.title}</span>
          {run.destination && <span className="tag">{run.destination}</span>}
          {run.busy && <span className="tag-brand">进行中…</span>}
        </span>
      }
      right={<span className="text-[11px] text-ink-faint">{relative(run.created_at)}</span>}
      bodyClass="p-4"
    >
      {isImage && run.input?.prompt && (
        <div className="text-[11px] text-ink-faint mb-3 leading-relaxed">提示词：{run.input.prompt}</div>
      )}
      {!isImage && (
        <div className="text-[11px] text-ink-faint mb-3">
          {run.input?.genre || '—'} · {(run.input?.mood || []).join('、') || '—'} · {run.input?.duration_s} 秒
        </div>
      )}
      <div className={clsx('grid gap-3', run.results.length >= 3 ? 'grid-cols-3' : 'grid-cols-2')}>
        {run.results.map((r: any) => (
          <div key={r.id} className={clsx('border rounded-card overflow-hidden flex flex-col', r.picked ? 'border-brand ring-1 ring-brand/30' : 'border-line')}>
            <div className="flex items-center gap-2 px-3 py-2 border-b border-line">
              <span className="text-[12px] font-medium truncate">{r.label}</span>
              {r.picked && <span className="tag-brand shrink-0">最佳</span>}
              <span className="ml-auto text-[10px] text-ink-faint truncate">{r.model}</span>
            </div>
            {r.status === 'failed' ? (
              <div className="p-3 text-[11px] text-human leading-relaxed">失败：{r.error}</div>
            ) : r.status !== 'done' ? (
              <div className="p-6 flex justify-center"><Spinner label={isImage ? '出图中' : '写词中'} /></div>
            ) : isImage ? (
              <a href={r.output.image_url} target="_blank" rel="noreferrer">
                <img src={r.output.image_url} alt={r.label} className="w-full aspect-video object-cover bg-ink/5" />
              </a>
            ) : (
              <div className="p-3 text-[12px] leading-relaxed max-h-[320px] overflow-y-auto">
                {(r.output.sections || []).map((s: any, i: number) => (
                  <div key={i} className="mb-2">
                    <div className="text-[11px] text-brand">{s.name}</div>
                    {(s.lines || []).map((ln: string, k: number) => (
                      <div key={k}>{ln}</div>
                    ))}
                  </div>
                ))}
              </div>
            )}
            {r.status === 'done' && (
              <div className="p-3 border-t border-line flex flex-col gap-1.5 mt-auto">
                <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-ink-soft">
                  <span>
                    费用 <b className="text-ink">¥{r.cost.toFixed(r.cost < 0.01 ? 4 : 2)}</b>
                    {!r.price_known && <span className="text-human">（未配置单价）</span>}
                  </span>
                  <span>用时 {r.elapsed_s}s</span>
                  {isImage ? (
                    <span>{r.output.width}×{r.output.height}</span>
                  ) : (
                    <>
                      <span>{r.output.lines} 行（要求 {r.output.want_lines}）</span>
                      <span className={r.output.meter_bad ? 'text-human' : ''}>字数越界 {r.output.meter_bad} 行</span>
                      <span>{r.input_tokens}+{r.output_tokens} token</span>
                    </>
                  )}
                </div>
                {isImage && r.ai_score !== null && (
                  <div className="text-[11px] text-ink-soft">
                    机器预审 <b className={r.ai_score >= 75 ? 'text-brand' : 'text-human'}>{r.ai_score}</b>
                    <span className="text-ink-faint"> · {r.ai_reason}</span>
                  </div>
                )}
                {!isImage && r.output.has_dot_name && <div className="text-[11px] text-human">歌词里出现了带间隔号的整串地名</div>}
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-ink-soft">我的评分</span>
                  <ScoreDots value={r.human_score} onChange={(v) => act(`rate-${r.id}`, () => api.benchRate(r.id, { score: v }))} />
                  <button
                    className={clsx('btn btn-sm ml-auto', r.picked && 'border-brand text-brand')}
                    disabled={!!busy}
                    onClick={() => act(`pick-${r.id}`, () => api.benchRate(r.id, { picked: !r.picked }))}
                  >
                    {r.picked ? '已选为最佳' : '选为最佳'}
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </Section>
  )
}

function Summary({ sum, opts }: { sum: any; opts: any }) {
  if (!sum) return <Spinner />
  const Table = ({ rows, lyric }: { rows: any[]; lyric?: boolean }) =>
    rows.length === 0 ? (
      <Empty text="还没有完成的对比" />
    ) : (
      <table className="w-full text-[12px]">
        <thead>
          <tr className="text-ink-faint text-left border-b border-line">
            <th className="py-1.5 font-normal">模型</th>
            <th className="font-normal">参加轮数</th>
            <th className="font-normal">均价</th>
            <th className="font-normal">平均用时</th>
            <th className="font-normal">人工均分</th>
            {!lyric && <th className="font-normal">机器预审均分</th>}
            {lyric && <th className="font-normal">平均字数越界行</th>}
            <th className="font-normal">被选为最佳</th>
            <th className="font-normal" title="人工均分 ÷ 均价。分数接近时，选它高的就是性价比高">每元得分</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((x) => (
            <tr key={x.key} className="border-b border-line/60">
              <td className="py-2">
                <div className="font-medium">{x.label}</div>
                <div className="text-[10px] text-ink-faint">{x.model}</div>
              </td>
              <td>{x.runs}</td>
              <td>
                ¥{x.avg_cost.toFixed(x.avg_cost < 0.01 ? 4 : 2)}
                {!x.price_known && <span className="text-human"> *</span>}
              </td>
              <td>{x.avg_elapsed_s}s</td>
              <td>{x.avg_human ?? '未评'}{x.avg_human ? <span className="text-ink-faint">（{x.rated} 次）</span> : null}</td>
              {!lyric && <td>{x.avg_ai ?? '—'}</td>}
              {lyric && <td>{x.avg_meter_bad}</td>}
              <td>{x.picked}</td>
              <td className="font-medium text-brand">{x.score_per_yuan ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    )
  return (
    <div className="flex flex-col gap-4">
      <Banner tone="ok">
        看法：<b>生图</b>占单支成本约 88%，优先看「每元得分」；<b>生曲</b>约 10%、<b>写词</b>不到 1%，主要看人工均分。
        人工均分要靠你在对比结果上打分才有。
      </Banner>
      <Section title="生图模型" bodyClass="p-4"><Table rows={sum.image || []} /></Section>
      <Section title="写词模型" bodyClass="p-4"><Table rows={sum.lyric || []} lyric /></Section>
      <Section title="生曲模型（来自选型轮盲听）" bodyClass="p-4">
        {(sum.music || []).length === 0 ? (
          <Empty text="还没有已揭晓的选型轮。在词曲工作台把模式切到「选型轮」发起盲听" />
        ) : (
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-ink-faint text-left border-b border-line">
                <th className="py-1.5 font-normal">模型</th>
                <th className="font-normal">参加轮数</th>
                <th className="font-normal">胜出</th>
                <th className="font-normal">盲听均分（1–5）</th>
              </tr>
            </thead>
            <tbody>
              {sum.music.map((x: any) => (
                <tr key={x.key} className="border-b border-line/60">
                  <td className="py-2 font-medium">{x.label}</td>
                  <td>{x.rounds}{x.rounds < 3 && <span className="text-ink-faint">（样本不足）</span>}</td>
                  <td>{x.wins}</td>
                  <td>{x.avg_score || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      <Section title="当前价格表（按 token 计费的模型）" bodyClass="p-4">
        <div className="text-[11px] text-ink-faint mb-2">
          单位：元 / 百万 token。价格会变，用环境变量 MODEL_PRICES 覆盖或新增；不在表里的模型费用显示「未配置单价」。
        </div>
        <div className="flex flex-wrap gap-2">
          {opts.prices.map((x: any) => (
            <span key={x.model} className="tag">
              {x.model}：输入 {x.input_per_m} / 输出 {x.output_per_m}
            </span>
          ))}
        </div>
      </Section>
    </div>
  )
}
