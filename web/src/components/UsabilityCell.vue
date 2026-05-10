<!--
  UsabilityCell — F1 "实际可用"列
  四档:可用 / Grace + 倒计时 / 待机 / 不可用
  round-12 F1 — emoji & inline-svg → Lucide,Bright v1 配色
-->
<template>
  <div class="flex items-center gap-2 min-w-0">
    <span class="relative flex shrink-0 w-6 h-6 rounded-md items-center justify-center"
      :class="badgeClass" :title="usability.hint">
      <CircleCheck v-if="kind === 'usable'" class="w-3.5 h-3.5" :stroke-width="2.25" />
      <TriangleAlert v-else-if="kind === 'grace'" class="w-3.5 h-3.5" :stroke-width="2.25" />
      <Hourglass v-else-if="kind === 'standby'" class="w-3.5 h-3.5" :stroke-width="2.25" />
      <CircleX v-else-if="kind === 'unusable'" class="w-3.5 h-3.5" :stroke-width="2.25" />
      <span v-else>—</span>
    </span>
    <div class="min-w-0">
      <div class="text-xs font-semibold leading-tight" :class="textClass">{{ usability.label }}</div>
      <div v-if="usability.hint" class="text-[10px] leading-tight opacity-70 truncate" :class="textClass">
        {{ usability.hint }}
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { CircleCheck, CircleX, TriangleAlert, Hourglass } from 'lucide-vue-next'
import { computeUsability } from '../composables/useStatus.js'

const props = defineProps({
  account: { type: Object, required: true },
})

const usability = computed(() => computeUsability(props.account))
const kind = computed(() => usability.value.kind)

const badgeClass = computed(() => {
  switch (kind.value) {
    case 'usable':
      return 'bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-600/20'
    case 'grace':
      return 'bg-orange-50 text-orange-700 ring-1 ring-inset ring-orange-600/25'
    case 'standby':
      return 'bg-amber-50 text-amber-800 ring-1 ring-inset ring-amber-600/25'
    case 'unusable':
      return 'bg-rose-50 text-rose-700 ring-1 ring-inset ring-rose-600/25'
    default:
      return 'bg-slate-100 text-slate-600 ring-1 ring-inset ring-slate-400/30'
  }
})
const textClass = computed(() => {
  switch (kind.value) {
    case 'usable': return 'text-emerald-700'
    case 'grace': return 'text-orange-700'
    case 'standby': return 'text-amber-800'
    case 'unusable': return 'text-rose-700'
    default: return 'text-slate-600'
  }
})
</script>
