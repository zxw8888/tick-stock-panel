import { describe, expect, it } from 'vitest'
import { buildPatternMarkers, patternDates, patternHitsAt, shortPatternLabel } from './kline-patterns'

const meta = [
  { key: 'hammer', label: '锤头' },
  { key: 'engulfing_pattern', label: '吞噬模式' },
  { key: 'doji', label: '十字' },
]

describe('buildPatternMarkers', () => {
  it('returns nothing when no pattern is selected', () => {
    expect(buildPatternMarkers([{ date: '2026-11-24', hammer: 100 }], { selected: [], meta })).toEqual([])
  })

  it('maps a bullish hit to an above label with bull color', () => {
    const [marker] = buildPatternMarkers([{ date: '2026-11-24', hammer: 100 }], { selected: ['hammer'], meta })
    expect(marker).toEqual({
      date: '2026-11-24', kind: 'buy', above: true, color: '#F0724A', label: '锤头', pattern: true,
    })
  })

  it('maps a bearish hit to bear color and ignores unselected patterns', () => {
    const rows = [{ date: '2026-11-24', hammer: -100, doji: 100 }]
    const [marker] = buildPatternMarkers(rows, { selected: ['hammer'], meta })
    expect(marker.kind).toBe('sell')
    expect(marker.color).toBe('#3FBF8F')
    expect(marker.label).toBe('锤头')
  })

  it('merges multiple hits on the same day and caps the label', () => {
    const rows = [{ date: '2026-11-24', hammer: 100, engulfing_pattern: -100, doji: 100 }]
    const [marker] = buildPatternMarkers(rows, { selected: ['hammer', 'engulfing_pattern', 'doji'], meta })
    expect(marker.label).toBe('锤头/吞噬模式+1')
    expect(marker.kind).toBe('buy')
  })

  it('treats a balanced day as neutral', () => {
    const rows = [{ date: '2026-11-24', hammer: 100, engulfing_pattern: -100 }]
    const [marker] = buildPatternMarkers(rows, { selected: ['hammer', 'engulfing_pattern'], meta })
    expect(marker.kind).toBe('neutral')
    expect(marker.color).toBe('#A1A1AA')
  })

  it('skips zero rows and normalizes date strings', () => {
    const rows = [{ date: '2026-11-24 00:00:00', hammer: 0 }, { date: '2026-11-25T00:00:00', hammer: 100 }]
    const markers = buildPatternMarkers(rows, { selected: ['hammer'], meta })
    expect(markers.map(marker => marker.date)).toEqual(['2026-11-25'])
  })

  it('falls back to the column name when metadata is missing', () => {
    const [marker] = buildPatternMarkers([{ date: '2026-11-24', unknown_pattern: 100 }], { selected: ['unknown_pattern'], meta })
    expect(marker.label).toBe('unknown_pattern')
  })
})

describe('pattern helpers', () => {
  it('exposes hit details with full labels', () => {
    const rows = [{ date: '2026-11-24', hammer: 100, doji: -100 }]
    expect(patternHitsAt(rows, '2026-11-24', meta)).toEqual([
      { label: '锤头', value: 100 },
      { label: '十字', value: -100 },
    ])
    expect(patternHitsAt(rows, '2026-11-25', meta)).toEqual([])
  })

  it('collects hit dates', () => {
    expect([...patternDates([{ date: '2026-11-24 00:00:00' }, { date: '2026-11-25' }])])
      .toEqual(['2026-11-24', '2026-11-25'])
  })

  it('shortens long labels but keeps short ones intact', () => {
    expect(shortPatternLabel('hammer', '锤头')).toBe('锤头')
    expect(shortPatternLabel('upside_gap_two_crows', '向上跳空的两只乌鸦')).toBe('跳空鸦')
  })
})
