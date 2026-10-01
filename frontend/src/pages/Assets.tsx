import { useCallback, useEffect, useState } from 'react'
import clsx from 'clsx'
import api from '../lib/api'
import { Banner, Chip, Empty, Field, Modal, PageHeader, Section, Spinner } from '../components/ui'
import { LICENSE_LABEL, TAG_DIM_LABEL, fmtDuration } from '../lib/format'

const DIMS = ['place', 'season', 'time', 'weather', 'mood', 'camera', 'subject'] as const

export default function AssetsPage() {
  const [q, setQ] = useState('')
  const [filters, setFilters] = useState<Record<string, string[]>>({})
  const [onlyUsable, setOnlyUsable] = useState(false)
  const [items, setItems] = useState<any[]>([])
  const [facets, setFacets] = useState<any>({ tags: {}, license_status: [], destination: [] })
  const [stats, setStats] = useState<any>(null)
  const [selected, setSelected] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [showUpload, setShowUpload] = useState(false)
  // 文生图弹窗：表单与结果状态都在 AiGenModal 内部维护，这里只管开关
  const [showAiGen, setShowAiGen] = useState(false)

  const search = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.assets({ q, filters, onlyUsable })
      setItems(r.items || [])
      if (r.facets) setFacets(r.facets)
      setErr('')
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setLoading(false)
    }
  }, [q, filters, onlyUsable])

  const loadFacets = useCallback(async () => {
    const r = await api.assetFacets()
    setFacets(r.facets)
    setStats(r.stats)
  }, [])

  useEffect(() => {
    loadFacets()
  }, [loadFacets])

  useEffect(() => {
    const t = setTimeout(search, 250)
    return () => clearTimeout(t)
  }, [search])

  const openDetail = async (id: string) => {
    try {
      setSelected(await api.asset(id))
    } catch (e: any) {
      setErr(e.message)
    }
  }

  const toggleFilter = (dim: string, value: string) => {
    setFilters((f) => {
      const cur = f[dim] || []
      const next = cur.includes(value) ? cur.filter((x) => x !== value) : [...cur, value]
      const out = { ...f, [dim]: next }
      if (!next.length) delete out[dim]
      return out
    })
  }

  const act = async (key: string, fn: () => Promise<any>, after?: () => void) => {
    setBusy(key)
    setErr('')
    try {
      await fn()
      after?.()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy('')
    }
  }

  return (
    <>
      <PageHeader
        title="素材库管理"
        subtitle={
          stats ? (
            <>
              共 {stats.total} 条 · 已授权 {stats.licensed} · 即将到期 {stats.expiring} · 缺授权 {stats.missing} · 待纠错{' '}
              {stats.need_review}
            </>
          ) : (
            '—'
          )
        }
        actions={
          <>
            <a className="btn" href={api.assetExportUrl} download>
              导出授权清单
            </a>
            <button className="btn" onClick={() => setShowAiGen(true)}>
              AI 生成景点素材
            </button>
            <button className="btn-primary" onClick={() => setShowUpload(true)}>
              批量上传素材
            </button>
          </>
        }
      />

      <div className="p-6 flex flex-col gap-4">
        {err && <Banner tone="error" onClose={() => setErr('')}>{err}</Banner>}
        {msg && <Banner tone="ok" onClose={() => setMsg('')}>{msg}</Banner>}

        <div className="card p-4 flex flex-col gap-3">
          <div className="flex items-center gap-3">
            <input
              className="input flex-1"
              placeholder="自然语言检索，例如：黄昏 古塔 慢推"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && search()}
            />
            <button className="btn" onClick={search}>
              检索
            </button>
            <button className="btn" onClick={() => { setQ(''); setFilters({}); setOnlyUsable(false) }}>
              清空
            </button>
            <label className="flex items-center gap-1.5 text-[12px] text-ink-soft whitespace-nowrap">
              <input type="checkbox" className="accent-[#0F5E57]" checked={onlyUsable} onChange={(e) => setOnlyUsable(e.target.checked)} />
              仅显示可用
            </label>
          </div>

          <div className="flex flex-col gap-2">
            {DIMS.map((d) => {
              const vals = facets.tags?.[d] || []
              if (!vals.length) return null
              return (
                <div key={d} className="flex items-start gap-2">
                  <span className="text-[11px] text-ink-faint w-14 shrink-0 pt-1">{TAG_DIM_LABEL[d]}</span>
                  <div className="flex flex-wrap gap-1.5">
                    {vals.map((v: string) => (
                      <Chip key={v} active={(filters[d] || []).includes(v)} onClick={() => toggleFilter(d, v)}>
                        {v}
                      </Chip>
                    ))}
                  </div>
                </div>
              )
            })}
            <div className="flex items-start gap-2">
              <span className="text-[11px] text-ink-faint w-14 shrink-0 pt-1">授权状态</span>
              <div className="flex flex-wrap gap-1.5">
                {(facets.license_status || []).map((v: string) => (
                  <Chip key={v} tone="human" active={(filters.license_status || []).includes(v)} onClick={() => toggleFilter('license_status', v)}>
                    {LICENSE_LABEL[v] || v}
                  </Chip>
                ))}
              </div>
            </div>
          </div>
        </div>

        <div className="grid grid-cols-[minmax(0,1fr)_336px] gap-5 items-start">
          <div>
            {loading ? (
              <Spinner label="检索中" />
            ) : items.length === 0 ? (
              <Empty
                text="没有匹配的素材"
                action={
                  <button className="btn" onClick={() => act('seed', () => api.seedAssets(), () => { loadFacets(); search() })}>
                    生成占位素材（无真实素材时可用）
                  </button>
                }
              />
            ) : (
              <div className="grid grid-cols-4 gap-3">
                {items.map((a) => (
                  <button
                    key={a.id}
                    onClick={() => openDetail(a.id)}
                    className={clsx(
                      'card overflow-hidden text-left hover:shadow-sm transition-shadow',
                      selected?.id === a.id && 'ring-1 ring-brand',
                    )}
                  >
                    <div className="relative aspect-video bg-page">
                      {a.thumb_url ? (
                        <img src={a.thumb_url} alt="" className="w-full h-full object-cover" />
                      ) : (
                        <div className="w-full h-full flex items-center justify-center text-[11px] text-ink-faint">无缩略图</div>
                      )}
                      {!a.usable && (
                        <span className="absolute top-1.5 left-1.5 tag-human">不可用</span>
                      )}
                      {a.need_review && a.usable && (
                        <span className="absolute top-1.5 left-1.5 tag-human">待纠错</span>
                      )}
                      {a.has_portrait && (
                        <span
                          className={clsx(
                            'absolute top-1.5 right-1.5',
                            a.portrait ? 'tag bg-white/85 text-ink-soft' : 'tag-human',
                          )}
                          title={
                            a.portrait
                              ? `肖像权已登记：${a.portrait}`
                              : '肖像权未登记，不会进入镜头候选（PRD §6.4）'
                          }
                        >
                          {a.portrait ? '含人物' : '肖像权未登记'}
                        </span>
                      )}
                    </div>
                    <div className="p-2.5">
                      <div className="text-[12px] font-medium truncate">{a.name}</div>
                      <div className="mt-1 flex items-center gap-1.5 text-[10px] text-ink-faint">
                        <span>{fmtDuration(a.duration_s)}</span>
                        <span>·</span>
                        <span>{a.width}×{a.height}</span>
                        {q && <span className="ml-auto text-brand">匹配 {a.score}</span>}
                      </div>
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        <span className={a.license_status === 'licensed' ? 'tag' : 'tag-human'}>
                          {LICENSE_LABEL[a.license_status] || a.license_status}
                        </span>
                        {Object.values(a.tags || {})
                          .flat()
                          .slice(0, 2)
                          .map((t: any, i: number) => (
                            <span key={i} className="tag">{t}</span>
                          ))}
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="sticky top-[92px]">
            {!selected ? (
              <Section title="素材详情">
                <Empty text="点击左侧卡片查看详情" />
              </Section>
            ) : (
              <DetailPanel
                a={selected}
                busy={busy}
                onReload={async () => {
                  await openDetail(selected.id)
                  loadFacets()
                  search()
                }}
                onAct={act}
                setMsg={setMsg}
                onClose={() => setSelected(null)}
              />
            )}
          </div>
        </div>
      </div>

      <UploadModal
        open={showUpload}
        onClose={() => setShowUpload(false)}
        onDone={(n) => {
          setShowUpload(false)
          setMsg(`已上传 ${n} 个素材`)
          loadFacets()
          search()
        }}
      />

      <AiGenModal
        open={showAiGen}
        onClose={() => setShowAiGen(false)}
        onDone={(m) => {
          setShowAiGen(false)
          setMsg(m)
          loadFacets()
          search()
        }}
      />
    </>
  )
}

function DetailPanel({
  a,
  busy,
  onReload,
  onAct,
  setMsg,
  onClose,
}: {
  a: any
  busy: string
  onReload: () => Promise<void>
  onAct: (k: string, fn: () => Promise<any>, after?: () => void) => Promise<void>
  setMsg: (s: string) => void
  onClose: () => void
}) {
  const [newTag, setNewTag] = useState({ dim: 'place', value: '' })
  const [license, setLicense] = useState({
    licensor: a.licensor || '',
    license_no: a.license_no || '',
    scope: a.scope || '',
    valid_until: a.valid_until || '',
    portrait: a.portrait || '',
    license_status: a.license_status,
  })

  return (
    <div className="card">
      <div className="flex items-center justify-between px-4 h-11 border-b border-line">
        <h2 className="text-[13px] font-medium">素材详情</h2>
        <button className="text-[12px] text-ink-faint hover:text-ink" onClick={onClose}>
          关闭
        </button>
      </div>
      <div className="p-4 flex flex-col gap-3 max-h-[calc(100vh-190px)] overflow-y-auto">
        {a.video_url && <video className="w-full rounded-btn bg-black" controls preload="metadata" src={a.video_url} />}

        <div className="text-[12px] font-medium break-all">{a.name}</div>

        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-ink-soft">
          <span>时长 {fmtDuration(a.duration_s)}</span>
          <span>{a.width}×{a.height} @{a.fps || '—'}fps</span>
          <span className="col-span-2 truncate" title={a.file_path}>路径 {a.file_path}</span>
          <span className="col-span-2">打标置信度 {a.tag_confidence} {a.need_review && <span className="text-human">（需纠错）</span>}</span>
        </div>

        <div className="border-t border-line pt-3">
          <div className="text-[12px] font-medium mb-2">AI 标签</div>
          <div className="flex flex-col gap-1.5">
            {DIMS.map((d) => (
              <div key={d} className="flex items-start gap-2">
                <span className="text-[11px] text-ink-faint w-14 shrink-0 pt-0.5">{TAG_DIM_LABEL[d]}</span>
                <div className="flex flex-wrap gap-1">
                  {(a.tags?.[d] || []).map((t: string) => (
                    <span key={t} className="tag group">
                      {t}
                      <button
                        className="ml-1 text-ink-faint hover:text-human"
                        disabled={busy === 'tag'}
                        onClick={() =>
                          onAct('tag', () => api.updateAsset(a.id, { del_tags: [{ dim: d, value: t }] }), () => {
                            setMsg('已删除标签并重新向量化')
                            onReload()
                          })
                        }
                      >
                        ×
                      </button>
                    </span>
                  ))}
                  {(a.tags?.[d] || []).length === 0 && <span className="text-[11px] text-ink-faint">—</span>}
                </div>
              </div>
            ))}
          </div>
          <div className="mt-2 flex items-center gap-1.5">
            <select className="input w-[92px]" value={newTag.dim} onChange={(e) => setNewTag({ ...newTag, dim: e.target.value })}>
              {DIMS.map((d) => (
                <option key={d} value={d}>
                  {TAG_DIM_LABEL[d]}
                </option>
              ))}
            </select>
            <input className="input flex-1" placeholder="新增标签" value={newTag.value} onChange={(e) => setNewTag({ ...newTag, value: e.target.value })} />
            <button
              className="btn btn-sm"
              disabled={!newTag.value || busy === 'tag'}
              onClick={() =>
                onAct('tag', () => api.updateAsset(a.id, { add_tags: [{ dim: newTag.dim, value: newTag.value }] }), () => {
                  setNewTag({ ...newTag, value: '' })
                  setMsg('已新增标签并重新向量化')
                  onReload()
                })
              }
            >
              添加
            </button>
          </div>
          <button
            className="btn btn-sm mt-2 w-full"
            disabled={busy === 'retag'}
            onClick={() => onAct('retag', () => api.retagAsset(a.id), () => { setMsg('已重新打标'); onReload() })}
          >
            用 VLM 重新打标
          </button>
        </div>

        <div className="border-t border-line pt-3 flex flex-col gap-2">
          <div className="text-[12px] font-medium">授权信息</div>
          <Field label="授权方">
            <input className="input" value={license.licensor} onChange={(e) => setLicense({ ...license, licensor: e.target.value })} />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="授权编号">
              <input className="input" value={license.license_no} onChange={(e) => setLicense({ ...license, license_no: e.target.value })} />
            </Field>
            <Field label="有效期至">
              <input className="input" placeholder="YYYY-MM-DD" value={license.valid_until} onChange={(e) => setLicense({ ...license, valid_until: e.target.value })} />
            </Field>
          </div>
          <Field label="授权范围">
            <input className="input" value={license.scope} onChange={(e) => setLicense({ ...license, scope: e.target.value })} />
          </Field>
          <Field label="肖像权说明" hint="含可识别人物时必须填写，否则不进入镜头候选">
            <input className="input" value={license.portrait} onChange={(e) => setLicense({ ...license, portrait: e.target.value })} />
          </Field>
          <Field label="授权状态">
            <select className="input" value={license.license_status} onChange={(e) => setLicense({ ...license, license_status: e.target.value })}>
              <option value="licensed">已授权</option>
              <option value="expiring">即将到期</option>
              <option value="missing">缺授权</option>
            </select>
          </Field>
          <button
            className="btn-primary btn-sm"
            disabled={busy === 'save'}
            onClick={() => onAct('save', () => api.updateAsset(a.id, license), () => { setMsg('授权信息已保存'); onReload() })}
          >
            保存授权信息
          </button>
        </div>

        <div className="border-t border-line pt-3 flex flex-col gap-2">
          <div className="text-[12px] font-medium">可用状态</div>
          <div className="flex items-center gap-2">
            <button
              className={clsx('btn btn-sm flex-1', !a.usable && 'border-human text-human')}
              disabled={busy === 'usable'}
              onClick={() =>
                onAct('usable', () => api.updateAsset(a.id, { usable: !a.usable }), () => {
                  setMsg(a.usable ? '已标记为不可用，相关项目会收到提示' : '已恢复可用')
                  onReload()
                })
              }
            >
              {a.usable ? '标记为不可用' : '恢复可用'}
            </button>
            <button
              className="btn btn-sm"
              disabled={busy === 'del'}
              onClick={() => onAct('del', () => api.deleteAsset(a.id), () => { setMsg('素材已删除'); onClose(); onReload() })}
            >
              删除
            </button>
          </div>
        </div>

        <div className="border-t border-line pt-3">
          <div className="text-[12px] font-medium mb-2">使用记录（{a.usage?.length || 0}）</div>
          {(a.usage || []).length === 0 ? (
            <div className="text-[11px] text-ink-faint">尚未被任何时间线使用</div>
          ) : (
            <div className="flex flex-col gap-1">
              {a.usage.map((u: any) => (
                <div key={u.clip_id} className="text-[11px] text-ink-soft truncate">
                  {u.lyric_line || '（无对应歌词行）'}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function UploadModal({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: (n: number) => void }) {
  const [files, setFiles] = useState<File[]>([])
  const [form, setForm] = useState({ destination: '', licensor: '', license_no: '', scope: '线上投放（含海外平台）', valid_until: '', portrait: '', license_status: 'licensed', hint: '' })
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [result, setResult] = useState<any>(null)

  return (
    <Modal
      open={open}
      title="批量上传素材"
      onClose={onClose}
      width="max-w-[680px]"
      footer={
        <>
          <button className="btn" onClick={onClose}>
            关闭
          </button>
          <button
            className="btn-primary"
            disabled={!files.length || busy}
            onClick={async () => {
              setBusy(true)
              setErr('')
              try {
                const r = await api.uploadAssets(files, form)
                setResult(r)
                if (r.created?.length) onDone(r.created.length)
              } catch (e: any) {
                setErr(e.message)
              } finally {
                setBusy(false)
              }
            }}
          >
            {busy ? `上传中（${files.length} 个，会自动抽帧打标）…` : `上传 ${files.length} 个文件`}
          </button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {err && <Banner tone="error">{err}</Banner>}
        <Banner tone="info">支持 mp4 / mov，单文件 ≤ 4GB。上传后自动抽帧、生成缩略图、VLM 打标并向量化。</Banner>
        <input
          type="file"
          multiple
          accept="video/mp4,video/quicktime,.mp4,.mov"
          onChange={(e) => setFiles(Array.from(e.target.files || []))}
          className="block w-full text-[12px] file:btn file:mr-2 file:border file:border-line file:bg-white file:rounded-btn file:h-btn file:px-3"
        />
        {files.length > 0 && (
          <div className="text-[11px] text-ink-soft max-h-[100px] overflow-y-auto">
            {files.map((f) => (
              <div key={f.name} className="truncate">
                {f.name} · {(f.size / 1024 / 1024).toFixed(1)} MB
              </div>
            ))}
          </div>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="目的地">
            <input className="input" value={form.destination} onChange={(e) => setForm({ ...form, destination: e.target.value })} />
          </Field>
          <Field label="授权方">
            <input className="input" value={form.licensor} onChange={(e) => setForm({ ...form, licensor: e.target.value })} />
          </Field>
          <Field label="授权编号">
            <input className="input" value={form.license_no} onChange={(e) => setForm({ ...form, license_no: e.target.value })} />
          </Field>
          <Field label="有效期至">
            <input className="input" placeholder="YYYY-MM-DD" value={form.valid_until} onChange={(e) => setForm({ ...form, valid_until: e.target.value })} />
          </Field>
          <Field label="肖像权说明">
            <input className="input" value={form.portrait} onChange={(e) => setForm({ ...form, portrait: e.target.value })} />
          </Field>
          <Field label="打标提示词" hint="无 VLM 时用于命中标签词表">
            <input className="input" value={form.hint} onChange={(e) => setForm({ ...form, hint: e.target.value })} />
          </Field>
        </div>
        {result && (
          <Banner tone={result.failed?.length ? 'warn' : 'ok'}>
            成功 {result.created?.length || 0} 个
            {result.failed?.length ? `，失败 ${result.failed.length} 个：${result.failed.map((f: any) => `${f.name}（${f.error}）`).join('；')}` : ''}
          </Banner>
        )}
      </div>
    </Modal>
  )
}


/** AI 文生图：按目的地生成真实景点素材，替换实验室的占位色块。 */
function AiGenModal({ open, onClose, onDone }: {
  open: boolean
  onClose: () => void
  onDone: (msg: string) => void
}) {
  const [form, setForm] = useState({ destination: '', theme: '', count: 6, overwrite: false })
  const [busy, setBusy] = useState('')
  const [res, setRes] = useState<any>(null)
  const [err, setErr] = useState('')

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="AI 生成景点素材"
      width="520px"
    >
      <div className="flex flex-col gap-3">
        <p className="text-[12px] text-ink-soft leading-relaxed">
          按目的地用文生图生成一批<strong>真实可考的景点画面</strong>，自动转成缓动视频片段并入库，
          替换掉实验室的占位色块。生成的素材按「AI 生成」登记授权，无第三方权利问题。
        </p>
        <Field label="目的地" hint="必填，写得越具体画面越准">
          <input
            className="input"
            placeholder="如：北京 / 北京·中轴线 / 湖南·永州"
            value={form.destination}
            onChange={(e) => setForm({ ...form, destination: e.target.value })}
          />
        </Field>
        <Field label="主题方向" hint="可选，影响选景与氛围">
          <input
            className="input"
            placeholder="如：中轴线与胡同 / 潇湘山水"
            value={form.theme}
            onChange={(e) => setForm({ ...form, theme: e.target.value })}
          />
        </Field>
        <div className="flex gap-4">
          <Field label="生成张数" hint="1-12，每张约 60–90 秒（6 张约 7 分钟）">
            <input
              type="number"
              min={1}
              max={12}
              className="input w-24"
              value={form.count}
              onChange={(e) => setForm({ ...form, count: Math.max(1, Math.min(12, Number(e.target.value))) })}
            />
          </Field>
          <Field label="同名处理">
            <label className="flex items-center gap-1.5 h-9 text-[12px]">
              <input
                type="checkbox"
                checked={form.overwrite}
                onChange={(e) => setForm({ ...form, overwrite: e.target.checked })}
              />
              覆盖同景点旧素材
            </label>
          </Field>
        </div>

        <button
          className="btn-primary"
          disabled={!!busy || !form.destination.trim()}
          onClick={async () => {
            setBusy('gen')
            setErr('')
            setRes(null)
            try {
              const r = await api.generateAiAssets({
                destination: form.destination.trim(),
                theme: form.theme.trim(),
                count: form.count,
                overwrite: form.overwrite,
              })
              setRes(r)
              onDone(`已生成 ${r.created} 条素材（${r.note}）`)
            } catch (e: any) {
              setErr(e?.message || String(e))
            } finally {
              setBusy('')
            }
          }}
        >
          {busy ? '正在生成…（6 张约 7 分钟，请勿关闭页面）' : '开始生成'}
        </button>

        {res && (
          <Banner tone={res.failed ? 'warn' : 'ok'}>
            {res.note}
            {!!(res.items || []).length && (
              <div className="mt-1 text-[11px] leading-relaxed">
                {(res.items || []).map((it: any) => it.name).join(' · ')}
              </div>
            )}
            {!!(res.errors || []).length && (
              <div className="mt-1 text-[11px] leading-relaxed">
                失败：{(res.errors || []).map((e: any) => `${e.place}（${e.error}）`).join('；')}
              </div>
            )}
          </Banner>
        )}
      </div>
    </Modal>
  )
}
