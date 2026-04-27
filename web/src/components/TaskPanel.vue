<template>
  <div class="mt-6 bg-gray-900 border border-gray-800 rounded-xl p-4">
    <div class="flex items-center justify-between mb-4 gap-3 flex-wrap">
      <h2 class="text-lg font-semibold text-white">{{ panelTitle }}</h2>
      <div v-if="runningTask" class="flex items-center gap-2 text-xs">
        <span class="text-gray-400">运行中:</span>
        <span class="font-mono text-gray-300">{{ runningTask.command }}</span>
        <span class="font-mono text-gray-500">{{ runningTask.task_id ? runningTask.task_id.slice(0,8) : '' }}</span>
        <button
          @click="cancelTask"
          :disabled="cancelling || cancelRequested"
          class="px-3 py-1.5 rounded-lg text-xs font-medium border transition"
          :class="cancelling || cancelRequested
            ? 'bg-gray-800 text-gray-500 border-gray-700 cursor-not-allowed'
            : 'bg-rose-600/10 text-rose-400 border-rose-500/30 hover:bg-rose-600/20'">
          {{ cancelRequested ? '停止中...(等当前步骤结束)' : (cancelling ? '停止中...' : '停止任务') }}
        </button>
      </div>
    </div>
    <div v-if="showAdminHint" class="mb-4 px-4 py-3 rounded-lg text-sm border bg-amber-500/10 text-amber-300 border-amber-500/20">
      {{ adminHint }}
    </div>
    <div class="flex flex-wrap gap-3">
      <button v-for="action in visibleActions" :key="action.key"
        @click="execute(action)"
        :disabled="isDisabled(action)"
        class="px-4 py-2 rounded-lg text-sm font-medium transition border"
        :class="isDisabled(action)
          ? 'bg-gray-800 text-gray-500 border-gray-700 cursor-not-allowed'
          : `${action.style} hover:opacity-80`">
        {{ action.label }}
      </button>
    </div>

    <!-- 注册域名切换（仅 pool 模式可见）-->
    <div v-if="mode === 'pool'" class="mt-4 flex flex-wrap items-center gap-2 text-sm">
      <label class="text-gray-400">子号注册域名:</label>
      <span class="text-gray-500">@</span>
      <input v-model="domainInput" type="text" placeholder="your-domain.com"
        class="w-56 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500" />
      <button @click="saveDomain" :disabled="domainBusy || !domainInput"
        class="px-3 py-1.5 bg-sky-600 hover:bg-sky-500 disabled:bg-gray-700 disabled:text-gray-500 text-white rounded-lg transition">
        {{ domainBusy ? '验证中...' : '保存并验证' }}
      </button>
      <span v-if="currentDomain" class="text-gray-500">当前: @{{ currentDomain }}</span>
      <span v-if="domainMsg" class="ml-2" :class="domainMsgOk ? 'text-emerald-400' : 'text-rose-400'">{{ domainMsg }}</span>
    </div>

    <!-- 参数输入 -->
    <div v-if="showParams" class="mt-4 flex items-center gap-3">
      <label class="text-sm text-gray-400">{{ paramLabel }}:</label>
      <input v-model.number="paramValue" type="number" min="1" :max="paramMax"
        class="w-24 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" />
      <button @click="confirmAction" :disabled="pendingAction && isDisabled(pendingAction)"
        class="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition">
        确认执行
      </button>
      <button @click="showParams = false"
        class="px-3 py-1.5 text-gray-400 hover:text-white text-sm transition">
        取消
      </button>
    </div>

    <!-- 结果提示 -->
    <div v-if="message" class="mt-4 px-4 py-3 rounded-lg text-sm" :class="messageClass">
      {{ message }}
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({
  runningTask: Object,
  adminStatus: {
    type: Object,
    default: null,
  },
  mode: {
    type: String,
    default: 'all',
  },
  // Round 8 — 母号订阅健康度,degraded 时禁用 fill-personal
  masterHealth: {
    type: Object,
    default: null,
  },
})
const emit = defineEmits(['task-started', 'refresh'])

const actions = [
  { key: 'rotate', group: 'pool', label: '智能轮转', method: 'startRotate', needParam: true, paramName: 'target', style: 'bg-blue-600 text-white border-blue-500' },
  { key: 'check', group: 'pool', label: '检查额度', method: 'startCheck', needParam: false, style: 'bg-emerald-600 text-white border-emerald-500' },
  { key: 'fill', group: 'pool', label: '补满成员', method: 'startFill', needParam: true, paramName: 'target', style: 'bg-violet-600 text-white border-violet-500' },
  { key: 'fill-personal', group: 'pool', label: '生成免费号', method: 'startFillPersonal', needParam: true, paramName: 'count', style: 'bg-fuchsia-600 text-white border-fuchsia-500' },
  { key: 'add', group: 'pool', label: '添加账号', method: 'startAdd', needParam: false, style: 'bg-amber-600 text-white border-amber-500' },
  { key: 'cleanup', group: 'pool', label: '清理成员', method: 'startCleanup', needParam: false, style: 'bg-rose-600 text-white border-rose-500' },
  { key: 'sync', group: 'sync', label: '同步 CPA', method: 'postSync', needParam: false, sync: true, allowWithoutAdmin: true, style: 'bg-cyan-600 text-white border-cyan-500' },
  { key: 'pull-cpa', group: 'sync', label: '拉取 CPA', method: 'postSyncFromCpa', needParam: false, sync: true, allowWithoutAdmin: true, style: 'bg-emerald-600 text-white border-emerald-500' },
  { key: 'sync-accounts', group: 'sync', label: '同步账号', method: 'postSyncAccounts', needParam: false, sync: true, allowWithoutAdmin: true, style: 'bg-sky-600 text-white border-sky-500' },
]

const showParams = ref(false)
const paramLabel = ref('')
const paramValue = ref(5)
const paramMax = ref(20)
const pendingAction = ref(null)

const cancelling = ref(false)
const cancelRequested = ref(false)

// 监听 task_id 变化,而非 runningTask 对象本身:
// - null → null    : 无变化,忽略
// - null → idA     : 新任务开始,重置按钮(上次遗留 cancelRequested=true 也要清)
// - idA → null     : 任务结束,重置
// - idA → idB      : A 结束 B 立即开始(轮询间隔内连续切换),也要重置,避免 B 显示"停止中"
watch(() => props.runningTask?.task_id, (newId, oldId) => {
  if (newId !== oldId) {
    cancelling.value = false
    cancelRequested.value = false
  }
})

// 刷新页面/切换路由后,如果后端已经标记 cancel_requested,UI 恢复"停止中"状态,不让用户重复点击
// immediate: true 确保首次挂载时也能立刻同步(Dashboard 第一次拿到 task 数据可能就带着 cancel_requested)
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
    messageClass.value = 'bg-amber-500/10 text-amber-300 border border-amber-500/20'
  } catch (e) {
    message.value = `停止失败: ${e.message}`
    messageClass.value = 'bg-red-500/10 text-red-400 border border-red-500/20'
  } finally {
    cancelling.value = false
    setTimeout(() => { if (messageClass.value.includes('amber')) message.value = '' }, 10000)
  }
}

// 注册域名切换状态
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

onMounted(() => {
  if (props.mode === 'pool') loadDomain()
})
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
  if (props.mode === 'sync') {
    return '同步类操作可独立使用：同步账号、同步 CPA、拉取 CPA。'
  }
  return '请先在「设置」页完成管理员登录后，轮转/补满/清理等账号池操作才会开放。'
})
const showAdminHint = computed(() => !adminReady.value && (props.mode === 'pool' || props.mode === 'sync'))

const masterDegraded = computed(() => {
  // Round 8 — fill-personal 在 master cancelled 时禁用,避免账号池堆 plan_drift 失败
  return !!(
    props.masterHealth
    && props.masterHealth.healthy === false
    && props.masterHealth.reason === 'subscription_cancelled'
  )
})

function isDisabled(action) {
  if (props.runningTask) return true
  if (!adminReady.value && !action.allowWithoutAdmin) return true
  if (action.key === 'fill-personal' && masterDegraded.value) return true
  return false
}

async function execute(action) {
  if (isDisabled(action)) return
  message.value = ''
  if (action.needParam) {
    pendingAction.value = action
    if (action.paramName === 'target') {
      paramLabel.value = '目标成员数'
      paramMax.value = 20
      paramValue.value = 5
    } else if (action.paramName === 'count') {
      // 免费号：目标规模可能到 200+，放开上限到 500
      paramLabel.value = '生成数量'
      paramMax.value = 500
      paramValue.value = 4
    } else {
      paramLabel.value = '最大席位'
      paramMax.value = 20
      paramValue.value = 5
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
      messageClass.value = 'bg-green-500/10 text-green-400 border border-green-500/20'
      emit('refresh')
    } else {
      const result = await api[action.method](param)
      message.value = `任务已提交: ${result.task_id}`
      messageClass.value = 'bg-blue-500/10 text-blue-400 border border-blue-500/20'
      emit('task-started')
    }
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border border-red-500/20'
  }
  setTimeout(() => { message.value = '' }, 8000)
}
</script>
