// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type RecommendedStrategiesResponse } from '@/lib/api'
import { RecommendedStrategiesDialog } from './RecommendedStrategiesDialog'

vi.mock('@/lib/api', () => ({
  api: {
    recommendedStrategies: vi.fn(),
    recommendedStrategiesInstall: vi.fn(),
  },
}))

const response: RecommendedStrategiesResponse = {
  pack_version: 'test',
  prompt_pending_ids: [],
  items: [],
}

let host: HTMLDivElement
let root: Root
let client: QueryClient
let onClose: ReturnType<typeof vi.fn>

function render(open: boolean) {
  return act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <RecommendedStrategiesDialog open={open} onClose={onClose} />
      </QueryClientProvider>,
    )
    await new Promise(resolve => setTimeout(resolve, 0))
  })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  vi.mocked(api.recommendedStrategies).mockResolvedValue(response)
  vi.mocked(api.recommendedStrategiesInstall).mockResolvedValue({ ...response, installed_ids: [], backup_dir: null })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  onClose = vi.fn()
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

it('关闭状态不挂载 Modal 遮罩', async () => {
  await render(false)
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(host.querySelector('.fixed.inset-0')).toBeNull()
  expect(api.recommendedStrategies).not.toHaveBeenCalled()
})

it('打开后挂载弹窗，关闭回调触发并卸载遮罩', async () => {
  await render(true)
  const dialog = host.querySelector('[role="dialog"]')
  expect(dialog).not.toBeNull()
  expect(host.querySelector('.fixed.inset-0')).not.toBeNull()

  const closeButton = host.querySelector<HTMLButtonElement>('button[aria-label="关闭"]')!
  expect(closeButton).toBeTruthy()
  await act(async () => closeButton.click())
  expect(onClose).toHaveBeenCalledTimes(1)

  await render(false)
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(host.querySelector('.fixed.inset-0')).toBeNull()
})
