<!--
  ToastHost — toast 容器,挂在 App 顶层
-->
<template>
  <div class="fixed top-3 right-3 z-[100] space-y-2 max-w-sm pointer-events-none">
    <transition-group tag="div" class="space-y-2">
      <div v-for="t in state.items" :key="t.id"
        class="pointer-events-auto rounded-xl border backdrop-blur-md shadow-2xl px-4 py-3 flex items-start gap-3"
        :class="[toneClass(t.tone), t.leaving ? 'animate-toast-out' : 'animate-toast-in']"
        @click="dismiss(t.id)">
        <span class="shrink-0 mt-0.5">
          <svg v-if="t.tone === 'success'" viewBox="0 0 16 16" class="w-4 h-4 text-emerald-300"><path fill="currentColor" d="M6.5 11.4L3.6 8.5l1.1-1.1 1.8 1.8L11.3 4l1.1 1.1z"/></svg>
          <svg v-else-if="t.tone === 'error'" viewBox="0 0 16 16" class="w-4 h-4 text-rose-300"><path fill="currentColor" d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          <svg v-else-if="t.tone === 'warning'" viewBox="0 0 16 16" class="w-4 h-4 text-amber-300"><path fill="currentColor" d="M8 1.5l7 12.5H1L8 1.5z"/></svg>
          <svg v-else viewBox="0 0 16 16" class="w-4 h-4 text-blue-300"><circle cx="8" cy="8" r="6" fill="currentColor" opacity="0.4"/><circle cx="8" cy="8" r="3" fill="currentColor"/></svg>
        </span>
        <div class="flex-1 min-w-0">
          <div class="text-sm font-semibold leading-tight" :class="titleColor(t.tone)">{{ t.text }}</div>
          <div v-if="t.detail" class="text-xs mt-1 opacity-70 break-all" :class="titleColor(t.tone)">{{ t.detail }}</div>
        </div>
      </div>
    </transition-group>
  </div>
</template>

<script setup>
import { useToast } from '../composables/useToast.js'

const { state, dismiss } = useToast()

function toneClass(tone) {
  return {
    success: 'bg-emerald-900/40 border-emerald-500/30',
    error: 'bg-rose-900/40 border-rose-500/30',
    warning: 'bg-amber-900/40 border-amber-500/30',
    info: 'bg-slate-900/60 border-slate-500/30',
  }[tone] || 'bg-slate-900/60 border-slate-500/30'
}
function titleColor(tone) {
  return {
    success: 'text-emerald-100',
    error: 'text-rose-100',
    warning: 'text-amber-100',
    info: 'text-slate-100',
  }[tone] || 'text-slate-100'
}
</script>
