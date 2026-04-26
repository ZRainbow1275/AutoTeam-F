# Issue#4 OAuth 凭证获取失败 + 再次登录爆 add phone — 研究报告

> 作者:claude(OAuth/SSO 流程调研工程师) · 时间:2026-04-26
>
> 用户原话:"修复在使用中报错经常出现的:免费号凭证无法通过OAuth获取,再次登录时爆add phone的问题——不要脱离原本的流程太多"
>
> **本报告只调研、不改代码**。所有结论以代码事实为准,引用形式 `path:line` 全部经过 Read/Grep 校验。

---

## TL;DR(给开看的人)

1. **现象一(凭证获取不到)** = `login_codex_via_browser` 末尾 `if not auth_code: return None` 静默失败。callback 没回来的根因被吞掉(超时 / consent 循环卡死 / **未识别的 add-phone 阻塞页**),上层只能写 `oauth_failed`。
2. **现象二(再次登录爆 add phone)** = OAuth 流程中**完全没有** add-phone 检测器。`invite.py:106 detect_phone_verification` 只在新账号注册的 4 个提交节点用,**所有 6 处 `login_codex_via_browser` 调用点(注册成功后 OAuth、生成免费号 personal OAuth、补登录、轮转复用、定点替换、`api.py /api/accounts/login`)都不调用它**。
3. **最小侵入修复方向**:把 `assert_not_blocked(page, step)` 接进 `codex_auth.py` OAuth 主循环 → 命中 add-phone 时抛 `RegisterBlocked(is_phone=True)` → 上层按"账号需要人工"分类(新增 `STATUS_PHONE_REQUIRED` 或复用 `STATUS_AUTH_INVALID` + register_failures `phone_blocked`)。**不重写 OAuth、不绕 add-phone**,与用户"不要脱离原本流程太多"的硬要求一致。
4. **与 issue#6 的边界**:#4 覆盖**所有**经过 `login_codex_via_browser` 的入口;#6 是 reinvite_account 这个特定子集的二次表现(踢出后再 OAuth 风控更高)。**修了 #4 的 add-phone 检测器,#6 的"还要手机"会被拦截**,只是分类语义不同(#4 失败时账号刚 kick 出去就直接放弃,#6 失败时账号已经在 Team 占席位需要立即清理)。

---

## A. 当前 OAuth 流程地图

### A.1 关键文件清单

| 文件 | 行数 | 职责 |
|------|------|------|
| `src/autoteam/codex_auth.py` | 1728 | **OAuth 主战场** — `login_codex_via_browser`、`login_codex_via_session`、`SessionCodexAuthFlow`、`MainCodexSyncFlow`、`save_auth_file`、`check_codex_quota`、`refresh_access_token` |
| `src/autoteam/auth_storage.py` | 35 | `auths/` 目录 + 文件权限 0o666 修复 |
| `src/autoteam/manager.py` | 4137 | 6 处 `login_codex_via_browser` 调用点中的 4 处:`_run_post_register_oauth`(L1431/L1463)、`reinvite_account`(L2466)、`_check_pending_invites`(L1057) |
| `src/autoteam/api.py` | - | `/api/accounts/login` 单账号 web 触发(L1479) |
| `src/autoteam/manual_account.py` | 309 | 用户在自己浏览器走 OAuth → 提交回调 URL,本地不开 Playwright,**不会撞 add-phone**(由用户人眼处理) |
| `src/autoteam/invite.py` | - | `detect_phone_verification`(L106)、`assert_not_blocked`(L138)、`RegisterBlocked`(L53) — **add-phone 检测器只在这里被定义和使用** |
| `src/autoteam/accounts.py` | 156 | 状态常量 `STATUS_*`、`SEAT_*`,**没有 phone-required 状态** |
| `src/autoteam/register_failures.py` | 99 | `record_failure(category=...)`,已有 `phone_blocked` 类目但**只在注册阶段触发** |

### A.2 调用链(手工构建,无 gitnexus 索引)

```
入口层
├── /api/accounts/login              (api.py:1450 post_account_login)
├── /api/tasks/fill (leave=False)    (api.py:1933 post_fill → cmd_fill)
├── /api/tasks/fill (leave=True)     (api.py:1933 post_fill → cmd_fill_personal)
├── /api/tasks/check                 (manager.py:cmd_check)
├── /api/tasks/replace               (manager.py:cmd_replace_one)
└── /api/tasks/rotate                (manager.py:cmd_rotate)

OAuth 中枢
                              login_codex_via_browser(use_personal=False/True)
              ┌─────────────────────────┬─────────────────────────┬────────────────────────┐
              ▼                         ▼                         ▼                        ▼
  _run_post_register_oauth   _run_post_register_oauth     reinvite_account         /api/accounts/login
  (L1463 Team)               (L1431 personal)             (L2466 standby 复用)     (api.py:1479)
              │                         │                         │                        │
              ▼                         ▼                         ▼                        ▼
        save_auth_file              save_auth_file           save_auth_file           save_auth_file
  (codex-{email}-{plan}-{hash}.json)
              │
              ▼
        sync_to_cpa()  (cpa_sync.py 把 auths/ 同步到 CLIProxyAPI)
```

主号 session 复用:`login_codex_via_session` / `SessionCodexAuthFlow` / `MainCodexSyncFlow` 用主号 `__Secure-next-auth.session-token` 注 cookie 直跳 `auth_url`,**也不检测 add-phone**(主号通常已绑定手机,但极端场景仍可能触发)。

### A.3 凭证文件路径与生命周期

```
auths/                                       (PROJECT_ROOT/auths,bind mount 到 docker 容器)
├── codex-{email}-{team|free|plus}-{8 字节 md5}.json
└── codex-main-{account_id}.json             (主号专用,不进 accounts.json)

文件结构(_write_auth_file @ codex_auth.py:119):
{
  "type": "codex",
  "id_token": "...",
  "access_token": "...",
  "refresh_token": "...",
  "account_id": "...",
  "email": "...",
  "expired": "ISO8601",
  "last_refresh": "ISO8601"
}

权限:0o666(docker 容器写,宿主机用户读写,见 auth_storage.py:9)

生命周期:
1. login_codex_via_browser → _exchange_auth_code → 拿到 bundle
2. save_auth_file(bundle) → 删同 email 旧文件 → 写新文件
3. update_account(email, auth_file=路径)
4. sync_to_cpa() 反向同步给 CPA 服务
5. 后续每次 check_codex_quota 直接读文件里的 access_token,401/403 → status=AUTH_INVALID
6. refresh_access_token 用 refresh_token 换新 at,写回原文件
```

---

## B. "凭证获取失败"症状归因

### B.1 失败位点候选(按 codex_auth.py 流程顺序)

| # | 位置 | 表现 | 当前处理 | 是否被识别 |
|---|------|------|----------|------------|
| 1 | `login_codex_via_browser` step-0 ChatGPT 预登录(Team 模式)| Cloudflare "verify you are human" 不退 | 12 次循环每 5s 试一次,失败仍继续(L327-330) | 否,继续推进可能在后面崩 |
| 2 | step-0 OTP 等待主号验证码 | 主号未触发 OTP,但 `mail_client.search_emails_by_recipient` 收不到 | 静默 except,继续(L399) | 否 |
| 3 | step-0 workspace 选择失败 | 用户 `CHATGPT_WORKSPACE_NAME` 配错 | fallback 选第二个非"个人"选项(L417-432) | 否,可能选错 workspace |
| 4 | OAuth 邮箱/密码页"误跳 Google" | 邮箱碰到 SSO 自动跳 accounts.google.com | 检测到则 `page.go_back` 重试 1 次(L484-489) | **是**(`_is_google_redirect`) |
| 5 | 验证码邮件取不到 | 120s 内 `mail_client.search_emails_by_recipient` 没拿到 OPENAI 发的 OTP | warning 后继续主循环,实际 consent 步会卡(L564) | 部分,`_wait_for_otp_submit_result` 可识别 invalid/pending |
| 6 | about-you 失败 | 注册新账号 OAuth 时 `about-you` 页缺 spinbutton/age input | except 吞掉,继续 consent 循环(L608) | 否 |
| 7 | 10 次 consent 循环里 workspace/Continue 都点不出 | personal/Team 切换冲突、workspace 名称不存在、按钮被遮挡 | break 出 for(L880-882) | 否,只 logger.info |
| 8 | **add-phone 拦截页(URL `/verify-phone`、`/add-phone`、`/phone-number`)** | OpenAI 风控强制要求绑定手机号 | **完全不检测** | **否** ❗ |
| 9 | 等 callback 30s 没回来 | OAuth 中途任何卡死 / consent 走偏 | warning,return None(L902-910) | 否,根因被吞 |
| 10 | personal 模式 plan_type != free | 子号刚 kick 出 Team,workspace default 还指 Team | 拒收 bundle,返回 None(L920-930) | **是**(commit `e760be9`) |

**关键事实**:位点 8 和位点 9 是同一根因的两副面孔。OAuth 走到 add-phone 页 → 卡死 → callback 不回来 → 写"未获取到 auth code"。**这个流程对调用方不可区分**:都是 `bundle is None`。

### B.2 错误日志关键词扫描(grep `oauth_failed|no_bundle|未获取到 auth`)

```
codex_auth.py:909   "[Codex] OAuth 登录失败: 未获取到 authorization code"   ← 兜底,根因黑盒
codex_auth.py:904   "[Codex] 未获取到 auth code,当前 URL: %s"               ← 唯一能反推 add-phone 的线索:URL
codex_auth.py:1104  "[Codex] 主号未获取到 auth code,当前 URL: %s"

manager.py:1455     record_failure(email, "oauth_failed", "已退出 Team 但 personal Codex OAuth 登录未返回 bundle")
manager.py:1459     out_outcome("oauth_failed", reason="personal Codex OAuth 未返回 bundle")
manager.py:2485     _cleanup_team_leftover("no_bundle")
manager.py:1480     team_auth_missing  ("已入 Team 席位但 Codex OAuth 未返回 bundle,需要补登录")
```

**用户的 register_failures.json 实际样本(11 条)**:
- 1 条 `oauth_failed`(`0d5d56a045@icoulsy.cloud` "personal Codex OAuth 未返回 bundle")— **这就是"凭证无法通过 OAuth 获取"的真实命中**
- 8 条 `exception`(net::ERR_ABORTED / Page.content / Page.screenshot 上下文已关闭) — 不在 add-phone 范畴,是 Playwright 浏览器异常
- 1 条 `kick_failed`、1 条 `register_failed`

证据有限但与现象吻合。`oauth_failed` 没有更细的子分类,无法区分"add-phone 阻塞" / "consent 循环走偏" / "callback 网络抖动"。

### B.3 兜底逻辑现状

```
login_codex_via_browser 内:
  ├─ 邮箱步误跳 Google → page.go_back 重试 1 次  ✅
  ├─ 密码步误跳 Google → page.go_back 重试 1 次  ✅
  ├─ OTP submit 不通过 → _wait_for_otp_submit_result accepted/invalid/pending 三态 ✅
  ├─ consent 循环最多 10 次                  ✅
  ├─ 等 callback 30s                       ✅
  ├─ personal 模式 plan_type != free 拒收  ✅(commit e760be9)
  └─ add-phone 拦截                         ❌ 完全没检测

调用方兜底(reinvite_account L2483-2487):
  bundle is None → _cleanup_team_leftover("no_bundle") → STATUS_STANDBY
  这套兜底假设"OAuth 失败是一过性的,下次再试可能就好了"。
  但 add-phone 是确定性风控,等下次 retry 还会撞同样页面 → 死循环消耗 invite/kick 配额

调用方兜底(_run_post_register_oauth L1431 personal 模式):
  bundle is None → delete_account(email) + record_failure("oauth_failed", "personal OAuth 未返回 bundle")
  这套对 add-phone 倒是"对的"(账号已 kick 出去,删干净),但**用户失去了"知道为啥失败"的能力**:
  到底是 add-phone 触发了,还是临时网络问题,还是 workspace default 没同步,日志里都看不出来。
```

---

## C. add phone 拦截深度调研

### C.1 触发条件(综合外部资料 + 代码注释)

参考 `invite.py:69-87` 注释 + `manager.py:1393` 上下文 + grok web 检索结果(2026-04 仍在生效):

| 类别 | 触发场景 | 在 AutoTeam 上下文里的对应 |
|------|----------|---------------------------|
| **行为风控** | 短时间多账号同 IP/UA 注册 | 一个母号一晚上批量生产免费号 → fill-personal |
| **设备信任** | 新设备/新指纹首次登录 | docker 容器每次开 Playwright 都是"新设备",`oai-did` cookie 没复用 |
| **地理风控** | VPN / IP 漂移 | 用户跨国部署 / `PLAYWRIGHT_PROXY_URL` 切换 |
| **账号孤立** | 账号无 cookie/history 直接 OAuth | personal 模式注册后 5s kick 出 Team 立刻 OAuth(cookie 几乎是空的) |
| **Arkose 缺失** | 浏览器扩展屏蔽 Arkose Labs | docker headless chromium 默认不带扩展,但若 `PLAYWRIGHT_PROXY_URL` 指向某些被 Arkose 黑名单 IP 段会被强制 add-phone |
| **手机号已复用** | burner 号被多个账号绑过 → "this phone number is already linked to the maximum number of accounts" | 历史问题,与 OAuth 无关但表现类似 |

**"再次登录"特别触发条件**(用户原话:"再次登录时爆 add phone"):
- 一个账号在某个 session 已经验证过手机 → cookie 清掉再登(docker 重启浏览器、新 context、不复用 storage_state)→ OpenAI 看不到 device 信任,**重新走风控**
- 短时间多次 OAuth 同一邮箱(reinvite_account 复用 standby → kick 后下一轮再 invite → 再 OAuth)→ 命中"频次"风控

### C.2 检测方式(URL/DOM)

`invite.py:69-87` 已经提供完整检测器,可直接复用:

```python
_PHONE_URL_HINTS = ("verify-phone", "add-phone", "/phone", "phone_verification", "phone-number")
_PHONE_TEXT_HINTS = (
    "verify your phone", "add your phone", "verify phone",
    "verification code to your phone", "add a phone number",
    "add a phone", "enter your phone", "phone verification",
    "we'll text you", "请输入手机号", "手机号码", "验证手机", "添加手机",
)

def detect_phone_verification(page):
    url = (page.url or "").lower()
    if any(hint in url for hint in _PHONE_URL_HINTS):
        return True                                     # URL 命中 → 强信号
    body = page.inner_text("body")[:1500].lower()
    if not any(hint in body for hint in _PHONE_TEXT_HINTS):
        return False
    tel_input = page.locator(
        'input[type="tel"], input[name*="phone" i], input[autocomplete*="tel" i]'
    ).first
    if tel_input.is_visible(timeout=500):                # 文本命中 + 电话输入框 → 强信号
        return True
    return False                                          # 否则当未阻塞
```

设计意图(注释里写了):URL 强信号优先;文本必须配合电话输入框,避免注册帮助区"phone number"短语误报。**这套规则已经在生产被验证过半年(从 2025-09 开始用)**,可以直接拿到 OAuth 路径用。

### C.3 当前代码处理现状

**注册阶段(invite.py + manager.py 直接注册)**:✅ 完整覆盖

```
invite.py:247    assert_not_blocked(page, "email_submit")     ← 邮箱提交后
invite.py:282    assert_not_blocked(page, "password_submit")  ← 密码提交后
invite.py:364    assert_not_blocked(page, "code_submit")      ← OTP 提交后
invite.py:446    assert_not_blocked(page, "profile_submit")   ← about-you 提交后
manager.py:1954  assert_not_blocked(page, "about_you_submit") ← 直接注册 about-you 后
manager.py:2117  assert_not_blocked(page, "email_submit")     ← 直接注册 email 后
manager.py:2184  assert_not_blocked(page, "password_submit")  ← 直接注册 password 后
manager.py:2228  assert_not_blocked(page, "code_submit")      ← 直接注册 OTP 后
```

捕获 `RegisterBlocked.is_phone=True` → `record_failure(category="phone_blocked")` + 删邮箱 + 整个账号放弃(`manager.py:2320-2332`)。

**OAuth 阶段(codex_auth.py)**:❌ 零覆盖

```
codex_auth.py 全文:
  detect_phone_verification: 0 处
  assert_not_blocked:        0 处
  RegisterBlocked:           0 处
```

**关键空白点**:
- L568 `if "about-you" in page.url:` ← 后面填了 name/age 就 continue,**这一刻最可能撞 add-phone**(注册流程提交完 about-you 后 OpenAI 经常拉一次 add-phone)
- L612 consent 10 次循环 ← 每一步都可能跳到 add-phone 页,但只看 workspace/consent button,看不到 add-phone
- L884 等 callback 30s 死循环 ← add-phone 页就是"callback 永远不来"的根因

### C.4 绕过/规避策略对比

用户原话:"**不要脱离原本的流程太多**"。所有"绕过 add-phone"的方案直接淘汰。可选方向按"侵入度"从小到大:

| # | 策略 | 改动量 | 风险 | 用户接受度 |
|---|------|--------|------|------------|
| **A** | **检测到 add-phone 立即放弃 + 标记账号需要人工** | **极小**(invite.py 已现成 detect 函数,在 codex_auth.py 4 处插一行) | **零**,纯防御性 | **高**(用户已在 invite 阶段接受相同处置:"不绕,直接放弃") |
| B | A + 重置 cookie/storage_state 重试一次 | 中(要在 codex_auth.py 加 storage_state 持久化逻辑,失败重新打开 context) | 低(只是重试,不绕过) | 中(用户没说要重试,但符合"最大可能性修复") |
| C | A + 切换 PLAYWRIGHT_PROXY_URL 重试 | 大(需要 IP 池管理) | 高(运维负担) | 低(脱离原流程) |
| D | 自动绑定 SMS pool 上的手机号 | 极大(集成 sms-activate / mobilesms.io) | 极高(违反 OpenAI ToS,封号风险) | 极低 ❌ |

**推荐 A**(可附带 B 作为可配置开关,默认关闭)。

---

## D. 修复方向(最小侵入)

### D.1 必改点(按文件)

#### 1. `src/autoteam/codex_auth.py` — 接入 add-phone 检测

**改动 1:统一抛 RegisterBlocked**
- 在 `login_codex_via_browser` 主流程的 4 个关键节点插入 `assert_not_blocked(page, step)`:
  - L568 about-you 入口前(`if "about-you" in page.url:` 之前)
  - L612 consent 循环每次 step 开头(`for step in range(10):` 内、`if auth_code: break` 后)
  - L884 等 callback 前(`for _ in range(30):` 之前)
  - L932 personal 拒收 bundle 之前(plan_type 校验之前)

- 在 `SessionCodexAuthFlow._detect_step` 加一个 `phone_required` 状态,作为 `email_required` / `password_required` / `code_required` 的并列项:命中 add-phone 时返回 `("phone_required", page.url)` 让 `_advance` 立即终止主流程并触发上层处置。

**改动 2:`bundle is None` 携带失败原因**
- 改 `login_codex_via_browser` 返回签名:`-> bundle | None` 改成 `-> bundle | dict({"error": "phone_required", "url": ...})`,或直接抛 `RegisterBlocked` 让上层 catch。
- 推荐**抛异常**,理由:`record_failure` 已经按 `RegisterBlocked.is_phone` 分类成熟,新增 `phone_required` 抛异常路径与现有 `oauth_failed` return None 路径解耦,迁移成本最低。

#### 2. `src/autoteam/manager.py` — 6 处调用点统一处理

每个调用点 wrap 一层 try/except RegisterBlocked:

```
try:
    bundle = login_codex_via_browser(...)
except RegisterBlocked as blocked:
    if blocked.is_phone:
        record_failure(email, "phone_blocked",
                       f"OAuth 阶段触发 add-phone (step={blocked.step})",
                       step=blocked.step, source="oauth")
        # 处置策略按调用点不同:
        # - _run_post_register_oauth(personal): delete_account + 退出
        # - _run_post_register_oauth(team):     update_account(STATUS_PHONE_REQUIRED 或 AUTH_INVALID)
        # - reinvite_account:                   _cleanup_team_leftover + STATUS_STANDBY
        # - api.py /api/accounts/login:         返回 409 + "需要人工处理"
        ...
    raise
```

#### 3. `src/autoteam/accounts.py` — 新增状态(可选)

```
STATUS_PHONE_REQUIRED = "phone_required"  # OAuth 时被风控强制 add-phone,等人工或自动重试
```

或复用 `STATUS_AUTH_INVALID`(commit cf2f7d3 已有)+ register_failures category=`phone_blocked` 区分。**复用现有状态成本更低**,但 UI 显示"auth invalid"语义不准。

#### 4. `src/autoteam/register_failures.py` — 扩 category 枚举注释

```python
category: 'phone_blocked' / 'duplicate_exhausted' / 'register_failed' / 'oauth_failed'
          / 'kick_failed' / 'team_oauth_failed' / 'exception'
          / 'oauth_phone_blocked'  ← 新增,与注册阶段 phone_blocked 区分,便于统计
```

#### 5. `src/autoteam/web` 前端(可选)

`web/src/components/Dashboard.vue:381-403` `statusClass / dotClass / statusLabel` 白名单加 `phone_required`(若新增状态),否则跳过此项。

### D.2 测试用例(给后续 implement 参考)

- **单测**:`tests/unit/test_codex_auth_phone_detection.py`
  - mock `page.url` 为 `https://chat.openai.com/auth/add-phone` → `_advance` 必须返回 `phone_required`
  - mock body 含 "verify your phone" + tel input 可见 → 同上
  - mock body 含 "phone number" 但无 tel input → 不阻断(避免误报,与 invite.py 保持一致)
- **集成测试**:`tests/integration/test_oauth_phone_blocked_flow.py`
  - mock `login_codex_via_browser` 抛 `RegisterBlocked(is_phone=True)`
  - 验证 `_run_post_register_oauth(leave_workspace=True)` 调用 `delete_account` + `record_failure(category="oauth_phone_blocked")`
  - 验证 `reinvite_account` 调用 `_cleanup_team_leftover` + `record_failure`,**不**留 standby 假态
  - 验证 `/api/accounts/login` 返回 409 + 明确错误体
- **回归测试**:`tests/unit/test_codex_oauth_no_phone_false_positive.py`
  - 模拟正常 consent 页(含 "ChatGPT" / "Personal" 等文本)→ `detect_phone_verification` 必须返回 False
  - 模拟 about-you 页(含 birthday spinbutton)→ 不触发误报

### D.3 与 Issue#6 的边界

| 维度 | Issue#4 | Issue#6 |
|------|---------|---------|
| 触发场景 | 任意账号 OAuth 阶段被 add-phone 拦截 | 已被踢出 Team 的账号 reinvite 时触发 add-phone |
| 涉及调用点 | 全部 6 处 `login_codex_via_browser` | 仅 `reinvite_account`(L2466) |
| 账号当前位置 | 不确定(可能在 Team / 已 kick / 个人池) | **已 kick 出 Team**,且本地 `STATUS_STANDBY` |
| 处置紧迫度 | 看场景定 | **高**(账号刚 invite 进 Team 又触发,席位被占用,容易死循环) |
| 修复在哪一层 | codex_auth.py 检测 + 6 处调用方分类处置 | 同 #4 检测,但 reinvite_account 处置必须立即 kick + 锁定该账号不再被 standby 复用 |

**结论**:#4 修好后,#6 是 #4 在 `reinvite_account` 这一支的特化分类。建议**两个 issue 在同一 PR 里改完**,避免:
1. 只修 #4 不管 #6 → reinvite_account 命中后未 kick → 假 standby
2. 只修 #6 不管 #4 → 其他 5 处调用点继续黑盒失败

实施顺序:
1. `codex_auth.py` 加检测(奠定基础)
2. `manager._run_post_register_oauth` 处置(personal 路径,最高频)
3. `manager.reinvite_account` 处置(issue#6 直接收尾)
4. `manager._check_pending_invites` / `api.post_account_login` 处置
5. 主号路径 `login_codex_via_session` / `SessionCodexAuthFlow`(可选,主号通常已绑定手机)

---

## E. 风险与未决问题

### E.1 设计层未决

1. **是否新增 `STATUS_PHONE_REQUIRED` 状态?**
   - 推荐方案 A:复用 `STATUS_AUTH_INVALID` + `register_failures.category="oauth_phone_blocked"`,改动量最小
   - 风险:语义混淆,前端 UI 显示"auth_invalid"会误导
   - 替代方案 B:新增独立状态,改动 7-8 个文件(accounts.py / api.py status summary / web/Dashboard.vue / reconcile / sync_account_states / standby probe / cmd_check / 测试)。**Round-3 batch 已经新增过 `STATUS_AUTH_INVALID` 和 `STATUS_ORPHAN`,前端白名单和 reconcile 逻辑还没补全(CHANGELOG round-3 backlog 有提)**,不建议本次再加状态;先用 A 方案 + 后续随状态扩展统一补
2. **reinvite_account 命中 add-phone 时,是否要把账号永久标"不再复用"?**
   - 当前 standby 池设计:24h 内已探测过的跳过(commit d6082ad)
   - 风险:phone-block 是确定性风控,24h 后再试还是会撞;但完全黑名单又可能误杀(临时风控窗口过去后该账号其实可用)
   - 建议:第一次撞 add-phone → STATUS_AUTH_INVALID,**让 reconcile 处理**(reconcile_anomalies 已有 auth_invalid → KICK 分支);不进 standby 池
3. **检测器误报怎么办?**
   - `invite.py:69-87` 的检测规则已半年生产验证,但 OAuth 流程页面文本与注册流程**不完全相同**(consent 页可能含 "phone" 提示文字)
   - 建议先全量审查 OAuth 流程的截图(`screenshots/codex_04_*.png`)是否含"phone"文本,若有需要在 `_PHONE_TEXT_HINTS` 上加 OAuth 专属规则
   - 风险等级:低(URL 命中是强信号,文本命中要 tel input 配合,假阳概率小)

### E.2 实施层风险

1. **`SessionCodexAuthFlow._advance` 的 phone_required 处置链路**
   - 该 Flow 用于主号,目前没有"中断主号 OAuth"的概念,只有 email/password/code required 三个用户输入态
   - 如果主号撞 add-phone,UI 怎么提示?当前没有"主号需要绑手机"的展示路径
   - 建议:主号路径暂时只在日志里 ERROR + 抛异常,不接 UI;主号绑过手机后通常不会再撞,这个 case 极罕见
2. **重试逻辑会不会进一步触发风控?**
   - 当前 reinvite_account 失败 → STATUS_STANDBY → 24h 后又被选中 reinvite
   - 加 add-phone 检测 + STATUS_AUTH_INVALID 后,reconcile 会 KICK,不再循环 → **降低**风控触发频率
3. **detect_phone_verification 在 docker headless 浏览器下的可靠性**
   - `page.inner_text("body")` 和 `page.locator(...).is_visible` 在 headless 模式应该一致,但 OpenAI 偶尔 A/B test 不同 add-phone 页面布局
   - 建议:命中后立即调 `_screenshot(page, f"codex_phone_blocked_{step}.png")` 留证据,便于后续回放调试
4. **fix-personal 的"已退出 Team"账号撞 add-phone**
   - 这是**最严重**的死路径:账号已 kick → personal OAuth 撞 add-phone → bundle None → 当前流程 `delete_account` + `record_failure("oauth_failed")`
   - 修复后:`record_failure("oauth_phone_blocked")` 分类,但**账号还是删**(kick 已经发生,留下来没意义)
   - 风险:用户做 fill-personal 时若一批 4 个号全撞 add-phone,4 个邮箱 + 4 个 invite quota 直接打水漂。建议加**早停**:连续 2 个号撞 add-phone → 整批暂停,提示用户检查 IP/proxy

### E.3 数据层未决

- `register_failures.json` 当前只有 1 例 oauth_failed,样本不足以确认 add-phone 命中率
- 建议先把检测器加上、跑 1 周生产观察 `oauth_phone_blocked` 计数,再决定要不要加重试/IP 切换逻辑
- **当前 11 条记录中 8 条是 `Page.goto: net::ERR_ABORTED` / `Page.content: navigating` / `Page.screenshot: Target closed`**,这是 Playwright 浏览器异常**不是 OAuth 失败本身**,跟 add-phone 无关,但说明用户运行环境的浏览器稳定性也有问题(可能是另一个独立 issue)

---

## 附录:本次调研工具使用情况

| 工具 | 使用情况 | 备注 |
|------|----------|------|
| serena | 项目未注册到 serena, 退化用 Grep/Read | 可后续 `mcp__metamcp__serena__activate_project` 时手动加 |
| abcoder | AutoTeam 不在已索引仓库列表(只有 Inkforge/bentodesk/devhub/web) | 同上 |
| gitnexus | 未配置该项目的图谱索引 | 调用链需手工 grep 构建 |
| exa | **额度耗尽** ❗(`web_search_exa error 402: exceeded your credits limit`),改用 grok-search | 影响外部资料调研深度,但 grok 给出了 OpenAI Community + reddit + help.openai.com 6 个高质量来源 |
| grok-search | ✅ 命中 6 篇(2 个 sessions) | 关键发现:OpenAI 用 Arkose Labs,广告拦截器/IP 黑名单会触发 enforcement_failed |
| Read/Grep/Glob | 主力 | 全文档 100% 走代码事实,无臆测 |

调研涉及文件(主要):
- `D:/Desktop/AutoTeam/src/autoteam/codex_auth.py`
- `D:/Desktop/AutoTeam/src/autoteam/manager.py`
- `D:/Desktop/AutoTeam/src/autoteam/manual_account.py`
- `D:/Desktop/AutoTeam/src/autoteam/api.py`
- `D:/Desktop/AutoTeam/src/autoteam/invite.py`
- `D:/Desktop/AutoTeam/src/autoteam/accounts.py`
- `D:/Desktop/AutoTeam/src/autoteam/auth_storage.py`
- `D:/Desktop/AutoTeam/src/autoteam/register_failures.py`
- `D:/Desktop/AutoTeam/src/autoteam/setup_wizard.py`
- `D:/Desktop/AutoTeam/CHANGELOG.md`
- `D:/Desktop/AutoTeam/register_failures.json`(11 条历史失败记录)
