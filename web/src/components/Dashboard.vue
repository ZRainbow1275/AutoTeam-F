<template>
  <div v-if="status">
    <!-- 统计卡片 -->
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-6">
      <div v-for="card in cards" :key="card.label"
        class="bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div class="text-sm text-gray-400">{{ card.label }}</div>
        <div class="text-3xl font-bold mt-1" :class="card.color">{{ card.value }}</div>
      </div>
    </div>

    <!-- 账号表格 -->
    <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
      <div class="px-4 py-3 border-b border-gray-800 flex items-center justify-between gap-3 flex-wrap">
        <h2 class="text-lg font-semibold text-white">账号列表</h2>
        <div class="flex items-center gap-2">
          <button
            v-if="selectedEmails.length"
            @click="batchDelete"
            :disabled="actionDisabled || batchDeleting"
            class="px-3 py-1.5 rounded-lg text-xs font-medium border transition"
            :class="actionDisabled || batchDeleting
              ? 'bg-gray-800 text-gray-500 border-gray-700 cursor-not-allowed'
              : 'bg-rose-600/10 text-rose-400 border-rose-500/30 hover:bg-rose-600/20'">
            {{ batchDeleting ? `批量删除中 ${batchProgress}` : `批量删除 (${selectedEmails.length})` }}
          </button>
          <button
            v-if="selectedEmails.length"
            @click="clearSelection"
            class="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-xs rounded-lg border border-gray-700 text-gray-400 hover:text-white transition">
            取消选择
          </button>
          <button @click="syncAccounts" :disabled="syncing"
            class="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-xs rounded-lg border border-gray-700 transition disabled:opacity-50 text-gray-400 hover:text-white">
            {{ syncing ? '同步中...' : '同步账号' }}
          </button>
        </div>
      </div>
      <div v-if="message" class="mx-4 mt-4 px-4 py-3 rounded-lg text-sm border" :class="messageClass">
        {{ message }}
      </div>
      <div v-if="!adminReady" class="mx-4 mt-4 px-4 py-3 rounded-lg text-sm border bg-amber-500/10 text-amber-300 border-amber-500/20">
        请先在「设置」页完成管理员登录后，才能操作账号。
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-gray-400 text-left border-b border-gray-800">
              <th class="px-3 py-3 font-medium w-8">
                <input
                  type="checkbox"
                  :checked="allSelectableChecked"
                  :indeterminate.prop="someSelectableChecked"
                  @change="toggleSelectAll"
                  :disabled="!selectableEmails.length"
                  class="accent-rose-500 cursor-pointer"
                  title="全选/取消全选(主号除外)" />
              </th>
              <th class="px-4 py-3 font-medium">#</th>
              <th class="px-4 py-3 font-medium">邮箱</th>
              <th class="px-4 py-3 font-medium">状态</th>
              <th class="px-4 py-3 font-medium text-right">5h 剩余</th>
              <th class="px-4 py-3 font-medium text-right">周 剩余</th>
              <th class="px-4 py-3 font-medium">5h 重置</th>
              <th class="px-4 py-3 font-medium">周 重置</th>
              <th class="px-4 py-3 font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(acc, i) in status.accounts" :key="acc.email"
              class="border-b border-gray-800/50 hover:bg-gray-800/30 transition"
              :class="isSelected(acc.email) ? 'bg-rose-500/5' : ''">
              <td class="px-3 py-3">
                <input
                  v-if="!acc.is_main_account"
                  type="checkbox"
                  :checked="isSelected(acc.email)"
                  @change="toggleSelect(acc.email)"
                  class="accent-rose-500 cursor-pointer" />
              </td>
              <td class="px-4 py-3 text-gray-500">{{ i + 1 }}</td>
              <td class="px-4 py-3 font-mono text-xs">{{ acc.email }}</td>
              <td class="px-4 py-3">
                <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium"
                  :class="statusClass(acc.status)">
                  <span class="w-1.5 h-1.5 rounded-full" :class="dotClass(acc.status)"></span>
                  {{ statusLabel(acc.status) }}
                </span>
              </td>
              <td class="px-4 py-3 text-right font-mono" :class="pctColor(quota(acc, 'primary'))">
                {{ quotaPct(acc, 'primary') }}
              </td>
              <td class="px-4 py-3 text-right font-mono" :class="pctColor(quota(acc, 'weekly'))">
                {{ quotaPct(acc, 'weekly') }}
              </td>
              <td class="px-4 py-3 text-gray-400 text-xs">{{ quotaReset(acc, 'primary') }}</td>
              <td class="px-4 py-3 text-gray-400 text-xs">{{ quotaReset(acc, 'weekly') }}</td>
              <td class="px-4 py-3 text-right space-x-2">
                <!-- 缺认证标识：personal 账号没有 auth_file → 在补登录按钮旁提示 -->
                <span
                  v-if="acc.status === 'personal' && !acc.auth_file"
                  class="inline-block px-2 py-0.5 mr-1 rounded text-[10px] bg-amber-500/10 text-amber-400 border border-amber-500/30"
                  title="未拿到 Codex auth_file，请点击补登录">
                  缺认证
                </span>
                <button
                  v-if="canLogin(acc)"
                  @click="loginAccount(acc.email)"
                  :disabled="actionDisabled || actionEmail === acc.email"
                  class="px-3 py-1.5 rounded-lg text-xs font-medium border transition"
                  :class="actionDisabled || actionEmail === acc.email
                    ? 'bg-gray-800 text-gray-500 border-gray-700 cursor-not-allowed'
                    : 'bg-blue-600/10 text-blue-400 border-blue-500/30 hover:bg-blue-600/20'">
                  {{ actionEmail === acc.email && actionType === 'login' ? '登录中...' : loginLabel(acc) }}
                </button>
                <button
                  v-if="!acc.is_main_account && acc.status === 'active'"
                  @click="kickAccount(acc.email)"
                  :disabled="actionDisabled || actionEmail === acc.email"
                  class="px-3 py-1.5 rounded-lg text-xs font-medium border transition"
                  :class="actionDisabled || actionEmail === acc.email
                    ? 'bg-gray-800 text-gray-500 border-gray-700 cursor-not-allowed'
                    : 'bg-amber-600/10 text-amber-400 border-amber-500/30 hover:bg-amber-600/20'">
                  {{ actionEmail === acc.email && actionType === 'kick' ? '移出中...' : '移出' }}
                </button>
                <button
                  v-if="acc.status === 'active' || acc.status === 'personal' || acc.is_main_account"
                  @click="exportCodexAuth(acc.email)"
                  :disabled="actionEmail === acc.email"
                  class="px-3 py-1.5 rounded-lg text-xs font-medium border transition bg-cyan-600/10 text-cyan-400 border-cyan-500/30 hover:bg-cyan-600/20">
                  导出
                </button>
                <button
                  v-if="!acc.is_main_account"
                  @click="removeAccount(acc.email)"
                  :disabled="actionDisabled || actionEmail === acc.email"
                  class="px-3 py-1.5 rounded-lg text-xs font-medium border transition"
                  :class="actionDisabled || actionEmail === acc.email
                    ? 'bg-gray-800 text-gray-500 border-gray-700 cursor-not-allowed'
                    : 'bg-rose-600/10 text-rose-400 border-rose-500/30 hover:bg-rose-600/20'">
                  {{ actionEmail === acc.email && actionType === 'delete' ? '删除中...' : '删除' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 注册失败明细 -->
      <div class="mt-6 bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div class="flex items-center justify-between mb-3">
          <div>
            <h2 class="text-lg font-semibold text-white">注册失败明细</h2>
            <div class="text-xs text-gray-500 mt-0.5">未能入池的注册尝试会写在这里（add-phone / duplicate / OAuth 失败等）</div>
          </div>
          <button @click="loadFailures" :disabled="failuresLoading"
            class="px-3 py-1.5 rounded-lg text-xs border bg-gray-800 hover:bg-gray-700 text-gray-300 border-gray-700 transition">
            {{ failuresLoading ? '加载中...' : '刷新' }}
          </button>
        </div>
        <div v-if="failuresCounts && Object.keys(failuresCounts).length" class="flex flex-wrap gap-2 mb-3 text-xs">
          <span v-for="(cnt, cat) in failuresCounts" :key="cat"
            class="px-2 py-1 rounded border bg-gray-800 border-gray-700 text-gray-300">
            {{ cat }}: <span class="text-rose-400 font-mono">{{ cnt }}</span>
          </span>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead class="text-xs text-gray-500 border-b border-gray-800">
              <tr>
                <th class="text-left px-3 py-2">时间</th>
                <th class="text-left px-3 py-2">邮箱</th>
                <th class="text-left px-3 py-2">类别</th>
                <th class="text-left px-3 py-2">原因</th>
                <th class="text-left px-3 py-2">附加</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-800/60 text-xs">
              <tr v-if="!failuresItems.length">
                <td class="px-3 py-4 text-gray-500" colspan="5">暂无失败记录</td>
              </tr>
              <tr v-for="(f, idx) in failuresItems" :key="idx">
                <td class="px-3 py-2 text-gray-400 font-mono">{{ fmtTs(f.timestamp) }}</td>
                <td class="px-3 py-2 text-gray-300 font-mono">{{ f.email || '-' }}</td>
                <td class="px-3 py-2">
                  <span class="px-2 py-0.5 rounded border text-[11px]"
                    :class="failureCategoryClass(f.category)">{{ f.category }}</span>
                </td>
                <td class="px-3 py-2 text-gray-400">{{ f.reason }}</td>
                <td class="px-3 py-2 text-gray-500 font-mono text-[11px]">{{ fmtFailureExtra(f) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Codex 认证导出弹窗 -->
      <div v-if="exportData" class="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4" @click.self="exportData = null">
        <div class="bg-gray-900 border border-gray-800 rounded-xl w-full max-w-2xl max-h-[80vh] flex flex-col">
          <div class="px-4 py-3 border-b border-gray-800 flex items-center justify-between">
            <h3 class="text-white font-semibold">Codex CLI 认证文件</h3>
            <button @click="exportData = null" class="text-gray-400 hover:text-white text-lg">&times;</button>
          </div>
          <div class="p-4 space-y-3 overflow-y-auto flex-1">
            <div class="px-3 py-2 bg-amber-500/10 border border-amber-500/20 rounded-lg text-sm text-amber-300 space-y-2">
              <div class="font-medium">使用步骤：</div>
              <ol class="list-decimal list-inside space-y-1 text-xs text-amber-400/90">
                <li>退出当前 Codex CLI 会话</li>
                <li>删除旧文件：<code class="bg-gray-800 px-1 rounded">rm ~/.codex/auth.json</code></li>
                <li>将下方内容保存到 <code class="bg-gray-800 px-1 rounded">~/.codex/auth.json</code>（Windows: <code class="bg-gray-800 px-1 rounded">%APPDATA%\codex\auth.json</code>）</li>
                <li>重新启动 Codex CLI</li>
              </ol>
              <div class="text-xs text-amber-400/60">导出后 Codex CLI 直连 OpenAI，不走 CPA 代理，响应更快。</div>
            </div>
            <div class="relative">
              <pre class="bg-gray-950 border border-gray-800 rounded-lg p-4 text-xs font-mono text-gray-300 overflow-x-auto whitespace-pre">{{ exportJson }}</pre>
              <button @click="copyExport"
                class="absolute top-2 right-2 px-2 py-1 rounded border text-xs transition"
                :class="copied
                  ? 'bg-green-600/20 text-green-400 border-green-500/30'
                  : 'bg-gray-800 hover:bg-gray-700 text-gray-400 hover:text-white border-gray-700'">
                {{ copied ? '复制成功' : '复制' }}
              </button>
            </div>
          </div>
          <div class="px-4 py-3 border-t border-gray-800 flex justify-end gap-3">
            <button @click="downloadExport"
              class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition">
              下载 auth.json
            </button>
            <button @click="exportData = null"
              class="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-sm text-gray-300 rounded-lg border border-gray-700 transition">
              关闭
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Loading skeleton -->
  <div v-else-if="loading" class="space-y-4">
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
      <div v-for="i in 4" :key="i" class="bg-gray-900 border border-gray-800 rounded-xl p-4 h-20 animate-pulse"></div>
    </div>
    <div class="bg-gray-900 border border-gray-800 rounded-xl h-64 animate-pulse"></div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({
  status: Object,
  loading: Boolean,
  runningTask: Object,
  adminStatus: {
    type: Object,
    default: null,
  },
})
const emit = defineEmits(['refresh'])

const actionEmail = ref('')
const actionType = ref('')
const syncing = ref(false)
const message = ref('')
const exportData = ref(null)
const copied = ref(false)
const messageClass = ref('')

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
    phone_blocked: 'bg-rose-500/10 text-rose-400 border-rose-500/30',
    duplicate_exhausted: 'bg-orange-500/10 text-orange-400 border-orange-500/30',
    register_failed: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
    oauth_failed: 'bg-purple-500/10 text-purple-400 border-purple-500/30',
    kick_failed: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/30',
    exception: 'bg-red-500/10 text-red-400 border-red-500/30',
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
  // 有任务完成（从有到无）时自动刷新一次失败日志
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
  if (allSelectableChecked.value) {
    selectedSet.value = new Set()
  } else {
    selectedSet.value = new Set(selectableEmails.value.map(e => e.toLowerCase()))
  }
}

function clearSelection() {
  selectedSet.value = new Set()
}

const cards = computed(() => {
  if (!props.status) return []
  const s = props.status.summary
  return [
    { label: '活跃', value: s.active, color: 'text-green-400' },
    { label: '待命', value: s.standby, color: 'text-yellow-400' },
    { label: '额度用完', value: s.exhausted, color: 'text-red-400' },
    { label: '免费号', value: s.personal || 0, color: 'text-fuchsia-400' },
    { label: '总计', value: s.total, color: 'text-white' },
  ]
})

function statusClass(s) {
  return {
    active: 'bg-green-500/10 text-green-400',
    exhausted: 'bg-red-500/10 text-red-400',
    standby: 'bg-yellow-500/10 text-yellow-400',
    pending: 'bg-gray-500/10 text-gray-400',
    personal: 'bg-fuchsia-500/10 text-fuchsia-400',
    auth_invalid: 'bg-orange-500/10 text-orange-400',
    orphan: 'bg-amber-500/10 text-amber-300',
  }[s] || 'bg-gray-500/10 text-gray-400'
}

function dotClass(s) {
  return {
    active: 'bg-green-400',
    exhausted: 'bg-red-400',
    standby: 'bg-yellow-400',
    pending: 'bg-gray-400',
    personal: 'bg-fuchsia-400',
    auth_invalid: 'bg-orange-400',
    orphan: 'bg-amber-300',
  }[s] || 'bg-gray-400'
}

function statusLabel(s) {
  return {
    active: 'Active',
    exhausted: 'Used up',
    standby: 'Standby',
    pending: 'Pending',
    personal: 'Personal',
    auth_invalid: '认证失效',
    orphan: '孤立',
  }[s] || s
}

function quota(acc, type) {
  const qi = props.status?.quota_cache?.[acc.email] || acc.last_quota
  if (!qi) return null
  const pct = type === 'primary' ? qi.primary_pct : qi.weekly_pct
  return 100 - (pct || 0)
}

function quotaPct(acc, type) {
  const val = quota(acc, type)
  return val !== null ? `${val}%` : '-'
}

function quotaReset(acc, type) {
  const qi = props.status?.quota_cache?.[acc.email] || acc.last_quota
  if (!qi) return '-'
  const ts = type === 'primary' ? qi.primary_resets_at : qi.weekly_resets_at
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function pctColor(val) {
  if (val === null) return 'text-gray-500'
  if (val > 30) return 'text-green-400'
  if (val > 0) return 'text-yellow-400'
  return 'text-red-400'
}

const exportJson = computed(() => {
  if (!exportData.value) return ''
  return JSON.stringify(exportData.value.codex_auth, null, 2)
})

async function exportCodexAuth(email) {
  try {
    exportData.value = await api.getCodexAuth(email)
    copied.value = false
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
    setTimeout(() => { message.value = '' }, 8000)
  }
}

async function copyExport() {
  try {
    await navigator.clipboard.writeText(exportJson.value)
  } catch {
    // HTTP 下 clipboard API 不可用，用 textarea fallback
    const ta = document.createElement('textarea')
    ta.value = exportJson.value
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
  copied.value = true
  setTimeout(() => { copied.value = false }, 3000)
}

function downloadExport() {
  const blob = new Blob([exportJson.value], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'auth.json'
  a.click()
  URL.revokeObjectURL(url)
}

async function syncAccounts() {
  syncing.value = true
  message.value = ''
  try {
    const result = await api.postSyncAccounts()
    message.value = result.message || '同步完成'
    messageClass.value = 'bg-green-500/10 text-green-400 border-green-500/20'
    emit('refresh')
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  } finally {
    syncing.value = false
    setTimeout(() => { message.value = '' }, 8000)
  }
}

function canLogin(acc) {
  // active 账号已经在 Team 里不需要再登录；personal + 已有 auth_file 也不需要
  if (acc.is_main_account) return false
  if (acc.status === 'active') return false
  if (acc.status === 'personal' && acc.auth_file) return false
  return true
}

function loginLabel(acc) {
  // personal 缺认证 / auth_invalid / orphan 都属于"需要补登录"语境
  if (acc.status === 'personal' || acc.status === 'auth_invalid' || acc.status === 'orphan') return '补登录'
  return '登录'
}

async function loginAccount(email) {
  if (actionDisabled.value) return

  actionEmail.value = email
  actionType.value = 'login'
  message.value = ''
  try {
    const result = await api.loginAccount(email)
    message.value = `已提交 ${email} 的登录任务: ${result.task_id}`
    messageClass.value = 'bg-blue-500/10 text-blue-400 border-blue-500/20'
    emit('refresh')
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  } finally {
    actionEmail.value = ''
    actionType.value = ''
    setTimeout(() => { message.value = '' }, 8000)
  }
}

async function kickAccount(email) {
  if (actionDisabled.value) return

  const ok = window.confirm(`确认将 ${email} 移出 Team？\n账号会变为 standby 状态，额度恢复后可重新复用。`)
  if (!ok) return

  actionEmail.value = email
  actionType.value = 'kick'
  message.value = ''
  try {
    const result = await api.kickAccount(email)
    message.value = result.message || `已将 ${email} 移出 Team`
    messageClass.value = 'bg-green-500/10 text-green-400 border-green-500/20'
    emit('refresh')
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  } finally {
    actionEmail.value = ''
    actionType.value = ''
    setTimeout(() => { message.value = '' }, 8000)
  }
}

async function removeAccount(email) {
  if (actionDisabled.value) return

  const ok = window.confirm(`确认删除账号 ${email}？\n这会同时清理本地记录、CPA、Team/Invite 和 CloudMail。`)
  if (!ok) return

  actionEmail.value = email
  actionType.value = 'delete'
  message.value = ''
  try {
    const result = await api.deleteAccount(email)
    message.value = result.message || `已删除 ${email}`
    messageClass.value = 'bg-green-500/10 text-green-400 border-green-500/20'
    emit('refresh')
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  } finally {
    actionEmail.value = ''
    actionType.value = ''
    setTimeout(() => { message.value = '' }, 8000)
  }
}

async function batchDelete() {
  if (actionDisabled.value || batchDeleting.value) return
  const emails = selectedEmails.value
  if (!emails.length) return

  const preview = emails.slice(0, 8).join('\n')
  const more = emails.length > 8 ? `\n...还有 ${emails.length - 8} 个` : ''
  const ok = window.confirm(
    `确认批量删除以下 ${emails.length} 个账号？这会清理本地记录、CPA、Team/Invite 和 CloudMail。\n\n${preview}${more}`
  )
  if (!ok) return

  batchDeleting.value = true
  batchProgress.value = `0/${emails.length}`
  message.value = ''
  try {
    const r = await api.deleteAccountsBatch(emails, true)
    const s = r?.summary || {}
    const failed = (r?.results || []).filter(x => !x.ok)
    if (failed.length === 0) {
      message.value = `批量删除完成:成功 ${s.ok}/${s.total}`
      messageClass.value = 'bg-green-500/10 text-green-400 border-green-500/20'
    } else {
      const head = failed.slice(0, 3).map(x => `${x.email}: ${x.error}`).join('; ')
      message.value = `批量删除部分失败(成功 ${s.ok}/${s.total}):${head}${failed.length > 3 ? ' …' : ''}`
      messageClass.value = 'bg-amber-500/10 text-amber-300 border-amber-500/20'
    }
    clearSelection()
    emit('refresh')
  } catch (e) {
    message.value = `批量删除失败: ${e.message}`
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  } finally {
    batchDeleting.value = false
    batchProgress.value = ''
    setTimeout(() => { message.value = '' }, 12000)
  }
}
</script>
