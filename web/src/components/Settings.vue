<template>
  <div class="space-y-5">
    <div>
      <div class="text-[10px] uppercase tracking-[0.3em] text-indigo-300/70 mb-1">Configuration</div>
      <h2 class="text-2xl font-extrabold text-white tracking-tight">设置</h2>
      <p class="text-sm text-gray-400 mt-1">管理员登录、巡检策略、邮箱后端、生命周期参数,皆可在此调整。</p>
    </div>

    <!-- F3 — 共享父级 master-health banner -->
    <MasterHealthBanner
      :master-health="masterHealth"
      :min-grace-until="minGraceUntil"
      :loading="masterHealthLoading"
      @refresh="onReloadMasterHealth" />

    <div class="glass rounded-2xl p-5">
      <div class="flex items-center justify-between gap-4 mb-4">
        <div>
          <h2 class="text-lg font-semibold text-white">管理员登录</h2>
          <p class="text-sm text-gray-400 mt-1">
            首次启动先在这里完成主号登录，系统会统一写入单个 state.json 文件，保存邮箱、session、workspace ID、workspace 名称；如果你走了密码登录，也会保留密码供主号 Codex 复用。
          </p>
        </div>
        <span
          class="min-w-[72px] px-3 py-1.5 rounded-full text-xs text-center whitespace-nowrap border"
          :class="adminConfigured
            ? 'bg-green-500/10 text-green-400 border-green-500/20'
            : adminBusy
              ? 'bg-yellow-500/10 text-yellow-300 border-yellow-500/20'
              : 'bg-gray-800 text-gray-400 border-gray-700'"
        >
          {{ adminConfigured ? '已配置' : adminBusy ? '登录中' : '未配置' }}
        </span>
      </div>

      <div v-if="message" class="mb-4 px-4 py-3 rounded-lg text-sm border" :class="messageClass">
        {{ message }}
      </div>

      <div v-if="adminConfigured && !adminBusy" class="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
        <div class="px-3 py-3 bg-gray-800/60 border border-gray-800 rounded-lg">
          <div class="text-gray-500 mb-1">管理员邮箱</div>
          <div class="font-mono text-white break-all">{{ props.adminStatus?.email || '-' }}</div>
        </div>
        <div class="px-3 py-3 bg-gray-800/60 border border-gray-800 rounded-lg">
          <div class="text-gray-500 mb-1">Workspace ID</div>
          <div class="font-mono text-white break-all">{{ props.adminStatus?.account_id || '-' }}</div>
        </div>
        <div class="px-3 py-3 bg-gray-800/60 border border-gray-800 rounded-lg md:col-span-2">
          <div class="text-gray-500 mb-1">Workspace 名称</div>
          <div class="text-white">{{ props.adminStatus?.workspace_name || '未识别' }}</div>
        </div>
        <div class="px-3 py-3 bg-gray-800/60 border border-gray-800 rounded-lg md:col-span-2">
          <div class="text-gray-500 mb-1">Session Token</div>
          <div v-if="props.adminStatus?.session_present" class="text-green-400 text-xs">已配置</div>
          <div v-else class="space-y-2">
            <div class="text-amber-400 text-xs">未配置（Team 管理功能需要 session token）</div>
            <div class="text-gray-400 text-xs space-y-2">
              <div>获取方式：</div>
              <ol class="list-decimal list-inside space-y-1">
                <li>
                  在浏览器中打开
                  <a href="https://chatgpt.com" target="_blank" rel="noreferrer" class="text-blue-400 hover:underline">
                    chatgpt.com
                  </a>
                  并登录管理员账号
                </li>
                <li>按 F12 打开开发者工具 → Application → Cookies → chatgpt.com</li>
                <li>找到 <code class="bg-gray-800 px-1 rounded">__Secure-next-auth.session-token</code></li>
                <li>
                  如果有 <code class="bg-gray-800 px-1 rounded">.0</code> 和
                  <code class="bg-gray-800 px-1 rounded">.1</code> 两个，将值按顺序拼接在一起
                </li>
                <li>粘贴到下方输入框</li>
              </ol>
            </div>
            <div class="space-y-2">
              <input
                v-model.trim="sessionToken"
                type="password"
                placeholder="粘贴 session token"
                class="w-full px-2 py-1.5 bg-gray-800 border border-gray-700 rounded text-xs text-white font-mono focus:outline-none focus:border-blue-500"
              />
              <div class="flex justify-end">
                <button
                  @click="importSessionToken"
                  :disabled="submitting || !sessionEmail || !sessionToken"
                  class="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs rounded transition disabled:opacity-50"
                >
                  {{ submitting ? '校验中...' : '保存' }}
                </button>
              </div>
            </div>
          </div>
        </div>
        <div class="px-3 py-3 bg-gray-800/60 border border-gray-800 rounded-lg md:col-span-2">
          <div class="text-gray-500 mb-1">管理员密码</div>
          <div class="text-white">{{ props.adminStatus?.password_saved ? '已保存，可用于主号 Codex 登录' : '未保存' }}</div>
        </div>
      </div>

      <div v-if="!adminBusy" class="mt-4">
        <div v-if="!adminConfigured" class="space-y-4">
          <div class="flex flex-col sm:flex-row gap-3">
            <input
              v-model.trim="email"
              type="email"
              autocomplete="username"
              placeholder="输入主号邮箱"
              class="flex-1 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500"
            />
            <button
              @click="startLogin"
              :disabled="submitting || !email"
              class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:opacity-50"
            >
              {{ submitting ? '提交中...' : '开始登录' }}
            </button>
          </div>

          <div class="border border-gray-800 rounded-xl p-4 bg-gray-800/30">
            <div class="text-sm font-medium text-white">或手动导入 session_token</div>
            <p class="text-xs text-gray-400 mt-1 mb-3">
              适合你已经在浏览器里拿到 <span class="font-mono">__Secure-next-auth.session-token</span> 的场景。系统会校验 token，并自动识别 workspace ID / 名称。
            </p>
            <div class="text-gray-400 text-xs space-y-2 mb-3">
              <div>获取方式：</div>
              <ol class="list-decimal list-inside space-y-1">
                <li>
                  在浏览器中打开
                  <a href="https://chatgpt.com" target="_blank" rel="noreferrer" class="text-blue-400 hover:underline">
                    chatgpt.com
                  </a>
                  并登录管理员账号
                </li>
                <li>按 F12 打开开发者工具 → Application → Cookies → chatgpt.com</li>
                <li>找到 <code class="bg-gray-800 px-1 rounded">__Secure-next-auth.session-token</code></li>
                <li>
                  如果有 <code class="bg-gray-800 px-1 rounded">.0</code> 和
                  <code class="bg-gray-800 px-1 rounded">.1</code> 两个，将值按顺序拼接在一起
                </li>
                <li>粘贴到下方输入框</li>
              </ol>
            </div>
            <div class="space-y-3">
              <input
                v-model.trim="sessionEmail"
                type="email"
                autocomplete="username"
                placeholder="输入主号邮箱"
                class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-cyan-500"
              />
              <textarea
                v-model.trim="sessionToken"
                rows="4"
                spellcheck="false"
                placeholder="粘贴完整 session_token"
                class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white font-mono focus:outline-none focus:border-cyan-500"
              ></textarea>
              <div class="flex justify-end">
                <button
                  @click="importSessionToken"
                  :disabled="submitting || !sessionEmail || !sessionToken"
                  class="px-4 py-2 bg-cyan-700 hover:bg-cyan-600 text-white text-sm rounded-lg transition disabled:opacity-50"
                >
                  {{ submitting ? '校验中...' : '导入 session_token' }}
                </button>
              </div>
            </div>
          </div>
        </div>

        <div v-else-if="!codexBusy" class="flex flex-wrap gap-3">
          <button
            @click="syncMainCodex"
            :disabled="submitting || syncingMain"
            class="px-4 py-2 bg-cyan-700 hover:bg-cyan-600 text-white text-sm rounded-lg transition disabled:opacity-50"
          >
            {{ syncingMain ? '同步中...' : '同步主号 Codex 到 CPA' }}
          </button>
          <button
            @click="logoutAdmin"
            :disabled="submitting || syncingMain"
            class="px-4 py-2 bg-rose-700/80 hover:bg-rose-700 text-white text-sm rounded-lg transition disabled:opacity-50"
          >
            {{ submitting ? '处理中...' : '清除登录态' }}
          </button>
        </div>
      </div>

      <div v-if="adminBusy" class="space-y-4">
        <div class="text-sm text-gray-300">
          当前邮箱: <span class="font-mono">{{ loginEmail || props.adminStatus?.email || '-' }}</span>
        </div>

        <div v-if="props.adminStatus?.login_step === 'password_required'" class="flex flex-col sm:flex-row gap-3">
          <input
            v-model="password"
            type="password"
            autocomplete="current-password"
            placeholder="输入主号密码"
            :disabled="submitting"
            class="flex-1 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500"
          />
          <button
            @click="submitPassword"
            :disabled="submitting || !password"
            class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:opacity-50"
          >
            {{ submitting ? '提交中...' : '提交密码' }}
          </button>
        </div>

        <div v-else-if="props.adminStatus?.login_step === 'code_required'" class="flex flex-col sm:flex-row gap-3">
          <input
            v-model.trim="code"
            type="text"
            inputmode="numeric"
            autocomplete="one-time-code"
            placeholder="输入邮箱验证码"
            :disabled="submitting"
            class="flex-1 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500"
          />
          <button
            @click="submitCode"
            :disabled="submitting || !code"
            class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:opacity-50 disabled:bg-gray-700 disabled:hover:bg-gray-700"
          >
            {{ submitting ? '提交中...' : '提交验证码' }}
          </button>
        </div>

        <div v-else-if="props.adminStatus?.login_step === 'workspace_required'" class="space-y-3">
          <div class="text-sm text-gray-300">
            请选择要进入的组织 / workspace
          </div>
          <select
            v-model="workspaceOptionId"
            :disabled="submitting"
            class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500"
          >
            <option disabled value="">请选择组织</option>
            <option
              v-for="opt in props.adminStatus?.workspace_options || []"
              :key="opt.id"
              :value="opt.id"
            >
              {{ opt.label }}{{ opt.kind === 'fallback' ? ' (可能是个人/免费)' : '' }}
            </option>
          </select>
          <button
            @click="submitWorkspace"
            :disabled="submitting || !workspaceOptionId"
            class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:opacity-50 disabled:bg-gray-700 disabled:hover:bg-gray-700"
          >
            {{ submitting ? '提交中...' : '确认组织选择' }}
          </button>
        </div>

        <div v-if="submitting && adminSubmittingHint" class="text-xs text-blue-300">
          {{ adminSubmittingHint }}
        </div>

        <div class="flex justify-end">
          <button
            @click="cancelLogin"
            :disabled="submitting"
            class="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-sm text-gray-200 rounded-lg border border-gray-700 transition disabled:opacity-50"
          >
            取消登录
          </button>
        </div>
      </div>

      <div v-if="codexBusy" class="mt-4 space-y-4 border-t border-gray-800 pt-4">
        <div class="text-sm text-gray-300">
          主号 Codex 登录继续中
        </div>

        <div v-if="props.codexStatus?.step === 'password_required'" class="flex flex-col sm:flex-row gap-3">
          <input
            v-model="codexPassword"
            type="password"
            autocomplete="current-password"
            placeholder="输入主号密码"
            :disabled="syncingMain"
            class="flex-1 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500"
          />
          <button
            @click="submitMainCodexPassword"
            :disabled="syncingMain || !codexPassword"
            class="px-4 py-2 bg-cyan-700 hover:bg-cyan-600 text-white text-sm rounded-lg transition disabled:opacity-50"
          >
            {{ syncingMain ? '提交中...' : '提交密码' }}
          </button>
        </div>

        <div v-else-if="props.codexStatus?.step === 'code_required'" class="flex flex-col sm:flex-row gap-3">
          <input
            v-model.trim="codexCode"
            type="text"
            inputmode="numeric"
            autocomplete="one-time-code"
            placeholder="输入主号 Codex 验证码"
            :disabled="syncingMain"
            class="flex-1 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500"
          />
          <button
            @click="submitMainCodexCode"
            :disabled="syncingMain || !codexCode"
            class="px-4 py-2 bg-cyan-700 hover:bg-cyan-600 text-white text-sm rounded-lg transition disabled:opacity-50"
          >
            {{ syncingMain ? '提交中...' : '提交验证码' }}
          </button>
        </div>

        <div v-if="syncingMain && codexSubmittingHint" class="text-xs text-cyan-300">
          {{ codexSubmittingHint }}
        </div>

        <div class="flex justify-end">
          <button
            @click="cancelMainCodexSync"
            :disabled="syncingMain"
            class="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-sm text-gray-200 rounded-lg border border-gray-700 transition disabled:opacity-50"
          >
            取消主号 Codex 登录
          </button>
        </div>
      </div>
    </div>

    <div class="glass rounded-2xl p-5">
      <div class="flex items-center justify-between mb-4">
        <h2 class="text-lg font-semibold text-white">巡检设置</h2>
        <span v-if="saved" class="text-xs text-green-400 transition">已保存</span>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div>
          <label class="block text-sm text-gray-400 mb-1">巡检间隔</label>
          <div class="flex items-center gap-2">
            <input v-model.number="form.interval" type="number" min="1"
              class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" />
            <span class="text-sm text-gray-500 shrink-0">分钟</span>
          </div>
        </div>
        <div>
          <label class="block text-sm text-gray-400 mb-1">额度阈值</label>
          <div class="flex items-center gap-2">
            <input v-model.number="form.threshold" type="number" min="1" max="100"
              class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" />
            <span class="text-sm text-gray-500 shrink-0">%</span>
          </div>
        </div>
        <div>
          <label class="block text-sm text-gray-400 mb-1">触发账号数</label>
          <div class="flex items-center gap-2">
            <input v-model.number="form.min_low" type="number" min="1"
              class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" />
            <span class="text-sm text-gray-500 shrink-0">个</span>
          </div>
        </div>
      </div>

      <div class="mt-3 flex items-center justify-between gap-3">
        <p class="text-xs text-gray-500">
          每 {{ form.interval }} 分钟检查一次，{{ form.min_low }} 个以上账号剩余低于 {{ form.threshold }}% 时自动轮转
        </p>
        <button @click="save" :disabled="saving"
          class="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:opacity-50">
          {{ saving ? '保存中...' : '保存' }}
        </button>
      </div>
    </div>

    <!-- SPEC-1 §FR-008 / AC-017 — 邮箱后端切换 (Settings 复用 SetupPage 4 步状态机) -->
    <div class="glass rounded-2xl p-5">
      <div class="flex items-center justify-between gap-4 mb-4">
        <div>
          <h2 class="text-lg font-semibold text-white">邮箱后端</h2>
          <p class="text-sm text-gray-400 mt-1">
            切换或重新配置临时邮箱服务（cf_temp_email / maillab）。SetupPage 完成后所有字段已落盘，这里仅在需要更换服务时使用。
          </p>
        </div>
        <span
          class="min-w-[72px] px-3 py-1.5 rounded-full text-xs text-center whitespace-nowrap border"
          :class="mailFormDirty
            ? 'bg-yellow-500/10 text-yellow-300 border-yellow-500/20'
            : 'bg-gray-800 text-gray-400 border-gray-700'"
        >
          {{ mailFormDirty ? '有改动' : '已配置' }}
        </span>
      </div>

      <div v-if="mailMessage" class="mb-4 px-4 py-3 rounded-lg text-sm border" :class="mailMessageClass">
        {{ mailMessage }}
      </div>

      <!-- 步骤 1+2+3:Provider 选择 / 连接 / 域名(Round 7 P2.2 抽 MailProviderCard) -->
      <MailProviderCard
        ref="mailCardRef"
        v-model="mailForm"
        mode="settings"
        @state-change="onMailStateChange"
        @verified="onMailVerified"
        @error="onMailError" />

      <div class="flex justify-end gap-3">
        <button
          @click="resetMailForm"
          :disabled="mailSaving"
          class="px-4 py-1.5 bg-gray-800 hover:bg-gray-700 text-sm text-gray-200 rounded-lg border border-gray-700 transition disabled:opacity-50">
          重置
        </button>
        <button
          @click="saveMailConfig"
          :disabled="mailSaving || mailState !== 'SAVE'"
          class="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:opacity-50">
          {{ mailSaving ? '保存中...' : '保存邮箱后端配置' }}
        </button>
      </div>
    </div>

    <!-- SPEC-2 §席位策略 + 探测节流 -->
    <div class="glass rounded-2xl p-5">
      <div class="mb-4">
        <h2 class="text-lg font-semibold text-white">账号生命周期</h2>
        <p class="text-sm text-gray-400 mt-1">
          控制邀请席位偏好(完整 ChatGPT 席位 vs 仅 Codex 席位),以及 sync 巡检识别"被踢"的探测节流。
        </p>
      </div>

      <div v-if="lifecycleMessage" class="mb-3 px-4 py-3 rounded-lg text-sm border" :class="lifecycleMessageClass">
        {{ lifecycleMessage }}
      </div>

      <div class="mb-4 p-3 bg-gray-800/40 border border-gray-800 rounded">
        <div class="flex items-center justify-between mb-2">
          <span class="text-sm text-white">邀请席位偏好</span>
          <span class="text-xs text-gray-400">{{ preferredSeatType === 'codex' ? '锁定 Codex 席位' : '优先 ChatGPT 完整席位' }}</span>
        </div>
        <div class="flex gap-2">
          <button
            @click="setPreferredSeatType('default')"
            :class="preferredSeatType === 'default' ? 'bg-blue-600 border-blue-500 text-white' : 'bg-gray-800 border-gray-700 text-gray-300'"
            class="flex-1 px-3 py-2 border rounded text-sm transition">
            <div class="font-medium">default</div>
            <div class="text-xs opacity-75 mt-0.5">优先 PATCH 升级到 ChatGPT 席位(老行为)</div>
          </button>
          <button
            @click="setPreferredSeatType('codex')"
            :class="preferredSeatType === 'codex' ? 'bg-blue-600 border-blue-500 text-white' : 'bg-gray-800 border-gray-700 text-gray-300'"
            class="flex-1 px-3 py-2 border rounded text-sm transition">
            <div class="font-medium">codex</div>
            <div class="text-xs opacity-75 mt-0.5">锁 codex-only 席位,跳过 PATCH(节约 ChatGPT 席位)</div>
          </button>
        </div>
      </div>

      <div class="p-3 bg-gray-800/40 border border-gray-800 rounded">
        <div class="text-sm text-white mb-2">sync 探测节流</div>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <div class="text-xs text-gray-400 mb-1">并发上限 (1-16)</div>
            <input
              v-model.number="syncProbeConcurrency"
              type="number"
              min="1" max="16"
              class="w-full px-2 py-1.5 bg-gray-800 border border-gray-700 rounded text-sm text-white" />
          </div>
          <div>
            <div class="text-xs text-gray-400 mb-1">同号去重冷却 (分钟,1-1440)</div>
            <input
              v-model.number="syncProbeCooldown"
              type="number"
              min="1" max="1440"
              class="w-full px-2 py-1.5 bg-gray-800 border border-gray-700 rounded text-sm text-white" />
          </div>
        </div>
        <div class="flex justify-end mt-3">
          <button
            @click="saveSyncProbe"
            :disabled="lifecycleSaving"
            class="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-xs rounded">
            {{ lifecycleSaving ? '保存中...' : '保存探测节流' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch, onMounted } from 'vue'
import { api } from '../api.js'
import MailProviderCard from './MailProviderCard.vue'
import MasterHealthBanner from './MasterHealthBanner.vue'

const props = defineProps({
  adminStatus: { type: Object, default: null },
  codexStatus: { type: Object, default: null },
  masterHealth: { type: Object, default: null },
  status: { type: Object, default: null },
})

const emit = defineEmits(['refresh', 'admin-progress', 'reload-master-health'])

// F3 — minGraceUntil 派生(父级传 status,这里取最早 grace_until)
const minGraceUntil = computed(() => {
  let min = null
  for (const acc of props.status?.accounts || []) {
    if (acc.status === 'degraded_grace' && typeof acc.grace_until === 'number') {
      if (min === null || acc.grace_until < min) min = acc.grace_until
    }
  }
  return min
})

const masterHealthLoading = ref(false)
async function onReloadMasterHealth() {
  masterHealthLoading.value = true
  try { emit('reload-master-health', true) }
  finally { setTimeout(() => { masterHealthLoading.value = false }, 1200) }
}

const form = ref({ interval: 5, threshold: 10, min_low: 2 })
const saving = ref(false)
const saved = ref(false)

const email = ref('')
const sessionEmail = ref('')
const sessionToken = ref('')
const password = ref('')
const code = ref('')
const workspaceOptionId = ref('')
const loginEmail = ref('')
const codexPassword = ref('')
const codexCode = ref('')
const submitting = ref(false)
const syncingMain = ref(false)
const message = ref('')
const messageClass = ref('')
const adminSubmittingHint = ref('')
const codexSubmittingHint = ref('')

const adminConfigured = computed(() => !!props.adminStatus?.configured)
const adminBusy = computed(() => !!props.adminStatus?.login_in_progress)
const codexBusy = computed(() => !!props.codexStatus?.in_progress)

watch(
  () => props.adminStatus,
  (next) => {
    if (next?.configured && next.email) {
      email.value = next.email
      sessionEmail.value = next.email
    }
    if (!next?.login_in_progress) {
      password.value = ''
      code.value = ''
      workspaceOptionId.value = ''
      adminSubmittingHint.value = ''
      loginEmail.value = next?.email || loginEmail.value
    }
    if (next?.login_step === 'workspace_required' && !workspaceOptionId.value) {
      const preferred = next?.workspace_options?.find(opt => opt.kind === 'preferred')
      workspaceOptionId.value = preferred?.id || next?.workspace_options?.[0]?.id || ''
    }
  },
  { immediate: true },
)

watch(
  () => props.codexStatus,
  (next) => {
    if (!next?.in_progress) {
      codexPassword.value = ''
      codexCode.value = ''
      codexSubmittingHint.value = ''
    }
  },
  { immediate: true },
)

// SPEC-2 — 席位偏好 + sync 探测节流(本块独立于上面的 mailForm)
const preferredSeatType = ref('default')
const syncProbeConcurrency = ref(5)
const syncProbeCooldown = ref(30)
const lifecycleSaving = ref(false)
const lifecycleMessage = ref('')
const lifecycleMessageClass = ref('')

function setLifecycleMessage(text, type = 'success') {
  lifecycleMessage.value = text
  lifecycleMessageClass.value = type === 'success'
    ? 'bg-green-500/10 text-green-400 border-green-500/20'
    : 'bg-red-500/10 text-red-400 border-red-500/20'
  window.clearTimeout(setLifecycleMessage._t)
  setLifecycleMessage._t = window.setTimeout(() => { lifecycleMessage.value = '' }, 8000)
}

async function setPreferredSeatType(value) {
  if (preferredSeatType.value === value) return
  lifecycleSaving.value = true
  try {
    const resp = await api.putPreferredSeatType(value)
    preferredSeatType.value = resp.value
    setLifecycleMessage(resp.message || `已设为 ${resp.value}`)
  } catch (e) {
    setLifecycleMessage(e.message || '保存失败', 'error')
  } finally {
    lifecycleSaving.value = false
  }
}

async function saveSyncProbe() {
  lifecycleSaving.value = true
  try {
    const resp = await api.putSyncProbe({
      concurrency: syncProbeConcurrency.value,
      cooldown_minutes: syncProbeCooldown.value,
    })
    syncProbeConcurrency.value = resp.concurrency
    syncProbeCooldown.value = resp.cooldown_minutes
    setLifecycleMessage(resp.message || '已保存')
  } catch (e) {
    setLifecycleMessage(e.message || '保存失败', 'error')
  } finally {
    lifecycleSaving.value = false
  }
}

onMounted(async () => {
  try {
    const cfg = await api.getAutoCheckConfig()
    form.value = {
      interval: Math.round(cfg.interval / 60),
      threshold: cfg.threshold,
      min_low: cfg.min_low,
    }
  } catch (e) {
    console.error('加载巡检配置失败:', e)
  }
  try {
    const seat = await api.getPreferredSeatType()
    preferredSeatType.value = seat?.value || 'default'
  } catch (e) {
    console.error('加载邀请席位偏好失败:', e)
  }
  try {
    const sp = await api.getSyncProbe()
    syncProbeConcurrency.value = sp?.concurrency ?? 5
    syncProbeCooldown.value = sp?.cooldown_minutes ?? 30
  } catch (e) {
    console.error('加载 sync 探测节流失败:', e)
  }
  // F3 — 母号订阅健康度由 App 级共享 props.masterHealth,不再本组件自管
})

function setMessage(text, type = 'success') {
  message.value = text
  messageClass.value = type === 'success'
    ? 'bg-green-500/10 text-green-400 border-green-500/20'
    : 'bg-red-500/10 text-red-400 border-red-500/20'
  window.clearTimeout(setMessage._timer)
  setMessage._timer = window.setTimeout(() => {
    message.value = ''
  }, 8000)
}

async function startLogin() {
  submitting.value = true
  adminSubmittingHint.value = '正在打开管理员登录页...'
  try {
    loginEmail.value = email.value
    const result = await api.startAdminLogin(email.value)
    setMessage(result.status === 'completed' ? '管理员登录完成' : '已进入下一步登录流程')
    emit('admin-progress')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
    adminSubmittingHint.value = ''
  }
}

async function importSessionToken() {
  submitting.value = true
  adminSubmittingHint.value = '正在校验 session_token 并识别 workspace...'
  try {
    loginEmail.value = sessionEmail.value
    const result = await api.submitAdminSession(sessionEmail.value, sessionToken.value)
    sessionToken.value = ''
    setMessage(result.status === 'completed' ? 'session_token 导入成功' : 'session_token 已提交')
    emit('refresh')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
    adminSubmittingHint.value = ''
  }
}

async function submitPassword() {
  submitting.value = true
  adminSubmittingHint.value = '密码已提交，正在等待登录页响应...'
  try {
    const result = await api.submitAdminPassword(password.value)
    setMessage(result.status === 'completed' ? '管理员登录完成' : '密码已提交，请继续下一步')
    emit('admin-progress')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
    adminSubmittingHint.value = ''
  }
}

async function submitCode() {
  submitting.value = true
  adminSubmittingHint.value = '验证码已提交，正在等待登录页响应，通常需要 5 到 10 秒...'
  try {
    const result = await api.submitAdminCode(code.value)
    setMessage(result.status === 'completed' ? '管理员登录完成' : '验证码已提交，请继续下一步')
    emit('admin-progress')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
    adminSubmittingHint.value = ''
  }
}

async function submitWorkspace() {
  submitting.value = true
  adminSubmittingHint.value = '组织选择已提交，正在等待登录页响应...'
  try {
    const result = await api.submitAdminWorkspace(workspaceOptionId.value)
    setMessage(result.status === 'completed' ? '管理员登录完成' : '组织选择已提交，请继续下一步')
    emit('admin-progress')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
    adminSubmittingHint.value = ''
  }
}

async function cancelLogin() {
  submitting.value = true
  try {
    await api.cancelAdminLogin()
    password.value = ''
    code.value = ''
    setMessage('管理员登录已取消')
    emit('refresh')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
  }
}

async function logoutAdmin() {
  submitting.value = true
  try {
    await api.logoutAdmin()
    password.value = ''
    code.value = ''
    setMessage('管理员登录态已清除')
    emit('refresh')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
  }
}

async function syncMainCodex() {
  syncingMain.value = true
  codexSubmittingHint.value = '正在打开主号 Codex 登录页...'
  try {
    const result = await api.startMainCodexSync()
    setMessage(result.status === 'completed' ? (result.message || '主号 Codex 已同步') : '主号 Codex 登录进入下一步')
    emit('admin-progress')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    syncingMain.value = false
    codexSubmittingHint.value = ''
  }
}

async function submitMainCodexPassword() {
  syncingMain.value = true
  codexSubmittingHint.value = '密码已提交，正在等待主号 Codex 登录页响应...'
  try {
    const result = await api.submitMainCodexPassword(codexPassword.value)
    setMessage(result.status === 'completed' ? (result.message || '主号 Codex 已同步') : '主号 Codex 密码已提交')
    emit('admin-progress')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    syncingMain.value = false
    codexSubmittingHint.value = ''
  }
}

async function submitMainCodexCode() {
  syncingMain.value = true
  codexSubmittingHint.value = '验证码已提交，正在等待主号 Codex 登录页响应，通常需要 5 到 10 秒...'
  try {
    const result = await api.submitMainCodexCode(codexCode.value)
    setMessage(result.status === 'completed' ? (result.message || '主号 Codex 已同步') : '主号 Codex 验证码已提交')
    emit('admin-progress')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    syncingMain.value = false
    codexSubmittingHint.value = ''
  }
}

async function cancelMainCodexSync() {
  syncingMain.value = true
  try {
    await api.cancelMainCodexSync()
    setMessage('主号 Codex 登录已取消')
    emit('refresh')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    syncingMain.value = false
  }
}

async function save() {
  saving.value = true
  saved.value = false
  try {
    const cfg = await api.setAutoCheckConfig({
      interval: form.value.interval * 60,
      threshold: form.value.threshold,
      min_low: form.value.min_low,
    })
    form.value = {
      interval: Math.round(cfg.interval / 60),
      threshold: cfg.threshold,
      min_low: cfg.min_low,
    }
    saved.value = true
    setTimeout(() => { saved.value = false }, 3000)
  } catch (e) {
    console.error('保存失败:', e)
  } finally {
    saving.value = false
  }
}

// ---------- SPEC-1 §FR-008 / AC-017 邮箱后端切换(Round 7 P2.2 抽 MailProviderCard) ----------
const mailState = ref('PROVIDER')
const mailForm = ref({
  MAIL_PROVIDER: 'cf_temp_email',
  CLOUDMAIL_BASE_URL: '',
  CLOUDMAIL_PASSWORD: '',
  CLOUDMAIL_DOMAIN: '',
  MAILLAB_API_URL: '',
  MAILLAB_USERNAME: '',
  MAILLAB_PASSWORD: '',
  MAILLAB_DOMAIN: '',
})
const mailInitialSnapshot = ref(null)
const mailSaving = ref(false)
const mailMessage = ref('')
const mailMessageClass = ref('')
const mailCardRef = ref(null)

const mailFormDirty = computed(() => {
  if (!mailInitialSnapshot.value) return false
  return JSON.stringify(mailForm.value) !== mailInitialSnapshot.value
})

function setMailMessage(text, type = 'success') {
  mailMessage.value = text
  if (type === 'success') mailMessageClass.value = 'bg-green-500/10 text-green-400 border-green-500/20'
  else if (type === 'warning') mailMessageClass.value = 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20'
  else mailMessageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  window.clearTimeout(setMailMessage._timer)
  setMailMessage._timer = window.setTimeout(() => { mailMessage.value = '' }, 10000)
}

function onMailStateChange(s) {
  mailState.value = s
}

function onMailVerified(payload) {
  if (payload?.leakedProbe) {
    setMailMessage(`⚠️ 探测邮箱回收失败,请到后台手动删除: ${payload.leakedProbe.email}`, 'warning')
  }
}

function onMailError(resp) {
  if (resp?.soft && resp.warnings) {
    setMailMessage('⚠️ ' + resp.warnings.join('; '), 'warning')
    return
  }
  setMailMessage(`${resp.error_code || 'ERROR'}: ${resp.message || '未知错误'}` +
    (resp.hint ? ` — ${resp.hint}` : ''), 'error')
}

async function saveMailConfig() {
  mailSaving.value = true
  try {
    await api.saveSetup({ ...mailForm.value })
    mailInitialSnapshot.value = JSON.stringify(mailForm.value)
    setMailMessage('保存成功 — 重启服务后生效', 'success')
  } catch (e) {
    setMailMessage('保存失败: ' + e.message, 'error')
  } finally {
    mailSaving.value = false
  }
}

function resetMailForm() {
  if (!mailInitialSnapshot.value) return
  mailForm.value = JSON.parse(mailInitialSnapshot.value)
  mailCardRef.value?.reset?.()
  mailState.value = 'PROVIDER'
  mailMessage.value = ''
}

async function loadMailSetup() {
  try {
    const result = await api.getSetupStatus()
    if (result?.fields) {
      for (const f of result.fields) {
        if (f.key in mailForm.value) {
          mailForm.value[f.key] = f.default || ''
        }
      }
    }
    if (result?.provider) mailForm.value.MAIL_PROVIDER = result.provider
    mailInitialSnapshot.value = JSON.stringify(mailForm.value)
  } catch (e) {
    console.error('加载邮箱后端配置失败:', e)
  }
}

onMounted(loadMailSetup)
</script>
