import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Eraser, ListPlus, Search, X } from 'lucide-react'
import { api, type PaperAutoScope } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { resolveWatchlistGroupColor } from '@/lib/watchlist-group-colors'

interface Props {
  value: PaperAutoScope | null | undefined
  onChange: (value: PaperAutoScope) => void
  compact?: boolean
}

const SCOPE_OPTIONS: Array<{ key: PaperAutoScope['scope']; label: string }> = [
  { key: 'all', label: '全市场' },
  { key: 'symbols', label: '手动指定股票' },
  { key: 'watchlist_group', label: '自选分组动态' },
]

export function normalizePaperAutoScope(value?: PaperAutoScope | null): PaperAutoScope {
  const scope = value?.scope ?? 'all'
  if (scope === 'symbols') {
    return { scope, symbols: value?.symbols ?? [], group_id: null }
  }
  if (scope === 'watchlist_group') {
    return { scope, symbols: [], group_id: value?.group_id ?? null }
  }
  return { scope: 'all', symbols: [], group_id: null }
}

export function isPaperAutoScopeValid(value?: PaperAutoScope | null): boolean {
  const cfg = normalizePaperAutoScope(value)
  if (cfg.scope === 'symbols') return cfg.symbols.length > 0
  if (cfg.scope === 'watchlist_group') return !!cfg.group_id
  return true
}

export function paperAutoScopeLabel(value?: PaperAutoScope | null): string {
  const cfg = normalizePaperAutoScope(value)
  if (cfg.scope === 'symbols') return `指定 ${cfg.symbols.length} 只`
  if (cfg.scope === 'watchlist_group') return `分组 ${cfg.group_id?.slice(0, 8) ?? ''}`
  return '全市场'
}

/** 模拟盘账户级股票范围选择器: 全市场 / 手动股票 / 自选分组动态。 */
export function AutoScopePicker({ value, onChange, compact }: Props) {
  const cfg = normalizePaperAutoScope(value)
  const [symbolQuery, setSymbolQuery] = useState('')
  const [importGroupId, setImportGroupId] = useState('')

  const watchlistQ = useQuery({ queryKey: QK.watchlist, queryFn: api.watchlistList })
  const groupsQ = useQuery({ queryKey: QK.watchlistGroups, queryFn: api.watchlistGroups })
  const symbolSearch = useQuery({
    queryKey: QK.instrumentSearch(symbolQuery, 'stock'),
    queryFn: () => api.instrumentSearch(symbolQuery, 20, 'stock'),
    enabled: cfg.scope === 'symbols' && symbolQuery.length > 0,
  })
  const namesQ = useQuery({
    queryKey: QK.instrumentNames(cfg.symbols),
    queryFn: () => api.instrumentNames(cfg.symbols),
    enabled: cfg.symbols.length > 0,
    staleTime: 5 * 60_000,
  })

  const entries = watchlistQ.data?.symbols ?? []
  const groups = groupsQ.data?.groups ?? []
  const selectedGroup = groups.find(g => g.id === cfg.group_id)
  const groupSymbols = useMemo(
    () => selectedGroup ? entries.filter(e => e.group_ids?.includes(selectedGroup.id)).map(e => e.symbol) : [],
    [selectedGroup, entries],
  )
  const nameBySymbol = useMemo(() => {
    const out: Record<string, string> = {}
    for (const e of entries) if (e.name) out[e.symbol] = e.name
    for (const [sym, name] of Object.entries(namesQ.data?.names ?? {})) if (name) out[sym] = name
    return out
  }, [entries, namesQ.data])

  const patch = (p: Partial<PaperAutoScope>) => onChange(normalizePaperAutoScope({ ...cfg, ...p }))
  const setScope = (scope: PaperAutoScope['scope']) => {
    if (scope === 'all') onChange({ scope, symbols: [], group_id: null })
    else if (scope === 'symbols') onChange({ scope, symbols: cfg.symbols, group_id: null })
    else onChange({ scope, symbols: [], group_id: cfg.group_id })
  }
  const addSymbol = (sym: string) => {
    if (!cfg.symbols.includes(sym)) patch({ symbols: [...cfg.symbols, sym] })
    setSymbolQuery('')
  }
  const importSymbols = (syms: string[]) => {
    const merged = [...cfg.symbols]
    for (const s of syms) if (!merged.includes(s)) merged.push(s)
    if (merged.length) patch({ symbols: merged })
  }

  return (
    <div className={compact ? 'space-y-1.5' : 'space-y-2'}>
      <div className="flex items-start gap-1.5">
        <select
          value={cfg.scope}
          onChange={e => setScope(e.target.value as PaperAutoScope['scope'])}
          className="h-7 w-32 shrink-0 rounded border border-border bg-base px-2 text-[11px] text-foreground"
        >
          {SCOPE_OPTIONS.map(o => <option key={o.key} value={o.key}>{o.label}</option>)}
        </select>

        {cfg.scope === 'all' && (
          <span className="text-[11px] text-muted">对全市场所有标的生效</span>
        )}

        {cfg.scope === 'symbols' && (
          <div className="min-w-0 flex-1 space-y-1.5">
            <div className="flex items-center gap-1.5">
              <div className="relative">
                <button
                  type="button"
                  onClick={() => importSymbols(entries.map(e => e.symbol))}
                  title="一次性导入当前全部自选；后续自选变化不影响本范围；如需动态跟随请改选自选分组动态"
                  className="inline-flex h-7 shrink-0 items-center gap-1 rounded border border-border bg-base px-2 text-[11px] text-secondary transition-colors hover:border-accent/30 hover:text-foreground"
                >
                  <ListPlus className="h-3 w-3" />自选导入
                </button>
              </div>
              <select
                value={importGroupId}
                onChange={e => setImportGroupId(e.target.value)}
                className="h-7 w-28 shrink-0 rounded border border-border bg-base px-1.5 text-[11px] text-secondary"
                title="从某个自选分组一次性导入当前成员"
              >
                <option value="">分组导入</option>
                {groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}
              </select>
              {importGroupId && (
                <button
                  type="button"
                  onClick={() => {
                    const syms = entries.filter(e => e.group_ids?.includes(importGroupId)).map(e => e.symbol)
                    importSymbols(syms)
                    setImportGroupId('')
                  }}
                  className="h-7 shrink-0 rounded border border-accent/30 bg-accent/10 px-2 text-[11px] text-accent transition-colors hover:bg-accent/20"
                >
                  导入
                </button>
              )}
              <div className="relative min-w-0 flex-1">
                <input
                  value={symbolQuery}
                  onChange={e => setSymbolQuery(e.target.value)}
                  placeholder="搜索代码或名称添加..."
                  className="h-7 w-full rounded border border-border bg-base pl-6 pr-2 text-[11px] text-foreground focus:outline-none focus:border-accent/50"
                />
                <Search className="absolute left-1.5 top-1.5 h-3.5 w-3.5 text-muted" />
                {symbolSearch.data && symbolSearch.data.results.length > 0 && (
                  <div className="absolute z-10 mt-1 max-h-40 w-full overflow-auto rounded border border-border bg-surface shadow-lg">
                    {symbolSearch.data.results.map(r => (
                      <button
                        key={r.symbol}
                        type="button"
                        onClick={() => addSymbol(r.symbol)}
                        className="block w-full px-2 py-1 text-left text-[11px] text-secondary transition-colors hover:bg-elevated hover:text-foreground"
                      >
                        <span className="font-mono text-foreground/80">{r.symbol}</span>
                        <span className="ml-1 text-muted">{r.name}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {cfg.symbols.length === 0 ? (
              <div className="rounded border border-dashed border-border px-2 py-1.5 text-[10px] text-warning">
                请至少添加一只股票，否则不会自动跟单
              </div>
            ) : (
              <div className="flex items-center justify-between">
                <span className="text-[10px] text-muted">已加入 <span className="font-mono tabular-nums text-secondary">{cfg.symbols.length}</span> 只</span>
                <button
                  type="button"
                  onClick={() => patch({ symbols: [] })}
                  className="inline-flex items-center gap-0.5 text-[10px] text-muted transition-colors hover:text-warning"
                >
                  <Eraser className="h-2.5 w-2.5" />清空
                </button>
              </div>
            )}
            {cfg.symbols.length > 0 && (
              <div className="flex max-h-28 flex-wrap gap-1 overflow-y-auto rounded border border-border/60 bg-base/40 p-1.5">
                {cfg.symbols.map(sym => (
                  <span key={sym} className="inline-flex items-center gap-1 rounded border border-border bg-elevated px-1.5 py-0.5 text-[10px] text-secondary">
                    <span className="max-w-24 truncate text-foreground/90" title={nameBySymbol[sym] ? `${nameBySymbol[sym]} ${sym}` : sym}>
                      {nameBySymbol[sym] ?? sym}
                    </span>
                    <span className="font-mono text-[9px] tabular-nums text-muted">{sym}</span>
                    <button
                      type="button"
                      onClick={() => patch({ symbols: cfg.symbols.filter(s => s !== sym) })}
                      className="text-muted transition-colors hover:text-danger"
                      title={`移除 ${sym}`}
                    >
                      <X className="h-2.5 w-2.5" />
                    </button>
                  </span>
                ))}
              </div>
            )}
          </div>
        )}

        {cfg.scope === 'watchlist_group' && (
          <div className="min-w-0 flex-1 space-y-1.5">
            <select
              value={cfg.group_id ?? ''}
              onChange={e => patch({ group_id: e.target.value || null })}
              className="h-7 w-full max-w-64 rounded border border-border bg-base px-2 text-[11px] text-foreground"
            >
              <option value="">选择自选分组...</option>
              {groups.map(g => (
                <option key={g.id} value={g.id}>{g.name}</option>
              ))}
            </select>
            {!selectedGroup ? (
              <div className="text-[10px] text-muted">
                还没有合适的分组，<Link to="/watchlist" className="text-accent hover:text-accent/80">去自选页创建 →</Link>
              </div>
            ) : groupSymbols.length === 0 ? (
              <div className="rounded border border-dashed border-border px-2 py-1.5 text-[10px] text-warning">
                该分组当前没有标的；后续在分组内添加会自动纳入模拟盘范围
              </div>
            ) : (
              <div className="flex max-h-24 flex-wrap gap-1 overflow-y-auto rounded border border-border/60 bg-base/40 p-1.5">
                {groupSymbols.map(sym => (
                  <span key={sym} className="inline-flex items-center gap-1 rounded border border-border bg-elevated px-1.5 py-0.5 text-[10px] text-secondary">
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${resolveWatchlistGroupColor(selectedGroup.color).dot}`} />
                    <span className="font-mono text-[9px] tabular-nums text-muted">{sym}</span>
                  </span>
                ))}
              </div>
            )}
            <div className="text-[10px] text-muted/70">动态绑定：分组内增删标的自动同步模拟盘范围，无需改账户设置</div>
          </div>
        )}
      </div>
    </div>
  )
}
