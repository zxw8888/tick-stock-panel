// @vitest-environment jsdom
// 监控规则阈值字段下拉: 后端给分组时按 optgroup 渲染 (61 个 K线形态平铺会
// 把下拉撑爆), 没给分组时回退平铺以兼容旧后端。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { ThresholdFieldOptions } from './RuleEditor'

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)

function render(ui: React.ReactElement) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  root = createRoot(container)
  act(() => {
    root!.render(ui)
  })
}

beforeEach(() => {
  container.innerHTML = ''
})

afterEach(() => {
  act(() => root?.unmount())
  root = null
})

const FIELDS = [
  { key: 'close', label: '收盘价' },
  { key: 'doji', label: '十字星 (K线形态)' },
]

it('groups fields into optgroups when the backend provides groups', () => {
  render(
    <select>
      <ThresholdFieldOptions
        fields={FIELDS}
        groups={[
          { key: 'quote', label: '行情', fields: [FIELDS[0]] },
          { key: 'pattern', label: 'K线形态', fields: [FIELDS[1]] },
        ]}
      />
    </select>,
  )
  const groups = [...container.querySelectorAll('optgroup')]
  expect(groups.map(g => g.getAttribute('label'))).toEqual(['行情', 'K线形态'])
  expect([...container.querySelectorAll('option')].map(o => o.value)).toEqual(['close', 'doji'])
})

it('falls back to a flat option list when groups are absent', () => {
  render(
    <select>
      <ThresholdFieldOptions fields={FIELDS} groups={[]} />
    </select>,
  )
  expect(container.querySelectorAll('optgroup')).toHaveLength(0)
  expect([...container.querySelectorAll('option')].map(o => o.value)).toEqual(['close', 'doji'])
})
