import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Loader2, Target } from 'lucide-react'
import { api, type IntegrityAsset, type PipelineJob } from '@/lib/api'
import { DatePicker } from '@/components/DatePicker'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'

const ASSET_LABELS: Record<IntegrityAsset, string> = {
  stock: '股票', etf: 'ETF', index: '指数', lof: 'LOF',
}
const MINUTE_CAPABLE_ASSETS: IntegrityAsset[] = ['stock', 'etf']

function pad(n: number) { return String(n).padStart(2, '0') }
function todayStr() {
  const d = new Date()
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/**
 * 单标的同步：给某一股票/ETF/LOF/指数回填更长历史；股票/ETF 可另拉分钟K。
 * 服务端会用标的维表重新解析资产类型，前端只负责选择与预校验。
 */
export function SymbolSyncPanel({ isRunning, job, onStarted }: {
  isRunning: boolean
  job?: PipelineJob | null
  onStarted: (jobId: string) => void
}) {
  const [symbol, setSymbol] = useState('')
  const [name, setName] = useState('')
  const [assetType, setAssetType] = useState<IntegrityAsset | null>(null)
  const [granularity, setGranularity] = useState<'daily' | 'minute'>('daily')
  const [range, setRange] = useState<'custom' | 'full'>('custom')
  const [startDate, setStartDate] = useState('2015-01-01')
  const [endDate, setEndDate] = useState(todayStr())
  const [ownJobId, setOwnJobId] = useState<string | null>(null)

  const sync = useMutation({
    mutationFn: () => api.symbolSync({
      symbol,
      asset_type: assetType ?? undefined,
      granularity,
      range,
      ...(range === 'custom' ? { start_date: startDate, end_date: endDate } : {}),
    }),
    onSuccess: ({ job_id }) => { setOwnJobId(job_id); onStarted(job_id) },
  })

  const today = todayStr()
  const minuteAllowed = !!assetType && MINUTE_CAPABLE_ASSETS.includes(assetType)
  const datesValid = range === 'full'
    || (!!startDate && !!endDate && startDate <= endDate && endDate <= today)
  const pending = isRunning || sync.isPending
  const canSubmit = !!symbol && datesValid && !pending
  const ownResult = job?.id === ownJobId && job?.status === 'succeeded' ? job.result : null
  const message = sync.error
    || (job?.id === ownJobId && job?.status === 'failed' ? job.error : null)
  const lastLog = job?.id === ownJobId ? job?.log?.at(-1)?.msg : null

  function handleSelect(nextSymbol: string, nextName: string, nextAssetType?: string) {
    setSymbol(nextSymbol)
    setName(nextName)
    if (nextAssetType === 'stock' || nextAssetType === 'etf'
      || nextAssetType === 'index' || nextAssetType === 'lof') {
      setAssetType(nextAssetType)
      if (!MINUTE_CAPABLE_ASSETS.includes(nextAssetType)) setGranularity('daily')
    } else {
      setAssetType(null)
    }
  }

  function setGranularitySafe(next: 'daily' | 'minute') {
    if (next === 'minute' && assetType && !MINUTE_CAPABLE_ASSETS.includes(assetType)) return
    setGranularity(next)
  }

  return (
    <section className="space-y-4" aria-label="单标的同步">
      <p className="text-xs text-secondary leading-relaxed">
        给选中的一个标的按自定义区间或全量回填数据。新数据按 (标的, 日期) 合并覆盖，不会删除其他历史；
        指数 / LOF 无分钟K源，只支持日K。
      </p>

      <div className="rounded-card border border-border bg-base/30 p-4 space-y-3">
        <div className="space-y-1.5">
          <div className="text-sm font-medium text-foreground">选择标的</div>
          <StockFinancialSearch
            assetTypes="stock,etf,index,lof"
            onSelect={handleSelect}
          />
          {symbol && (
            <div className="flex flex-wrap items-center gap-2 text-[11px] text-secondary">
              <Target className="h-3.5 w-3.5 text-accent" />
              <span className="font-mono">{symbol}</span>
              <span className="text-muted">{name}</span>
              <span className="rounded-full border border-border bg-elevated/40 px-2 py-0.5">
                {assetType ? ASSET_LABELS[assetType] : '由服务端识别'}
              </span>
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <div className="text-xs text-secondary">数据粒度</div>
          <div className="inline-flex overflow-hidden rounded-btn border border-border">
            <button
              type="button"
              onClick={() => setGranularitySafe('daily')}
              className={`px-3 py-1.5 text-xs transition-colors ${
                granularity === 'daily' ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-accent/8'
              }`}
            >
              日K
            </button>
            <button
              type="button"
              onClick={() => setGranularitySafe('minute')}
              disabled={!!assetType && !minuteAllowed}
              className={`px-3 py-1.5 text-xs transition-colors disabled:opacity-40 ${
                granularity === 'minute' ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-accent/8'
              }`}
            >
              分钟K
            </button>
          </div>
          {assetType && !minuteAllowed && (
            <span className="text-[11px] text-muted">指数 / LOF 无分钟K源</span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <div className="text-xs text-secondary">历史范围</div>
          <div className="inline-flex overflow-hidden rounded-btn border border-border">
            <button
              type="button"
              onClick={() => setRange('custom')}
              className={`px-3 py-1.5 text-xs transition-colors ${
                range === 'custom' ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-accent/8'
              }`}
            >
              自定义
            </button>
            <button
              type="button"
              onClick={() => setRange('full')}
              className={`px-3 py-1.5 text-xs transition-colors ${
                range === 'full' ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-accent/8'
              }`}
            >
              全量
            </button>
          </div>
        </div>

        {range === 'custom' && (
          <div className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-secondary">起始日期</span>
              <DatePicker
                value={startDate}
                onChange={setStartDate}
                min="1990-12-19"
                max={endDate || today}
                align="right"
                buttonClassName="font-mono text-xs"
              />
            </div>
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-secondary">结束日期</span>
              <DatePicker
                value={endDate}
                onChange={setEndDate}
                min={startDate}
                max={today}
                align="right"
                buttonClassName="font-mono text-xs"
              />
            </div>
            {!datesValid && (
              <p role="alert" className="text-[11px] text-danger">
                请检查日期：起始不能晚于结束，且不能晚于今天。
              </p>
            )}
          </div>
        )}

        {range === 'full' && (
          <div className="rounded-btn border border-border bg-elevated/30 p-3 text-[11px] text-muted">
            全量会按数据源可用历史分窗回溯。日K可到安全窗口上限；分钟K受数据源窗口限制，不承诺覆盖任意久远历史。
          </div>
        )}

        <div className="text-[10px] text-muted">
          本次将同步 <span className="font-mono text-secondary">{granularity === 'daily' ? '日K' : '分钟K'}</span>
          {' · '}<span className="font-mono text-secondary">{range === 'custom' ? `${startDate} ~ ${endDate}` : '全量'}</span>
        </div>

        <button
          onClick={() => sync.mutate()}
          disabled={!canSubmit}
          className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-white text-xs font-medium hover:bg-accent disabled:opacity-40 disabled:pointer-events-none transition-colors duration-150"
        >
          {pending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Target className="h-3 w-3" />}
          {pending ? '任务进行中…' : '开始单标的同步'}
        </button>

        {sync.data?.reused && (
          <p role="status" className="text-[11px] text-warning">
            已有数据任务在运行，本次请求已复用该任务。
          </p>
        )}
        {message && (
          <p role="alert" className="text-xs text-danger">
            {message instanceof Error ? message.message : String(message)}
          </p>
        )}
        {job?.id === ownJobId && job?.status === 'running' && (
          <div role="status" className="rounded-btn border border-accent/25 bg-accent/5 p-3 text-xs text-secondary">
            {lastLog || '任务正在准备，请稍候…'}
            <span className="ml-2 font-mono text-accent">{job.progress}%</span>
          </div>
        )}
        {ownResult && (
          <div className="rounded-card border border-border bg-elevated/30 p-3 space-y-2 text-xs">
            <div className="flex items-center gap-1.5 text-bear">
              <CheckCircle2 className="h-4 w-4" />
              单标的同步完成
            </div>
            <div className="text-secondary">
              写入 <span className="font-mono">{ownResult.written_rows ?? 0}</span> 行
              {ownResult.actual?.start && ownResult.actual?.end && (
                <> · 实际区间 <span className="font-mono">{ownResult.actual.start} ~ {ownResult.actual.end}</span></>
              )}
              {ownResult.windows_fetched != null && (
                <> · 窗口 <span className="font-mono">{ownResult.windows_fetched}</span></>
              )}
            </div>
            {!!ownResult.warnings?.length && (
              <div className="space-y-1 text-warning">
                {ownResult.warnings.map((warning, i) => (
                  <div key={i} className="flex items-start gap-1.5">
                    <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                    <span>{warning}</span>
                  </div>
                ))}
              </div>
            )}
            {!!ownResult.refreshed_views?.length && (
              <div className="text-[11px] text-muted">
                已刷新视图：{ownResult.refreshed_views.join('、')}
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  )
}
