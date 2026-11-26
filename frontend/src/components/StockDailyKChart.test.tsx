// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { KlineRow } from '@/lib/api'
import { StockDailyKChart } from './StockDailyKChart'

const fixture = vi.hoisted(() => ({ rows: [
  { date: '2026-11-03', open: 10, high: 11, low: 10, close: 11, volume: 100, signal_limit_up: true, consecutive_limit_ups: 3 },
  { date: '2026-11-04', open: 11, high: 12, low: 10, close: 10.5, volume: 100, signal_limit_up: true, signal_broken_limit_up: true },
  { date: '2026-11-05', open: 10.5, high: 11, low: 10, close: 10.8, volume: 100 },
  { date: '2026-11-06', open: 10.2, high: 11.88, low: 9.72, close: 9.72, volume: 100, signal_limit_down: true, signal_broken_limit_up: true },
] as KlineRow[] }))
const queries = vi.hoisted(() => ({ calls: [] as string[] }))
vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: { queryKey?: readonly unknown[]; enabled?: boolean }) => {
    const key = Array.isArray(options?.queryKey) ? String(options.queryKey[0]) : ''
    queries.calls.push(`${key}:${options?.enabled === false ? 'off' : 'on'}`)
    if (key === 'kline-patterns-meta') {
      return {
        data: { patterns: [{ key: 'hammer', label: '锤头' }, { key: 'engulfing_pattern', label: '吞噬模式' }] },
        isLoading: false, isError: false,
      }
    }
    if (key === 'kline-patterns') {
      return { data: { patterns: [], rows: [{ date: '2026-11-05', hammer: 100 }] }, isLoading: false, isError: false }
    }
    return { data: fixture, isLoading: false, isError: false }
  },
}))
vi.mock('@/lib/kline', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/kline')>(), klineDailyQueryOptions: vi.fn(() => ({})),
}))
vi.mock('./charts/LightweightDaily', () => ({ LightweightDaily: (props: any) => <output>{JSON.stringify(props)}</output> }))
vi.mock('./EChartsCandlestick', () => ({ EChartsCandlestick: (props: any) => <output>{JSON.stringify(props)}</output> }))

let host: HTMLDivElement, root: Root
const originalRows = fixture.rows
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  queries.calls = []
  fixture.rows = originalRows
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); host.remove(); localStorage.clear() })
const props = () => JSON.parse(host.querySelector('output')!.textContent!)
const render = async (extra: Partial<Parameters<typeof StockDailyKChart>[0]> = {}) => act(async () => {
  root.render(<StockDailyKChart symbol="605365.SH" renderer="lightweight" {...extra} />)
})

it('identifies limit events from existing signals, prioritizing a limit-down close over a broken limit-up', async () => {
  await render({ markers: [{ date: '2026-11-05', kind: 'buy', label: '策略买点' }] })
  expect((host.querySelector('[aria-label="异动显示方式"]') as HTMLSelectElement).value).toBe('label')
  expect(props().markerStyle).toBe('label')
  expect(props().markers).toEqual([
    expect.objectContaining({ label: '策略买点' }),
    expect.objectContaining({ date: '2026-11-03', event: 'limit_up', label: '3', color: '#FACC15' }),
    expect.objectContaining({ date: '2026-11-04', event: 'broken_limit_up', label: '炸', color: '#8B5CF6' }),
    expect.objectContaining({ date: '2026-11-06', event: 'limit_down', kind: 'sell', above: false, label: '跌', color: '#22D3EE' }),
  ])
  expect(props().markers[0].event).toBeUndefined()
})

it.each([
  ['bearish recovery', { open: 4.75, signal_limit_down_recovery: false }, 'limit_down_recovery'],
  ['bullish recovery', { open: 4.6, signal_limit_down_recovery: true }, 'limit_down_recovery'],
  ['doji recovery', { open: 4.7 }, 'limit_down_recovery'],
  ['no custom signal', { signal_limit_down_recovery: undefined }, 'limit_down_recovery'],
  ['not touching the raw limit', { raw_low: 9.01 }, undefined],
  ['still at the raw limit', { raw_close: 9.005 }, undefined],
  ['no-limit day overrides old signal', { limit_down_price: null, signal_limit_down_recovery: true }, undefined],
  ['invalid raw price', { raw_low: NaN, signal_limit_down_recovery: true }, undefined],
  ['missing raw price', { raw_close: undefined }, undefined],
  ['legacy recovery signal', { raw_low: undefined, raw_close: undefined, limit_down_price: undefined, signal_limit_down_recovery: true }, 'limit_down_recovery'],
  ['legacy absent signal', { raw_low: undefined, raw_close: undefined, limit_down_price: undefined }, undefined],
  ['sealed limit-down wins', { signal_limit_down: true }, 'limit_down'],
  ['broken limit-up wins a two-sided touch', { signal_broken_limit_up: true }, 'broken_limit_up'],
  ['sealed limit-up wins', { signal_limit_up: true }, 'limit_up'],
] as [string, Partial<KlineRow>, string | undefined][])('identifies %s from raw limit metadata without changing signal definitions', async (_name, patch, event) => {
  const row: KlineRow = { date: '2026-11-05', open: 4.75, high: 5, low: 4.5, close: 4.7, volume: 100,
    raw_low: 9, raw_close: 9.4, limit_down_price: 9.005, ...patch }
  fixture.rows = [row]
  await render()
  expect(props().markers.map((marker: { event?: string }) => marker.event)).toEqual(event ? [event] : [])
  expect(fixture.rows[0]).toEqual(row)
  await render({ renderer: 'echarts' })
  expect(props().markers.some((marker: { event?: string }) => marker.event === 'limit_down_recovery')).toBe(false)
  await render({ showLimitMarkers: false })
  expect(props().markers).toEqual([])
})

it('persists the style independently of the existing marker on/off switch', async () => {
  await render()
  const select = host.querySelector('[aria-label="异动显示方式"]') as HTMLSelectElement
  expect([...select.options].map(option => [option.value, option.text])).toEqual([['label', '文字'], ['candle', '颜色']])
  await act(async () => { select.value = 'candle'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(props().markerStyle).toBe('candle')
  expect(JSON.parse(localStorage.getItem('stock_daily_marker_style')!)).toBe('candle')
  const toggle = [...host.querySelectorAll('button')].find(button => button.textContent === '异动')!
  await act(async () => toggle.click())
  expect(props().showMarkers).toBe(false)
  expect(select.disabled).toBe(true)
  await act(async () => toggle.click())
  expect(props().showMarkers).toBe(true)
  await act(async () => root.unmount())
  root = createRoot(host)
  await render()
  expect(props().markerStyle).toBe('candle')
})

it('falls back from a saved arrow preference to text labels without discarding candle preferences', async () => {
  localStorage.setItem('stock_daily_marker_style', JSON.stringify('arrow'))
  await render()
  expect(props().markerStyle).toBe('label')
  expect((host.querySelector('[aria-label="异动显示方式"]') as HTMLSelectElement).value).toBe('label')
  expect(host.querySelector('option[value="arrow"]')).toBeNull()
})

it('rejects unknown saved styles and leaves legacy ECharts consumers on their existing behavior', async () => {
  localStorage.setItem('stock_daily_marker_style', JSON.stringify('unsupported'))
  await render()
  expect(props().markerStyle).toBe('label')
  await render({ renderer: 'echarts' })
  expect(host.querySelector('[aria-label="异动显示方式"]')).toBeNull()
  expect(props().markerStyle).toBeUndefined()
  expect(props().markers.some((marker: { event?: string }) => marker.event === 'limit_down')).toBe(false)
  expect(props().markers).toContainEqual(expect.objectContaining({ date: '2026-11-06', event: 'broken_limit_up' }))
})

it('hides the style selector wherever the existing event toggle is unavailable', async () => {
  await render({ showMarkerToggle: false })
  expect(host.querySelector('[aria-label="异动显示方式"]')).toBeNull()
  await render({ showLimitMarkers: false })
  expect(host.querySelector('[aria-label="异动显示方式"]')).toBeNull()
})

it('keeps the pattern switch off by default and skips its requests entirely', async () => {
  await render()
  expect(host.querySelector('[aria-label="K线形态标注"]')).toBeNull()
  expect(queries.calls).toContain('kline-patterns-meta:off')
  expect(queries.calls).toContain('kline-patterns:off')
  expect(props().markers.some((marker: { pattern?: boolean }) => marker.pattern)).toBe(false)
})

it('enables pattern markers, persists the toggle, and paints the hit on the chart', async () => {
  await render()
  const toggle = host.querySelector('button[title="显示K线上的蜡烛形态标注"]') as HTMLButtonElement
  await act(async () => toggle.click())

  expect(queries.calls).toContain('kline-patterns-meta:on')
  expect(JSON.parse(localStorage.getItem('stock_daily_pattern_markers')!)).toEqual({ enabled: true, selected: [] })
  expect(host.querySelector('[aria-label="K线形态标注"]')).not.toBeNull()
  // 打开开关但还没选形态时, 选择面板自动展开
  expect(document.querySelector('[aria-label="选择K线形态"]')).not.toBeNull()

  await act(async () => {
    const checkbox = [...document.querySelectorAll('[role="checkbox"]')]
      .find(node => node.textContent?.includes('锤头')) as HTMLButtonElement
    checkbox.click()
  })
  expect(JSON.parse(localStorage.getItem('stock_daily_pattern_markers')!)).toEqual({ enabled: true, selected: ['hammer'] })
  expect(props().markers).toContainEqual(expect.objectContaining({
    date: '2026-11-05', label: '锤头', pattern: true, above: true,
  }))

  await act(async () => (host.querySelector('button[title="隐藏K线上的蜡烛形态标注"]') as HTMLButtonElement).click())
  expect(props().markers.some((marker: { pattern?: boolean }) => marker.pattern)).toBe(false)
  expect(JSON.parse(localStorage.getItem('stock_daily_pattern_markers')!)).toEqual({ enabled: false, selected: ['hammer'] })
})

it('hides the pattern switch when marker toggles are disabled', async () => {
  await render({ showMarkerToggle: false })
  expect(host.querySelector('[aria-label="K线形态标注"]')).toBeNull()
})
