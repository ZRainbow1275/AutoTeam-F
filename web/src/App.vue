<template>
  <!-- 初始配置页 -->
  <SetupPage v-if="needSetup" @configured="onSetupDone" />

  <!-- 登录页 -->
  <div v-else-if="!authenticated" class="min-h-screen flex items-center justify-center px-4">
    <div class="glass rounded-2xl p-8 w-full max-w-sm relative overflow-hidden">
      <div class="absolute -top-20 -right-20 w-56 h-56 rounded-full opacity-40 blur-3xl pointer-events-none"
        style="background: radial-gradient(circle, rgba(99, 102, 241, 0.45), transparent 60%);"></div>
      <div class="relative">
        <div class="text-[10px] uppercase tracking-[0.3em] text-indigo-300/70 mb-1">Account Operations</div>
        <h1 class="text-2xl font-extrabold text-white mb-1 tracking-tight">AutoTeam</h1>
        <p class="text-sm text-gray-400 mb-6">输入管理 API Key 进入控制台</p>
        <div v-if="authError"
          class="mb-4 px-3 py-2.5 rounded-lg text-sm bg-rose-500/10 text-rose-300 border border-rose-500/30">
          {{ authError }}
        </div>
        <input
          v-model.trim="inputKey"
          type="password"
          placeholder="API Key"
          @keyup.enter="doLogin"
          class="w-full px-3.5 py-2.5 bg-white/[0.03] border border-white/10 rounded-xl text-sm text-white
                 font-mono placeholder:text-gray-600 focus-ring focus:border-indigo-400/40 mb-4 transition" />
        <AtButton variant="primary" class="w-full" :loading="authLoading" :disabled="!inputKey" @click="doLogin">
          {{ authLoading ? '验证中…' : '进入控制台' }}
        </AtButton>
      </div>
    </div>
  </div>

  <!-- 主面板 -->
  <div v-else class="flex min-h-screen">
    <!-- 侧边栏 -->
    <Sidebar :active="currentPage" :loading="loading" :auth-required="authRequired"
      @navigate="currentPage = $event" @refresh="refresh" @logout="doLogout" />

    <!-- 主内容区 -->
    <div class="flex-1 p-4 md:p-6 overflow-y-auto pb-20 md:pb-6 max-w-screen-2xl mx-auto w-full">
      <!-- 任务执行中提示 -->
      <div v-if="busyTask"
        class="flex items-center gap-2.5 text-sm text-amber-300 mb-4 px-3 py-2 rounded-xl border border-amber-500/20 bg-amber-500/5 w-fit animate-rise">
        <span class="animate-spin inline-block w-3.5 h-3.5 border-2 border-amber-300 border-t-transparent rounded-full"></span>
        <span class="font-medium">
          {{ busyTask.command === 'admin-login'
            ? '管理员登录中...'
            : busyTask.command === 'main-codex-sync'
              ? '主号 Codex 同步中...'
              : `${busyTask.command} 执行中...` }}
        </span>
      </div>

      <!-- 页面内容 -->
      <Dashboard v-if="currentPage === 'dashboard'"
        :status="status" :loading="loading" :running-task="busyTask" :admin-status="adminStatus"
        :master-health="masterHealth" @refresh="refresh" @reload-master-health="reloadMasterHealth" />

      <TeamMembers v-else-if="currentPage === 'team'" />

      <PoolPage v-else-if="currentPage === 'pool'"
        :running-task="busyTask" :admin-status="adminStatus" :master-health="masterHealth" :status="status"
        @task-started="onTaskStarted" @refresh="refresh" @reload-master-health="reloadMasterHealth" />

      <SyncPage v-else-if="currentPage === 'sync'"
        :running-task="busyTask" :admin-status="adminStatus"
        @task-started="onTaskStarted" @refresh="refresh" />

      <OAuthPage v-else-if="currentPage === 'oauth'"
        :manual-account-status="manualAccountStatus" @refresh="refresh" @progress="onAdminProgress" />

      <TaskHistoryPage v-else-if="currentPage === 'tasks'"
        :tasks="tasks" />

      <LogViewer v-else-if="currentPage === 'logs'" />

      <Settings v-else-if="currentPage === 'settings'"
        :admin-status="adminStatus" :codex-status="codexStatus"
        :master-health="masterHealth" :status="status"
        @refresh="refresh" @admin-progress="onAdminProgress" @reload-master-health="reloadMasterHealth" />
    </div>

    <ToastHost />
  </div>
</template>

<script setup>
import { computed, ref, onMounted, onUnmounted, watch } from 'vue'
import { api, setApiKey, clearApiKey } from './api.js'
import SetupPage from './components/SetupPage.vue'
import Sidebar from './components/Sidebar.vue'
import Dashboard from './components/Dashboard.vue'
import TeamMembers from './components/TeamMembers.vue'
import PoolPage from './components/PoolPage.vue'
import SyncPage from './components/SyncPage.vue'
import TaskHistoryPage from './components/TaskHistoryPage.vue'
import LogViewer from './components/LogViewer.vue'
import OAuthPage from './components/OAuthPage.vue'
import Settings from './components/Settings.vue'
import ToastHost from './components/ToastHost.vue'
import AtButton from './components/AtButton.vue'

const needSetup = ref(false)
const authenticated = ref(false)
const authRequired = ref(false)
const authLoading = ref(false)
const authError = ref('')
const inputKey = ref('')
const currentPage = ref('dashboard')

const status = ref(null)
const adminStatus = ref(null)
const codexStatus = ref(null)
const manualAccountStatus = ref(null)
const tasks = ref([])
const loading = ref(false)
const runningTask = ref(null)
// Round 9 — master-health 提到 App 级,4 个页面共享同一份(避免每页各刷各的)
const masterHealth = ref(null)
const masterHealthLoading = ref(false)
const busyTask = computed(() => {
  if (adminStatus.value?.login_in_progress) {
    return { command: 'admin-login' }
  }
  if (codexStatus.value?.in_progress) {
    return { command: 'main-codex-sync' }
  }
  return runningTask.value
})

let pollTimer = null

async function checkAuth() {
  try {
    const result = await api.checkAuth()
    authenticated.value = result.authenticated
    authRequired.value = result.auth_required
    return result.authenticated
  } catch (e) {
    if (e.status === 401) {
      authenticated.value = false
      authRequired.value = true
      return false
    }
    authenticated.value = true
    authRequired.value = false
    return true
  }
}

async function doLogin() {
  authError.value = ''
  authLoading.value = true
  try {
    setApiKey(inputKey.value)
    const ok = await checkAuth()
    if (!ok) {
      clearApiKey()
      authError.value = 'API Key 无效'
    } else {
      inputKey.value = ''
      refresh()
      startPolling(600000)
    }
  } catch (e) {
    clearApiKey()
    authError.value = e.message
  } finally {
    authLoading.value = false
  }
}

function doLogout() {
  clearApiKey()
  authenticated.value = false
  stopPolling()
}

async function refresh() {
  loading.value = true
  try {
    const [s, t, admin, codex, manualAccount] = await Promise.all([
      api.getStatus(),
      api.getTasks(),
      api.getAdminStatus(),
      api.getMainCodexStatus(),
      api.getManualAccountStatus(),
    ])
    status.value = s
    tasks.value = t
    adminStatus.value = admin
    codexStatus.value = codex
    manualAccountStatus.value = manualAccount
    runningTask.value = t.find(t => t.status === 'running' || t.status === 'pending') || null
  } catch (e) {
    if (e.status === 401) {
      authenticated.value = false
      return
    }
    console.error('刷新失败:', e)
  } finally {
    loading.value = false
  }
}

async function reloadMasterHealth(forceRefresh = false) {
  if (!adminStatus.value?.configured) return
  masterHealthLoading.value = true
  try {
    masterHealth.value = await api.getMasterHealth(!!forceRefresh)
  } catch (e) {
    console.error('加载母号健康度失败:', e)
  } finally {
    masterHealthLoading.value = false
  }
}

function onTaskStarted() {
  startPolling(10000)
  refresh()
}

function onAdminProgress() {
  startPolling(10000)
  refresh()
}

function startPolling(interval = 600000) {
  stopPolling()
  pollTimer = setInterval(async () => {
    await refresh()
    if (!busyTask.value && interval < 600000) {
      startPolling(600000)
    }
  }, interval)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function checkSetup() {
  try {
    const result = await api.getSetupStatus()
    return result.configured
  } catch {
    return true // 接口不存在说明是旧版本，跳过
  }
}

function onSetupDone() {
  needSetup.value = false
  checkAuth().then(ok => {
    if (ok) {
      refresh()
      startPolling(600000)
    }
  })
}

// admin 配置完成后自动拉一次 master-health
watch(
  () => adminStatus.value?.configured,
  (configured) => {
    if (configured && !masterHealth.value) reloadMasterHealth(false)
  }
)

onMounted(async () => {
  const setupOk = await checkSetup()
  if (!setupOk) {
    needSetup.value = true
    return
  }
  const ok = await checkAuth()
  if (ok) {
    refresh()
    startPolling(600000)
  }
})

onUnmounted(() => {
  stopPolling()
})
</script>
