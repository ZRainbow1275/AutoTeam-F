<template>
  <!-- 桌面端侧边栏 — round-12 F1 Bright v1 -->
  <nav class="hidden md:flex w-56 shrink-0 min-h-screen flex-col p-4 relative
              border-r border-hairline bg-surface">
    <!-- 顶部装饰条 -->
    <div class="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-indigo-400/40 to-transparent"></div>

    <div class="mb-8 px-2">
      <div class="flex items-center gap-2 mb-1">
        <span class="inline-block w-2 h-2 rounded-sm bg-gradient-to-br from-indigo-500 to-violet-600"></span>
        <h1 class="text-lg font-extrabold text-ink-950 tracking-tight">AutoTeam</h1>
      </div>
      <p class="text-[10px] uppercase tracking-[0.2em] text-ink-400 ml-4">Operations Console</p>
    </div>

    <div class="space-y-0.5 flex-1">
      <button v-for="item in items" :key="item.key"
        @click="$emit('navigate', item.key)"
        class="w-full text-left px-3 py-2 rounded-xl text-sm transition-all flex items-center gap-2.5
               relative group focus-ring"
        :class="active === item.key
          ? 'bg-indigo-50 text-indigo-700'
          : 'text-ink-600 hover:bg-ink-100 hover:text-ink-950'">
        <span v-if="active === item.key" class="absolute left-0 top-2 bottom-2 w-0.5 rounded-r-full
          bg-gradient-to-b from-indigo-500 to-violet-600"></span>
        <component :is="item.icon" class="w-4 h-4 shrink-0" :stroke-width="2" />
        <span class="font-medium">{{ item.label }}</span>
      </button>
    </div>

    <div class="space-y-0.5 pt-4 border-t border-hairline">
      <button @click="$emit('refresh')" :disabled="loading"
        class="w-full text-left px-3 py-2 rounded-xl text-sm transition flex items-center gap-2.5
               text-ink-600 hover:bg-ink-100 hover:text-ink-950 disabled:opacity-50 focus-ring">
        <RefreshCw class="w-4 h-4 shrink-0" :class="loading ? 'animate-spin' : ''" :stroke-width="2" />
        <span class="font-medium">{{ loading ? '刷新中…' : '刷新数据' }}</span>
      </button>
      <button v-if="authRequired" @click="$emit('logout')"
        class="w-full text-left px-3 py-2 rounded-xl text-sm transition flex items-center gap-2.5
               text-ink-600 hover:bg-rose-50 hover:text-rose-700 focus-ring">
        <LogOut class="w-4 h-4 shrink-0" :stroke-width="2" />
        <span class="font-medium">登出</span>
      </button>
    </div>
  </nav>

  <!-- 移动端底部 tab 栏 -->
  <nav class="md:hidden fixed bottom-0 left-0 right-0 z-50 flex
              border-t border-hairline backdrop-blur-md bg-surface/95">
    <button v-for="item in items" :key="item.key"
      @click="$emit('navigate', item.key)"
      class="flex-1 flex flex-col items-center py-2 text-[10px] transition relative"
      :class="active === item.key ? 'text-indigo-700' : 'text-ink-500 hover:text-ink-700'">
      <span v-if="active === item.key"
        class="absolute top-0 left-1/4 right-1/4 h-0.5 rounded-b-full bg-gradient-to-r from-indigo-500 to-violet-600"></span>
      <component :is="item.icon" class="w-5 h-5" :stroke-width="2" />
      <span class="mt-0.5 font-medium">{{ item.mobileLabel || item.label }}</span>
    </button>
  </nav>
</template>

<script setup>
import {
  ChartPie,
  Users,
  RefreshCw,
  RotateCw,
  KeyRound,
  Scroll,
  ClipboardList,
  Settings,
  LogOut,
} from 'lucide-vue-next'

defineProps({
  active: String,
  loading: Boolean,
  authRequired: Boolean,
})
defineEmits(['navigate', 'refresh', 'logout'])

// round-12 F1 — emoji → lucide-vue-next 命名一致性
const items = [
  { key: 'dashboard', icon: ChartPie, label: '仪表盘', mobileLabel: '仪表盘' },
  { key: 'team', icon: Users, label: 'Team 成员', mobileLabel: '成员' },
  { key: 'pool', icon: RotateCw, label: '账号池操作', mobileLabel: '账号池' },
  { key: 'sync', icon: RefreshCw, label: '同步中心', mobileLabel: '同步' },
  { key: 'oauth', icon: KeyRound, label: 'OAuth 登录', mobileLabel: 'OAuth' },
  { key: 'tasks', icon: Scroll, label: '任务历史', mobileLabel: '任务' },
  { key: 'logs', icon: ClipboardList, label: '日志', mobileLabel: '日志' },
  { key: 'settings', icon: Settings, label: '设置', mobileLabel: '设置' },
]
</script>
