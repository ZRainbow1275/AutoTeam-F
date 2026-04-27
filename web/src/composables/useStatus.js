// 账号状态/可用性/时间格式化工具集中位
// spec 引用:account-state-machine.md v2.0 §2.1 / §3.2 / §5.3

export const STATUS_LABELS = {
  active: 'Active',
  exhausted: 'Used up',
  standby: 'Standby',
  pending: 'Pending',
  personal: 'Personal',
  auth_invalid: '认证失效',
  orphan: '孤立',
  degraded_grace: 'Grace',
}

// F2 — 状态视觉:每个状态唯一的渐变 + dot color + label
// 维度:base(背景), text(文字), border(边框), dot(脉冲点), gradient(badge 背景渐变)
export const STATUS_STYLES = {
  active: {
    base: 'bg-emerald-500/[0.08]',
    text: 'text-emerald-300',
    border: 'border-emerald-400/30',
    dot: 'bg-emerald-400',
    pulse: true,
    gradient: 'from-emerald-400/20 via-emerald-500/10 to-teal-500/15',
  },
  personal: {
    base: 'bg-violet-500/[0.08]',
    text: 'text-violet-300',
    border: 'border-violet-400/30',
    dot: 'bg-violet-400',
    pulse: false,
    gradient: 'from-violet-400/20 via-fuchsia-500/15 to-purple-500/15',
  },
  standby: {
    base: 'bg-amber-500/[0.08]',
    text: 'text-amber-300',
    border: 'border-amber-400/30',
    dot: 'bg-amber-400',
    pulse: false,
    gradient: 'from-amber-400/15 via-yellow-500/10 to-amber-600/15',
  },
  // GRACE — 橙→红渐变,grace_until 越接近 0 越红
  degraded_grace: {
    base: 'bg-orange-500/[0.10]',
    text: 'text-orange-200',
    border: 'border-orange-400/40',
    dot: 'bg-orange-400',
    pulse: true,
    gradient: 'from-amber-500/25 via-orange-500/20 to-rose-500/25',
  },
  auth_invalid: {
    base: 'bg-rose-500/[0.08]',
    text: 'text-rose-300',
    border: 'border-rose-400/30',
    dot: 'bg-rose-400',
    pulse: false,
    gradient: 'from-rose-500/15 via-red-500/10 to-rose-600/15',
  },
  pending: {
    base: 'bg-slate-500/[0.10]',
    text: 'text-slate-300',
    border: 'border-slate-400/25',
    dot: 'bg-slate-400',
    pulse: true,
    gradient: 'from-slate-500/15 via-zinc-500/10 to-slate-600/15',
  },
  exhausted: {
    base: 'bg-red-700/[0.12]',
    text: 'text-red-300',
    border: 'border-red-500/30',
    dot: 'bg-red-500',
    pulse: false,
    gradient: 'from-red-700/20 via-rose-700/15 to-red-800/20',
  },
  orphan: {
    base: 'bg-yellow-600/[0.10]',
    text: 'text-yellow-200',
    border: 'border-yellow-500/30',
    dot: 'bg-yellow-400',
    pulse: false,
    gradient: 'from-yellow-500/15 via-amber-500/10 to-yellow-600/15',
  },
}

export function statusLabel(s) {
  return STATUS_LABELS[s] || s || '-'
}
export function statusStyle(s) {
  return STATUS_STYLES[s] || STATUS_STYLES.pending
}

// F1 — "实际可用性" 派生:四档 ✅ ⚠️ 💤 ❌
// 输入:account 对象(含 status, last_quota, grace_until 等)
// 输出:{ kind: 'usable'|'grace'|'standby'|'unusable', label, hint, tone }
export function computeUsability(acc) {
  if (!acc) return { kind: 'unknown', label: '—', hint: '', tone: 'neutral' }
  const s = acc.status
  const q = acc.last_quota || {}
  const noQuota = q.primary_total === 0 || q.no_quota === true
  const hasError = q.error || q.status === 'auth_error'

  if (s === 'auth_invalid' || s === 'orphan') {
    return { kind: 'unusable', label: '不可用', hint: '认证失效 / 席位异常', tone: 'rose' }
  }
  if (hasError) {
    return { kind: 'unusable', label: '不可用', hint: '配额检测错误', tone: 'rose' }
  }
  if (s === 'degraded_grace') {
    const remain = formatGraceRemain(acc.grace_until)
    return {
      kind: 'grace',
      label: 'Grace',
      hint: remain ? `剩 ${remain}` : '母号已 cancel,grace 期内仍可用',
      tone: 'orange',
      remainMs: graceRemainMs(acc.grace_until),
    }
  }
  if (s === 'standby' || s === 'pending') {
    return { kind: 'standby', label: '待机', hint: s === 'pending' ? '注册中' : '等待 quota 恢复', tone: 'amber' }
  }
  if (s === 'exhausted') {
    return { kind: 'standby', label: '待机', hint: '已耗尽,等 5h 重置', tone: 'amber' }
  }
  if (s === 'personal') {
    if (noQuota) return { kind: 'unusable', label: '不可用', hint: '无配额', tone: 'rose' }
    return { kind: 'usable', label: '可用', hint: 'Personal free', tone: 'emerald' }
  }
  if (s === 'active') {
    if (noQuota) return { kind: 'unusable', label: '不可用', hint: '无配额', tone: 'rose' }
    const primaryRemain = 100 - (q.primary_pct || 0)
    if (primaryRemain <= 0) return { kind: 'standby', label: '待机', hint: '5h 配额耗尽', tone: 'amber' }
    return { kind: 'usable', label: '可用', hint: `5h 剩余 ${primaryRemain}%`, tone: 'emerald' }
  }
  return { kind: 'unknown', label: '—', hint: '', tone: 'neutral' }
}

// ── 时间工具 ──
export function nowSec() {
  return Date.now() / 1000
}

export function graceRemainMs(graceUntil) {
  if (!graceUntil || typeof graceUntil !== 'number') return null
  return Math.max(0, graceUntil * 1000 - Date.now())
}

export function formatGraceRemain(graceUntil) {
  const ms = graceRemainMs(graceUntil)
  if (ms === null) return ''
  if (ms <= 0) return '已到期'
  const totalMin = Math.floor(ms / 60000)
  const days = Math.floor(totalMin / (60 * 24))
  const hours = Math.floor((totalMin % (60 * 24)) / 60)
  const minutes = totalMin % 60
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

export function formatGraceDate(graceUntil) {
  if (!graceUntil) return ''
  const d = new Date(graceUntil * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// GRACE 倒计时颜色:< 24h 红 / < 7d 橙 / 其它 amber
export function graceUrgencyClass(graceUntil) {
  const ms = graceRemainMs(graceUntil)
  if (ms === null) return 'text-amber-300'
  const days = ms / (1000 * 60 * 60 * 24)
  if (days < 1) return 'text-rose-300'
  if (days < 7) return 'text-orange-300'
  return 'text-amber-300'
}

// 配额数值与颜色 (Dashboard 复用)
export function quotaRemainingPct(qi, type = 'primary') {
  if (!qi) return null
  const pct = type === 'primary' ? qi.primary_pct : qi.weekly_pct
  return 100 - (pct || 0)
}

export function quotaPctText(qi, type = 'primary') {
  if (!qi) return '-'
  if (type === 'primary' && (qi.primary_total === 0 || qi.no_quota === true)) return '无配额'
  const v = quotaRemainingPct(qi, type)
  return v === null ? '-' : `${v}%`
}

export function quotaPctColor(remain) {
  if (remain === null || remain === undefined) return 'text-gray-500'
  if (remain > 30) return 'text-emerald-300'
  if (remain > 0) return 'text-amber-300'
  return 'text-rose-300'
}

export function formatQuotaReset(qi, type = 'primary') {
  if (!qi) return '-'
  const ts = type === 'primary' ? qi.primary_resets_at : qi.weekly_resets_at
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
