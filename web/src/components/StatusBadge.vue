<!--
  StatusBadge — 7+1 状态徽章 (F2)
  统一视觉:渐变背景 + 玻璃边框 + (可选)脉冲点
  GRACE 状态额外接受 grace_until,显示倒计时
-->
<template>
  <span
    class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold
           border backdrop-blur-sm select-none whitespace-nowrap tracking-wide"
    :class="[style.text, style.border, isGraceUrgent ? 'grace-urgent' : '']"
    :style="bgStyle">
    <span class="relative inline-flex">
      <span class="block w-1.5 h-1.5 rounded-full" :class="style.dot"></span>
      <span
        v-if="style.pulse"
        class="absolute inset-0 w-1.5 h-1.5 rounded-full animate-pulse-dot"
        :class="style.dot"
        aria-hidden="true"></span>
    </span>
    <span class="leading-none uppercase">{{ label }}</span>
    <span
      v-if="isGrace && remainText"
      class="ml-1 px-1.5 py-0.5 rounded-md text-[10px] font-mono normal-case tracking-normal"
      :class="urgencyClass"
      :title="`grace 截止 ${graceDate}`">
      {{ remainText }}
    </span>
  </span>
</template>

<script setup>
import { computed } from 'vue'
import {
  statusLabel,
  statusStyle,
  formatGraceRemain,
  formatGraceDate,
  graceUrgencyClass,
  graceRemainMs,
} from '../composables/useStatus.js'

const props = defineProps({
  status: { type: String, required: true },
  // 可选,GRACE 状态时使用
  graceUntil: { type: [Number, null], default: null },
})

const style = computed(() => statusStyle(props.status))
const label = computed(() => statusLabel(props.status))

const isGrace = computed(() => props.status === 'degraded_grace')
const remainText = computed(() => (isGrace.value ? formatGraceRemain(props.graceUntil) : ''))
const graceDate = computed(() => formatGraceDate(props.graceUntil))
const urgencyClass = computed(() => `${graceUrgencyClass(props.graceUntil)} bg-black/30`)
const isGraceUrgent = computed(() => {
  if (!isGrace.value) return false
  const ms = graceRemainMs(props.graceUntil)
  return ms !== null && ms < 7 * 24 * 60 * 60 * 1000 // < 7d 闪
})

// 用 inline-style 实现渐变背景,避免 tailwind purge 漏掉动态 class
const bgStyle = computed(() => {
  const map = {
    active: 'linear-gradient(135deg, rgba(52, 211, 153, 0.20) 0%, rgba(20, 184, 166, 0.12) 100%)',
    personal: 'linear-gradient(135deg, rgba(167, 139, 250, 0.22) 0%, rgba(217, 70, 239, 0.14) 100%)',
    standby: 'linear-gradient(135deg, rgba(251, 191, 36, 0.18) 0%, rgba(245, 158, 11, 0.10) 100%)',
    degraded_grace: 'linear-gradient(135deg, rgba(251, 191, 36, 0.25) 0%, rgba(249, 115, 22, 0.22) 50%, rgba(244, 63, 94, 0.20) 100%)',
    auth_invalid: 'linear-gradient(135deg, rgba(244, 63, 94, 0.18) 0%, rgba(239, 68, 68, 0.12) 100%)',
    pending: 'linear-gradient(135deg, rgba(148, 163, 184, 0.18) 0%, rgba(100, 116, 139, 0.10) 100%)',
    exhausted: 'linear-gradient(135deg, rgba(220, 38, 38, 0.22) 0%, rgba(127, 29, 29, 0.14) 100%)',
    orphan: 'linear-gradient(135deg, rgba(234, 179, 8, 0.18) 0%, rgba(202, 138, 4, 0.10) 100%)',
  }
  return { background: map[props.status] || map.pending }
})
</script>
