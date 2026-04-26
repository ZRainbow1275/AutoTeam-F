<!--
  MailProviderCard.vue — Round 7 P2.2:把 SetupPage.vue + Settings.vue 各自重复的
  testConnection / verifyDomain 状态机抽到一个共享组件,改一处即生效。

  契约(SPEC-1 §1):
    props.modelValue — 父组件 form 对象(MAIL_PROVIDER / CLOUDMAIL_BASE_URL / ... 共 8 字段)
    props.mode       — 'setup' | 'settings',控制 UI 提示语 / 完成后跳转语义
    emits update:modelValue   — v-model 双向绑定支持
    emits state-change(state) — 状态机切换通知,父组件可联动 SAVE 解锁等
    emits verified(payload)   — 域名验证完成的回调(payload: { domain, detectedProvider, leakedProbe })
    emits error(resp)         — 通用 error 上抛(给父组件按 mode 自定义提示策略)
-->
<template>
  <div class="mail-provider-card">
    <!-- 步骤 1:Provider 选择 -->
    <div class="mb-3 p-3 bg-gray-800/40 border border-gray-800 rounded">
      <div class="flex items-center justify-between mb-2">
        <span class="text-sm text-white">{{ mode === 'setup' ? '1. 邮箱后端' : '1. 后端类型' }}</span>
        <span class="text-xs" :class="state === 'PROVIDER' ? 'text-yellow-400' : 'text-green-400'">
          {{ state === 'PROVIDER' ? '请选择' : '已选 ' + form.MAIL_PROVIDER }}
        </span>
      </div>
      <div class="flex gap-2">
        <button
          v-for="opt in providerOptions" :key="opt.value"
          @click="selectProvider(opt.value)"
          :class="form.MAIL_PROVIDER === opt.value
            ? 'bg-blue-600 border-blue-500 text-white'
            : 'bg-gray-800 border-gray-700 text-gray-300'"
          class="flex-1 px-3 py-2 border rounded text-sm transition">
          <div class="font-medium">{{ opt.label }}</div>
          <div class="text-xs opacity-75 mt-0.5">{{ opt.desc }}</div>
        </button>
      </div>
    </div>

    <!-- 步骤 2:服务器连接 -->
    <div class="mb-3 p-3 rounded border" :class="cardClass('CONNECTION')">
      <div class="flex items-center justify-between mb-2">
        <span class="text-sm text-white">2. 服务器连接</span>
        <span class="text-xs" :class="connectionStatusClass">{{ connectionStatus }}</span>
      </div>
      <div class="space-y-2" :class="state === 'PROVIDER' ? 'opacity-40 pointer-events-none' : ''">
        <input
          v-model="form[baseUrlKey]"
          type="text"
          :placeholder="form.MAIL_PROVIDER === 'maillab' ? 'https://your-maillab.example.com' : 'https://example.com/api'"
          class="w-full px-2 py-1.5 bg-gray-800 border border-gray-700 rounded text-sm text-white" />
        <input
          v-if="form.MAIL_PROVIDER === 'maillab'"
          v-model="form.MAILLAB_USERNAME"
          type="text"
          placeholder="管理员邮箱 (MAILLAB_USERNAME)"
          class="w-full px-2 py-1.5 bg-gray-800 border border-gray-700 rounded text-sm text-white" />
        <input
          v-model="form[passwordKey]"
          type="password"
          :placeholder="passwordPlaceholder"
          class="w-full px-2 py-1.5 bg-gray-800 border border-gray-700 rounded text-sm text-white" />
        <button
          @click="testConnection"
          :disabled="testing || !canTestConnection"
          class="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-xs rounded">
          {{ testing ? '测试中...' : '测试连接' }}
        </button>
      </div>
    </div>

    <!-- 步骤 3:域名归属 -->
    <div class="mb-3 p-3 rounded border" :class="cardClass('DOMAIN')">
      <div class="flex items-center justify-between mb-2">
        <span class="text-sm text-white">3. 域名归属</span>
        <span class="text-xs" :class="domainStatusClass">{{ domainStatus }}</span>
      </div>
      <div class="space-y-2" :class="!canEnterDomain ? 'opacity-40 pointer-events-none' : ''">
        <select
          v-if="domainList && domainList.length"
          v-model="selectedDomain"
          class="w-full px-2 py-1.5 bg-gray-800 border border-gray-700 rounded text-sm text-white">
          <option v-for="d in domainList" :key="d" :value="stripAt(d)">{{ d }}</option>
        </select>
        <input
          v-else
          v-model="selectedDomain"
          type="text"
          placeholder="example.com"
          class="w-full px-2 py-1.5 bg-gray-800 border border-gray-700 rounded text-sm text-white" />
        <button
          @click="verifyDomain"
          :disabled="verifying || !selectedDomain"
          class="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-xs rounded">
          {{ verifying ? '验证中...' : '验证归属' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({
  modelValue: { type: Object, required: true },
  mode: { type: String, default: 'setup' },
})

const emit = defineEmits(['update:modelValue', 'state-change', 'verified', 'error'])

// form 通过 v-model 双向绑定;直接 mutate 内部对象再 emit 完整对象保持 reactivity。
const form = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const state = ref('PROVIDER')
const testing = ref(false)
const verifying = ref(false)
const detectedProvider = ref(null)
const domainList = ref(null)
const selectedDomain = ref('')

const providerOptions = [
  { value: 'cf_temp_email', label: 'cf_temp_email', desc: 'dreamhunter2333/cloudflare_temp_email' },
  { value: 'maillab', label: 'maillab', desc: 'maillab/cloud-mail (skymail.ink)' },
]

const baseUrlKey = computed(() => form.value.MAIL_PROVIDER === 'maillab' ? 'MAILLAB_API_URL' : 'CLOUDMAIL_BASE_URL')
const passwordKey = computed(() => form.value.MAIL_PROVIDER === 'maillab' ? 'MAILLAB_PASSWORD' : 'CLOUDMAIL_PASSWORD')
const domainKey = computed(() => form.value.MAIL_PROVIDER === 'maillab' ? 'MAILLAB_DOMAIN' : 'CLOUDMAIL_DOMAIN')

const passwordPlaceholder = computed(() => {
  if (props.mode === 'settings') {
    return passwordKey.value + ' (留空表示沿用旧密码)'
  }
  return passwordKey.value
})

const canTestConnection = computed(() => {
  const baseOk = !!form.value[baseUrlKey.value]
  const pwdOk = !!form.value[passwordKey.value]
  if (form.value.MAIL_PROVIDER === 'maillab') {
    return baseOk && !!form.value.MAILLAB_USERNAME && pwdOk
  }
  return baseOk && pwdOk
})

const canEnterDomain = computed(() => state.value === 'DOMAIN' || state.value === 'SAVE')

const connectionStatus = computed(() => {
  if (state.value === 'PROVIDER') return '待选 provider'
  if (testing.value) return '测试中...'
  if (state.value === 'CONNECTION') return '请测试连接'
  return detectedProvider.value ? `已通过 (${detectedProvider.value})` : '已通过'
})
const connectionStatusClass = computed(() => {
  if (state.value === 'PROVIDER' || state.value === 'CONNECTION') return 'text-yellow-400'
  return 'text-green-400'
})

const domainStatus = computed(() => {
  if (!canEnterDomain.value) return '需先通过测试连接'
  if (verifying.value) return '验证中...'
  if (state.value === 'DOMAIN') return '请验证域名归属'
  return '已通过'
})
const domainStatusClass = computed(() => state.value === 'SAVE' ? 'text-green-400' : 'text-yellow-400')

watch(state, (s) => emit('state-change', s))

function cardClass(targetState) {
  const order = ['PROVIDER', 'CONNECTION', 'DOMAIN', 'SAVE']
  const cur = order.indexOf(state.value)
  const tgt = order.indexOf(targetState)
  if (tgt > cur) return 'bg-gray-800/20 border-gray-800'
  if (tgt < cur) return 'bg-green-900/10 border-green-900/40'
  return 'bg-blue-900/10 border-blue-700/40'
}

function stripAt(d) {
  return (d || '').replace(/^@/, '').trim()
}

function selectProvider(value) {
  form.value.MAIL_PROVIDER = value
  state.value = 'CONNECTION'
  detectedProvider.value = null
  domainList.value = null
  selectedDomain.value = ''
}

async function testConnection() {
  testing.value = true
  try {
    const fp = await api.probeMailProvider({
      provider: form.value.MAIL_PROVIDER,
      step: 'fingerprint',
      base_url: form.value[baseUrlKey.value],
    })
    if (!fp.ok) { emit('error', fp); return }
    detectedProvider.value = fp.detected_provider
    domainList.value = fp.domain_list
    if (fp.warnings && fp.warnings.length) {
      emit('error', { warnings: fp.warnings, soft: true })
    }

    const cred = await api.probeMailProvider({
      provider: form.value.MAIL_PROVIDER,
      step: 'credentials',
      base_url: form.value[baseUrlKey.value],
      username: form.value.MAILLAB_USERNAME,
      password: form.value.MAILLAB_PASSWORD,
      admin_password: form.value.CLOUDMAIL_PASSWORD,
    })
    if (!cred.ok) { emit('error', cred); return }
    state.value = 'DOMAIN'

    if (domainList.value && domainList.value.length === 1) {
      selectedDomain.value = stripAt(domainList.value[0])
    }
  } catch (e) {
    emit('error', { error_code: 'REQUEST_FAILED', message: e.message })
  } finally {
    testing.value = false
  }
}

async function verifyDomain() {
  verifying.value = true
  try {
    const own = await api.probeMailProvider({
      provider: form.value.MAIL_PROVIDER,
      step: 'domain_ownership',
      base_url: form.value[baseUrlKey.value],
      username: form.value.MAILLAB_USERNAME,
      password: form.value.MAILLAB_PASSWORD,
      admin_password: form.value.CLOUDMAIL_PASSWORD,
      domain: selectedDomain.value,
    })
    if (!own.ok) { emit('error', own); return }
    form.value[domainKey.value] = '@' + selectedDomain.value
    if (form.value.MAIL_PROVIDER === 'maillab') {
      form.value.CLOUDMAIL_DOMAIN = form.value.CLOUDMAIL_DOMAIN || ('@' + selectedDomain.value)
    }
    state.value = 'SAVE'
    emit('verified', {
      domain: selectedDomain.value,
      detectedProvider: detectedProvider.value,
      leakedProbe: own.cleaned === false ? own.leaked_probe : null,
    })
  } catch (e) {
    emit('error', { error_code: 'REQUEST_FAILED', message: e.message })
  } finally {
    verifying.value = false
  }
}

defineExpose({ state, reset: () => {
  state.value = 'PROVIDER'
  detectedProvider.value = null
  domainList.value = null
  selectedDomain.value = ''
} })
</script>
