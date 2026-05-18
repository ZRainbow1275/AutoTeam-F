<template>
  <div class="glass rounded-lg p-5">
    <div class="flex items-center justify-between mb-4 gap-3 flex-wrap">
      <div>
        <h2 class="text-base font-bold text-ink-950 tracking-tight">{{ panelTitle }}</h2>
        <p class="text-[11px] text-ink-500 mt-0.5">提交后立即进入后台任务观察窗口,完成后自动同步状态。</p>
      </div>
      <div v-if="runningTask" class="flex items-center gap-2 text-xs">
        <span class="text-ink-500 uppercase tracking-widest text-[10px]">Running</span>
        <span class="font-mono text-amber-800 px-2 py-0.5 rounded bg-amber-50 border border-amber-200">{{ runningTask.command }}</span>
        <span class="font-mono text-ink-600">{{ runningTask.task_id ? runningTask.task_id.slice(0, 8) : '' }}</span>
        <AtButton variant="danger" size="sm" :loading="cancelling" :disabled="cancelRequested" @click="cancelTask">
          {{ cancelRequested ? '停止中…' : '停止任务' }}
        </AtButton>
      </div>
    </div>

    <div v-if="showAdminHint"
      class="mb-4 px-4 py-2.5 rounded-lg text-sm border bg-amber-50 text-amber-800 border-amber-200">
      {{ adminHint }}
    </div>

    <!-- 操作按钮区:visualy grouped -->
    <div class="flex flex-wrap gap-2.5">
      <button v-for="action in visibleActions" :key="action.key"
        @click="execute(action)"
        :disabled="isDisabled(action)"
        class="relative h-10 px-4 rounded-lg text-sm font-semibold border transition-all
               lift-hover focus-ring select-none whitespace-nowrap
               disabled:opacity-50 disabled:cursor-not-allowed disabled:bg-ink-100 disabled:text-ink-400 disabled:border-hairline"
        :class="actionColorClass(action)">
        <span class="inline-flex items-center gap-2">
          <span v-if="isSubmitting(action)" class="inline-block w-4 h-4 rounded-full border-2 border-current border-t-transparent animate-spin"></span>
          <component v-else :is="action.icon" class="w-4 h-4 shrink-0" :stroke-width="2" />
          {{ isSubmitting(action) ? '提交中' : action.label }}
        </span>
      </button>
    </div>

    <!-- round-12 F2 — rotate 实时进度面板 (SSE) -->
    <div class="mt-4 rounded-lg border border-hairline bg-surface">
      <button type="button"
        class="w-full flex items-center justify-between gap-3 px-4 py-2.5 text-sm focus-ring rounded-lg"
        @click="showProgress = !showProgress">
        <span class="inline-flex items-center gap-2 font-semibold text-ink-700">
          <Activity class="w-4 h-4" :class="rotateStream.isConnected.value ? 'text-emerald-600' : 'text-ink-400'" :stroke-width="2" />
          实时进度
          <span v-if="rotateStream.events.value.length"
            class="ml-1 px-1.5 py-0.5 rounded-full text-[10px] font-mono bg-indigo-50 text-indigo-700 border border-indigo-200">
            {{ rotateStream.events.value.length }}
          </span>
          <span v-if="!rotateStream.isConnected.value" class="text-[11px] text-ink-400 font-normal">
            (未连接 SSE)
          </span>
        </span>
        <component :is="showProgress ? ChevronUp : ChevronDown" class="w-4 h-4 text-ink-400" :stroke-width="2" />
      </button>
      <div v-if="showProgress" class="px-4 pb-3 pt-1 space-y-1.5 max-h-64 overflow-y-auto">
        <div v-if="!rotateStream.events.value.length" class="text-xs text-ink-400 py-2 text-center">
          暂无转移事件 — 启动一次轮转/补满任务,这里会实时显示账号状态变更。
        </div>
        <div v-for="(ev, idx) in rotateStream.events.value" :key="ev.ts + ':' + idx"
          class="flex items-start gap-2 px-2.5 py-1.5 rounded-lg border text-xs animate-rise"
          :class="transitionTone(ev)">
          <component :is="transitionIcon(ev)" class="w-3.5 h-3.5 mt-0.5 shrink-0" :stroke-width="2" />
          <div class="flex-1 min-w-0">
            <div class="font-mono truncate">{{ ev.email }}</div>
            <div class="text-[11px] opacity-80">
              <span class="font-mono">{{ statusLabel(ev.from) || '—' }}</span>
              <span class="mx-1">→</span>
              <span class="font-mono font-semibold">{{ statusLabel(ev.to) }}</span>
              <span v-if="ev.reason" class="ml-1 opacity-70">· {{ ev.reason }}</span>
            </div>
          </div>
          <span class="text-[10px] font-mono opacity-60 mt-0.5 shrink-0">{{ formatTransitionTime(ev.ts) }}</span>
        </div>
      </div>
    </div>

    <!-- 注册域名切换(仅 pool 模式可见) -->
    <div v-if="mode === 'pool'"
      class="mt-5 p-3 rounded-lg border border-hairline bg-ink-50 flex flex-wrap items-center gap-2 text-sm">
      <span class="text-[10px] uppercase tracking-widest text-ink-500 font-semibold mr-1">注册域名</span>
      <span class="text-ink-400">@</span>
      <input v-model="domainInput" type="text" placeholder="your-domain.com"
        class="flex-1 min-w-[180px] px-3 py-1.5 bg-surface border border-hairline rounded-lg text-ink-950 text-sm font-mono focus-ring transition" />
      <AtButton variant="primary" size="sm" :loading="domainBusy" :disabled="!domainInput" @click="saveDomain">
        保存并验证
      </AtButton>
      <span v-if="currentDomain" class="text-[11px] text-ink-500 font-mono">当前: @{{ currentDomain }}</span>
      <span v-if="domainMsg" class="ml-1 text-[11px] font-medium" :class="domainMsgOk ? 'text-emerald-700' : 'text-rose-700'">{{ domainMsg }}</span>
    </div>

    <!-- 参数输入 -->
    <div v-if="showParams"
      class="mt-4 p-3 rounded-lg border border-indigo-200 bg-indigo-50 flex items-center gap-3 animate-rise">
      <label class="text-[11px] uppercase tracking-widest text-indigo-700 font-semibold">{{ paramLabel }}</label>
      <input v-model.number="paramValue" type="number" min="1" :max="paramMax"
        class="w-24 px-3 py-1.5 bg-surface border border-hairline rounded-lg text-ink-950 text-sm font-mono focus-ring tabular" />
      <AtButton variant="primary" size="sm" :loading="!!executingActionKey" @click="confirmAction">确认执行</AtButton>
      <AtButton variant="ghost" size="sm" @click="showParams = false">取消</AtButton>
    </div>

    <!-- 结果提示 -->
    <div v-if="message" class="mt-4 px-4 py-2.5 rounded-lg text-sm border animate-rise" :class="messageClass">
      {{ message }}
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  RotateCw,
  ChartPie,
  Plus,
  Coins,
  UserPlus,
  Brush,
  RefreshCw,
  Download,
  Users,
  Activity,
  Check,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
} from 'lucide-vue-next'
import { api } from '../api.js'
import AtButton from './AtButton.vue'
import { useRotateStream } from '../composables/useRotateStream.js'
import { statusLabel } from '../composables/useStatus.js'

const props = defineProps({
  runningTask: Object,
  adminStatus: { type: Object, default: null },
  mode: { type: String, default: 'all' },
  // Round 8 — 母号订阅健康度,degraded 时禁用 fill-personal
  masterHealth: { type: Object, default: null },
  rotateStream: { type: Object, default: null },
})
const emit = defineEmits(['task-started', 'refresh'])

// round-12 F2/F3 — 共享 SSE 流直接用于实时进度展示;
// App 级状态层负责统一 invalidation,这里不再额外挂一层桥接。
const rotateStream = props.rotateStream || useRotateStream()
const showProgress = ref(false)

function transitionIcon(ev) {
  // 派发 lucide icon:to=active/personal → Check;to=auth_invalid/orphan → AlertTriangle;
  // 其余 (pending/standby/exhausted/grace) → Activity
  const t = ev?.to
  if (t === 'active' || t === 'personal') return Check
  if (t === 'auth_invalid' || t === 'orphan' || t === 'exhausted') return AlertTriangle
  return Activity
}

function transitionTone(ev) {
  const t = ev?.to
  if (t === 'active' || t === 'personal') {
    return 'text-emerald-700 bg-emerald-50 border-emerald-200'
  }
  if (t === 'auth_invalid' || t === 'orphan' || t === 'exhausted') {
    return 'text-rose-700 bg-rose-50 border-rose-200'
  }
  if (t === 'degraded_grace' || t === 'standby') {
    return 'text-amber-700 bg-amber-50 border-amber-200'
  }
  return 'text-indigo-700 bg-indigo-50 border-indigo-200'
}

function formatTransitionTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

// round-12 F1 — emoji → Lucide
const actions = [
  { key: 'rotate', group: 'pool', label: '智能轮转', icon: RotateCw, method: 'startRotate', needParam: true, paramName: 'target', tone: 'primary' },
  { key: 'check', group: 'pool', label: '检查额度', icon: ChartPie, method: 'startCheck', needParam: false, tone: 'emerald' },
  { key: 'fill', group: 'pool', label: '补满成员', icon: Plus, method: 'startFill', needParam: true, paramName: 'target', tone: 'teal' },
  { key: 'fill-personal', group: 'pool', label: '生成免费号', icon: Coins, method: 'startFillPersonal', needParam: true, paramName: 'count', tone: 'lime' },
  { key: 'add', group: 'pool', label: '添加账号', icon: UserPlus, method: 'startAdd', needParam: false, tone: 'amber' },
  { key: 'cleanup', group: 'pool', label: '清理成员', icon: Brush, method: 'startCleanup', needParam: false, tone: 'rose' },
  { key: 'sync', group: 'sync', label: '同步 CPA', icon: RefreshCw, method: 'postSync', needParam: false, sync: true, allowWithoutAdmin: true, tone: 'cyan' },
  { key: 'pull-cpa', group: 'sync', label: '拉取 CPA', icon: Download, method: 'postSyncFromCpa', needParam: false, sync: true, allowWithoutAdmin: true, tone: 'emerald' },
  { key: 'sync-accounts', group: 'sync', label: '同步账号', icon: Users, method: 'postSyncAccounts', needParam: false, sync: true, allowWithoutAdmin: true, tone: 'sky' },
]

const showParams = ref(false)
const paramLabel = ref('')
const paramValue = ref(3)
const paramMax = ref(3)
const pendingAction = ref(null)
const executingActionKey = ref('')

const cancelling = ref(false)
const cancelRequested = ref(false)

watch(() => props.runningTask?.task_id, (newId, oldId) => {
  if (newId !== oldId) {
    cancelling.value = false
    cancelRequested.value = false
  }
})
watch(() => props.runningTask?.cancel_requested, (v) => {
  if (v) cancelRequested.value = true
}, { immediate: true })

async function cancelTask() {
  if (cancelling.value || cancelRequested.value) return
  const task = props.runningTask
  if (!task) return
  const ok = window.confirm(`确认停止当前任务?\n\n命令: ${task.command}\nID: ${task.task_id}\n\n当前步骤(如正在浏览器内跑的账号)会先跑完,之后不再启动下一步。`)
  if (!ok) return
  cancelling.value = true
  try {
    const r = await api.cancelTask()
    cancelRequested.value = true
    message.value = r.message || '已请求停止'
    messageClass.value = 'bg-amber-50 text-amber-800 border-amber-200'
    emit('refresh')
  } catch (e) {
    message.value = `停止失败: ${e.message}`
    messageClass.value = 'bg-rose-50 text-rose-700 border-rose-200'
  } finally {
    cancelling.value = false
    setTimeout(() => { if (messageClass.value.includes('amber')) message.value = '' }, 10000)
  }
}

const domainInput = ref('')
const currentDomain = ref('')
const domainBusy = ref(false)
const domainMsg = ref('')
const domainMsgOk = ref(false)

async function loadDomain() {
  try {
    const d = await api.getRegisterDomain()
    currentDomain.value = d.domain || ''
    if (!domainInput.value) domainInput.value = d.domain || ''
  } catch (e) {
    domainMsg.value = `读取失败: ${e.message}`
    domainMsgOk.value = false
  }
}

async function saveDomain() {
  if (!domainInput.value) return
  domainBusy.value = true
  domainMsg.value = ''
  try {
    const r = await api.setRegisterDomain(domainInput.value.replace(/^@/, '').trim(), true)
    currentDomain.value = r.domain || ''
    domainMsg.value = r.message || '已保存'
    domainMsgOk.value = true
  } catch (e) {
    domainMsg.value = e.message
    domainMsgOk.value = false
  } finally {
    domainBusy.value = false
    setTimeout(() => { domainMsg.value = '' }, 8000)
  }
}

onMounted(() => { if (props.mode === 'pool') loadDomain() })
watch(() => props.mode, (m) => { if (m === 'pool') loadDomain() })

const message = ref('')
const messageClass = ref('')
const adminReady = computed(() => !!props.adminStatus?.configured)
const visibleActions = computed(() => {
  if (props.mode === 'all') return actions
  return actions.filter(action => action.group === props.mode)
})
const panelTitle = computed(() => {
  if (props.mode === 'pool') return '账号池操作'
  if (props.mode === 'sync') return '同步操作'
  return '操作'
})
const adminHint = computed(() => {
  if (props.mode === 'sync') return '同步类操作可独立使用:同步账号、同步 CPA、拉取 CPA。'
  return '请先在「设置」页完成管理员登录后,轮转/补满/清理等账号池操作才会开放。'
})
const showAdminHint = computed(() => !adminReady.value && (props.mode === 'pool' || props.mode === 'sync'))

const masterDegraded = computed(() => !!(
  props.masterHealth
  && props.masterHealth.healthy === false
  && props.masterHealth.reason === 'subscription_cancelled'
))

function isDisabled(action) {
  if (executingActionKey.value) return true
  if (props.runningTask) return true
  if (!adminReady.value && !action.allowWithoutAdmin) return true
  if (action.key === 'fill-personal' && masterDegraded.value) return true
  return false
}

function isSubmitting(action) {
  return executingActionKey.value === action.key
}

// 按 tone 给按钮配色 — round-12 F1 Bright v1
function actionColorClass(action) {
  const map = {
    primary: 'text-indigo-700 bg-indigo-50 border-indigo-200 hover:bg-indigo-100 hover:border-indigo-300',
    emerald: 'text-emerald-700 bg-emerald-50 border-emerald-200 hover:bg-emerald-100',
    teal: 'text-teal-700 bg-teal-50 border-teal-200 hover:bg-teal-100',
    lime: 'text-lime-700 bg-lime-50 border-lime-200 hover:bg-lime-100',
    amber: 'text-amber-800 bg-amber-50 border-amber-200 hover:bg-amber-100',
    rose: 'text-rose-700 bg-rose-50 border-rose-200 hover:bg-rose-100',
    cyan: 'text-cyan-700 bg-cyan-50 border-cyan-200 hover:bg-cyan-100',
    sky: 'text-sky-700 bg-sky-50 border-sky-200 hover:bg-sky-100',
  }
  return map[action.tone] || map.primary
}

async function execute(action) {
  if (isDisabled(action)) return
  message.value = ''
  if (action.needParam) {
    pendingAction.value = action
    if (action.paramName === 'target') {
      paramLabel.value = '目标成员数'; paramMax.value = 3; paramValue.value = 3
    } else if (action.paramName === 'count') {
      paramLabel.value = '生成数量'; paramMax.value = 2; paramValue.value = 1
    } else {
      paramLabel.value = '最大席位'; paramMax.value = 3; paramValue.value = 3
    }
    showParams.value = true
    return
  }
  await doExecute(action)
}

async function confirmAction() {
  showParams.value = false
  if (pendingAction.value) {
    await doExecute(pendingAction.value, paramValue.value)
    pendingAction.value = null
  }
}

async function doExecute(action, param) {
  executingActionKey.value = action.key
  try {
    if (action.sync) {
      const result = await api[action.method]()
      message.value = result.message || '操作完成'
      messageClass.value = 'bg-emerald-50 text-emerald-700 border-emerald-200'
      emit('refresh')
    } else {
      const result = await api[action.method](param)
      message.value = `任务已提交: ${result.task_id}`
      messageClass.value = 'bg-sky-50 text-sky-700 border-sky-200'
      emit('task-started')
    }
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-rose-50 text-rose-700 border-rose-200'
  } finally {
    executingActionKey.value = ''
  }
  setTimeout(() => { message.value = '' }, 8000)
}
</script>
