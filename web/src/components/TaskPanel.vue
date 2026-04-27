<template>
  <div class="glass rounded-2xl p-5">
    <div class="flex items-center justify-between mb-4 gap-3 flex-wrap">
      <div>
        <h2 class="text-base font-bold text-white tracking-tight">{{ panelTitle }}</h2>
        <p class="text-[11px] text-gray-500 mt-0.5">点击按钮提交后台任务,轮询返回结果</p>
      </div>
      <div v-if="runningTask" class="flex items-center gap-2 text-xs">
        <span class="text-gray-500 uppercase tracking-widest text-[10px]">Running</span>
        <span class="font-mono text-amber-300 px-2 py-0.5 rounded bg-amber-500/10 border border-amber-500/30">{{ runningTask.command }}</span>
        <span class="font-mono text-gray-600">{{ runningTask.task_id ? runningTask.task_id.slice(0, 8) : '' }}</span>
        <AtButton variant="danger" size="sm" :loading="cancelling" :disabled="cancelRequested" @click="cancelTask">
          {{ cancelRequested ? '停止中…' : '停止任务' }}
        </AtButton>
      </div>
    </div>

    <div v-if="showAdminHint"
      class="mb-4 px-4 py-2.5 rounded-xl text-sm border bg-amber-500/10 text-amber-300 border-amber-500/30">
      {{ adminHint }}
    </div>

    <!-- 操作按钮区:visualy grouped -->
    <div class="flex flex-wrap gap-2.5">
      <button v-for="action in visibleActions" :key="action.key"
        @click="execute(action)"
        :disabled="isDisabled(action)"
        class="relative h-10 px-4 rounded-xl text-sm font-semibold border transition-all
               lift-hover focus-ring select-none whitespace-nowrap
               disabled:opacity-50 disabled:cursor-not-allowed disabled:bg-white/[0.02] disabled:text-gray-500 disabled:border-white/[0.06]"
        :class="actionColorClass(action)">
        <span class="inline-flex items-center gap-2">
          <span class="text-base leading-none">{{ action.icon }}</span>
          {{ action.label }}
        </span>
      </button>
    </div>

    <!-- 注册域名切换(仅 pool 模式可见) -->
    <div v-if="mode === 'pool'"
      class="mt-5 p-3 rounded-xl border border-white/[0.04] bg-white/[0.02] flex flex-wrap items-center gap-2 text-sm">
      <span class="text-[10px] uppercase tracking-widest text-gray-500 font-semibold mr-1">注册域名</span>
      <span class="text-gray-600">@</span>
      <input v-model="domainInput" type="text" placeholder="your-domain.com"
        class="flex-1 min-w-[180px] px-3 py-1.5 bg-black/30 border border-white/10 rounded-lg text-white text-sm font-mono focus-ring focus:border-indigo-400/40 transition" />
      <AtButton variant="primary" size="sm" :loading="domainBusy" :disabled="!domainInput" @click="saveDomain">
        保存并验证
      </AtButton>
      <span v-if="currentDomain" class="text-[11px] text-gray-500 font-mono">当前: @{{ currentDomain }}</span>
      <span v-if="domainMsg" class="ml-1 text-[11px] font-medium" :class="domainMsgOk ? 'text-emerald-300' : 'text-rose-300'">{{ domainMsg }}</span>
    </div>

    <!-- 参数输入 -->
    <div v-if="showParams"
      class="mt-4 p-3 rounded-xl border border-indigo-500/30 bg-indigo-500/5 flex items-center gap-3 animate-rise">
      <label class="text-[11px] uppercase tracking-widest text-indigo-300 font-semibold">{{ paramLabel }}</label>
      <input v-model.number="paramValue" type="number" min="1" :max="paramMax"
        class="w-24 px-3 py-1.5 bg-black/30 border border-white/10 rounded-lg text-white text-sm font-mono focus-ring focus:border-indigo-400/40 tabular" />
      <AtButton variant="primary" size="sm" @click="confirmAction">确认执行</AtButton>
      <AtButton variant="ghost" size="sm" @click="showParams = false">取消</AtButton>
    </div>

    <!-- 结果提示 -->
    <div v-if="message" class="mt-4 px-4 py-2.5 rounded-xl text-sm border animate-rise" :class="messageClass">
      {{ message }}
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'
import AtButton from './AtButton.vue'

const props = defineProps({
  runningTask: Object,
  adminStatus: { type: Object, default: null },
  mode: { type: String, default: 'all' },
  // Round 8 — 母号订阅健康度,degraded 时禁用 fill-personal
  masterHealth: { type: Object, default: null },
})
const emit = defineEmits(['task-started', 'refresh'])

const actions = [
  { key: 'rotate', group: 'pool', label: '智能轮转', icon: '🔁', method: 'startRotate', needParam: true, paramName: 'target', tone: 'primary' },
  { key: 'check', group: 'pool', label: '检查额度', icon: '📊', method: 'startCheck', needParam: false, tone: 'emerald' },
  { key: 'fill', group: 'pool', label: '补满成员', icon: '➕', method: 'startFill', needParam: true, paramName: 'target', tone: 'violet' },
  { key: 'fill-personal', group: 'pool', label: '生成免费号', icon: '🪙', method: 'startFillPersonal', needParam: true, paramName: 'count', tone: 'fuchsia' },
  { key: 'add', group: 'pool', label: '添加账号', icon: '🆕', method: 'startAdd', needParam: false, tone: 'amber' },
  { key: 'cleanup', group: 'pool', label: '清理成员', icon: '🧹', method: 'startCleanup', needParam: false, tone: 'rose' },
  { key: 'sync', group: 'sync', label: '同步 CPA', icon: '🔄', method: 'postSync', needParam: false, sync: true, allowWithoutAdmin: true, tone: 'cyan' },
  { key: 'pull-cpa', group: 'sync', label: '拉取 CPA', icon: '⬇️', method: 'postSyncFromCpa', needParam: false, sync: true, allowWithoutAdmin: true, tone: 'emerald' },
  { key: 'sync-accounts', group: 'sync', label: '同步账号', icon: '👥', method: 'postSyncAccounts', needParam: false, sync: true, allowWithoutAdmin: true, tone: 'sky' },
]

const showParams = ref(false)
const paramLabel = ref('')
const paramValue = ref(5)
const paramMax = ref(20)
const pendingAction = ref(null)

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
    messageClass.value = 'bg-amber-500/10 text-amber-300 border-amber-500/30'
  } catch (e) {
    message.value = `停止失败: ${e.message}`
    messageClass.value = 'bg-rose-500/10 text-rose-300 border-rose-500/30'
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
  if (props.runningTask) return true
  if (!adminReady.value && !action.allowWithoutAdmin) return true
  if (action.key === 'fill-personal' && masterDegraded.value) return true
  return false
}

// 按 tone 给按钮配色,统一玻璃化
function actionColorClass(action) {
  const map = {
    primary: 'text-indigo-100 bg-gradient-to-br from-indigo-500/25 to-violet-500/25 border-indigo-400/40 hover:from-indigo-500/40 hover:to-violet-500/40 shadow-glow-blue',
    emerald: 'text-emerald-100 bg-emerald-500/15 border-emerald-400/30 hover:bg-emerald-500/25',
    violet: 'text-violet-100 bg-violet-500/15 border-violet-400/30 hover:bg-violet-500/25',
    fuchsia: 'text-fuchsia-100 bg-fuchsia-500/15 border-fuchsia-400/30 hover:bg-fuchsia-500/25',
    amber: 'text-amber-100 bg-amber-500/15 border-amber-400/30 hover:bg-amber-500/25',
    rose: 'text-rose-100 bg-rose-500/15 border-rose-400/30 hover:bg-rose-500/25',
    cyan: 'text-cyan-100 bg-cyan-500/15 border-cyan-400/30 hover:bg-cyan-500/25',
    sky: 'text-sky-100 bg-sky-500/15 border-sky-400/30 hover:bg-sky-500/25',
  }
  return map[action.tone] || map.primary
}

async function execute(action) {
  if (isDisabled(action)) return
  message.value = ''
  if (action.needParam) {
    pendingAction.value = action
    if (action.paramName === 'target') {
      paramLabel.value = '目标成员数'; paramMax.value = 20; paramValue.value = 5
    } else if (action.paramName === 'count') {
      paramLabel.value = '生成数量'; paramMax.value = 500; paramValue.value = 4
    } else {
      paramLabel.value = '最大席位'; paramMax.value = 20; paramValue.value = 5
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
  try {
    if (action.sync) {
      const result = await api[action.method]()
      message.value = result.message || '操作完成'
      messageClass.value = 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30'
      emit('refresh')
    } else {
      const result = await api[action.method](param)
      message.value = `任务已提交: ${result.task_id}`
      messageClass.value = 'bg-blue-500/10 text-blue-300 border-blue-500/30'
      emit('task-started')
    }
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-rose-500/10 text-rose-300 border-rose-500/30'
  }
  setTimeout(() => { message.value = '' }, 8000)
}
</script>
