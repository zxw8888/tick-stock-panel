import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown, Search, X } from 'lucide-react'
import type { KlinePatternMeta } from '@/lib/api'

const MENU_WIDTH = 320
const MENU_MAX_HEIGHT = 420
const VIEWPORT_GAP = 8
const TRIGGER_GAP = 6

interface Props {
  /** 全部形态元数据 (后端 /patterns/meta)。 */
  meta: readonly KlinePatternMeta[]
  /** 当前选中的形态 key (顺序无关)。 */
  selected: readonly string[]
  onChange: (next: string[]) => void
  disabled?: boolean
  /** 首次挂载即展开 (开关打开但还没选形态时, 直接把选择面板亮出来)。 */
  defaultOpen?: boolean
}

/**
 * 形态多选面板: 61 个形态按「看涨 / 看跌 / 双向」分组太武断 (多数形态本身双值),
 * 因此只提供搜索 + 全选/清空 + 勾选列表。面板打开时才渲染, 关闭即卸载。
 */
export function PatternPicker({ meta, selected, onChange, disabled = false, defaultOpen = false }: Props) {
  const [open, setOpen] = useState(defaultOpen)
  const [query, setQuery] = useState('')
  const [position, setPosition] = useState({ top: 0, left: 0 })
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  const chosen = useMemo(() => new Set(selected), [selected])
  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    if (!keyword) return meta
    return meta.filter(item => item.label.toLowerCase().includes(keyword) || item.key.toLowerCase().includes(keyword))
  }, [meta, query])

  const placeMenu = () => {
    const trigger = triggerRef.current
    if (!trigger) return
    const rect = trigger.getBoundingClientRect()
    const menuHeight = Math.min(menuRef.current?.offsetHeight ?? MENU_MAX_HEIGHT, MENU_MAX_HEIGHT)
    const dropUp = window.innerHeight - rect.bottom < menuHeight + TRIGGER_GAP && rect.top > rect.bottom
    const top = dropUp
      ? Math.max(VIEWPORT_GAP, rect.top - menuHeight - TRIGGER_GAP)
      : Math.min(rect.bottom + TRIGGER_GAP, window.innerHeight - menuHeight - VIEWPORT_GAP)
    const left = Math.max(
      VIEWPORT_GAP,
      Math.min(rect.right - MENU_WIDTH, window.innerWidth - MENU_WIDTH - VIEWPORT_GAP),
    )
    setPosition({ top, left })
  }

  useLayoutEffect(() => { if (open) placeMenu() }, [open, filtered.length])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      setOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const toggle = (key: string) => {
    onChange(chosen.has(key) ? selected.filter(item => item !== key) : [...selected, key])
  }

  const label = selected.length === 0 ? '形态' : `形态 ${selected.length}`

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-pressed={open}
        aria-label="K线形态标注"
        title="在 K 线上标注命中的蜡烛形态"
        disabled={disabled}
        onClick={() => { if (!open) placeMenu(); setOpen(v => !v) }}
        className={`flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-mono transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:opacity-40 ${
          selected.length > 0 ? 'bg-[#F0724A]/10 text-[#F0724A]' : 'bg-elevated text-secondary hover:text-foreground'
        }`}
      >
        {label}
        <ChevronDown className="h-3 w-3" />
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          role="dialog"
          aria-label="选择K线形态"
          style={{ top: position.top, left: position.left, width: MENU_WIDTH, maxHeight: MENU_MAX_HEIGHT }}
          className="fixed z-50 flex flex-col overflow-hidden rounded-md border border-border bg-surface shadow-xl"
        >
          <div className="flex items-center gap-2 border-b border-border/70 px-2 py-1.5">
            <Search className="h-3 w-3 shrink-0 text-muted" />
            <input
              autoFocus
              value={query}
              onChange={event => setQuery(event.target.value)}
              placeholder="搜索形态 (中文/列名)"
              aria-label="搜索形态"
              className="min-w-0 flex-1 bg-transparent text-[11px] text-foreground outline-none placeholder:text-muted"
            />
            {query && (
              <button type="button" onClick={() => setQuery('')} aria-label="清空搜索" className="text-muted hover:text-foreground">
                <X className="h-3 w-3" />
              </button>
            )}
          </div>
          <div className="flex items-center justify-between border-b border-border/70 px-2 py-1 text-[10px] text-muted">
            <span>已选 {selected.length} / {meta.length}</span>
            <span className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => onChange(meta.map(item => item.key))}
                className="hover:text-foreground"
              >
                全选
              </button>
              <button type="button" onClick={() => onChange([])} className="hover:text-foreground">清空</button>
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto py-1">
            {filtered.length === 0 && <div className="px-2 py-3 text-center text-[11px] text-muted">没有匹配的形态</div>}
            {filtered.map(item => {
              const active = chosen.has(item.key)
              return (
                <button
                  key={item.key}
                  type="button"
                  role="checkbox"
                  aria-checked={active}
                  onClick={() => toggle(item.key)}
                  className={`flex w-full items-center gap-2 px-2 py-1 text-left text-[11px] transition-colors hover:bg-elevated ${
                    active ? 'text-[#F0724A]' : 'text-secondary'
                  }`}
                >
                  <span className={`flex h-3 w-3 shrink-0 items-center justify-center rounded-sm border ${
                    active ? 'border-[#F0724A] bg-[#F0724A]/20' : 'border-border'
                  }`}>
                    {active && <Check className="h-2.5 w-2.5" />}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{item.label}</span>
                  <span className="shrink-0 font-mono text-[9px] text-muted">{item.key}</span>
                </button>
              )
            })}
          </div>
        </div>,
        document.body,
      )}
    </>
  )
}
