<template>
  <div class="space-y-5">
    <!-- 标题 -->
    <div>
      <div class="text-[10px] uppercase tracking-[0.3em] text-indigo-300/70 mb-1">Pool Operations</div>
      <h2 class="text-2xl font-extrabold text-white tracking-tight">账号池操作</h2>
      <p class="text-sm text-gray-400 mt-1 max-w-2xl">
        集中执行轮转、检查、补满、添加、清理等会直接影响账号池状态的动作。
      </p>
    </div>

    <!-- F3 Master health banner(共享 App 级数据) -->
    <MasterHealthBanner
      :master-health="masterHealth"
      :min-grace-until="minGraceUntil"
      :loading="false"
      @refresh="$emit('reload-master-health', true)" />

    <TaskPanel
      mode="pool"
      :running-task="runningTask"
      :admin-status="adminStatus"
      :master-health="masterHealth"
      @task-started="$emit('task-started')"
      @refresh="$emit('refresh')" />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import TaskPanel from './TaskPanel.vue'
import MasterHealthBanner from './MasterHealthBanner.vue'

const props = defineProps({
  runningTask: Object,
  adminStatus: Object,
  masterHealth: { type: Object, default: null },
  status: { type: Object, default: null },
})

defineEmits(['task-started', 'refresh', 'reload-master-health'])

const minGraceUntil = computed(() => {
  let min = null
  for (const acc of props.status?.accounts || []) {
    if (acc.status === 'degraded_grace' && typeof acc.grace_until === 'number') {
      if (min === null || acc.grace_until < min) min = acc.grace_until
    }
  }
  return min
})
</script>
