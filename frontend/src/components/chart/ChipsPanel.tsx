import { useMemo, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type KlineChipsPayload } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** 获利盘红 / 套牢盘绿, 与 A 股涨跌配色一致。 */
const PROFIT_COLOR = '#F0724A'
const TRAPPED_COLOR = '#3FBF8F'
const AVG_COST_COLOR = '#FACC15'

interface Props {
  symbol: string
  /** 目标交易日 YYYY-MM-DD; 缺省 = 最后一根 */
  date?: string | null
  /** 参与衰减的交易日数 */
  cyqDays?: number
  height?: number
  className?: string
  /** 头部右侧补充信息 (例如当前价) */
  footer?: ReactNode
}

function fmtPct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

/**
 * 筹码分布面板 (CYQ)。
 *
 * 竖向价格轴 + 横向筹码柱: 现价以下 (获利盘) 用暖色, 以上 (套牢盘) 用冷色,
 * 平均成本画一条黄色虚线。数据来自 /api/kline/chips, 单次按需计算, 不参与
 * 逐行指标管线。
 */
export function ChipsPanel({ symbol, date, cyqDays = 210, height = 320, className, footer }: Props) {
  const chipsQuery = useQuery({
    queryKey: QK.klineChips(symbol, date, cyqDays),
    queryFn: () => api.klineChips(symbol, { date: date ?? undefined, cyqDays }),
    enabled: !!symbol,
    staleTime: 5 * 60_000,
  })

  const payload = chipsQuery.data?.chips
  const source = chipsQuery.data?.turnover_source

  const model = useMemo(() => buildChipsModel(payload), [payload])

  if (chipsQuery.isLoading) {
    return <div className={`text-xs text-muted ${className ?? ''}`}>筹码分布计算中…</div>
  }
  if (chipsQuery.isError) {
    return <div className={`text-xs text-danger ${className ?? ''}`}>筹码分布加载失败</div>
  }
  if (!payload || !model) {
    return (
      <div className={`text-xs text-muted ${className ?? ''}`}>
        暂无筹码数据{chipsQuery.data?.detail ? ` (${chipsQuery.data.detail})` : ''}
      </div>
    )
  }

  const { bins, maxChips, avgCost, currentPrice, benefit } = model
  // 150 档柱全画，但价格刻度按面板高度抽稀，避免 400px 内 150 行文字叠成黑块。
  const labelStride = Math.max(1, Math.ceil(bins.length / Math.max(1, Math.floor(height / 14))))

  return (
    <div className={className}>
      <div className="mb-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[10px]">
        <span className="text-muted">获利盘 <span className="font-mono" style={{ color: PROFIT_COLOR }}>{fmtPct(benefit)}</span></span>
        <span className="text-muted">平均成本 <span className="font-mono" style={{ color: AVG_COST_COLOR }}>{payload.avg_cost}</span></span>
        <span className="text-muted">90% <span className="font-mono text-secondary">{payload.percent_chips?.['90']?.priceRange?.join(' ~ ') ?? '—'}</span></span>
        <span className="text-muted">集中度 <span className="font-mono text-secondary">{fmtPct(payload.percent_chips?.['90']?.concentration)}</span></span>
        {source === 'volume_proxy' && (
          <span className="text-muted/70" title="ETF/LOF/指数没有换手率字段, 用相对成交量做衰减代理, 形状可信但绝对刻度不代表真实换手">
            · 成交量代理
          </span>
        )}
        {chipsQuery.data?.date && <span className="text-muted/60">· {chipsQuery.data.date}</span>}
      </div>

      <div className="relative" style={{ height }}>
        {bins.map((bin, index) => {
          const width = maxChips > 0 ? (bin.value / maxChips) * 100 : 0
          const isProfit = currentPrice != null && bin.price <= currentPrice
          const isAvg = avgCost != null && Math.abs(bin.price - avgCost) <= bin.step / 2
          const showLabel = index % labelStride === 0
          return (
            <div
              key={index}
              className="absolute left-0 right-0 flex items-center gap-1"
              style={{ bottom: `${(index / bins.length) * 100}%`, height: `${100 / bins.length}%` }}
            >
              <span className="w-12 shrink-0 text-right font-mono text-[9px] text-muted">
                {showLabel ? bin.price.toFixed(2) : ''}
              </span>
              <div className="relative h-full min-w-0 flex-1">
                <div
                  className="h-[70%] rounded-sm"
                  style={{
                    width: `${width}%`,
                    backgroundColor: isProfit ? PROFIT_COLOR : TRAPPED_COLOR,
                    opacity: 0.55,
                  }}
                />
                {isAvg && <div className="absolute inset-x-0 top-1/2 border-t border-dashed" style={{ borderColor: AVG_COST_COLOR }} />}
              </div>
            </div>
          )
        })}
        {/* 现价横线 */}
        {currentPrice != null && (
          <div
            className="pointer-events-none absolute inset-x-0 border-t"
            style={{
              bottom: `${priceRatio(bins, currentPrice) * 100}%`,
              borderColor: '#E4E4E7',
              opacity: 0.35,
            }}
          />
        )}
      </div>
      {footer && <div className="mt-1 text-[10px] text-muted">{footer}</div>}
    </div>
  )
}

interface ChipsModel {
  bins: { price: number; value: number; step: number }[]
  maxChips: number
  avgCost: number | null
  currentPrice: number | null
  benefit: number
}

function buildChipsModel(payload: Partial<KlineChipsPayload> | undefined): ChipsModel | null {
  const x = payload?.x
  const y = payload?.y
  if (!x?.length || !y?.length || x.length !== y.length) return null

  const step = y.length > 1 ? y[1] - y[0] : 0.01
  const bins = y.map((price, index) => ({ price, value: x[index] ?? 0, step }))
  const maxChips = Math.max(...bins.map(bin => bin.value), 0)
  const avgCost = payload?.avg_cost != null ? Number(payload.avg_cost) : null
  // 现价: b 为「>= 现价的第一个档位」的 1-based 索引, 回推档位中心
  const b = payload?.b
  const currentPrice = b != null && b >= 1 && b <= y.length
    ? y[b - 1]
    : null

  return {
    bins,
    maxChips,
    avgCost: avgCost != null && Number.isFinite(avgCost) ? avgCost : null,
    currentPrice,
    benefit: payload?.benefit_part ?? 0,
  }
}

/** 价格 → 在 y 轴上的相对位置 (0 = 最低档, 1 = 最高档)。 */
function priceRatio(bins: { price: number }[], price: number): number {
  if (bins.length < 2) return 0
  const lo = bins[0].price
  const hi = bins[bins.length - 1].price
  if (hi <= lo) return 0
  return Math.max(0, Math.min(1, (price - lo) / (hi - lo)))
}
