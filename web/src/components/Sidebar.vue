<template>
  <!-- 桌面端侧边栏 -->
  <nav class="hidden md:flex w-56 shrink-0 min-h-screen flex-col p-4 relative
              border-r border-white/[0.04] bg-gradient-to-b from-ink-900/80 to-ink-950/90 backdrop-blur-sm">
    <!-- 顶部装饰 -->
    <div class="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-indigo-400/40 to-transparent"></div>

    <div class="mb-8 px-2">
      <div class="flex items-center gap-2 mb-1">
        <span class="inline-block w-2 h-2 rounded-sm bg-gradient-to-br from-indigo-400 to-violet-500 shadow-glow-blue"></span>
        <h1 class="text-lg font-extrabold text-white tracking-tight">AutoTeam</h1>
      </div>
      <p class="text-[10px] uppercase tracking-[0.2em] text-gray-600 ml-4">Operations Console</p>
    </div>

    <div class="space-y-0.5 flex-1">
      <button v-for="item in items" :key="item.key"
        @click="$emit('navigate', item.key)"
        class="w-full text-left px-3 py-2 rounded-xl text-sm transition-all flex items-center gap-2.5
               relative group focus-ring"
        :class="active === item.key
          ? 'bg-gradient-to-r from-indigo-500/15 via-violet-500/10 to-transparent text-white shadow-inner-soft'
          : 'text-gray-400 hover:bg-white/[0.04] hover:text-gray-100'">
        <span v-if="active === item.key" class="absolute left-0 top-2 bottom-2 w-0.5 rounded-r-full
          bg-gradient-to-b from-indigo-400 to-violet-500"></span>
        <span class="text-base">{{ item.icon }}</span>
        <span class="font-medium">{{ item.label }}</span>
      </button>
    </div>

    <div class="space-y-0.5 pt-4 border-t border-white/[0.04]">
      <button @click="$emit('refresh')" :disabled="loading"
        class="w-full text-left px-3 py-2 rounded-xl text-sm transition flex items-center gap-2.5
               text-gray-400 hover:bg-white/[0.04] hover:text-white disabled:opacity-50 focus-ring">
        <span class="text-base inline-block" :class="loading ? 'animate-spin' : ''">🔄</span>
        <span class="font-medium">{{ loading ? '刷新中…' : '刷新数据' }}</span>
      </button>
      <button v-if="authRequired" @click="$emit('logout')"
        class="w-full text-left px-3 py-2 rounded-xl text-sm transition flex items-center gap-2.5
               text-gray-400 hover:bg-rose-500/10 hover:text-rose-300 focus-ring">
        <span class="text-base">🚪</span>
        <span class="font-medium">登出</span>
      </button>
    </div>
  </nav>

  <!-- 移动端底部 tab 栏 -->
  <nav class="md:hidden fixed bottom-0 left-0 right-0 z-50 flex
              border-t border-white/[0.04] backdrop-blur-md bg-ink-900/90">
    <button v-for="item in items" :key="item.key"
      @click="$emit('navigate', item.key)"
      class="flex-1 flex flex-col items-center py-2 text-[10px] transition relative"
      :class="active === item.key ? 'text-indigo-300' : 'text-gray-500 hover:text-gray-300'">
      <span v-if="active === item.key"
        class="absolute top-0 left-1/4 right-1/4 h-0.5 rounded-b-full bg-gradient-to-r from-indigo-400 to-violet-500"></span>
      <span class="text-lg">{{ item.icon }}</span>
      <span class="mt-0.5 font-medium">{{ item.mobileLabel || item.label }}</span>
    </button>
  </nav>
</template>

<script setup>
defineProps({
  active: String,
  loading: Boolean,
  authRequired: Boolean,
})
defineEmits(['navigate', 'refresh', 'logout'])

const items = [
  { key: 'dashboard', icon: '📊', label: '仪表盘', mobileLabel: '仪表盘' },
  { key: 'team', icon: '👥', label: 'Team 成员', mobileLabel: '成员' },
  { key: 'pool', icon: '🔁', label: '账号池操作', mobileLabel: '账号池' },
  { key: 'sync', icon: '🔄', label: '同步中心', mobileLabel: '同步' },
  { key: 'oauth', icon: '🔐', label: 'OAuth 登录', mobileLabel: 'OAuth' },
  { key: 'tasks', icon: '📜', label: '任务历史', mobileLabel: '任务' },
  { key: 'logs', icon: '📋', label: '日志', mobileLabel: '日志' },
  { key: 'settings', icon: '⚙️', label: '设置', mobileLabel: '设置' },
]
</script>
