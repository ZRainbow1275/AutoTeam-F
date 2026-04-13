<template>
  <!-- 桌面端侧边栏 -->
  <nav class="hidden md:flex w-48 shrink-0 bg-gray-900 border-r border-gray-800 min-h-screen p-4 flex-col">
    <div class="mb-6">
      <h1 class="text-lg font-bold text-white">AutoTeam</h1>
      <p class="text-xs text-gray-500 mt-0.5">账号轮转管理</p>
    </div>
    <div class="space-y-1 flex-1">
      <button v-for="item in items" :key="item.key"
        @click="$emit('navigate', item.key)"
        class="w-full text-left px-3 py-2 rounded-lg text-sm transition flex items-center gap-2"
        :class="active === item.key
          ? 'bg-blue-600/20 text-blue-400'
          : 'text-gray-400 hover:bg-gray-800 hover:text-white'">
        <span class="text-base">{{ item.icon }}</span>
        {{ item.label }}
      </button>
    </div>
    <div class="space-y-1 pt-4 border-t border-gray-800">
      <button @click="$emit('refresh')" :disabled="loading"
        class="w-full text-left px-3 py-2 rounded-lg text-sm transition flex items-center gap-2 text-gray-400 hover:bg-gray-800 hover:text-white disabled:opacity-50">
        <span class="text-base">🔄</span>
        {{ loading ? '刷新中...' : '刷新数据' }}
      </button>
      <button v-if="authRequired" @click="$emit('logout')"
        class="w-full text-left px-3 py-2 rounded-lg text-sm transition flex items-center gap-2 text-gray-400 hover:bg-gray-800 hover:text-red-400">
        <span class="text-base">🚪</span>
        登出
      </button>
    </div>
  </nav>

  <!-- 移动端底部 tab 栏 -->
  <nav class="md:hidden fixed bottom-0 left-0 right-0 bg-gray-900 border-t border-gray-800 z-50 flex">
    <button v-for="item in items" :key="item.key"
      @click="$emit('navigate', item.key)"
      class="flex-1 flex flex-col items-center py-2 text-xs transition"
      :class="active === item.key
        ? 'text-blue-400'
        : 'text-gray-500'">
      <span class="text-lg">{{ item.icon }}</span>
      <span class="mt-0.5">{{ item.mobileLabel || item.label }}</span>
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
  { key: 'tasks', icon: '⚡', label: '操作 & 任务', mobileLabel: '操作' },
  { key: 'logs', icon: '📋', label: '日志', mobileLabel: '日志' },
  { key: 'settings', icon: '⚙️', label: '设置', mobileLabel: '设置' },
]
</script>
