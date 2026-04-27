<!--
  UsabilityCell — F1 "实际可用"列
  四档:✅ 可用 / ⚠️ Grace + 倒计时 / 💤 待机 / ❌ 不可用
-->
<template>
  <div class="flex items-center gap-2 min-w-0">
    <span class="relative flex shrink-0 w-6 h-6 rounded-md items-center justify-center text-xs font-bold"
      :class="badgeClass" :title="usability.hint">
      <span v-if="kind === 'usable'" aria-hidden="true">
        <svg viewBox="0 0 16 16" class="w-3.5 h-3.5"><path fill="currentColor" d="M6.5 11.4L3.6 8.5l1.1-1.1 1.8 1.8L11.3 4l1.1 1.1z"/></svg>
      </span>
      <span v-else-if="kind === 'grace'" aria-hidden="true">
        <svg viewBox="0 0 16 16" class="w-3.5 h-3.5"><path fill="currentColor" d="M8 1.5l7 12.5H1L8 1.5zm0 4.5v3M8 11h.01" stroke="currentColor" stroke-width="0.5"/></svg>
      </span>
      <span v-else-if="kind === 'standby'" aria-hidden="true">
        <svg viewBox="0 0 16 16" class="w-3.5 h-3.5"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 4.5V8l2.2 1.4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
      </span>
      <span v-else-if="kind === 'unusable'" aria-hidden="true">
        <svg viewBox="0 0 16 16" class="w-3.5 h-3.5"><path fill="currentColor" d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
      </span>
      <span v-else>—</span>
    </span>
    <div class="min-w-0">
      <div class="text-xs font-semibold leading-tight" :class="textClass">{{ usability.label }}</div>
      <div v-if="usability.hint" class="text-[10px] leading-tight opacity-60 truncate" :class="textClass">
        {{ usability.hint }}
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { computeUsability } from '../composables/useStatus.js'

const props = defineProps({
  account: { type: Object, required: true },
})

const usability = computed(() => computeUsability(props.account))
const kind = computed(() => usability.value.kind)

const badgeClass = computed(() => {
  switch (kind.value) {
    case 'usable':
      return 'bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-400/30'
    case 'grace':
      return 'bg-orange-500/15 text-orange-300 ring-1 ring-orange-400/40'
    case 'standby':
      return 'bg-amber-500/15 text-amber-300 ring-1 ring-amber-400/30'
    case 'unusable':
      return 'bg-rose-500/15 text-rose-300 ring-1 ring-rose-400/30'
    default:
      return 'bg-slate-500/15 text-slate-300 ring-1 ring-slate-400/20'
  }
})
const textClass = computed(() => {
  switch (kind.value) {
    case 'usable': return 'text-emerald-300'
    case 'grace': return 'text-orange-200'
    case 'standby': return 'text-amber-200'
    case 'unusable': return 'text-rose-200'
    default: return 'text-slate-300'
  }
})
</script>
