// 极简 toast — F5 列表行操作反馈
import { reactive } from 'vue'

const state = reactive({ items: [] })
let _id = 0

function push(payload) {
  const id = ++_id
  const item = {
    id,
    tone: payload.tone || 'info', // success | error | warning | info
    text: payload.text || '',
    detail: payload.detail || '',
    duration: payload.duration ?? 4000,
    leaving: false,
  }
  state.items.push(item)
  if (item.duration > 0) {
    setTimeout(() => dismiss(id), item.duration)
  }
  return id
}

function dismiss(id) {
  const idx = state.items.findIndex((t) => t.id === id)
  if (idx < 0) return
  state.items[idx].leaving = true
  setTimeout(() => {
    const j = state.items.findIndex((t) => t.id === id)
    if (j >= 0) state.items.splice(j, 1)
  }, 200)
}

export function useToast() {
  return {
    state,
    success: (text, detail, opts = {}) => push({ tone: 'success', text, detail, ...opts }),
    error: (text, detail, opts = {}) => push({ tone: 'error', text, detail, ...opts }),
    warn: (text, detail, opts = {}) => push({ tone: 'warning', text, detail, ...opts }),
    info: (text, detail, opts = {}) => push({ tone: 'info', text, detail, ...opts }),
    dismiss,
  }
}
