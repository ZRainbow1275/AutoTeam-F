# Master Codex OAuth via session_token 落登录页修复

## Goal

修复 admin 导入 session_token 后,刷新主号 Codex 认证文件失败的问题:

```
02:27:29 WARN  [Codex] 主号 OAuth 先落到了登录页,尝试先建立 ChatGPT 登录态后重试...
02:27:47 ERROR [Codex] session 无法直接用于主号 Codex OAuth,仍落在登录页
02:27:49 WARN  [API] session_token 导入完成,但刷新主号认证文件失败:
              无法基于管理员登录态生成主号 Codex 认证文件
```

## What I already know

### 现象与触发链

- 用户通过 `/api/admin/login/session` 导入 admin session_token 成功
- 后端调 `refresh_main_auth_file()` (`src/autoteam/codex_auth.py:1601-1612`)
- → `login_codex_via_session()` 用 Playwright 打开 `auth.openai.com/oauth/authorize?...`
- → 落到 ChatGPT 登录页(看到 email input),不是 OAuth 同意页
- → fallback: `goto https://chatgpt.com/auth/login` 再重试 → 仍落登录页
- → return None → 抛 `RuntimeError("无法基于管理员登录态生成主号 Codex 认证文件")`

### 本地当前实现关键点 (`codex_auth.py:1062-1177` `login_codex_via_session`)

- 用 admin 的 ChatGPT `__Secure-next-auth.session-token` 启动 browser context
- 直接 `page.goto(auth_url)` 跳 `https://auth.openai.com/oauth/authorize?...`
- 检测 `input[name="email"]` 可见即认为"未登录"
- 重试方案是 `page.goto("https://chatgpt.com/auth/login")` 再回 OAuth — 这只对 chatgpt.com 域生效,**不影响 auth.openai.com 的 session**

### 推测根因

`auth.openai.com` 与 `chatgpt.com` 是不同域,session-token cookie 域是 chatgpt.com / .openai.com 部分子域。Codex OAuth 入口需要 `auth.openai.com` 自己的 session(由 chat.openai.com 的 sso 中转建立),**单独导入 chatgpt.com 的 session-token 不能直接换到 auth 子域的登录态**。

### Round 8 已落地能力

- `oauth_workspace.py` (decode/select/UI fallback)
- `manager.py` post-register OAuth M-T1 master probe
- 但子号 OAuth 走的是 ChatGPT 邀请短链,**不是这个 master 自登录路径**

## Assumptions(待 research 验证)

- A1. 上游 cnitlrt/AutoTeam 主号 Codex 认证生成走"非浏览器" HTTP 路径(直接 POST `/oauth/authorize` 拿 code)
- A2. 或:上游需要 admin 提前 `https://chatgpt.com/api/auth/session` 触发 sso 中转,把 access_token 存进来,再调 OAuth
- A3. 或:上游用 `Authorization: Bearer <chatgpt access_token>` 直接换 codex token,不走 PKCE
- A4. 当前本地 PKCE state/code_verifier 的生成方式与上游一致(round 4 移植自上游)

## Research References

- 待 research 子 agent 产出:
  - [research/upstream-master-codex-oauth.md] — 上游 cnitlrt/AutoTeam 主号 Codex 认证生成路径
  - [research/local-impl-trace.md] — 本地 `login_codex_via_session` + `refresh_main_auth_file` 调用栈与 PKCE 实现对照

## Open Questions

(已答 — research 双证据指向 Approach A,无需用户再决策)

## Decision (ADR-lite)

**Context**:`login_codex_via_session()` (codex_auth.py:1003-1177) 是**遗留死代码**:
- 落 email-input 页时 fallback 跳 `chatgpt.com/auth/login` 重试 — 这条 fallback **upstream 没有**且实际无用(只是再触发一遍 chatgpt.com 已经做过的登录态加载)
- 同文件 1180+ 行已存在 `SessionCodexAuthFlow` 类(子号路径在用),它的 `_advance` → `_auto_fill_email` 在 email-input 页**自动填 admin email** 然后继续 OAuth consent 流程
- upstream `cnitlrt/AutoTeam` 早已把 `login_codex_via_session()` 改成 thin wrapper 委托给 `SessionCodexAuthFlow`(upstream codex_auth.py:1017-1043)

**Decision**:**Approach A — 把 `login_codex_via_session()` 1003-1177 替换为 thin wrapper,委托给 `SessionCodexAuthFlow`**(对齐 upstream)。

**Why not Approach B(in-place 加 _auto_fill_email + 守 _account cookie)**:
- 保留 1003-1177 这个 ~175 行的近重复实现是技术债
- A 维护一套代码,B 维护两套,长期 A 优
- A 自动获得 SessionCodexAuthFlow 已有的 OTP 切换 / workspace 选择 / 完整 _advance 状态机能力

**Consequences**:
- 1003-1177(~175 行)删除 → wrapper ~25 行
- 必须验证 local `SessionCodexAuthFlow` 与 upstream 等价(`_auto_fill_email` / `_advance` / `_detect_step` / `_inject_auth_cookies` 字段对齐)
- 测试需新增 mock SessionCodexAuthFlow 验证 wrapper 调用契约
- 不改 `refresh_main_auth_file()` 与 `save_main_auth_file()` 签名,API 层无感知

### Approach A 实施大纲

**B1 — 后端核心**:
1. 删除 `codex_auth.py:1003-1177` 的 `login_codex_via_session()` body
2. 替换为 thin wrapper(参考 upstream codex_auth.py:1017-1043,代码见 research/upstream-master-codex-oauth.md §5):
   ```python
   def login_codex_via_session():
       flow = SessionCodexAuthFlow(
           email=get_admin_email(),
           session_token=get_admin_session_token(),
           account_id=get_chatgpt_account_id(),
           workspace_name=get_chatgpt_workspace_name(),
           password="",
           password_callback=None,
           auth_file_callback=lambda _bundle: "",
       )
       try:
           result = flow.start()
           if result.get("step") != "completed":
               logger.warning("[Codex] 主号 session OAuth 未完成: step=%s detail=%s",
                              result.get("step"), result.get("detail"))
               return None
           info = flow.complete()
           return info.get("bundle")
       finally:
           flow.stop()
   ```
3. 验证 local `SessionCodexAuthFlow._auto_fill_email` 存在且等价 upstream — 不在则补
4. 验证 local `SessionCodexAuthFlow._inject_auth_cookies` 守了 `if self.account_id:`(upstream codex_auth.py:1203 有,本地若没需补)

**B2 — 单测**:
- `tests/unit/test_round10_master_codex_session.py`(新增)
  - mock `SessionCodexAuthFlow.start` 返回 `step=completed`,断言 wrapper 调 complete + 返回 bundle
  - mock `start` 返回 `step=email_required`,断言 wrapper 返回 None + log warning
  - mock `start` raise → wrapper finally 必跑 `flow.stop()`
- `tests/unit/test_round10_refresh_main_auth.py`(新增)
  - mock `login_codex_via_session` 返回完整 bundle,断言 `refresh_main_auth_file` 调 `save_main_auth_file` + 返回 dict
  - mock 返回 None → 抛 RuntimeError 文案不变(向后兼容)

**B3 — 实测自验**(对应 AC6):
- check 阶段 quality-reviewer 必须 dry-run 调:
  1. 准备 admin session_token + account_id + workspace_name(用现有 state.json)
  2. 触发 `refresh_main_auth_file()`
  3. 断言生成的 `accounts/codex-main-*.json` 含非空 access_token / refresh_token / id_token
  4. 用 `access_token` 调 `cheap_codex_smoke()` → 200(或合理 4xx 配额)
  5. 用 `refresh_token` POST `auth.openai.com/oauth/token` grant_type=refresh_token → 拿到新 access_token
  6. 截 JSON 摘录与 JWT exp 写入 review-report

**S1 — Spec 同步**:
- 新增 `prompts/0426/spec/shared/master-auth-file.md`(由 trellis-update-spec 阶段产出)
- 描述 admin session → SessionCodexAuthFlow → save_main_auth_file 全链路
- 引用 round 10 commit 与 AC6 自验证据

## Requirements (evolving)

### MVP 必修

- [R1] admin 导入 session_token 后,`/api/admin/session-token` 能成功生成 `codex-main-<id>.json`
- [R2] 不破坏现有 invite/OAuth 路径(子号注册)
- [R3] 复现路径明确:重启 → 导入 session → 自动刷新 → 文件落地

## Acceptance Criteria

- [ ] AC1. POST /api/admin/login/session 返回 `{ok: true, main_auth: {email, auth_file, plan_type}}`,文件存在 `accounts/codex-main-*.json`
- [ ] AC2. 文件结构与子号 OAuth 产物一致(access_token / refresh_token / id_token / plan_type)
- [ ] AC3. 后续 cheap_codex_smoke(主号 token) 200
- [ ] AC4. pytest 全绿(实测 baseline 265,Round 10 新增 ≥3,目标 ≥268) + ruff 0
- [ ] AC5. 不破坏 round 1-9 既有路径
- [ ] **AC6. 实测自验**(用户新增要求 2026-04-28):quality-reviewer 必须 dry-run 调一次完整流程,断言:
  - 生成的 `codex-main-*.json` 包含**非空** `access_token`(JWT,exp > now+600s)
  - 包含**非空** `refresh_token`(长字符串,与 access_token 不同)
  - `id_token` 字段存在(可空但若有需为合法 JWT)
  - `account_id` / `email` / `plan_type` 字段齐全
  - 使用 `access_token` 调一次真实 `/backend-api/codex/responses` 拿到 200(或合理的 4xx 配额错误,不能 401)
  - **使用 `refresh_token` 调一次刷新接口**(`auth.openai.com/oauth/token` grant_type=refresh_token),拿到新的 access_token 不抛错

## Definition of Done

- 单测覆盖新路径(mock OAuth endpoints)
- 文档:`prompts/0426/spec/shared/master-auth-file.md`(新增)说明 admin session → master codex auth 生成流程
- review-report PASS,且 §自验章节包含 AC6 全部 6 项实测证据(JWT exp / refresh round-trip 截图或 JSON 摘录)

## Out of Scope

- 多母号支持(round 9+ backlog)
- 邮箱密码登录走 OAuth(已有 SessionCodexAuthFlow,本任务不动)

## Technical Notes

- `src/autoteam/codex_auth.py:1062-1177` `login_codex_via_session`(Playwright 路径)
- `src/autoteam/codex_auth.py:1601-1612` `refresh_main_auth_file`(入口)
- `src/autoteam/codex_auth.py:1180+` `SessionCodexAuthFlow`(子号路径,可参考但不动)
- `src/autoteam/codex_auth.py:_build_auth_url + _generate_pkce + _exchange_auth_code`(PKCE 工具)
- `src/autoteam/api.py` `/api/admin/login/session` endpoint(api.py:1223-1271,调 refresh_main_auth_file)
- 上游参考:https://github.com/cnitlrt/AutoTeam(用户指定)

## 用户决策原文

"我认证主号之后还有这样的问题 https://github.com/cnitlrt/AutoTeam 参考上游项目是怎么做成功的"(2026-04-28)
