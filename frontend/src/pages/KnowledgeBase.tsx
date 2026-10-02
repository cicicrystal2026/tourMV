import { useCallback, useEffect, useState } from 'react'
import clsx from 'clsx'
import api from '../lib/api'
import { Banner, Empty, Field, Modal, PageHeader, Spinner } from '../components/ui'

/**
 * 文化知识库（PRD V1.1 §6.7 / D4）。
 *
 * 规则写在界面上，让运营一看就懂：
 * - 只有「已核实」的条目才会被写词与史实核对当作出处引用；
 * - AI 起草的条目只是草稿，核对无误、填上真实出处后点「核实」才生效。
 */
const SOURCE_TYPES: Record<string, string> = {
  official: '官方',
  academic: '学术',
  encyclopedia: '百科',
  field: '人工查证',
  other: '其他',
}

export default function KnowledgeBase() {
  const [destinations, setDestinations] = useState<string[]>([])
  const [dest, setDest] = useState('')
  const [filter, setFilter] = useState<'all' | 'verified' | 'draft'>('all')
  const [q, setQ] = useState('')
  const [items, setItems] = useState<any[] | null>(null)
  const [coverage, setCoverage] = useState<any>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [edit, setEdit] = useState<any>(null)        // 新建 / 编辑
  const [verifying, setVerifying] = useState<any>(null)

  useEffect(() => {
    api
      .projects()
      .then((ps: any[]) => {
        const ds = [...new Set(ps.map((p) => p.destination).filter(Boolean))] as string[]
        setDestinations(ds)
        if (!dest && ds.length) setDest(ds[0])
      })
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const load = useCallback(async () => {
    try {
      const r = await api.kbList({
        destination: dest,
        q,
        verified: filter === 'all' ? undefined : filter === 'verified',
      })
      setItems(r.items || [])
      setCoverage(r.coverage)
      setErr('')
    } catch (e: any) {
      setErr(e?.message || String(e))
    }
  }, [dest, q, filter])

  useEffect(() => {
    load()
  }, [load])

  const act = async (key: string, fn: () => Promise<any>, ok?: string) => {
    setBusy(key)
    setErr('')
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

  return (
    <>
      <PageHeader
        title="文化知识库"
        subtitle="写词与史实核对的依据。只有「已核实」的条目会被当作出处引用；AI 起草的条目要人核对、填上真实出处后才生效。"
        actions={
          <>
            <button
              className="btn"
              disabled={!!busy || !dest}
              title="让 AI 先起草一批条目，你只需逐条核对、补出处"
              onClick={() =>
                act('draft', () => api.kbDraft(dest, 8), `已为「${dest}」起草条目，请逐条核对后点「核实」`)
              }
            >
              {busy === 'draft' ? 'AI 起草中…' : 'AI 起草 8 条'}
            </button>
            <button
              className="btn-primary"
              onClick={() =>
                setEdit({ destination: dest, title: '', content: '', source_name: '', source_url: '', source_type: 'official', verified: true })
              }
            >
              新增条目
            </button>
          </>
        }
      />

      <div className="p-6 flex flex-col gap-4">
        {err && <Banner tone="error" onClose={() => setErr('')}>{err}</Banner>}
        {msg && <Banner tone="ok" onClose={() => setMsg('')}>{msg}</Banner>}

        <div className="card px-4 py-3 flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-[12px] text-ink-soft">
            目的地
            <input
              className="input h-8 w-[180px]"
              list="kb-dests"
              value={dest}
              onChange={(e) => setDest(e.target.value)}
              placeholder="如 吉林·长白山"
            />
            <datalist id="kb-dests">
              {destinations.map((d) => (
                <option key={d} value={d} />
              ))}
            </datalist>
          </label>
          <div className="flex rounded-btn border border-line overflow-hidden text-[11px]">
            {(
              [
                ['all', '全部'],
                ['verified', '已核实'],
                ['draft', '待核实'],
              ] as const
            ).map(([k, label]) => (
              <button
                key={k}
                className={clsx('px-2.5 py-1', filter === k ? 'bg-brand text-white' : 'bg-white text-ink-soft')}
                onClick={() => setFilter(k)}
              >
                {label}
              </button>
            ))}
          </div>
          <input
            className="input h-8 flex-1 min-w-[160px]"
            placeholder="关键词检索，如：满语 天池 火山"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          {coverage && (
            <span className={coverage.enough ? 'tag-brand' : 'tag-human'}>
              「{coverage.destination}」已核实 {coverage.verified} 条
              {!coverage.enough && `（建议至少 ${coverage.min} 条）`}
              {coverage.unverified > 0 && ` · 待核实 ${coverage.unverified}`}
            </span>
          )}
        </div>

        {items === null ? (
          <Spinner label="加载知识库" />
        ) : items.length === 0 ? (
          <Empty
            text={
              dest
                ? `「${dest}」还没有条目。可以「AI 起草」一批再逐条核对，或「新增条目」手动录入。`
                : '先选择或输入一个目的地'
            }
          />
        ) : (
          <div className="grid grid-cols-2 gap-3">
            {items.map((e) => (
              <div
                key={e.id}
                className={clsx('card p-3.5 flex flex-col gap-2', !e.verified && 'border-human/40 bg-human-soft/20')}
              >
                <div className="flex items-center gap-2">
                  <span className={e.verified ? 'tag-brand' : 'tag-human'}>
                    {e.verified ? '已核实' : e.source_type === 'ai_draft' ? 'AI 起草·待核实' : '待核实'}
                  </span>
                  <span className="text-[13px] font-medium truncate">{e.title}</span>
                  <span className="ml-auto text-[10px] text-ink-faint shrink-0">{e.destination}</span>
                </div>
                <div className="text-[12px] text-ink-soft leading-relaxed">{e.content}</div>
                <div className="text-[11px] text-ink-faint">
                  出处：{e.source_name || '—'}
                  {e.source_type !== 'ai_draft' && ` · ${e.source_type_label}`}
                  {e.source_url && (
                    <a className="ml-1 text-brand underline" href={e.source_url} target="_blank" rel="noreferrer">
                      链接
                    </a>
                  )}
                  {e.from_check_id && <span className="ml-1 tag">来自史实疑点核实</span>}
                </div>
                <div className="flex gap-1.5 mt-auto">
                  {!e.verified && (
                    <button
                      className="btn-primary btn-sm"
                      disabled={!!busy}
                      onClick={() =>
                        setVerifying({
                          ...e,
                          source_type: e.source_type === 'ai_draft' ? 'encyclopedia' : e.source_type,
                        })
                      }
                    >
                      核实…
                    </button>
                  )}
                  <button className="btn btn-sm" disabled={!!busy} onClick={() => setEdit({ ...e })}>
                    编辑
                  </button>
                  <button
                    className="btn btn-sm"
                    disabled={!!busy}
                    onClick={() => {
                      if (confirm(`删除条目「${e.title}」？`)) act(`del-${e.id}`, () => api.kbDelete(e.id), '已删除')
                    }}
                  >
                    删除
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <Modal
        open={!!edit}
        title={edit?.id ? '编辑条目' : '新增条目'}
        onClose={() => setEdit(null)}
        width="max-w-[620px]"
        footer={
          <>
            <button className="btn" onClick={() => setEdit(null)}>
              取消
            </button>
            <button
              className="btn-primary"
              disabled={!!busy}
              onClick={() =>
                act(
                  'save',
                  async () => {
                    if (edit.id) await api.kbUpdate(edit.id, edit)
                    else await api.kbCreate(edit)
                    setEdit(null)
                  },
                  '已保存',
                )
              }
            >
              保存
            </button>
          </>
        }
      >
        {edit && (
          <div className="flex flex-col gap-3">
            <Field label="目的地">
              <input className="input" value={edit.destination} onChange={(e) => setEdit({ ...edit, destination: e.target.value })} />
            </Field>
            <Field label="标题">
              <input className="input" value={edit.title} onChange={(e) => setEdit({ ...edit, title: e.target.value })} placeholder="如：满语名称由来" />
            </Field>
            <Field label="内容（摘录可查证的事实）">
              <textarea
                className="input h-28 py-2 leading-relaxed"
                value={edit.content}
                onChange={(e) => setEdit({ ...edit, content: e.target.value })}
              />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="出处（来源名称）">
                <input className="input" value={edit.source_name} onChange={(e) => setEdit({ ...edit, source_name: e.target.value })} placeholder="如：长白山管委会官网" />
              </Field>
              <Field label="来源类型">
                <select className="input" value={edit.source_type} onChange={(e) => setEdit({ ...edit, source_type: e.target.value })}>
                  {Object.entries(SOURCE_TYPES).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                  {edit.source_type === 'ai_draft' && <option value="ai_draft">AI 起草</option>}
                </select>
              </Field>
            </div>
            <Field label="链接 / 文献（可选）">
              <input className="input" value={edit.source_url} onChange={(e) => setEdit({ ...edit, source_url: e.target.value })} />
            </Field>
            {!edit.id && (
              <label className="flex items-center gap-2 text-[12px]">
                <input
                  type="checkbox"
                  className="accent-[#0F5E57]"
                  checked={!!edit.verified}
                  onChange={(e) => setEdit({ ...edit, verified: e.target.checked })}
                />
                我已核对，直接标为「已核实」（必须填写出处）
              </label>
            )}
          </div>
        )}
      </Modal>

      <Modal
        open={!!verifying}
        title="核实条目"
        onClose={() => setVerifying(null)}
        footer={
          <>
            <button className="btn" onClick={() => setVerifying(null)}>
              取消
            </button>
            <button
              className="btn-primary"
              disabled={!!busy || !verifying?.source_name?.trim()}
              onClick={() =>
                act(
                  'verify',
                  async () => {
                    await api.kbVerify(verifying.id, {
                      source_name: verifying.source_name,
                      source_url: verifying.source_url,
                      source_type: verifying.source_type,
                    })
                    setVerifying(null)
                  },
                  '已核实，写词与史实核对会引用这条',
                )
              }
            >
              确认核实
            </button>
          </>
        }
      >
        {verifying && (
          <div className="flex flex-col gap-3">
            <div className="text-[12px]">
              <div className="font-medium">{verifying.title}</div>
              <div className="text-ink-soft mt-1 leading-relaxed">{verifying.content}</div>
            </div>
            <Banner tone="warn">请对照公开资料确认内容无误，再填写你实际查证的出处。AI 给的来源提示只是线索，不能直接当出处。</Banner>
            <div className="grid grid-cols-2 gap-3">
              <Field label="出处（来源名称）">
                <input
                  className="input"
                  value={verifying.source_name}
                  onChange={(e) => setVerifying({ ...verifying, source_name: e.target.value })}
                />
              </Field>
              <Field label="来源类型">
                <select
                  className="input"
                  value={verifying.source_type}
                  onChange={(e) => setVerifying({ ...verifying, source_type: e.target.value })}
                >
                  {Object.entries(SOURCE_TYPES).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <Field label="链接 / 文献（可选）">
              <input
                className="input"
                value={verifying.source_url || ''}
                onChange={(e) => setVerifying({ ...verifying, source_url: e.target.value })}
              />
            </Field>
          </div>
        )}
      </Modal>
    </>
  )
}
