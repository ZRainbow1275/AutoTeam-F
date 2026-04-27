<!--
  MasterHealthBanner — F3 母号订阅健康度横幅,4 状态视觉
  spec:master-subscription-health.md v1.1 §6 (UI banner) + §13 (endpoint 守恒)

  Props:
    masterHealth: {healthy: bool, reason: string, evidence: object}
    minGraceUntil:  Optional<number>  集中显示"最近一个子号 grace 到期" 的 epoch
                                       从父级 status.accounts 派生最早 grace_until
  Emits:
    refresh: 立即重测按钮触发
-->
<template>
  <div v-if="visible" class="relative overflow-hidden rounded-2xl border lift-hover"
    :class="[tone.border, 'shadow-2xl']" :style="containerStyle" role="alert">
    <!-- 背景装饰 -->
    <div class="absolute inset-0 pointer-events-none opacity-60" :style="meshStyle"></div>
    <!-- 角标 -->
    <div class="absolute top-0 right-0 px-3 py-1 text-[10px] font-mono uppercase tracking-widest rounded-bl-xl"
      :class="[tone.tagBg, tone.tagText]">
      {{ tone.tag }}
    </div>

    <div class="relative px-5 py-4">
      <div class="flex items-start gap-4">
        <!-- 图标 -->
        <div class="relative shrink-0 w-11 h-11 rounded-xl flex items-center justify-center"
          :class="tone.iconWrap">
          <svg v-if="severity === 'critical'" viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 9v4M12 17h.01M5.07 19h13.86c1.54 0 2.5-1.67 1.73-3L13.73 4a2 2 0 00-3.46 0L3.34 16c-.77 1.33.19 3 1.73 3z" stroke-linejoin="round" stroke-linecap="round"/>
          </svg>
          <svg v-else-if="severity === 'warning'" viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="9"/>
            <path d="M12 7v6M12 16h.01" stroke-linecap="round"/>
          </svg>
          <svg v-else-if="severity === 'info'" viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="9"/>
            <path d="M11 11h1v5h1M12 7.5h.01" stroke-linecap="round"/>
          </svg>
          <svg v-else viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M5 13l4 4L19 7" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>

          <span v-if="severity === 'critical'" class="absolute -top-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-rose-400">
            <span class="absolute inset-0 rounded-full bg-rose-400 animate-ping opacity-75"></span>
          </span>
        </div>

        <!-- 文案 -->
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-3 flex-wrap">
            <h3 class="text-base font-bold" :class="tone.title">{{ title }}</h3>
            <span v-if="graceCountdown" class="font-mono text-xs px-2 py-0.5 rounded-md bg-black/30 border border-white/10"
              :class="graceCountdownColor">
              grace · {{ graceCountdown }}
            </span>
          </div>
          <p class="text-sm mt-1 leading-relaxed" :class="tone.body">{{ description }}</p>
          <div v-if="evidenceLine" class="text-[11px] font-mono mt-2 break-all opacity-70" :class="tone.body">
            {{ evidenceLine }}
          </div>
        </div>

        <!-- 操作 -->
        <div class="shrink-0 flex flex-col items-end gap-2">
          <button
            @click="$emit('refresh')"
            :disabled="loading"
            class="h-8 px-3 rounded-lg text-xs font-semibold border transition lift-hover focus-ring
                   disabled:opacity-50 disabled:cursor-not-allowed"
            :class="tone.btn">
            <span v-if="loading" class="inline-block w-3 h-3 mr-1.5 rounded-full border-2 border-current border-t-transparent animate-spin align-[-2px]"></span>
            {{ loading ? '检测中…' : '立即重测' }}
          </button>
          <span v-if="lastProbed" class="text-[10px] font-mono opacity-50" :class="tone.body">
            上次探测:{{ lastProbed }}{{ cached ? ' · cache' : '' }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { formatGraceRemain, graceUrgencyClass } from '../composables/useStatus.js'

const props = defineProps({
  masterHealth: { type: Object, default: null },
  minGraceUntil: { type: [Number, null], default: null },
  loading: Boolean,
})
defineEmits(['refresh'])

// reason → severity 映射 (F3 PRD)
//   subscription_cancelled (with grace) → warning (yellow → orange,degraded)
//   subscription_cancelled (no grace)   → critical (red)
//   workspace_missing / role_not_owner / auth_invalid → critical
//   network_error                       → info (gray, stale data)
//   active                              → ok (green,微提示;visible=false)
const severity = computed(() => {
  const m = props.masterHealth
  if (!m) return 'hidden'
  if (m.healthy === true || m.reason === 'active') return 'hidden'
  const r = m.reason
  if (r === 'subscription_cancelled') {
    // 有 grace 期 → degraded;否则 → cancelled critical
    if (props.minGraceUntil && props.minGraceUntil * 1000 > Date.now()) return 'warning'
    return 'critical'
  }
  if (r === 'workspace_missing' || r === 'role_not_owner' || r === 'auth_invalid') {
    return 'critical'
  }
  if (r === 'network_error') return 'info'
  return 'critical'
})

const visible = computed(() => severity.value !== 'hidden')

const title = computed(() => {
  const r = props.masterHealth?.reason
  switch (severity.value) {
    case 'warning':
      return '母号订阅 grace 期 · 期限内仍可使用'
    case 'critical':
      if (r === 'subscription_cancelled') return '母号订阅已 cancel · 请续费或切换母号'
      if (r === 'workspace_missing') return 'Workspace 不存在 · account_id 漂移'
      if (r === 'role_not_owner') return '权限异常 · 当前角色非 owner/admin'
      if (r === 'auth_invalid') return '主号 session 失效 · 请重新登录'
      return '母号探针拒绝'
    case 'info':
      return '母号探针失败 · 数据可能 stale'
    default:
      return ''
  }
})

const description = computed(() => {
  const r = props.masterHealth?.reason
  switch (severity.value) {
    case 'warning':
      return 'master 母号已 cancel_at_period_end,grace 期内子号 wham 仍 200 plan=team。fill 池不增量,但用户可继续消耗已有 quota。倒计时见右上角。'
    case 'critical':
      if (r === 'subscription_cancelled') return 'eligible_for_auto_reactivation=true,新 invite 必拿 plan_type=free。请续订或更换母号,fill-personal 入口已 503 拒绝。'
      if (r === 'workspace_missing') return '/backend-api/accounts items[] 中找不到目标 workspace,可能是 master 已切换。请重新核对 admin_state。'
      if (r === 'role_not_owner') return '当前用户角色不是 owner/admin,无法管理 workspace。请用正确账号重新登录。'
      if (r === 'auth_invalid') return 'master session 401/403,请到「设置」页清除登录态后重新导入 session_token。'
      return '请到「设置」页处理。'
    case 'info':
      return '/backend-api/accounts 网络抖动或 5xx,本次未拿到最新订阅状态。建议稍后重测。'
    default:
      return ''
  }
})

const evidenceLine = computed(() => {
  const ev = props.masterHealth?.evidence
  if (!ev) return ''
  const parts = []
  const accId = ev.account_id || ev.raw_account_item?.id
  if (accId) parts.push(`account_id=${accId}`)
  if (ev.current_user_role) parts.push(`role=${ev.current_user_role}`)
  if (ev.http_status) parts.push(`http=${ev.http_status}`)
  if (ev.detail) parts.push(`detail=${ev.detail}`)
  return parts.join(' · ')
})

const lastProbed = computed(() => {
  const ts = props.masterHealth?.evidence?.probed_at
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
})

const cached = computed(() => props.masterHealth?.evidence?.cache_hit === true)

const graceCountdown = computed(() => {
  if (severity.value !== 'warning') return ''
  return formatGraceRemain(props.minGraceUntil) || ''
})
const graceCountdownColor = computed(() => graceUrgencyClass(props.minGraceUntil))

// 配色三档
const tone = computed(() => {
  if (severity.value === 'warning') {
    return {
      tag: 'GRACE',
      tagBg: 'bg-amber-500/30',
      tagText: 'text-amber-100',
      iconWrap: 'bg-amber-500/15 text-amber-300 border border-amber-400/30',
      title: 'text-amber-100',
      body: 'text-amber-100/80',
      border: 'border-amber-500/40',
      btn: 'bg-amber-500/15 hover:bg-amber-500/25 text-amber-100 border-amber-400/40',
    }
  }
  if (severity.value === 'info') {
    return {
      tag: 'STALE',
      tagBg: 'bg-slate-500/30',
      tagText: 'text-slate-100',
      iconWrap: 'bg-slate-500/15 text-slate-300 border border-slate-400/30',
      title: 'text-slate-100',
      body: 'text-slate-200/80',
      border: 'border-slate-500/30',
      btn: 'bg-slate-500/15 hover:bg-slate-500/25 text-slate-100 border-slate-400/30',
    }
  }
  return {
    tag: 'ALERT',
    tagBg: 'bg-rose-500/30',
    tagText: 'text-rose-100',
    iconWrap: 'bg-rose-500/15 text-rose-300 border border-rose-400/30',
    title: 'text-rose-100',
    body: 'text-rose-100/85',
    border: 'border-rose-500/40',
    btn: 'bg-rose-500/15 hover:bg-rose-500/25 text-rose-100 border-rose-400/40',
  }
})

const containerStyle = computed(() => {
  const bg = {
    warning: 'linear-gradient(135deg, rgba(120, 53, 15, 0.45) 0%, rgba(154, 52, 18, 0.35) 60%, rgba(159, 18, 57, 0.30) 100%)',
    critical: 'linear-gradient(135deg, rgba(127, 29, 29, 0.55) 0%, rgba(159, 18, 57, 0.42) 100%)',
    info: 'linear-gradient(135deg, rgba(30, 41, 59, 0.55) 0%, rgba(15, 23, 42, 0.45) 100%)',
  }
  return { background: bg[severity.value] || bg.critical }
})

const meshStyle = computed(() => {
  const m = {
    warning: 'radial-gradient(ellipse 60% 60% at 90% 10%, rgba(251, 146, 60, 0.18), transparent 60%), radial-gradient(ellipse 60% 80% at 10% 100%, rgba(251, 113, 133, 0.12), transparent 60%)',
    critical: 'radial-gradient(ellipse 70% 70% at 95% 0%, rgba(251, 113, 133, 0.20), transparent 60%), radial-gradient(ellipse 50% 70% at 0% 100%, rgba(220, 38, 38, 0.18), transparent 60%)',
    info: 'radial-gradient(ellipse 60% 60% at 100% 0%, rgba(148, 163, 184, 0.12), transparent 60%)',
  }
  return { background: m[severity.value] || m.critical }
})
</script>
