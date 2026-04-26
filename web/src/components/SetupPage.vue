<template>
  <div class="min-h-screen flex items-center justify-center p-4">
    <div class="bg-gray-900 border border-gray-800 rounded-xl p-6 w-full max-w-2xl">
      <h1 class="text-xl font-bold text-white text-center mb-2">AutoTeam 初始配置</h1>
      <p class="text-sm text-gray-400 text-center mb-6">首次使用请按步骤填写以下配置</p>

      <div v-if="message" class="mb-4 px-4 py-3 rounded-lg text-sm border" :class="messageClass">
        {{ message }}
      </div>

      <!-- 卡片 1+2+3:Mail Provider 选择 / 连接 / 域名(Round 7 P2.2 抽 MailProviderCard) -->
      <MailProviderCard
        v-model="form"
        mode="setup"
        @state-change="onMailStateChange"
        @verified="onMailVerified"
        @error="onMailError" />

      <!-- 卡片 4:其他配置 -->
      <div class="mb-4 p-4 rounded-lg border" :class="cardClass('SAVE')">
        <h2 class="text-sm font-semibold text-white mb-3">4. 其他配置</h2>
        <div class="space-y-3" :class="state !== 'SAVE' ? 'opacity-40 pointer-events-none' : ''">
          <div v-for="field in otherFields" :key="field.key">
            <label class="block text-xs text-gray-400 mb-1">
              {{ field.prompt }}
              <span v-if="!field.optional" class="text-red-400">*</span>
              <span v-if="field.key === 'API_KEY'" class="text-gray-500 text-[10px] ml-1">(留空自动生成)</span>
            </label>
            <input
              v-model="form[field.key]"
              :type="field.key.includes('PASSWORD') || field.key.includes('KEY') ? 'password' : 'text'"
              :placeholder="field.default || ''"
              class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-sm text-white" />
          </div>
        </div>
      </div>

      <button
        @click="save"
        :disabled="saving || state !== 'SAVE'"
        class="w-full px-4 py-2.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium rounded-lg transition">
        {{ saving ? '验证并保存中...' : '保存配置' }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { api, setApiKey } from '../api.js'
import MailProviderCard from './MailProviderCard.vue'

const emit = defineEmits(['configured'])

// 状态机:PROVIDER → CONNECTION → DOMAIN → SAVE (SPEC-1 §4.1)
// Round 7 P2.2 — 状态机已下沉到 MailProviderCard,父组件只持有 state 联动 SAVE 卡片解锁。
const state = ref('PROVIDER')
const form = reactive({
  MAIL_PROVIDER: 'cf_temp_email',
  CLOUDMAIL_BASE_URL: '',
  CLOUDMAIL_PASSWORD: '',
  CLOUDMAIL_DOMAIN: '',
  MAILLAB_API_URL: '',
  MAILLAB_USERNAME: '',
  MAILLAB_PASSWORD: '',
  MAILLAB_DOMAIN: '',
  CPA_URL: 'http://127.0.0.1:8317',
  CPA_KEY: '',
  PLAYWRIGHT_PROXY_URL: '',
  PLAYWRIGHT_PROXY_BYPASS: '',
  API_KEY: '',
})

const fields = ref([])
const saving = ref(false)
const message = ref('')
const messageClass = ref('')

const otherFields = computed(() => fields.value.filter(f =>
  !['MAIL_PROVIDER', 'CLOUDMAIL_BASE_URL', 'CLOUDMAIL_EMAIL', 'CLOUDMAIL_PASSWORD', 'CLOUDMAIL_DOMAIN',
    'MAILLAB_API_URL', 'MAILLAB_USERNAME', 'MAILLAB_PASSWORD', 'MAILLAB_DOMAIN'].includes(f.key)
))

function cardClass(targetState) {
  const order = ['PROVIDER', 'CONNECTION', 'DOMAIN', 'SAVE']
  const cur = order.indexOf(state.value)
  const tgt = order.indexOf(targetState)
  if (tgt > cur) return 'bg-gray-800/20 border-gray-800'
  if (tgt < cur) return 'bg-green-900/10 border-green-900/40'
  return 'bg-blue-900/10 border-blue-700/40'
}

function onMailStateChange(s) {
  state.value = s
}

function onMailVerified(payload) {
  if (payload?.leakedProbe) {
    message.value = `⚠️ 探测邮箱回收失败,请到后台手动删除: ${payload.leakedProbe.email}`
    messageClass.value = 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20'
  }
}

function onMailError(resp) {
  if (resp?.soft && resp.warnings) {
    message.value = '⚠️ ' + resp.warnings.join('; ')
    messageClass.value = 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20'
    return
  }
  message.value = `${resp.error_code || 'ERROR'}: ${resp.message || '未知错误'}` +
    (resp.hint ? ` — ${resp.hint}` : '')
  messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
}

async function save() {
  saving.value = true
  message.value = ''
  try {
    const result = await api.saveSetup({ ...form })
    if (result.api_key) setApiKey(result.api_key)
    message.value = result.message || '保存成功'
    messageClass.value = 'bg-green-500/10 text-green-400 border-green-500/20'
    setTimeout(() => emit('configured'), 1000)
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  try {
    const result = await api.getSetupStatus()
    fields.value = result.fields
    for (const f of result.fields) {
      if (form[f.key] === '' || form[f.key] === undefined) {
        form[f.key] = f.default || ''
      }
    }
    if (result.provider) form.MAIL_PROVIDER = result.provider
  } catch (e) {
    message.value = '获取配置状态失败: ' + e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  }
})
</script>
