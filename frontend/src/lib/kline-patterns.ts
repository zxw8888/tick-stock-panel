/**
 * K 线蜡烛形态标注: 把 /api/kline/patterns 的命中行转成图表 marker。
 *
 * 后端按需计算 (开关打开才请求), 这里只做展示层聚合:
 *   - 看涨 (+100/+200) 红字、看跌 (-100/-200) 绿字, 与 A 股涨跌配色一致
 *   - 同一天命中多个形态时合成一条标签, 避免 markPoint 叠成一坨
 *   - 标签用短名, 长中文名在 tooltip/选择面板里给全
 */
import type { ChartMarker } from './chart-types'

/** 形态标签配色: 看涨红 / 看跌绿 (A 股习惯)。 */
export const PATTERN_BULL_COLOR = '#F0724A'
export const PATTERN_BEAR_COLOR = '#3FBF8F'

/** 一天最多合并多少个形态名, 超出用 +N 收尾, 防止标签横向铺满。 */
const MAX_LABELS_PER_DAY = 2

export interface PatternHitRow {
  date: string
  [key: string]: number | string
}

export interface PatternMetaEntry {
  key: string
  label: string
}

/** 形态中文名的展示缩写: 只做「太长会压住蜡烛」的那几个, 其余原样。 */
const SHORT_LABELS: Record<string, string> = {
  upside_gap_two_crows: '跳空鸦',
  identical_three_crows: '三胞鸦',
  three_stars_in_the_south: '南方三星',
  concealing_baby_swallow: '藏婴吞没',
  kicking_bull_bear: '反冲',
  rising_falling_three: '三法',
  upside_downside_gap: '跳空三法',
  up_down_gap: '跳空并列阳',
  three_inside_up_down: '三内部',
  three_outside_up_down: '三外部',
  harami_cross_pattern: '十字孕线',
  long_legged_doji: '长脚十字',
  gravestone_doji: '墓碑十字',
  dragonfly_doji: '蜻蜓十字',
  closing_marubozu: '收盘缺影',
  long_line_candle: '长蜡烛',
  short_line_candle: '短蜡烛',
  high_wave_candle: '风高浪大',
  three_line_strike: '三线打击',
  modified_hikkake_pattern: '修正陷阱',
  advance_block: '大敌当前',
  stick_sandwich: '条形三明治',
  unique_3_river: '奇特三河',
  three_white_soldiers: '三个白兵',
  three_black_crows: '三只乌鸦',
}

export function shortPatternLabel(key: string, label?: string): string {
  return SHORT_LABELS[key] ?? label ?? key
}

export interface PatternMarkerOptions {
  /** 选中的形态 key; 空数组 = 未选择任何形态 (不标注)。 */
  selected: readonly string[]
  /** 形态元数据 (来自 /api/kline/patterns/meta), 用于取中文名。 */
  meta: readonly PatternMetaEntry[]
}

/**
 * 命中行 → ChartMarker。
 *
 * 只处理 selected 里的形态; 同一根 K 线命中多个时合成一条 above 标签,
 * 颜色取该日「净方向」(看涨计数 - 看跌计数) 的符号, 打平用中性色。
 */
export function buildPatternMarkers(
  rows: readonly PatternHitRow[],
  options: PatternMarkerOptions,
): ChartMarker[] {
  const { selected } = options
  if (selected.length === 0) return []

  const labelOf = new Map(options.meta.map(entry => [entry.key, entry.label]))
  const wanted = new Set(selected)
  const markers: ChartMarker[] = []

  for (const row of rows) {
    const hits: { key: string; value: number }[] = []
    for (const key of selected) {
      const raw = row[key]
      const value = typeof raw === 'number' ? raw : Number(raw)
      if (!wanted.has(key) || !Number.isFinite(value) || value === 0) continue
      hits.push({ key, value })
    }
    if (hits.length === 0) continue

    const bull = hits.filter(hit => hit.value > 0).length
    const bear = hits.filter(hit => hit.value < 0).length
    const color = bull > bear ? PATTERN_BULL_COLOR : bear > bull ? PATTERN_BEAR_COLOR : '#A1A1AA'
    const shown = hits.slice(0, MAX_LABELS_PER_DAY)
      .map(hit => shortPatternLabel(hit.key, labelOf.get(hit.key)))
    const label = hits.length > MAX_LABELS_PER_DAY
      ? `${shown.join('/')}+${hits.length - MAX_LABELS_PER_DAY}`
      : shown.join('/')

    markers.push({
      date: String(row.date).slice(0, 10),
      kind: bull > bear ? 'buy' : bear > bull ? 'sell' : 'neutral',
      above: true,
      color,
      label,
      pattern: true,
    })
  }

  return markers
}

/** 该日命中的形态明细, 供信息条/悬停展示 (中文全名 + 方向)。 */
export function patternHitsAt(
  rows: readonly PatternHitRow[],
  date: string,
  meta: readonly PatternMetaEntry[],
): { label: string; value: number }[] {
  const row = rows.find(item => String(item.date).slice(0, 10) === date)
  if (!row) return []
  const labelOf = new Map(meta.map(entry => [entry.key, entry.label]))
  const result: { label: string; value: number }[] = []
  for (const entry of meta) {
    const raw = row[entry.key]
    const value = typeof raw === 'number' ? raw : Number(raw)
    if (Number.isFinite(value) && value !== 0) result.push({ label: labelOf.get(entry.key) ?? entry.key, value })
  }
  return result
}

/** 形态命中行的日期集合, 用于「有形态」的快速判断。 */
export function patternDates(rows: readonly PatternHitRow[]): Set<string> {
  return new Set(rows.map(row => String(row.date).slice(0, 10)))
}
