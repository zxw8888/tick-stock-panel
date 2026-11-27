import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { CloudDownload, Database, Loader2, Radar } from 'lucide-react'
import { api, type WeisoftSyncResult } from '@/lib/api'
import { toast } from '@/components/Toast'

function todayYmd(): string {
  // 客户端时区为 Asia/Shanghai; toLocaleDateString('sv-SE') 固定输出 YYYY-MM-DD
  return new Date().toLocaleDateString('sv-SE').replaceAll('-', '')
}

function fmtBytes(n: number): string {
  if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(2)} GB`
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`
  if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(0)} KB`
  return `${n} B`
}

/** 金字塔在线同步: 把官方 FileData 包合并进金字塔本地 .dat。
 *
 * 与「向前扩展历史」不同 —— 那个是把数据拉进 TSP 自己的 parquet;
 * 这里是把金字塔安装目录的数据补全, 之后由 weisoft provider 直接读取。
 */
export function WeisoftSyncPanel() {
  const [ymd, setYmd] = useState(todayYmd)
  const [market, setMarket] = useState<'SH' | 'SZ'>('SH')
  const [period, setPeriod] = useState<'Day' | 'Min1' | 'Min5'>('Day')
  const [codes, setCodes] = useState('')
  const [result, setResult] = useState<WeisoftSyncResult | null>(null)

  const status = useQuery({
    queryKey: ['weisoft', 'status'],
    queryFn: api.weisoftStatus,
    staleTime: 30_000,
  })

  const probe = useMutation({
    mutationFn: () => api.weisoftProbe(ymd),
    onError: (e: Error) => toast(`探测失败: ${e.message}`, 'error'),
  })

  const sync = useMutation({
    mutationFn: (dryRun: boolean) => {
      const list = codes.split(/[\s,，]+/).map((s) => s.trim()).filter(Boolean)
      return api.weisoftSync({
        market, period, ymd, dry_run: dryRun, use_cache: true,
        ...(list.length > 0 ? { codes: list } : {}),
      })
    },
    onSuccess: (data) => {
      setResult(data)
      if (!data.found) {
        toast(`${data.ymd} 服务器无包`, 'error')
        return
      }
      toast(
        data.dry_run
          ? `演练: ${data.symbols} 个标的, 将新增 ${data.added} 根`
          : `已同步 ${data.symbols} 个标的, 新增 ${data.added} 根`,
        'success',
      )
    },
    onError: (e: Error) => toast(`同步失败: ${e.message}`, 'error'),
  })

  const cacheRows = useMemo(() => {
    const cache = status.data?.cache ?? {}
    const rows: { period: string; market: string; packages: number; bytes: number; first: string; last: string }[] = []
    for (const [p, byMarket] of Object.entries(cache)) {
      for (const [m, info] of Object.entries(byMarket)) {
        rows.push({ period: p, market: m, ...info })
      }
    }
    return rows
  }, [status.data])

  if (status.isLoading) {
    return (
      <div className="mt-4 flex items-center gap-2 text-xs text-muted">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> 读取金字塔数据概览…
      </div>
    )
  }

  if (!status.data?.configured) {
    return (
      <div className="mt-4 rounded-lg border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning/90">
        未找到金字塔 Data 目录。设置环境变量 <code className="font-mono">WEISOFT_DATA_ROOT</code>{' '}
        指向 <code className="font-mono">D:\software\Weisoft Stock(x64)\Data</code> 后重试。
      </div>
    )
  }

  const periods = status.data.periods ?? {}
  const busy = probe.isPending || sync.isPending

  return (
    <div className="mt-4 rounded-lg border border-border/60 bg-elevated/20 p-4 space-y-3">
      <div className="flex items-center gap-2">
        <CloudDownload className="h-4 w-4 text-accent" />
        <span className="text-xs font-medium text-foreground">在线包同步</span>
        <span className="text-[10px] text-muted">下载官方 FileData 包 → 合并进金字塔本地 .dat</span>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-[11px] text-secondary">
        <Database className="h-3 w-3" />
        <span className="font-mono">{status.data.data_root}</span>
        <span className="text-muted">·</span>
        <span className="font-mono">{status.data.server}</span>
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[10px] text-muted sm:grid-cols-4">
        {Object.entries(periods).flatMap(([p, byMarket]) =>
          Object.entries(byMarket)
            .filter(([, n]) => n > 0)
            .map(([m, n]) => (
              <span key={`${p}-${m}`}>
                <span className="text-secondary">{m}</span> {p}: <span className="font-mono">{n}</span> 个
              </span>
            )),
        )}
      </div>

      {cacheRows.length > 0 && (
        <div className="text-[10px] text-muted">
          DataCache:{' '}
          {cacheRows.map((r, i) => (
            <span key={`${r.period}-${r.market}`}>
              {i > 0 && ' · '}
              {r.market} {r.period} {r.packages} 包 {fmtBytes(r.bytes)} ({r.first}~{r.last})
            </span>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <input
          value={ymd}
          onChange={(e) => setYmd(e.target.value.replace(/\D/g, '').slice(0, 8))}
          className="h-7 w-[92px] rounded-btn border border-border bg-base px-2 font-mono text-[11px] text-foreground"
          placeholder="YYYYMMDD"
        />
        <input
          value={codes}
          onChange={(e) => setCodes(e.target.value)}
          className="h-7 w-[150px] rounded-btn border border-border bg-base px-2 font-mono text-[11px] text-foreground"
          placeholder="代码(可选) 600000,510300"
        />
        <select
          value={market}
          onChange={(e) => setMarket(e.target.value as 'SH' | 'SZ')}
          className="h-7 rounded-btn border border-border bg-base px-2 text-[11px] text-foreground"
        >
          <option value="SH">沪 SH</option>
          <option value="SZ">深 SZ</option>
        </select>
        <select
          value={period}
          onChange={(e) => setPeriod(e.target.value as 'Day' | 'Min1' | 'Min5')}
          className="h-7 rounded-btn border border-border bg-base px-2 text-[11px] text-foreground"
        >
          <option value="Day">日K</option>
          <option value="Min1">1分钟</option>
          <option value="Min5">5分钟</option>
        </select>

        <button
          onClick={() => probe.mutate()}
          disabled={busy || ymd.length !== 8}
          className="inline-flex h-7 items-center gap-1 rounded-btn border border-border px-2.5 text-[11px] text-secondary hover:bg-elevated disabled:opacity-40"
        >
          {probe.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Radar className="h-3 w-3" />}
          探测
        </button>
        <button
          onClick={() => sync.mutate(true)}
          disabled={busy || ymd.length !== 8}
          className="inline-flex h-7 items-center gap-1 rounded-btn border border-border px-2.5 text-[11px] text-secondary hover:bg-elevated disabled:opacity-40"
        >
          演练
        </button>
        <button
          onClick={() => sync.mutate(false)}
          disabled={busy || ymd.length !== 8}
          className="inline-flex h-7 items-center gap-1 rounded-btn bg-accent/90 px-2.5 text-[11px] font-medium text-white hover:bg-accent disabled:opacity-40"
        >
          {sync.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <CloudDownload className="h-3 w-3" />}
          同步
        </button>
      </div>

      {probe.data && (
        <div className="text-[10px] text-muted">
          探测 {probe.data.ymd}:{' '}
          {probe.data.packages.map((p) => (
            <span key={`${p.market}-${p.period}`} className="mr-2">
              {p.market} {p.period}{' '}
              <span className={p.status === 200 ? 'text-bull' : 'text-danger'}>
                {p.status === 200 ? fmtBytes(p.bytes) : `HTTP ${p.status}`}
              </span>
            </span>
          ))}
        </div>
      )}

      {result && result.found && (
        <div className="text-[10px] text-secondary">
          {result.ymd} {result.market} {result.period}: {result.symbols} 个标的, 新增{' '}
          <span className="font-mono text-bull">{result.added}</span>, 覆盖{' '}
          <span className="font-mono">{result.replaced}</span>
          {result.dry_run && <span className="ml-1 text-muted">(演练未写盘)</span>}
        </div>
      )}
    </div>
  )
}
