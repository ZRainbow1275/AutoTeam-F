<template>
  <div>
    <h2 class="text-xl font-bold text-white mb-2">账号池操作</h2>
    <p class="text-sm text-gray-400 mb-6">
      这里集中放轮转、检查、补满、添加、清理等会直接影响账号池状态的操作。
    </p>
    <!-- Round 8 — master degraded 提示横幅(spec §6.4) -->
    <div
      v-if="masterHealth && masterHealth.healthy === false && masterHealth.reason === 'subscription_cancelled'"
      class="mb-4 px-4 py-3 rounded-lg text-sm bg-red-500/10 text-red-300 border border-red-500/30"
    >
      母号 ChatGPT Team 订阅已 cancel,「生成免费号」按钮已禁用。请到「设置」页处理后再试。
    </div>
    <TaskPanel
      mode="pool"
      :running-task="runningTask"
      :admin-status="adminStatus"
      :master-health="masterHealth"
      @task-started="$emit('task-started')"
      @refresh="$emit('refresh')"
    />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
import TaskPanel from './TaskPanel.vue'

const props = defineProps({
  runningTask: Object,
  adminStatus: Object,
})

defineEmits(['task-started', 'refresh'])

// Round 8 — 母号订阅健康度(走 5min cache 不强刷)
const masterHealth = ref(null)
onMounted(async () => {
  if (!props.adminStatus?.configured) return
  try {
    masterHealth.value = await api.getMasterHealth(false)
  } catch (e) {
    console.error('加载母号健康度失败:', e)
  }
})
</script>
