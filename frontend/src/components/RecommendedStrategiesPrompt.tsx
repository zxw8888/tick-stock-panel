import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Loader2, Sparkles } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { invalidateStrategyLibrary } from '@/lib/strategyLibrary'
import { suitableTags } from '@/lib/suitableFor'
import { toast } from '@/components/Toast'
import { Modal } from '@/components/Modal'
import { cn } from '@/lib/cn'

const BUTTON = 'inline-flex h-8 items-center justify-center gap-1.5 rounded-btn border border-border px-3 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40'

export function RecommendedStrategiesPrompt() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const query = useQuery({
    queryKey: QK.recommendedStrategies,
    queryFn: ({ signal }) => api.recommendedStrategies(signal),
    staleTime: 60_000,
    retry: 1,
  })

  const pendingIds = query.data?.prompt_pending_ids ?? []
  const pending = (query.data?.items ?? []).filter(item => pendingIds.includes(item.id))
  if (!pending.length) return null

  const ack = async () => {
    await api.recommendedStrategiesPromptAck(pendingIds)
    await qc.invalidateQueries({ queryKey: QK.recommendedStrategies })
  }

  const later = async () => {
    if (busy) return
    setBusy(true)
    try {
      await ack()
    } catch (error) {
      toast(`稍后处理失败：${error instanceof Error ? error.message : '请重试'}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  const addAndGo = async () => {
    if (busy) return
    setBusy(true)
    try {
      const snapshot = await api.strategyPoolSnapshot()
      const archived = new Set(Object.entries(snapshot.meta).filter(([, meta]) => meta.archived).map(([id]) => id))
      const installable = pending
        .filter(item => item.status !== 'missing' && item.status !== 'conflict' && item.status !== 'not_installed')
        .map(item => item.id)
      const next = [...snapshot.pool]
      for (const id of installable) {
        if (!archived.has(id) && !next.includes(id)) next.push(id)
      }
      if (next.length !== snapshot.pool.length) {
        await api.strategyPoolSetEntries(next, false, snapshot.revision)
      }
      await ack()
      await invalidateStrategyLibrary(qc)
      toast(`已加入 ${installable.length} 个推荐策略到策略池`, 'success')
      navigate('/screener')
    } catch (error) {
      toast(`加入策略池失败：${error instanceof Error ? error.message : '请重试'}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      onClose={() => { void later() }}
      ariaLabel="推荐策略已就绪"
      closeOnBackdrop={!busy}
      panelClassName="w-[94%] max-w-lg rounded-card border border-border bg-surface shadow-2xl"
    >
      <div className="flex items-start gap-3 border-b border-border px-5 py-4">
        <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-amber-500/10 text-amber-400">
          <Sparkles className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-foreground">推荐策略已就绪</h2>
          <p className="mt-1 text-[11px] leading-5 text-muted">
            本版本自带 {pending.length} 个日线策略，已安装到策略库。选择是否立即加入策略池并去选股。
          </p>
        </div>
      </div>
      <div className="max-h-[42vh] space-y-1.5 overflow-y-auto px-5 py-3">
        {pending.map(item => (
          <div key={item.id} className="rounded-lg border border-border/60 bg-base/30 px-3 py-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs font-medium text-foreground">{item.name}</span>
              <span className="rounded border border-amber-500/25 bg-amber-500/10 px-1 py-px text-[9px] text-amber-400">{item.category}</span>
              {suitableTags(item.suitable_for, 2).map(tag => (
                <span key={tag} className="rounded-full bg-elevated px-1.5 py-px text-[9px] text-muted">{tag}</span>
              ))}
            </div>
            <p className="mt-0.5 line-clamp-2 text-[10px] leading-4 text-muted">{item.description}</p>
          </div>
        ))}
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
        <button type="button" disabled={busy} onClick={() => void later()} className={BUTTON}>
          稍后
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void addAndGo()}
          className={cn(BUTTON, 'border-blue-600 bg-blue-600 text-white hover:bg-blue-700 hover:text-white')}
        >
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ArrowRight className="h-3.5 w-3.5" />}
          加入策略池并去选股
        </button>
      </div>
    </Modal>
  )
}
