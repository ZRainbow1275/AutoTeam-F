<template>
  <div v-if="status" class="space-y-6">
    <!-- F3 Master health banner -->
    <MasterHealthBanner
      :master-health="masterHealth"
      :min-grace-until="minGraceUntil"
      :loading="masterHealthBusy"
      @refresh="onRefreshMasterHealth" />

    <!-- F6 Pool health card -->
    <PoolHealthCard
      :accounts="status.accounts || []"
      :master-health="masterHealth"
      :min-grace-until="minGraceUntil" />

    <!-- 状态分布卡片(色彩区分明显) -->
    <div class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-5 gap-3">
      <div v-for="card in cards" :key="card.label"
        class="glass-soft rounded-xl p-3.5 lift-hover relative overflow-hidden">
        <div class="absolute top-0 right-0 w-16 h-16 rounded-full opacity-20 blur-xl pointer-events-none"
          :style="{ background: card.glow }"></div>
        <div class="text-[10px] uppercase tracking-widest text-gray-500 font-semibold flex items-center gap-1.5">
          <span class="w-1 h-1 rounded-full" :style="{ background: card.dot }"></span>
          {{ card.label }}
        </div>
        <div class="text-3xl font-extrabold mt-1.5 tabular leading-none" :class="card.color">{{ card.value }}</div>
      </div>
    </div>

    <!-- 账号表格 -->
    <div class="glass rounded-2xl overflow-hidden">
      <div class="px-5 py-4 border-b border-white/[0.04] flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h2 class="text-base font-bold text-white tracking-tight">账号列表</h2>
          <p class="text-[11px] text-gray-500 mt-0.5">{{ status.accounts?.length || 0 }} 个账号 · 实时配额来自 quota_cache</p>
        </div>
        <div class="flex items-center gap-2">
          <AtButton v-if="selectedEmails.length"
            variant="danger" size="sm" :loading="batchDeleting" :disabled="actionDisabled"
            confirm @click="batchDelete">
            <template #icon>
              <svg viewBox="0 0 16 16" class="w-3.5 h-3.5"><path fill="currentColor" d="M5 6v6h1V6H5zm2 0v6h1V6H7zm2 0v6h1V6H9zm2 0v6h1V6h-1zM4 4l1-2h6l1 2h2v1H2V4h2zm1 1v9a2 2 0 002 2h2a2 2 0 002-2V5H5z"/></svg>
            </template>
            {{ batchDeleting ? `批量删除 ${batchProgress}` : `批量删除 (${selectedEmails.length})` }}
          </AtButton>
          <AtButton v-if="selectedEmails.length" variant="ghost" size="sm" @click="clearSelection">
            取消选择
          </AtButton>
          <AtButton variant="secondary" size="sm" :loading="syncing" @click="syncAccounts">
            <template #icon>
              <svg viewBox="0 0 16 16" class="w-3.5 h-3.5"><path fill="none" stroke="currentColor" stroke-width="1.6" d="M3 8a5 5 0 018.5-3.5L13 3M13 8a5 5 0 01-8.5 3.5L3 13M13 3v3h-3M3 13v-3h3" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </template>
            {{ syncing ? '同步中…' : '同步账号' }}
          </AtButton>
        </div>
      </div>

      <div v-if="message" class="mx-5 mt-4 px-4 py-2.5 rounded-xl text-sm border animate-rise" :class="messageClass">
        {{ message }}
      </div>
      <div v-if="!adminReady"
        class="mx-5 mt-4 px-4 py-2.5 rounded-xl text-sm border bg-amber-500/10 text-amber-300 border-amber-500/30">
        请先在「设置」页完成管理员登录后,才能操作账号。
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-gray-500 text-left border-b border-white/[0.04] text-[10px] uppercase tracking-widest">
              <th class="px-3 py-3 font-semibold w-8">
                <input
                  type="checkbox"
                  :checked="allSelectableChecked"
                  :indeterminate.prop="someSelectableChecked"
                  @change="toggleSelectAll"
                  :disabled="!selectableEmails.length"
                  class="accent-indigo-500 cursor-pointer w-3.5 h-3.5"
                  title="全选/取消全选(主号除外)" />
              </th>
              <th class="px-3 py-3 font-semibold">#</th>
              <th class="px-4 py-3 font-semibold">邮箱</th>
              <th class="px-4 py-3 font-semibold">状态</th>
              <th class="px-4 py-3 font-semibold">实际可用</th>
              <th class="px-4 py-3 font-semibold text-right">5h 剩余</th>
              <th class="px-4 py-3 font-semibold text-right">周 剩余</th>
              <th class="px-4 py-3 font-semibold">5h 重置</th>
              <th class="px-4 py-3 font-semibold">周 重置</th>
              <th class="px-4 py-3 font-semibold text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(acc, i) in status.accounts" :key="acc.email"
              class="row-hoverable border-b border-white/[0.03] hover:bg-white/[0.02] group"
              :class="isSelected(acc.email) ? 'bg-indigo-500/[0.05]' : ''">
              <td class="px-3 py-3.5">
                <input
                  v-if="!acc.is_main_account"
                  type="checkbox"
                  :checked="isSelected(acc.email)"
                  @change="toggleSelect(acc.email)"
                  class="accent-indigo-500 cursor-pointer w-3.5 h-3.5" />
              </td>
              <td class="px-3 py-3.5 text-gray-600 font-mono text-xs">{{ String(i + 1).padStart(2, '0') }}</td>
              <td class="px-4 py-3.5">
                <div class="flex items-center gap-2">
                  <span v-if="acc.is_main_account"
                    class="text-[9px] uppercase tracking-widest font-bold px-1.5 py-0.5 rounded
                           bg-gradient-to-br from-indigo-500/30 to-violet-500/30 text-indigo-100 border border-indigo-400/30">
                    Master
                  </span>
                  <span class="font-mono text-[12px] text-gray-100">{{ acc.email }}</span>
                </div>
              </td>
              <td class="px-4 py-3.5">
                <StatusBadge :status="acc.status" :grace-until="acc.grace_until" />
              </td>
              <td class="px-4 py-3.5">
                <UsabilityCell :account="acc" />
              </td>
              <td class="px-4 py-3.5 text-right font-mono tabular text-[12px]" :class="pctColor(quota(acc, 'primary'))">
                {{ quotaPct(acc, 'primary') }}
              </td>
              <td class="px-4 py-3.5 text-right font-mono tabular text-[12px]" :class="pctColor(quota(acc, 'weekly'))">
                {{ quotaPct(acc, 'weekly') }}
              </td>
              <td class="px-4 py-3.5 text-gray-500 font-mono text-[11px]">{{ quotaReset(acc, 'primary') }}</td>
              <td class="px-4 py-3.5 text-gray-500 font-mono text-[11px]">{{ quotaReset(acc, 'weekly') }}</td>
              <td class="px-4 py-3.5 text-right">
                <div class="flex items-center justify-end gap-1.5 flex-wrap">
                  <span v-if="acc.status === 'personal' && !acc.auth_file"
                    class="inline-block px-1.5 py-0.5 rounded text-[9px] uppercase tracking-wide
                           bg-amber-500/10 text-amber-300 border border-amber-500/30"
                    title="未拿到 Codex auth_file,请点击补登录">
                    缺认证
                  </span>
                  <AtButton v-if="canLogin(acc)"
                    variant="primary" size="sm"
                    :loading="actionEmail === acc.email && actionType === 'login'"
                    :disabled="actionDisabled || (actionEmail === acc.email && actionType !== 'login')"
                    @click="loginAccount(acc.email)">
                    {{ loginLabel(acc) }}
                  </AtButton>
                  <AtButton v-if="!acc.is_main_account && acc.status === 'active'"
                    variant="secondary" size="sm"
                    :loading="actionEmail === acc.email && actionType === 'kick'"
                    :disabled="actionDisabled || (actionEmail === acc.email && actionType !== 'kick')"
                    @click="kickAccount(acc.email)">
                    移出
                  </AtButton>
                  <AtButton v-if="acc.status === 'active' || acc.status === 'personal' || acc.is_main_account"
                    variant="ghost" size="sm"
                    :disabled="actionEmail === acc.email"
                    @click="exportCodexAuth(acc.email)">
                    <template #icon>
                      <svg viewBox="0 0 16 16" class="w-3.5 h-3.5"><path fill="currentColor" d="M8 1l3 3h-2v5H7V4H5l3-3zM3 12v-2h1v2h8v-2h1v2a1 1 0 01-1 1H4a1 1 0 01-1-1z"/></svg>
                    </template>
                    导出
                  </AtButton>
                  <AtButton v-if="!acc.is_main_account"
                    variant="danger" size="sm" confirm
                    :loading="actionEmail === acc.email && actionType === 'delete'"
                    :disabled="actionDisabled || (actionEmail === acc.email && actionType !== 'delete')"
                    @click="removeAccount(acc.email)">
                    删除
                  </AtButton>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 注册失败明细 -->
      <div class="border-t border-white/[0.04] p-5 bg-black/10">
        <div class="flex items-center justify-between mb-3">
          <div>
            <h2 class="text-sm font-bold text-white tracking-tight">注册失败明细</h2>
            <div class="text-[11px] text-gray-500 mt-0.5">未能入池的注册尝试 (add-phone / duplicate / OAuth 失败 等)</div>
          </div>
          <AtButton variant="ghost" size="sm" :loading="failuresLoading" @click="loadFailures">
            刷新
          </AtButton>
        </div>
        <div v-if="failuresCounts && Object.keys(failuresCounts).length" class="flex flex-wrap gap-1.5 mb-3 text-[11px]">
          <span v-for="(cnt, cat) in failuresCounts" :key="cat"
            class="px-2 py-0.5 rounded-md border bg-white/[0.02] border-white/[0.06] text-gray-300">
            {{ cat }}: <span class="text-rose-300 font-mono ml-0.5 tabular">{{ cnt }}</span>
          </span>
        </div>
        <div class="overflow-x-auto rounded-xl border border-white/[0.04]">
          <table class="w-full text-sm">
            <thead class="text-[10px] uppercase tracking-widest text-gray-500 border-b border-white/[0.04] bg-white/[0.01]">
              <tr>
                <th class="text-left px-3 py-2 font-semibold">时间</th>
                <th class="text-left px-3 py-2 font-semibold">邮箱</th>
                <th class="text-left px-3 py-2 font-semibold">类别</th>
                <th class="text-left px-3 py-2 font-semibold">原因</th>
                <th class="text-left px-3 py-2 font-semibold">附加</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-white/[0.03] text-xs">
              <tr v-if="!failuresItems.length">
                <td class="px-3 py-4 text-gray-600 italic" colspan="5">暂无失败记录</td>
              </tr>
              <tr v-for="(f, idx) in failuresItems" :key="idx" class="hover:bg-white/[0.02]">
                <td class="px-3 py-2 text-gray-500 font-mono">{{ fmtTs(f.timestamp) }}</td>
                <td class="px-3 py-2 text-gray-300 font-mono">{{ f.email || '-' }}</td>
                <td class="px-3 py-2">
                  <span class="px-1.5 py-0.5 rounded-md border text-[10px] font-semibold uppercase tracking-wide"
                    :class="failureCategoryClass(f.category)">{{ f.category }}</span>
                </td>
                <td class="px-3 py-2 text-gray-400">{{ f.reason }}</td>
                <td class="px-3 py-2 text-gray-600 font-mono text-[10px]">{{ fmtFailureExtra(f) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Codex 认证导出弹窗 -->
      <div v-if="exportData"
        class="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4 animate-rise"
        @click.self="exportData = null">
        <div class="glass rounded-2xl w-full max-w-2xl max-h-[80vh] flex flex-col">
          <div class="px-5 py-4 border-b border-white/[0.04] flex items-center justify-between">
            <h3 class="text-white font-bold tracking-tight">Codex CLI 认证文件</h3>
            <button @click="exportData = null" class="text-gray-400 hover:text-white text-2xl leading-none transition">&times;</button>
          </div>
          <div class="p-5 space-y-3 overflow-y-auto flex-1">
            <div class="px-3 py-2.5 bg-amber-500/10 border border-amber-500/30 rounded-xl text-sm text-amber-200 space-y-2">
              <div class="font-semibold">使用步骤:</div>
              <ol class="list-decimal list-inside space-y-1 text-xs text-amber-300/90">
                <li>退出当前 Codex CLI 会话</li>
                <li>删除旧文件:<code class="bg-black/40 px-1 rounded">rm ~/.codex/auth.json</code></li>
                <li>将下方内容保存到 <code class="bg-black/40 px-1 rounded">~/.codex/auth.json</code>(Windows: <code class="bg-black/40 px-1 rounded">%APPDATA%\codex\auth.json</code>)</li>
                <li>重新启动 Codex CLI</li>
              </ol>
              <div class="text-xs text-amber-300/60">导出后 Codex CLI 直连 OpenAI,不走 CPA 代理,响应更快。</div>
            </div>
            <div class="relative">
              <pre class="bg-black/60 border border-white/[0.06] rounded-xl p-4 text-xs font-mono text-gray-300 overflow-x-auto whitespace-pre">{{ exportJson }}</pre>
              <AtButton :variant="copied ? 'primary' : 'secondary'" size="sm"
                class="absolute top-2 right-2" @click="copyExport">
                {{ copied ? '已复制' : '复制' }}
              </AtButton>
            </div>
          </div>
          <div class="px-5 py-4 border-t border-white/[0.04] flex justify-end gap-2">
            <AtButton variant="primary" @click="downloadExport">
              <template #icon>
                <svg viewBox="0 0 16 16" class="w-3.5 h-3.5"><path fill="currentColor" d="M8 11l-4-4h2.5V2h3v5H12L8 11zM3 13h10v1H3z"/></svg>
              </template>
              下载 auth.json
            </AtButton>
            <AtButton variant="secondary" @click="exportData = null">关闭</AtButton>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Loading skeleton -->
  <div v-else-if="loading" class="space-y-4">
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
      <div v-for="i in 4" :key="i" class="glass-soft rounded-xl p-4 h-20 shimmer-bg"></div>
    </div>
    <div class="glass-soft rounded-2xl h-64 shimmer-bg"></div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'
import StatusBadge from './StatusBadge.vue'
import UsabilityCell from './UsabilityCell.vue'
import MasterHealthBanner from './MasterHealthBanner.vue'
import PoolHealthCard from './PoolHealthCard.vue'
import AtButton from './AtButton.vue'
import {
  quotaRemainingPct as _qr,
  quotaPctText,
  formatQuotaReset,
  quotaPctColor,
} from '../composables/useStatus.js'
import { useToast } from '../composables/useToast.js'

const toast = useToast()

const props = defineProps({
  status: Object,
  loading: Boolean,
  runningTask: Object,
  adminStatus: { type: Object, default: null },
  masterHealth: { type: Object, default: null },
})
const emit = defineEmits(['refresh', 'reload-master-health'])

const actionEmail = ref('')
const actionType = ref('')
const syncing = ref(false)
const message = ref('')
const exportData = ref(null)
const copied = ref(false)
const messageClass = ref('')
const masterHealthBusy = ref(false)

// 批量删除选中态:按邮箱(小写)保存,便于跨刷新复用
const selectedSet = ref(new Set())
const batchDeleting = ref(false)
const batchProgress = ref('')

// 失败日志面板状态
const failuresItems = ref([])
const failuresCounts = ref({})
const failuresLoading = ref(false)

async function loadFailures() {
  failuresLoading.value = true
  try {
    const r = await api.getRegisterFailures(50)
    failuresItems.value = r.items || []
    failuresCounts.value = r.counts || {}
  } catch (e) {
    console.error('loadFailures', e)
  } finally {
    failuresLoading.value = false
  }
}

function fmtTs(ts) {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  const pad = n => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function failureCategoryClass(cat) {
  const map = {
    phone_blocked: 'bg-rose-500/10 text-rose-300 border-rose-500/30',
    duplicate_exhausted: 'bg-orange-500/10 text-orange-300 border-orange-500/30',
    register_failed: 'bg-amber-500/10 text-amber-300 border-amber-500/30',
    oauth_failed: 'bg-purple-500/10 text-purple-300 border-purple-500/30',
    kick_failed: 'bg-yellow-500/10 text-yellow-300 border-yellow-500/30',
    exception: 'bg-red-500/10 text-red-300 border-red-500/30',
    master_subscription_degraded: 'bg-orange-500/10 text-orange-300 border-orange-500/30',
  }
  return map[cat] || 'bg-gray-500/10 text-gray-400 border-gray-500/30'
}

function fmtFailureExtra(f) {
  const keys = ['step', 'register_attempts', 'duplicate_swaps', 'stage']
  const parts = []
  for (const k of keys) {
    if (f[k] !== undefined && f[k] !== null && f[k] !== '') parts.push(`${k}=${f[k]}`)
  }
  return parts.join(' ') || '-'
}

onMounted(loadFailures)
watch(() => props.runningTask, (cur, prev) => {
  if (prev && !cur) loadFailures()
})

const adminReady = computed(() => !!props.adminStatus?.configured)
const actionDisabled = computed(() => !!props.runningTask || !adminReady.value)

const selectableEmails = computed(() =>
  (props.status?.accounts || []).filter(a => !a.is_main_account).map(a => a.email)
)
const selectedEmails = computed(() =>
  selectableEmails.value.filter(e => selectedSet.value.has(e.toLowerCase()))
)
const allSelectableChecked = computed(() =>
  selectableEmails.value.length > 0 && selectedEmails.value.length === selectableEmails.value.length
)
const someSelectableChecked = computed(() =>
  selectedEmails.value.length > 0 && selectedEmails.value.length < selectableEmails.value.length
)

function isSelected(email) {
  return selectedSet.value.has((email || '').toLowerCase())
}
function toggleSelect(email) {
  const key = (email || '').toLowerCase()
  const next = new Set(selectedSet.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  selectedSet.value = next
}
function toggleSelectAll() {
  if (allSelectableChecked.value) selectedSet.value = new Set()
  else selectedSet.value = new Set(selectableEmails.value.map(e => e.toLowerCase()))
}
function clearSelection() { selectedSet.value = new Set() }

// Round 9 — 派生 minGraceUntil:从所有 GRACE 子号取最早到期者,banner/PoolHealthCard 共用
const minGraceUntil = computed(() => {
  let min = null
  for (const acc of props.status?.accounts || []) {
    if (acc.status === 'degraded_grace' && typeof acc.grace_until === 'number') {
      if (min === null || acc.grace_until < min) min = acc.grace_until
    }
  }
  return min
})

const cards = computed(() => {
  if (!props.status) return []
  const s = props.status.summary || {}
  const dg = s.degraded_grace ?? (props.status.accounts || []).filter(a => a.status === 'degraded_grace').length
  return [
    { label: 'Active', value: s.active || 0, color: 'text-emerald-300', dot: 'rgb(52, 211, 153)', glow: 'radial-gradient(circle, rgba(52, 211, 153, 0.45), transparent 60%)' },
    { label: 'Standby', value: s.standby || 0, color: 'text-amber-300', dot: 'rgb(251, 191, 36)', glow: 'radial-gradient(circle, rgba(251, 191, 36, 0.40), transparent 60%)' },
    { label: 'Grace', value: dg, color: 'text-orange-300', dot: 'rgb(251, 146, 60)', glow: 'radial-gradient(circle, rgba(251, 146, 60, 0.40), transparent 60%)' },
    { label: 'Personal', value: s.personal || 0, color: 'text-violet-300', dot: 'rgb(167, 139, 250)', glow: 'radial-gradient(circle, rgba(167, 139, 250, 0.40), transparent 60%)' },
    { label: 'Total', value: s.total || 0, color: 'text-white', dot: 'rgb(99, 102, 241)', glow: 'radial-gradient(circle, rgba(99, 102, 241, 0.45), transparent 60%)' },
  ]
})

function quota(acc, type) {
  const qi = props.status?.quota_cache?.[acc.email] || acc.last_quota
  if (!qi) return null
  return _qr(qi, type)
}
function quotaPct(acc, type) {
  const qi = props.status?.quota_cache?.[acc.email] || acc.last_quota
  return quotaPctText(qi, type)
}
function quotaReset(acc, type) {
  const qi = props.status?.quota_cache?.[acc.email] || acc.last_quota
  return formatQuotaReset(qi, type)
}
function pctColor(remain) { return quotaPctColor(remain) }

const exportJson = computed(() => {
  if (!exportData.value) return ''
  return JSON.stringify(exportData.value.codex_auth, null, 2)
})

async function exportCodexAuth(email) {
  try {
    exportData.value = await api.getCodexAuth(email)
    copied.value = false
  } catch (e) {
    toast.error('导出失败', e.message)
  }
}

async function copyExport() {
  try {
    await navigator.clipboard.writeText(exportJson.value)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = exportJson.value
    ta.style.position = 'fixed'; ta.style.opacity = '0'
    document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta)
  }
  copied.value = true
  setTimeout(() => { copied.value = false }, 2400)
}

function downloadExport() {
  const blob = new Blob([exportJson.value], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = 'auth.json'; a.click()
  URL.revokeObjectURL(url)
}

async function syncAccounts() {
  syncing.value = true
  try {
    const result = await api.postSyncAccounts()
    toast.success('同步完成', result.message || '')
    emit('refresh')
  } catch (e) {
    toast.error('同步失败', e.message)
  } finally {
    syncing.value = false
  }
}

function canLogin(acc) {
  if (acc.is_main_account) return false
  if (acc.status === 'active') return false
  if (acc.status === 'personal' && acc.auth_file) return false
  if (acc.status === 'degraded_grace') return false // grace 期内仍可用,无需补登录
  return true
}
function loginLabel(acc) {
  if (acc.status === 'personal' || acc.status === 'auth_invalid' || acc.status === 'orphan') return '补登录'
  return '登录'
}

async function loginAccount(email) {
  if (actionDisabled.value) return
  actionEmail.value = email; actionType.value = 'login'
  try {
    const result = await api.loginAccount(email)
    toast.success('登录任务已提交', `${email} · ${result.task_id}`)
    emit('refresh')
  } catch (e) {
    toast.error('提交登录任务失败', e.message)
  } finally {
    actionEmail.value = ''; actionType.value = ''
  }
}

async function kickAccount(email) {
  if (actionDisabled.value) return
  const ok = window.confirm(`确认将 ${email} 移出 Team?\n账号会变为 standby 状态,额度恢复后可重新复用。`)
  if (!ok) return
  actionEmail.value = email; actionType.value = 'kick'
  try {
    const result = await api.kickAccount(email)
    toast.success('已移出 Team', result.message || email)
    emit('refresh')
  } catch (e) {
    toast.error('移出失败', e.message)
  } finally {
    actionEmail.value = ''; actionType.value = ''
  }
}

async function removeAccount(email) {
  if (actionDisabled.value) return
  // 二次确认已在 AtButton(confirm) 内做了第一次,这里再用 native confirm 提示破坏性操作细节
  const ok = window.confirm(`确认删除账号 ${email}?\n这会清理本地记录、CPA、Team/Invite 和 CloudMail。`)
  if (!ok) return
  actionEmail.value = email; actionType.value = 'delete'
  try {
    const result = await api.deleteAccount(email)
    toast.success('账号已删除', result.message || email)
    emit('refresh')
  } catch (e) {
    toast.error('删除失败', e.message)
  } finally {
    actionEmail.value = ''; actionType.value = ''
  }
}

async function batchDelete() {
  if (actionDisabled.value || batchDeleting.value) return
  const emails = selectedEmails.value
  if (!emails.length) return
  const preview = emails.slice(0, 8).join('\n')
  const more = emails.length > 8 ? `\n…还有 ${emails.length - 8} 个` : ''
  const ok = window.confirm(
    `确认批量删除以下 ${emails.length} 个账号?这会清理本地记录、CPA、Team/Invite 和 CloudMail。\n\n${preview}${more}`
  )
  if (!ok) return
  batchDeleting.value = true; batchProgress.value = `0/${emails.length}`
  try {
    const r = await api.deleteAccountsBatch(emails, true)
    const s = r?.summary || {}
    const failed = (r?.results || []).filter(x => !x.ok)
    if (failed.length === 0) {
      toast.success('批量删除完成', `成功 ${s.ok}/${s.total}`)
    } else {
      const head = failed.slice(0, 3).map(x => `${x.email}: ${x.error}`).join('; ')
      toast.warn('批量删除部分失败', `成功 ${s.ok}/${s.total}; ${head}${failed.length > 3 ? ' …' : ''}`)
    }
    clearSelection()
    emit('refresh')
  } catch (e) {
    toast.error('批量删除失败', e.message)
  } finally {
    batchDeleting.value = false; batchProgress.value = ''
  }
}

async function onRefreshMasterHealth() {
  masterHealthBusy.value = true
  try {
    emit('reload-master-health', true)
    toast.info('已请求重测', '走 force_refresh,绕过 5min cache')
  } finally {
    setTimeout(() => { masterHealthBusy.value = false }, 1200)
  }
}
</script>
