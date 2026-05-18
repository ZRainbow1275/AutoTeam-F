<template>
  <div class="space-y-5">
    <!-- 标题 -->
    <div>
      <div class="text-[10px] uppercase tracking-[0.3em] text-indigo-700 mb-1">Pool Operations</div>
      <h2 class="text-2xl font-extrabold text-ink-950 tracking-tight">账号池操作</h2>
      <p class="text-sm text-ink-500 mt-1 max-w-2xl">
        集中执行轮转、检查、补满、添加、清理等会直接影响账号池状态的动作。
      </p>
    </div>

    <!-- F3 Master health banner(共享 App 级数据) -->
    <MasterHealthBanner
      :master-health="masterHealth"
      :min-grace-until="minGraceUntil"
      :loading="false"
      @refresh="$emit('reload-master-health', true)" />

    <div class="glass rounded-lg p-4 lg:p-5 space-y-4">
      <div class="flex items-start justify-between gap-3 flex-wrap">
        <div class="flex items-start gap-3 min-w-0">
          <div class="shrink-0 w-10 h-10 rounded-lg border flex items-center justify-center"
            :class="ipv6Tone.iconWrap">
            <Network class="w-4 h-4" :stroke-width="2" />
          </div>
          <div class="min-w-0">
            <div class="text-[10px] uppercase tracking-[0.3em] text-ink-500 font-semibold">
              IPv6 Proxy Pool
            </div>
            <h3 class="text-base font-bold tracking-tight mt-0.5" :class="ipv6Tone.titleClass">
              {{ ipv6Tone.title }}
            </h3>
            <p class="text-xs text-ink-500 mt-1 max-w-3xl leading-relaxed">
              {{ ipv6Subtitle }}
            </p>
          </div>
        </div>
        <div class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-semibold"
          :class="ipv6Tone.badgeClass">
          <span class="w-1.5 h-1.5 rounded-full" :class="ipv6Tone.dotClass"></span>
          {{ ipv6BadgeText }}
        </div>
      </div>

      <div class="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        <div v-for="item in ipv6Metrics" :key="item.label"
          class="rounded-lg border border-hairline bg-ink-50 px-3 py-2.5">
          <div class="text-[10px] uppercase tracking-widest text-ink-500 font-semibold">
            {{ item.label }}
          </div>
          <div class="mt-1 text-lg font-bold tabular text-ink-950 truncate">
            {{ item.value }}
          </div>
        </div>
      </div>

      <div v-if="ipv6Messages.length" class="space-y-2">
        <div v-for="message in ipv6Messages" :key="message.kind + ':' + message.text"
          class="flex items-start gap-2 rounded-lg border px-3 py-2 text-xs"
          :class="message.kind === 'error'
            ? 'bg-rose-50 border-rose-200 text-rose-800'
            : 'bg-amber-50 border-amber-200 text-amber-800'">
          <AlertTriangle class="w-3.5 h-3.5 mt-0.5 shrink-0" :stroke-width="2" />
          <span class="min-w-0 break-words">{{ message.text }}</span>
        </div>
      </div>

      <div v-if="ipv6Entries.length" class="rounded-lg border border-hairline overflow-hidden">
        <div class="px-3 py-2 bg-ink-50 border-b border-hairline flex items-center justify-between gap-3">
          <div class="text-[10px] uppercase tracking-widest text-ink-500 font-semibold">
            当前分配
          </div>
          <div v-if="ipv6HiddenEntries > 0" class="text-[11px] text-ink-500">
            另有 {{ ipv6HiddenEntries }} 个
          </div>
        </div>
        <div class="divide-y divide-hairline">
          <div v-for="entry in ipv6Entries" :key="entry.email"
            class="px-3 py-2.5 grid grid-cols-1 sm:grid-cols-[minmax(0,1fr)_auto_auto] gap-2 sm:items-center text-xs">
            <div class="min-w-0">
              <div class="font-mono text-ink-900 truncate">{{ entry.email }}</div>
              <div class="text-ink-500 font-mono truncate">{{ entry.ipv6_addr }}</div>
            </div>
            <div class="font-mono text-ink-600 tabular">:{{ entry.port }}</div>
            <div class="inline-flex items-center gap-1.5 justify-self-start sm:justify-self-end"
              :class="entry.healthy ? 'text-emerald-700' : 'text-rose-700'">
              <Activity class="w-3.5 h-3.5" :stroke-width="2" />
              {{ entry.healthy ? 'healthy' : 'unhealthy' }}
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="glass rounded-lg p-4 lg:p-5 space-y-4">
      <div class="flex items-start justify-between gap-3 flex-wrap">
        <div class="flex items-start gap-3 min-w-0">
          <div class="shrink-0 w-10 h-10 rounded-lg border flex items-center justify-center"
            :class="cliproxyTone.iconWrap">
            <Activity class="w-4 h-4" :stroke-width="2" />
          </div>
          <div class="min-w-0">
            <div class="text-[10px] uppercase tracking-[0.3em] text-ink-500 font-semibold">
              CLIProxyAPI Health
            </div>
            <h3 class="text-base font-bold tracking-tight mt-0.5" :class="cliproxyTone.titleClass">
              {{ cliproxyTone.title }}
            </h3>
            <p class="text-xs text-ink-500 mt-1 max-w-3xl leading-relaxed">
              {{ cliproxySubtitle }}
            </p>
          </div>
        </div>
        <div class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-semibold"
          :class="cliproxyTone.badgeClass">
          <span class="w-1.5 h-1.5 rounded-full" :class="cliproxyTone.dotClass"></span>
          {{ cliproxyBadgeText }}
        </div>
      </div>

      <div class="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        <div v-for="item in cliproxyMetrics" :key="item.label"
          class="rounded-lg border border-hairline bg-ink-50 px-3 py-2.5">
          <div class="text-[10px] uppercase tracking-widest text-ink-500 font-semibold">
            {{ item.label }}
          </div>
          <div class="mt-1 text-lg font-bold tabular text-ink-950 truncate">
            {{ item.value }}
          </div>
        </div>
      </div>

      <div v-if="cliproxyMessage" class="flex items-start gap-2 rounded-lg border px-3 py-2 text-xs"
        :class="cliproxyHealth?.ok
          ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
          : 'bg-amber-50 border-amber-200 text-amber-800'">
        <AlertTriangle v-if="!cliproxyHealth?.ok" class="w-3.5 h-3.5 mt-0.5 shrink-0" :stroke-width="2" />
        <Activity v-else class="w-3.5 h-3.5 mt-0.5 shrink-0" :stroke-width="2" />
        <span class="min-w-0 break-words">{{ cliproxyMessage }}</span>
      </div>
    </div>

    <TaskPanel
      mode="pool"
      :running-task="runningTask"
      :admin-status="adminStatus"
      :master-health="masterHealth"
      :rotate-stream="rotateStream"
      @task-started="$emit('task-started')"
      @refresh="$emit('refresh')" />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import TaskPanel from './TaskPanel.vue'
import MasterHealthBanner from './MasterHealthBanner.vue'
import { Activity, AlertTriangle, Network } from 'lucide-vue-next'

const props = defineProps({
  runningTask: Object,
  adminStatus: Object,
  masterHealth: { type: Object, default: null },
  status: { type: Object, default: null },
  rotateStream: { type: Object, default: null },
})

defineEmits(['task-started', 'refresh', 'reload-master-health'])

const minGraceUntil = computed(() => {
  let min = null
  for (const acc of props.status?.accounts || []) {
    if (acc.status === 'degraded_grace' && typeof acc.grace_until === 'number') {
      if (min === null || acc.grace_until < min) min = acc.grace_until
    }
  }
  return min
})

const ipv6Pool = computed(() => props.status?.ipv6_pool || null)
const cliproxyHealth = computed(() => props.status?.cliproxy || null)
const ipv6Preflight = computed(() => ipv6Pool.value?.preflight || {})
const ipv6Entries = computed(() => {
  const entries = Array.isArray(ipv6Pool.value?.entries) ? ipv6Pool.value.entries : []
  return entries.slice(0, 4)
})
const ipv6HiddenEntries = computed(() => {
  const entries = Array.isArray(ipv6Pool.value?.entries) ? ipv6Pool.value.entries.length : 0
  return Math.max(0, entries - ipv6Entries.value.length)
})

const ipv6Tone = computed(() => {
  const pool = ipv6Pool.value
  if (!pool) {
    return {
      title: '未上报',
      titleClass: 'text-slate-700',
      iconWrap: 'bg-slate-50 text-slate-700 border-slate-200',
      badgeClass: 'bg-slate-50 text-slate-700 border-slate-200',
      dotClass: 'bg-slate-500',
    }
  }
  if (!pool.enabled && !pool.required) {
    return {
      title: '默认关闭',
      titleClass: 'text-slate-700',
      iconWrap: 'bg-slate-50 text-slate-700 border-slate-200',
      badgeClass: 'bg-slate-50 text-slate-700 border-slate-200',
      dotClass: 'bg-slate-500',
    }
  }
  if (!pool.ok) {
    return {
      title: '需要处理',
      titleClass: 'text-rose-700',
      iconWrap: 'bg-rose-50 text-rose-700 border-rose-200',
      badgeClass: 'bg-rose-50 text-rose-700 border-rose-200',
      dotClass: 'bg-rose-500',
    }
  }
  if (pool.count > 0) {
    return {
      title: '运行中',
      titleClass: 'text-emerald-700',
      iconWrap: 'bg-emerald-50 text-emerald-700 border-emerald-200',
      badgeClass: 'bg-emerald-50 text-emerald-700 border-emerald-200',
      dotClass: 'bg-emerald-500',
    }
  }
  return {
    title: '等待分配',
    titleClass: 'text-amber-700',
    iconWrap: 'bg-amber-50 text-amber-700 border-amber-200',
    badgeClass: 'bg-amber-50 text-amber-700 border-amber-200',
    dotClass: 'bg-amber-500',
  }
})

const ipv6Subtitle = computed(() => {
  const pool = ipv6Pool.value
  if (!pool) return '当前 `/api/status` 尚未返回 IPv6 pool 信息。'
  if (!pool.enabled && !pool.required) return '默认关闭，不启动本地代理进程，也不会改动本机网络配置。'
  if (pool.required && !pool.enabled) return 'Required 已开启，但 pool 未启用；账号网络访问不会静默回退。'
  if (!pool.ok) return pool.last_error || firstMessage(ipv6Preflight.value.errors) || '预检或代理健康检查未通过。'
  if (pool.count > 0) return `已为 ${pool.count} 个账号分配独立出口；状态会随账号池刷新自动更新。`
  return 'IPv6 pool 已启用，等待注册、登录或同步路径为账号分配出口。'
})

const ipv6BadgeText = computed(() => {
  const pool = ipv6Pool.value
  if (!pool) return 'unknown'
  if (!pool.enabled && !pool.required) return 'disabled'
  if (pool.required && !pool.ok) return 'required'
  return pool.ok ? 'ok' : 'check'
})

const ipv6Metrics = computed(() => {
  const pool = ipv6Pool.value || {}
  return [
    { label: '分配数', value: String(pool.count ?? 0) },
    { label: '异常', value: String(pool.unhealthy_count ?? 0) },
    { label: '过期', value: String(pool.expired_count ?? 0) },
    { label: '端口使用', value: formatUsage(pool) },
  ]
})

const ipv6Messages = computed(() => {
  const preflight = ipv6Preflight.value || {}
  const errors = Array.isArray(preflight.errors) ? preflight.errors : []
  const warnings = Array.isArray(preflight.warnings) ? preflight.warnings : []
  const messages = errors.map((text) => ({ kind: 'error', text: formatIpv6Message(text) }))
  if (ipv6Pool.value?.last_error) {
    messages.push({ kind: 'error', text: ipv6Pool.value.last_error })
  }
  return [
    ...messages,
    ...warnings.map((text) => ({ kind: 'warning', text: formatIpv6Message(text) })),
  ].slice(0, 4)
})

const cliproxyTone = computed(() => {
  const health = cliproxyHealth.value
  if (!health) {
    return {
      title: '未上报',
      titleClass: 'text-slate-700',
      iconWrap: 'bg-slate-50 text-slate-700 border-slate-200',
      badgeClass: 'bg-slate-50 text-slate-700 border-slate-200',
      dotClass: 'bg-slate-500',
    }
  }
  if (health.ok) {
    return {
      title: '可用',
      titleClass: 'text-emerald-700',
      iconWrap: 'bg-emerald-50 text-emerald-700 border-emerald-200',
      badgeClass: 'bg-emerald-50 text-emerald-700 border-emerald-200',
      dotClass: 'bg-emerald-500',
    }
  }
  return {
    title: '需要处理',
    titleClass: 'text-amber-700',
    iconWrap: 'bg-amber-50 text-amber-700 border-amber-200',
    badgeClass: 'bg-amber-50 text-amber-700 border-amber-200',
    dotClass: 'bg-amber-500',
  }
})

const cliproxyBadgeText = computed(() => {
  const health = cliproxyHealth.value
  if (!health) return 'unknown'
  if (health.cached) return health.ok ? 'cached ok' : 'cached check'
  return health.ok ? 'ok' : 'check'
})

const cliproxySubtitle = computed(() => {
  const health = cliproxyHealth.value
  if (!health) return '当前 `/api/status` 尚未返回 CLIProxyAPI 健康信息。'
  if (!health.management_api?.ok) return formatCliproxyReason(health.management_api?.reason || 'management_api_unavailable')
  if (!health.provider_auth?.ok) return formatCliproxyReason(health.provider_auth?.reason || 'provider_auth_unavailable')
  return `管理接口可读，Codex provider 有 ${health.provider_auth?.available ?? 0} 个可用认证候选。`
})

const cliproxyMetrics = computed(() => {
  const health = cliproxyHealth.value || {}
  const auth = health.provider_auth || {}
  const mgmt = health.management_api || {}
  return [
    { label: '认证总数', value: String(auth.total ?? 0) },
    { label: '可用', value: String(auth.available ?? 0) },
    { label: '禁用', value: String(auth.disabled ?? 0) },
    { label: '延迟', value: mgmt.latency_ms == null ? '-' : `${mgmt.latency_ms}ms` },
  ]
})

const cliproxyMessage = computed(() => {
  const health = cliproxyHealth.value
  if (!health) return ''
  if (health.safe_read_only !== true) return '健康检查必须保持只读，请检查后端实现。'
  if (health.error) return health.error
  if (!health.management_api?.ok) return formatCliproxyReason(health.management_api?.reason)
  if (!health.provider_auth?.ok) return formatCliproxyReason(health.provider_auth?.reason)
  return '健康检查仅读取管理元数据，不会上传、删除或刷新远端认证文件。'
})

function firstMessage(values) {
  return Array.isArray(values) && values.length ? formatIpv6Message(values[0]) : ''
}

function formatIpv6Message(value) {
  const labels = {
    ipv6_required_but_disabled: 'Required 已开启，但 IPv6 pool 未启用。',
    missing_ipv6_prefix: '缺少 IPV6_PREFIX。',
    missing_ipv6_iface: '缺少 IPV6_IFACE。',
    missing_ip_command: '当前环境找不到 ip 命令。',
    invalid_port_range: 'IPv6 proxy 端口范围无效。',
    missing_sudo_command: '配置要求 sudo，但当前环境找不到 sudo。',
    status_unavailable: 'IPv6 pool 状态暂不可用。',
  }
  return labels[value] || String(value || '')
}

function formatCliproxyReason(value) {
  const labels = {
    missing_config: '缺少 CPA_URL 或 CPA_KEY，无法读取 CLIProxyAPI 管理接口。',
    request_failed: 'CLIProxyAPI 管理接口请求失败。',
    non_200: 'CLIProxyAPI 管理接口返回非 200 状态。',
    non_json: 'CLIProxyAPI 管理接口返回非 JSON 内容。',
    invalid_files_payload: 'CLIProxyAPI 管理接口响应缺少 files 列表。',
    management_api_unavailable: 'CLIProxyAPI 管理接口不可用。',
    no_provider_auth: 'CLIProxyAPI 中没有 Codex provider 认证文件。',
    provider_auth_all_unavailable: 'Codex provider 认证文件全部处于禁用、错误或不可用状态。',
    provider_auth_has_candidates: 'Codex provider 有可用认证候选。',
    health_check_exception: 'CLIProxyAPI 健康检查异常。',
  }
  return labels[value] || String(value || '状态暂不可用。')
}

function formatUsage(pool) {
  const count = Number(pool.used_ports ?? 0)
  const capacity = Number(pool.port_capacity ?? 0)
  if (!capacity) return `${count}/0`
  const pct = Number(pool.port_usage_ratio)
  const pctText = Number.isFinite(pct) ? ` · ${(pct * 100).toFixed(0)}%` : ''
  return `${count}/${capacity}${pctText}`
}
</script>
