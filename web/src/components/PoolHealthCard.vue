<!--
  PoolHealthCard — F6 Dashboard 顶部账号池健康度卡片
  - 4 数字 (可用 / Grace / 待机 / 不可用)
  - 占比环形图 (Donut)
  - master_health inline 状态
-->
<template>
  <div class="glass rounded-2xl p-5 lg:p-6 lift-hover relative overflow-hidden">
    <!-- 背景装饰 -->
    <div class="absolute -top-16 -right-16 w-64 h-64 rounded-full opacity-30 blur-3xl pointer-events-none"
      :style="{ background: 'radial-gradient(circle, rgba(99, 102, 241, 0.30), transparent 60%)' }"></div>

    <div class="relative flex flex-col lg:flex-row items-start lg:items-center gap-6">
      <!-- 左:donut + 中心总数 -->
      <div class="shrink-0">
        <HealthDonut
          :size="140"
          :thickness="14"
          :segments="segments"
          :center-value="counts.total"
          center-label="账号池"
          :center-hint="trendHint" />
      </div>

      <!-- 中:四档数字 -->
      <div class="flex-1 grid grid-cols-2 sm:grid-cols-4 gap-3 w-full">
        <div v-for="card in cards" :key="card.key"
          class="rounded-xl p-3 border bg-white/[0.02] hover:bg-white/[0.05] transition-all"
          :class="card.borderClass">
          <div class="flex items-center gap-1.5 text-[10px] uppercase tracking-widest font-semibold"
            :class="card.labelClass">
            <span class="w-1.5 h-1.5 rounded-full" :style="{ background: card.color }"></span>
            {{ card.label }}
          </div>
          <div class="text-3xl font-bold tabular mt-1" :class="card.valueClass">
            {{ card.value }}
          </div>
          <div class="text-[10px] mt-0.5 opacity-60" :class="card.valueClass">
            {{ percent(card.value) }}
          </div>
        </div>
      </div>

      <!-- 右:master health inline -->
      <div class="shrink-0 lg:max-w-xs w-full lg:w-auto">
        <div class="rounded-xl border p-3 flex items-start gap-3"
          :class="masterTone.border" :style="{ background: masterTone.bg }">
          <div class="shrink-0 w-9 h-9 rounded-lg flex items-center justify-center"
            :class="masterTone.iconWrap">
            <svg viewBox="0 0 16 16" class="w-4 h-4">
              <path :fill="masterTone.iconFill" d="M3 2.5h10v3a4.99 4.99 0 0 1-5 5 4.99 4.99 0 0 1-5-5v-3zM5 12.5h6v1.5H5z"/>
            </svg>
          </div>
          <div class="min-w-0 flex-1">
            <div class="text-[10px] uppercase tracking-widest font-semibold opacity-70" :class="masterTone.text">
              母号订阅
            </div>
            <div class="text-sm font-bold leading-tight mt-0.5" :class="masterTone.text">
              {{ masterTitle }}
            </div>
            <div class="text-[10px] mt-0.5 opacity-70 truncate" :class="masterTone.text">
              {{ masterSubtitle }}
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import HealthDonut from './HealthDonut.vue'
import { computeUsability, formatGraceRemain } from '../composables/useStatus.js'

const props = defineProps({
  accounts: { type: Array, default: () => [] },
  masterHealth: { type: Object, default: null },
  minGraceUntil: { type: [Number, null], default: null },
})

const counts = computed(() => {
  const c = { usable: 0, grace: 0, standby: 0, unusable: 0, total: 0 }
  for (const acc of props.accounts || []) {
    if (acc.is_main_account) continue
    const u = computeUsability(acc).kind
    if (u === 'usable') c.usable++
    else if (u === 'grace') c.grace++
    else if (u === 'standby') c.standby++
    else if (u === 'unusable') c.unusable++
    c.total++
  }
  return c
})

const cards = computed(() => [
  {
    key: 'usable', label: '可用', value: counts.value.usable,
    color: 'rgba(52, 211, 153, 1)',
    borderClass: 'border-emerald-500/20',
    labelClass: 'text-emerald-300',
    valueClass: 'text-emerald-200',
  },
  {
    key: 'grace', label: 'Grace', value: counts.value.grace,
    color: 'rgba(251, 146, 60, 1)',
    borderClass: 'border-orange-500/30',
    labelClass: 'text-orange-300',
    valueClass: 'text-orange-200',
  },
  {
    key: 'standby', label: '待机', value: counts.value.standby,
    color: 'rgba(251, 191, 36, 1)',
    borderClass: 'border-amber-500/20',
    labelClass: 'text-amber-300',
    valueClass: 'text-amber-200',
  },
  {
    key: 'unusable', label: '不可用', value: counts.value.unusable,
    color: 'rgba(244, 63, 94, 1)',
    borderClass: 'border-rose-500/20',
    labelClass: 'text-rose-300',
    valueClass: 'text-rose-200',
  },
])

const segments = computed(() =>
  cards.value.filter((c) => c.value > 0).map((c) => ({ key: c.key, value: c.value, color: c.color }))
)

const trendHint = computed(() => {
  const total = counts.value.total
  if (!total) return '无账号'
  const ratio = ((counts.value.usable / total) * 100).toFixed(0)
  return `${ratio}% 可用`
})

function percent(v) {
  if (!counts.value.total) return '0%'
  return `${((v / counts.value.total) * 100).toFixed(0)}%`
}

// 母号订阅 inline 状态
const masterTone = computed(() => {
  const m = props.masterHealth
  const r = m?.reason
  if (!m || m.healthy === true || r === 'active') {
    return {
      bg: 'linear-gradient(135deg, rgba(16, 185, 129, 0.10), rgba(20, 184, 166, 0.05))',
      border: 'border-emerald-500/20',
      iconWrap: 'bg-emerald-500/15',
      iconFill: 'rgb(110, 231, 183)',
      text: 'text-emerald-200',
    }
  }
  if (r === 'subscription_cancelled') {
    return {
      bg: 'linear-gradient(135deg, rgba(251, 146, 60, 0.14), rgba(244, 63, 94, 0.10))',
      border: 'border-orange-500/30',
      iconWrap: 'bg-orange-500/15',
      iconFill: 'rgb(253, 186, 116)',
      text: 'text-orange-200',
    }
  }
  if (r === 'network_error') {
    return {
      bg: 'linear-gradient(135deg, rgba(100, 116, 139, 0.14), rgba(71, 85, 105, 0.10))',
      border: 'border-slate-500/25',
      iconWrap: 'bg-slate-500/15',
      iconFill: 'rgb(203, 213, 225)',
      text: 'text-slate-200',
    }
  }
  return {
    bg: 'linear-gradient(135deg, rgba(244, 63, 94, 0.14), rgba(220, 38, 38, 0.10))',
    border: 'border-rose-500/30',
    iconWrap: 'bg-rose-500/15',
    iconFill: 'rgb(253, 164, 175)',
    text: 'text-rose-200',
  }
})

const masterTitle = computed(() => {
  const m = props.masterHealth
  if (!m) return '未探测'
  if (m.healthy === true || m.reason === 'active') return 'Healthy'
  if (m.reason === 'subscription_cancelled') {
    if (props.minGraceUntil && props.minGraceUntil * 1000 > Date.now()) return 'Grace 期内'
    return 'Cancelled'
  }
  if (m.reason === 'workspace_missing') return 'Workspace 漂移'
  if (m.reason === 'role_not_owner') return '权限异常'
  if (m.reason === 'auth_invalid') return 'Session 失效'
  if (m.reason === 'network_error') return '网络抖动'
  return m.reason || '异常'
})

const masterSubtitle = computed(() => {
  const m = props.masterHealth
  if (!m) return '尚未拉取 / 设置 → 探测'
  if (m.healthy === true || m.reason === 'active') return '订阅 active'
  if (m.reason === 'subscription_cancelled' && props.minGraceUntil) {
    const r = formatGraceRemain(props.minGraceUntil)
    return r ? `grace 剩 ${r}` : 'grace 已过期'
  }
  return m.evidence?.detail || ''
})
</script>
