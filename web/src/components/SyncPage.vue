<template>
  <div class="space-y-5">
    <div>
      <div class="text-[10px] uppercase tracking-[0.3em] text-indigo-300/70 mb-1">Sync Center</div>
      <h2 class="text-2xl font-extrabold text-white tracking-tight">同步中心</h2>
      <p class="text-sm text-gray-400 mt-1">本地账号池对账、同步到 CPA、从 CPA 反向拉取认证文件。</p>
    </div>
    <TaskPanel
      mode="sync"
      :running-task="runningTask"
      :admin-status="adminStatus"
      @task-started="$emit('task-started')"
      @refresh="$emit('refresh')"
    />
  </div>
</template>

<script setup>
import TaskPanel from './TaskPanel.vue'

defineProps({
  runningTask: Object,
  adminStatus: Object,
})

defineEmits(['task-started', 'refresh'])
</script>
