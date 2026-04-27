<!--
  AtButton — 全站统一三档按钮 (F4)
  variant: primary | secondary | danger | ghost
  size:    sm | md
  disabled / loading 态完整,icon 槽位
-->
<template>
  <button
    :type="type"
    :disabled="disabled || loading"
    @click="onClick"
    :class="classes"
    class="relative inline-flex items-center justify-center gap-2 font-medium rounded-xl
           lift-hover focus-ring select-none whitespace-nowrap
           disabled:cursor-not-allowed disabled:opacity-50">
    <span v-if="loading"
      class="inline-block w-3.5 h-3.5 rounded-full border-2 border-current border-t-transparent animate-spin"
      aria-hidden="true"></span>
    <slot name="icon" v-else></slot>
    <span :class="loading ? 'opacity-70' : ''" class="leading-none">
      <slot />
    </span>
  </button>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  variant: { type: String, default: 'secondary' }, // primary | secondary | danger | ghost
  size: { type: String, default: 'md' }, // sm | md
  type: { type: String, default: 'button' },
  disabled: Boolean,
  loading: Boolean,
  // danger 二次确认 — 第一次点击进入 confirming 状态,2.4s 内再点才真触发
  confirm: Boolean,
  confirmHint: { type: String, default: '再点一次确认' },
})
const emit = defineEmits(['click'])

const confirming = ref(false)
let confirmTimer = null

function onClick(e) {
  if (props.disabled || props.loading) return
  if (props.variant === 'danger' && props.confirm && !confirming.value) {
    confirming.value = true
    clearTimeout(confirmTimer)
    confirmTimer = setTimeout(() => {
      confirming.value = false
    }, 2400)
    return
  }
  confirming.value = false
  clearTimeout(confirmTimer)
  emit('click', e)
}

const sizeClasses = {
  sm: 'h-7 px-2.5 text-xs',
  md: 'h-9 px-3.5 text-sm',
}

const variantClasses = computed(() => {
  if (props.variant === 'primary') {
    return [
      'text-white border border-blue-400/30',
      'bg-gradient-to-br from-blue-500 via-indigo-500 to-violet-500',
      'shadow-glow-blue hover:shadow-[0_12px_32px_-10px_rgba(99,102,241,0.6)]',
      'hover:from-blue-400 hover:via-indigo-400 hover:to-violet-400',
      'active:from-blue-600 active:via-indigo-600 active:to-violet-600',
    ].join(' ')
  }
  if (props.variant === 'danger') {
    if (confirming.value) {
      return [
        'text-white border border-rose-400/40',
        'bg-gradient-to-br from-rose-600 via-red-600 to-rose-700',
        'shadow-glow-rose ring-2 ring-rose-400/40',
      ].join(' ')
    }
    return [
      'text-rose-300 border border-rose-500/30',
      'bg-rose-500/[0.08] hover:bg-rose-500/15',
      'hover:text-rose-200 hover:border-rose-400/50',
    ].join(' ')
  }
  if (props.variant === 'ghost') {
    return [
      'text-gray-400 border border-transparent',
      'hover:bg-white/[0.04] hover:text-white',
    ].join(' ')
  }
  // secondary
  return [
    'text-gray-300 border border-white/10 bg-white/[0.03]',
    'hover:bg-white/[0.07] hover:text-white hover:border-white/20',
    'active:bg-white/[0.10]',
  ].join(' ')
})

const classes = computed(() => [
  sizeClasses[props.size] || sizeClasses.md,
  variantClasses.value,
])

defineExpose({ confirming })
</script>
