import { useEffect, useState } from 'react'
import api from '../lib/api'
import { Banner, Modal, Spinner } from './ui'

/** 项目复盘（PRD V1.1 F1-11）：人工工时、费用、体验指标。工时可补录，原值保留。 */
export default function ProjectReview({ projectId, open, onClose }: { projectId: string; open: boolean; onClose: () => void }) {
  const [data, setData] = useState<any>(null)
  const [err, setErr] = useState('')
  const [editing, setEditing] = useState<string>('')
  const [minutes, setMinutes] = useState('')

  const load = () =>
    api
      .projectReview(projectId)
      .then((d: any) => {
        setData(d)
        setErr('')
      })
      .catch((e: any) => setErr(e?.message || String(e)))

  useEffect(() => {
    if (open) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, projectId])

  const pct = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`)

  return (
    <Modal open={open} title={`项目复盘${data ? ' · ' + data.project.name : ''}`} onClose={onClose} width="max-w-[720px]">
      {err && <Banner tone="error">{err}</Banner>}
      {!data ? (
        !err && <Spinner />
      ) : (
        <div className="flex flex-col gap-4 text-[12px]">
          <div className="grid grid-cols-3 gap-3">
            <Stat
              label="人工工时"
              value={`${data.work.total_minutes} 分钟`}
              hint={data.work.within_target ? '在 1 个工作日目标内' : '超过 1 个工作日目标'}
              bad={!data.work.within_target}
            />
            <Stat label="费用合计" value={`¥${data.cost.total.toFixed(2)}`} hint="API + 生图 + 生曲" />
            <Stat
              label="歌词人工改动"
              value={pct(data.experience.lyric_edit_ratio)}
              hint="目标 ≤30%"
              bad={(data.experience.lyric_edit_ratio ?? 0) > data.experience.lyric_edit_target}
            />
          </div>

          <div>
            <div className="font-medium mb-1.5">各人工节点</div>
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-ink-faint text-left">
                  <th className="font-normal pb-1">节点</th>
                  <th className="font-normal pb-1 text-right">有效操作</th>
                  <th className="font-normal pb-1 text-right">停留（墙钟）</th>
                  <th className="font-normal pb-1 text-right">补录</th>
                  <th className="pb-1"></th>
                </tr>
              </thead>
              <tbody>
                {data.work.nodes.length === 0 && (
                  <tr>
                    <td colSpan={5} className="py-2 text-ink-faint">
                      还没有工时记录（V1.1 之前的项目没有记录；之后的项目会自动记）
                    </td>
                  </tr>
                )}
                {data.work.nodes.map((n: any, i: number) => (
                  <tr key={i} className="border-t border-line">
                    <td className="py-1.5">
                      {n.label}
                      {n.open && <span className="ml-1 tag-human">进行中</span>}
                    </td>
                    <td className="text-right">{n.active_minutes} 分</td>
                    <td className="text-right text-ink-faint">{n.wall_minutes ?? '—'} 分</td>
                    <td className="text-right">
                      {editing === n.node ? (
                        <span className="inline-flex gap-1">
                          <input className="input h-6 w-16 text-[11px]" value={minutes} onChange={(e) => setMinutes(e.target.value)} />
                          <button
                            className="text-brand underline"
                            onClick={() =>
                              api.workManual(projectId, n.node, Number(minutes) || 0).then(() => {
                                setEditing('')
                                load()
                              })
                            }
                          >
                            存
                          </button>
                        </span>
                      ) : n.manual_minutes !== null && n.manual_minutes !== undefined ? (
                        `${n.manual_minutes} 分`
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="text-right">
                      <button
                        className="text-brand underline"
                        onClick={() => {
                          setEditing(n.node)
                          setMinutes(String(n.manual_minutes ?? n.active_minutes))
                        }}
                      >
                        补录
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-1 text-[10px] text-ink-faint">
              「有效操作」只算页面在前台且有键鼠操作的时间；离线沟通、外部查资料等可以「补录」，自动记录的原值保留。
            </div>
          </div>

          <div>
            <div className="font-medium mb-1.5">费用明细</div>
            <div className="flex flex-wrap gap-2">
              {Object.entries(data.cost.by_kind).map(([k, v]: any) => (
                <span key={k} className="tag">
                  {({ llm: '大模型', image: '生图', vlm: '视觉预审', music: '生曲' } as any)[k] || k} ¥{v.cost.toFixed(2)}
                  {v.calls ? ` · ${v.calls} 次` : ''}
                </span>
              ))}
            </div>
          </div>

          <div>
            <div className="font-medium mb-1.5">画面构成</div>
            <div className="text-ink-soft">
              实拍 {pct(data.experience.real_shot_ratio)} · AI {pct(data.experience.ai_shot_ratio)}（建议 AI ≤30%，仅提醒）
            </div>
          </div>
        </div>
      )}
    </Modal>
  )
}

function Stat({ label, value, hint, bad }: { label: string; value: string; hint?: string; bad?: boolean }) {
  return (
    <div className="border border-line rounded-btn p-3">
      <div className="text-[11px] text-ink-faint">{label}</div>
      <div className={bad ? 'text-[18px] font-medium text-human-dark' : 'text-[18px] font-medium'}>{value}</div>
      {hint && <div className="text-[10px] text-ink-faint mt-0.5">{hint}</div>}
    </div>
  )
}
