import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Download, Loader2, RefreshCw, ShieldAlert, Sparkles, X } from 'lucide-react'
import { api, type RecommendedStrategyItem, type RecommendedStrategyStatus } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { invalidateStrategyLibrary } from '@/lib/strategyLibrary'
import {
  MARKET_CAP_LABELS,
  STYLE_LABELS,
  matchesSuitableFor,
  suitableFilterOptions,
  suitableSections,
  suitableTags,
} from '@/lib/suitableFor'
import { toast } from '@/components/Toast'
import { Modal } from '@/components/Modal'
import { cn } from '@/lib/cn'

interface Props {
  open: boolean
  onClose: () => void
  onInstalled?: (ids: string[]) => void
}

const STATUS_META: Record<RecommendedStrategyStatus, { label: string; cls: string }> = {
  not_installed: { label: '未安装', cls: 'border-border text-muted' },
  installed: { label: '已安装', cls: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400' },
  update_available: { label: '可更新', cls: 'border-amber-500/30 bg-amber-500/10 text-amber-400' },
  modified: { label: '已修改', cls: 'border-sky-500/30 bg-sky-500/10 text-sky-400' },
  missing: { label: '文件缺失', cls: 'border-danger/30 bg-danger/10 text-danger' },
  conflict: { label: '同名冲突', cls: 'border-danger/30 bg-danger/10 text-danger' },
}

const ACTION_LABEL: Partial<Record<RecommendedStrategyStatus, string>> = {
  not_installed: '安装',
  missing: '重新安装',
  update_available: '更新',
  modified: '覆盖',
  conflict: '覆盖',
}

const BUTTON = 'inline-flex h-8 items-center justify-center gap-1.5 rounded-btn border border-border px-2.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40'

function actionable(item: RecommendedStrategyItem): boolean {
  return item.status !== 'installed'
}

function fmtTime(iso: string | null): string {
  return iso ? iso.replace('T', ' ').slice(0, 16) : ''
}

export function RecommendedStrategiesDialog({ open, onClose, onInstalled }: Props) {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [marketCap, setMarketCap] = useState('')
  const [style, setStyle] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmIds, setConfirmIds] = useState<string[] | null>(null)
  const initializedPack = useRef('')

  const query = useQuery({
    queryKey: QK.recommendedStrategies,
    queryFn: ({ signal }) => api.recommendedStrategies(signal),
    enabled: open,
    staleTime: 10_000,
  })

  const items = query.data?.items ?? []
  const options = useMemo(() => suitableFilterOptions(items), [items])
  const visible = useMemo(
    () => items.filter(item => matchesSuitableFor(item.suitable_for, { marketCap, style })),
    [items, marketCap, style],
  )

  useEffect(() => {
    if (!query.data) return
    if (initializedPack.current === query.data.pack_version) return
    initializedPack.current = query.data.pack_version
    setSelected(new Set(query.data.items.filter(actionable).map(item => item.id)))
  }, [query.data])

  const toggle = (id: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const refresh = async () => {
    await invalidateStrategyLibrary(qc)
    await qc.invalidateQueries({ queryKey: QK.recommendedStrategies })
  }

  const install = async (ids: string[], overwrite: boolean) => {
    if (!ids.length || busy) return
    setBusy(true)
    try {
      const result = await api.recommendedStrategiesInstall(ids, overwrite)
      const changed = result.installed_ids ?? []
      toast(
        changed.length
          ? `已${overwrite ? '覆盖安装' : '安装'} ${changed.length} 个推荐策略${result.backup_dir ? '（原文件已备份）' : ''}`
          : '推荐策略文件已是最新',
        'success',
      )
      await refresh()
      setSelected(new Set((result.items ?? []).filter(actionable).map(item => item.id)))
      setConfirmIds(null)
      if (changed.length) onInstalled?.(changed)
    } catch (error) {
      const message = error instanceof Error ? error.message : '安装失败'
      if (!overwrite && message.includes('覆盖')) {
        setConfirmIds(ids)
      } else {
        toast(`推荐策略安装失败：${message}`, 'error')
      }
    } finally {
      setBusy(false)
    }
  }

  const selectedIds = [...selected].filter(id => {
    const item = items.find(entry => entry.id === id)
    return item ? actionable(item) : false
  })
  const allVisibleSelected = visible.length > 0 && visible.every(item => !actionable(item) || selected.has(item.id))

  return (
    <>
      <Modal
        active={!confirmIds}
        onClose={onClose}
        ariaLabel="推荐策略库"
        closeOnBackdrop={!busy}
        panelClassName="flex h-[90%] w-[96%] max-w-[1120px] flex-col overflow-hidden rounded-card border border-border bg-surface shadow-2xl"
      >
        <header className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-5 py-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground">
              <Sparkles className="h-4 w-4 text-amber-400" />
              推荐策略库
              {query.data?.pack_version && (
                <span className="rounded border border-border px-1.5 py-0.5 text-[10px] font-normal text-muted">
                  pack {query.data.pack_version}
                </span>
              )}
            </h2>
            <p className="mt-1 text-[11px] leading-5 text-muted">
              10 个经过整理的日线策略。安装只写入策略库，不会自动加入策略池；可在此按市值和风格筛选。
            </p>
          </div>
          <button type="button" aria-label="关闭" disabled={busy} onClick={onClose} className="rounded p-1 text-muted hover:bg-elevated hover:text-foreground disabled:opacity-40">
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border bg-surface px-5 py-2.5">
          <label className="flex items-center gap-1.5 text-[11px] text-secondary">
            <input
              type="checkbox"
              aria-label="全选当前筛选下可安装的推荐策略"
              checked={allVisibleSelected}
              disabled={busy || visible.length === 0}
              onChange={() => {
                setSelected(prev => {
                  const next = new Set(prev)
                  const ids = visible.filter(actionable).map(item => item.id)
                  if (ids.every(id => next.has(id))) ids.forEach(id => next.delete(id))
                  else ids.forEach(id => next.add(id))
                  return next
                })
              }}
              className="accent-accent"
            />
            全选
          </label>
          <select value={marketCap} onChange={event => setMarketCap(event.target.value)} aria-label="按市值筛选推荐策略" className="h-7 rounded border border-border bg-base px-1.5 text-[11px] text-secondary focus:border-accent focus:outline-none">
            <option value="">全部市值</option>
            {options.marketCaps.map(value => <option key={value} value={value}>{MARKET_CAP_LABELS[value] ?? value}</option>)}
          </select>
          <select value={style} onChange={event => setStyle(event.target.value)} aria-label="按风格筛选推荐策略" className="h-7 rounded border border-border bg-base px-1.5 text-[11px] text-secondary focus:border-accent focus:outline-none">
            <option value="">全部风格</option>
            {options.styles.map(value => <option key={value} value={value}>{STYLE_LABELS[value] ?? value}</option>)}
          </select>
          <span className="text-[10px] text-muted">显示 {visible.length}/{items.length}</span>
          <div className="ml-auto flex items-center gap-2">
            <button type="button" className={BUTTON} disabled={busy || query.isFetching} onClick={() => void query.refetch()}>
              <RefreshCw className={cn('h-3.5 w-3.5', query.isFetching && 'animate-spin')} />
              刷新
            </button>
            <button type="button" disabled={busy || selectedIds.length === 0} onClick={() => void install(selectedIds, false)} className={cn(BUTTON, 'border-accent/40 bg-accent/10 text-accent hover:bg-accent/15')}>
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
              安装所选 {selectedIds.length > 0 && <span className="tabular-nums">{selectedIds.length}</span>}
            </button>
          </div>
        </div>

        {query.isError && (
          <div role="alert" className="shrink-0 border-b border-danger/20 bg-danger/5 px-5 py-2 text-xs text-danger">
            推荐策略清单加载失败：{query.error instanceof Error ? query.error.message : '请稍后重试'}
          </div>
        )}

        <div className="scrollbar-gutter-stable min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-3">
          {query.isLoading && (
            <p className="flex items-center justify-center gap-2 py-16 text-xs text-muted">
              <Loader2 className="h-4 w-4 animate-spin" />推荐策略加载中…
            </p>
          )}
          {!query.isLoading && !query.isError && visible.length === 0 && (
            <p className="py-16 text-center text-xs text-muted">没有符合当前筛选条件的推荐策略。</p>
          )}
          <div className="space-y-2">
            {visible.map(item => {
              const status = STATUS_META[item.status]
              const tags = suitableTags(item.suitable_for)
              const sections = suitableSections(item.suitable_for)
              const action = ACTION_LABEL[item.status]
              return (
                <article key={item.id} className={cn('rounded-xl border p-3 transition-colors', selected.has(item.id) ? 'border-accent/30 bg-accent/[0.04]' : 'border-border/70 bg-base/20')}>
                  <div className="flex items-start gap-3">
                    <input
                      type="checkbox"
                      aria-label={`选择推荐策略：${item.name}`}
                      checked={selected.has(item.id)}
                      disabled={busy || !actionable(item)}
                      onChange={() => toggle(item.id)}
                      className="mt-1 shrink-0 accent-accent disabled:opacity-30"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <h3 className="text-sm font-medium text-foreground">{item.name}</h3>
                        <span className={cn('rounded border px-1 py-px text-[10px]', status.cls)}>{status.label}</span>
                        <span className="rounded border border-border px-1 py-px text-[10px] text-muted">{item.category}</span>
                        {item.asset_types.map(asset => <span key={asset} className="rounded border border-border px-1 py-px text-[10px] text-muted">{asset === 'etf' ? 'ETF' : '股票'}</span>)}
                        <span className="text-[10px] text-muted">v{item.version}</span>
                      </div>
                      <p className="mt-1 text-[11px] leading-5 text-secondary">{item.description}</p>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1">
                        {tags.map(tag => <span key={tag} className="rounded-full bg-elevated px-1.5 py-0.5 text-[10px] text-secondary">{tag}</span>)}
                        {item.installed_version && item.installed_version !== item.version && (
                          <span className="text-[10px] text-amber-400">已装 v{item.installed_version}</span>
                        )}
                        {item.installed_at && <span className="text-[10px] text-muted">安装于 {fmtTime(item.installed_at)}</span>}
                      </div>
                      <div className="mt-2 grid gap-1 rounded-lg border border-border/50 bg-base/30 px-2.5 py-2 text-[10px] leading-4 sm:grid-cols-2">
                        {sections.map(section => (
                          <div key={section.label} className="flex gap-1.5">
                            <span className="shrink-0 text-muted">{section.label}</span>
                            <span className="text-secondary">{section.values.join(' / ')}</span>
                          </div>
                        ))}
                        {item.suitable_for?.note && <p className="text-muted sm:col-span-2">{item.suitable_for.note}</p>}
                        {item.sources.length > 0 && <p className="text-muted/70 sm:col-span-2">依据：{item.sources.join('；')}</p>}
                      </div>
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-1.5">
                      {action && (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => {
                            if (item.status === 'modified' || item.status === 'conflict') setConfirmIds([item.id])
                            else void install([item.id], item.status === 'update_available')
                          }}
                          className={cn(BUTTON, 'h-7 px-2 text-[11px]')}
                        >
                          {item.status === 'update_available' ? <RefreshCw className="h-3 w-3" /> : <Download className="h-3 w-3" />}
                          {action}
                        </button>
                      )}
                      {item.status === 'installed' && <CheckCircle2 className="h-4 w-4 text-emerald-400" aria-label="已安装" />}
                      {(item.status === 'modified' || item.status === 'conflict') && <ShieldAlert className="h-4 w-4 text-danger" aria-label="需要确认覆盖" />}
                    </div>
                  </div>
                </article>
              )
            })}
          </div>
        </div>

        <footer className="flex shrink-0 items-center justify-between gap-3 border-t border-border px-5 py-2.5">
          <p className="text-[11px] text-muted">覆盖安装会先备份原文件到 <code className="text-secondary">data/user_data/recommended_strategy_backups</code>。</p>
          <button type="button" disabled={busy} onClick={onClose} className={cn(BUTTON, 'border-blue-600 bg-blue-600 text-white hover:bg-blue-700 hover:text-white')}>完成</button>
        </footer>
      </Modal>

      {confirmIds && (
        <Modal
          onClose={() => setConfirmIds(null)}
          ariaLabel="确认覆盖推荐策略"
          closeOnBackdrop={!busy}
          panelClassName="w-[92%] max-w-md rounded-card border border-border bg-surface p-5 shadow-2xl"
        >
          <h3 className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <ShieldAlert className="h-4 w-4 text-danger" />
            覆盖本地文件？
          </h3>
          <p className="mt-2 text-xs leading-5 text-secondary">
            目标策略文件已存在且内容与推荐包不同。继续会先备份原文件，再用推荐版本覆盖。
          </p>
          <p className="mt-1.5 break-all text-[10px] text-muted">{confirmIds.join(', ')}</p>
          <div className="mt-4 flex justify-end gap-2">
            <button type="button" disabled={busy} onClick={() => setConfirmIds(null)} className={BUTTON}>取消</button>
            <button type="button" disabled={busy} onClick={() => void install(confirmIds, true)} className={cn(BUTTON, 'border-danger/40 bg-danger/10 text-danger hover:bg-danger/15')}>
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              备份并覆盖
            </button>
          </div>
        </Modal>
      )}
    </>
  )
}
