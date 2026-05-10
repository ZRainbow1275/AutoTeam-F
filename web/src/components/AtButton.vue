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
    // round-12 F1 — text-on-accent 绕过 §Compat 层 text-white 翻转,保留白前景
    return [
      'text-on-accent border border-indigo-500/30',
      'bg-gradient-to-br from-indigo-500 via-indigo-600 to-violet-600',
      'shadow-card hover:shadow-ring-accent',
      'hover:from-indigo-400 hover:via-indigo-500 hover:to-violet-500',
      'active:from-indigo-600 active:via-indigo-700 active:to-violet-700',
    ].join(' ')
  }
  if (props.variant === 'danger') {
    if (confirming.value) {
      return [
        'text-on-accent border border-rose-500/30',
        'bg-gradient-to-br from-rose-500 via-red-600 to-rose-700',
        'shadow-card ring-2 ring-rose-400/40',
      ].join(' ')
    }
    return [
      'text-rose-700 border border-rose-200',
      'bg-rose-50 hover:bg-rose-100',
      'hover:text-rose-800 hover:border-rose-300',
    ].join(' ')
  }
  if (props.variant === 'ghost') {
    return [
      'text-ink-600 border border-transparent',
      'hover:bg-ink-100 hover:text-ink-950',
    ].join(' ')
  }
  // secondary
  return [
    'text-ink-700 border border-hairline bg-surface',
    'hover:bg-ink-100 hover:text-ink-950 hover:border-hairline-strong',
    'active:bg-ink-200',
  ].join(' ')
})

const classes = computed(() => [
  sizeClasses[props.size] || sizeClasses.md,
  variantClasses.value,
])

defineExpose({ confirming })
</script>
