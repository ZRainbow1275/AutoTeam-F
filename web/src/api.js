const BASE = '/api'

function getApiKey() {
  return localStorage.getItem('autoteam_api_key') || ''
}

export function setApiKey(key) {
  localStorage.setItem('autoteam_api_key', key)
}

export function clearApiKey() {
  localStorage.removeItem('autoteam_api_key')
}

async function request(method, path, body = null) {
  const headers = { 'Content-Type': 'application/json' }
  const key = getApiKey()
  if (key) {
    headers['Authorization'] = `Bearer ${key}`
  }
  const opts = { method, headers }
  if (body) opts.body = JSON.stringify(body)
  const resp = await fetch(`${BASE}${path}`, opts)
  let data
  try {
    data = await resp.json()
  } catch {
    const err = new Error(`HTTP ${resp.status}: 服务器返回了非 JSON 响应`)
    err.status = resp.status
    throw err
  }
  if (!resp.ok) {
    const msg = data?.detail?.message || data?.detail || `HTTP ${resp.status}`
    const err = new Error(msg)
    err.status = resp.status
    throw err
  }
  return data
}

export const api = {
  checkAuth: () => request('GET', '/auth/check'),
  getSetupStatus: () => request('GET', '/setup/status'),
  saveSetup: (config) => request('POST', '/setup/save', config),
  // SPEC-1 §4.2 — 三步分阶段 mail provider 探测
  probeMailProvider: (payload) => request('POST', '/mail-provider/probe', payload),

  getStatus: () => request('GET', '/status'),
  getAdminStatus: () => request('GET', '/admin/status'),
  getMainCodexStatus: () => request('GET', '/main-codex/status'),
  getManualAccountStatus: () => request('GET', '/manual-account/status'),
  getAccounts: () => request('GET', '/accounts'),
  getActiveAccounts: () => request('GET', '/accounts/active'),
  getStandbyAccounts: () => request('GET', '/accounts/standby'),
  deleteAccount: (email) => request('DELETE', `/accounts/${encodeURIComponent(email)}`),
  deleteAccountsBatch: (emails, continueOnError = true) => request('POST', '/accounts/delete-batch', { emails, continue_on_error: continueOnError }),
  loginAccount: (email) => request('POST', '/accounts/login', { email }),
  getCodexAuth: (email) => request('GET', `/accounts/${encodeURIComponent(email)}/codex-auth`),
  kickAccount: (email) => request('POST', `/accounts/${encodeURIComponent(email)}/kick`),
  getCpaFiles: () => request('GET', '/cpa/files'),

  startAdminLogin: (email) => request('POST', '/admin/login/start', { email }),
  submitAdminSession: (email, sessionToken) => request('POST', '/admin/login/session', { email, session_token: sessionToken }),
  submitAdminPassword: (password) => request('POST', '/admin/login/password', { password }),
  submitAdminCode: (code) => request('POST', '/admin/login/code', { code }),
  submitAdminWorkspace: (optionId) => request('POST', '/admin/login/workspace', { option_id: optionId }),
  cancelAdminLogin: () => request('POST', '/admin/login/cancel'),
  logoutAdmin: () => request('POST', '/admin/logout'),
  startMainCodexSync: () => request('POST', '/main-codex/start'),
  submitMainCodexPassword: (password) => request('POST', '/main-codex/password', { password }),
  submitMainCodexCode: (code) => request('POST', '/main-codex/code', { code }),
  cancelMainCodexSync: () => request('POST', '/main-codex/cancel'),
  startManualAccount: () => request('POST', '/manual-account/start'),
  submitManualAccountCallback: (redirectUrl) => request('POST', '/manual-account/callback', { redirect_url: redirectUrl }),
  cancelManualAccount: () => request('POST', '/manual-account/cancel'),

  postSync: () => request('POST', '/sync'),
  postSyncFromCpa: () => request('POST', '/sync/from-cpa'),
  postSyncAccounts: () => request('POST', '/sync/accounts'),
  postSyncMainCodex: () => request('POST', '/sync/main-codex'),

  startRotate: (target = 5) => request('POST', '/tasks/rotate', { target }),
  startCheck: () => request('POST', '/tasks/check'),
  startAdd: () => request('POST', '/tasks/add'),
  startFill: (target = 5) => request('POST', '/tasks/fill', { target, leave_workspace: false }),
  startFillPersonal: (count = 1) => request('POST', '/tasks/fill', { target: count, leave_workspace: true }),
  startCleanup: (maxSeats = null) => request('POST', '/tasks/cleanup', { max_seats: maxSeats }),

  getTasks: () => request('GET', '/tasks'),
  getTask: (id) => request('GET', `/tasks/${id}`),
  cancelTask: () => request('POST', '/tasks/cancel'),

  getAutoCheckConfig: () => request('GET', '/config/auto-check'),
  setAutoCheckConfig: (cfg) => request('PUT', '/config/auto-check', cfg),

  getRegisterDomain: () => request('GET', '/config/register-domain'),
  setRegisterDomain: (domain, verify = true) => request('PUT', '/config/register-domain', { domain, verify }),

  // SPEC-2 — 邀请席位偏好(default/codex)
  getPreferredSeatType: () => request('GET', '/config/preferred-seat-type'),
  putPreferredSeatType: (value) => request('PUT', '/config/preferred-seat-type', { value }),

  // SPEC-2 — sync_account_states 被踢探测节流(并发上限 + 冷却分钟)
  getSyncProbe: () => request('GET', '/config/sync-probe'),
  putSyncProbe: (payload) => request('PUT', '/config/sync-probe', payload),

  getRegisterFailures: (limit = 50) => request('GET', `/register-failures?limit=${limit}`),

  // Round 8 — SPEC-2 v1.5 §6.2:母号订阅健康度独立端点
  getMasterHealth: (forceRefresh = false) => request(
    'GET', `/admin/master-health${forceRefresh ? '?force_refresh=1' : ''}`,
  ),

  getTeamMembers: () => request('GET', '/team/members'),
  removeTeamMember: (payload) => request('POST', '/team/members/remove', payload),
  getLogs: (limit = 100, since = 0) => request('GET', `/logs?limit=${limit}&since=${since}`),
}

// Round 7 FR-D7 — 把后端 task["error"] 字符串关键字解析为 UI 可消费的语义错误。
// Round 6 P1.3 决策让 manager 在 task["error"] 中携带 `phone_required` / `register_blocked`
// 关键字串。前端 UI 在 getTask 轮询命中 task.error 后调用本函数,根据 category 给出
// 友好 toast 而不是裸字符串。
//
// 入参:task — 后端 GET /api/tasks/{id} 的响应对象(含可选 error 字段)
// 返回:
//   { category: 'phone_required',     friendly_message: '该账号需要绑定手机才能完成 OAuth', detail }
//   { category: 'register_blocked',   friendly_message: '...',                              detail }
//   { category: 'generic',            friendly_message: '<原 error 字符串>',                detail }
//   null — task 没有 error 字段(任务进行中或成功)
export function parseTaskError(task) {
  if (!task || !task.error) return null
  const errStr = String(task.error)
  const lower = errStr.toLowerCase()
  if (lower.includes('phone_required')) {
    return {
      category: 'phone_required',
      friendly_message: '该账号需要绑定手机才能完成 OAuth',
      detail: errStr,
    }
  }
  if (lower.includes('register_blocked')) {
    return {
      category: 'register_blocked',
      friendly_message: '该账号注册被阻断,请检查 OAuth 状态',
      detail: errStr,
    }
  }
  return {
    category: 'generic',
    friendly_message: errStr,
    detail: errStr,
  }
}
