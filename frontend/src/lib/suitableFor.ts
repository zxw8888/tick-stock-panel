import type { StrategySuitableFor } from './api'

export const MARKET_CAP_LABELS: Record<string, string> = {
  micro: '微盘',
  small: '小盘',
  mid: '中盘',
  large: '大盘',
  mega: '超大盘',
  all: '不限市值',
}

export const STYLE_LABELS: Record<string, string> = {
  trend: '趋势',
  leader: '龙头',
  breakout: '突破',
  momentum: '动量',
  reversal: '反转',
  mean_reversion: '均值回归',
  value: '价值',
  growth: '成长',
  defensive: '防御',
  low_vol: '低波',
  rotation: '轮动',
  volume: '量价',
}

export const VOLATILITY_LABELS: Record<string, string> = {
  low: '低波动',
  normal: '常规波动',
  high: '高波动',
}

export const LIQUIDITY_LABELS: Record<string, string> = {
  low: '低流动性',
  normal: '一般流动性',
  high: '高流动性',
}

const labelOf = (map: Record<string, string>, value: string) => map[value] ?? value

export function suitableTags(sf?: StrategySuitableFor | null, max = 3): string[] {
  if (!sf) return []
  const candidates = [
    ...(sf.styles ?? []).map(value => labelOf(STYLE_LABELS, value)),
    ...(sf.market_cap ?? []).filter(value => value !== 'all').map(value => labelOf(MARKET_CAP_LABELS, value)),
    ...(sf.volatility ?? []).map(value => labelOf(VOLATILITY_LABELS, value)),
  ]
  return [...new Set(candidates)].slice(0, max)
}

export interface SuitableSection {
  label: string
  values: string[]
}

export function suitableSections(sf?: StrategySuitableFor | null): SuitableSection[] {
  if (!sf) return []
  const sections: SuitableSection[] = [
    { label: '市值', values: (sf.market_cap ?? []).map(value => labelOf(MARKET_CAP_LABELS, value)) },
    { label: '风格', values: (sf.styles ?? []).map(value => labelOf(STYLE_LABELS, value)) },
    { label: '波动', values: (sf.volatility ?? []).map(value => labelOf(VOLATILITY_LABELS, value)) },
    { label: '流动性', values: (sf.liquidity ?? []).map(value => labelOf(LIQUIDITY_LABELS, value)) },
    { label: '板块', values: sf.boards ?? [] },
    { label: '回避', values: sf.avoid ?? [] },
  ]
  return sections.filter(section => section.values.length > 0)
}

export function matchesSuitableFor(
  sf: StrategySuitableFor | null | undefined,
  filters: { marketCap?: string; style?: string },
): boolean {
  if (!sf) return !filters.marketCap && !filters.style
  if (filters.marketCap && !(sf.market_cap ?? []).includes(filters.marketCap)) return false
  if (filters.style && !(sf.styles ?? []).includes(filters.style)) return false
  return true
}

export function suitableFilterOptions(items: readonly { suitable_for?: StrategySuitableFor }[]) {
  const marketCaps = new Set<string>()
  const styles = new Set<string>()
  for (const item of items) {
    for (const value of item.suitable_for?.market_cap ?? []) {
      if (value !== 'all') marketCaps.add(value)
    }
    for (const value of item.suitable_for?.styles ?? []) styles.add(value)
  }
  return {
    marketCaps: [...marketCaps].sort((a, b) => labelOf(MARKET_CAP_LABELS, a).localeCompare(labelOf(MARKET_CAP_LABELS, b), 'zh-Hans-CN')),
    styles: [...styles].sort((a, b) => labelOf(STYLE_LABELS, a).localeCompare(labelOf(STYLE_LABELS, b), 'zh-Hans-CN')),
  }
}
