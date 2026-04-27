# SPEC-Shared: Master Codex Auth File 生成与刷新

- **Version**: v1.0(Round 10 落地)
- **Status**: STABLE
- **Owner**: AutoTeam codex_auth 子系统
- **Last update**: 2026-04-28
- **Trigger**: Round 10 修复 admin session_token 导入后主号 OAuth 落登录页(commit pending)

---

## §1 Scope

本 spec 描述 **master(母号)Codex 认证文件**的生成、保存、刷新链路,与子号 OAuth 路径(`oauth-workspace-selection.md` v1.0)解耦但共享 `SessionCodexAuthFlow` 状态机。

**输入**:admin 通过 `/api/admin/login/session` 导入的 `session_token` + `account_id` + `workspace_name`(state.json 落地)

**输出**:`accounts/codex-main-{account_id}.json`,字段见 §3。

**与子号路径的关系**:

| 维度 | 主号路径(本 spec) | 子号路径(oauth-workspace-selection.md) |
|---|---|---|
| 入口 | admin session 导入后 `refresh_main_auth_file()` | invite 接受后 `_run_post_register_oauth_*` |
| 文件命名 | `codex-main-{account_id}.json` | `codex-{email}-{plan_type}-{md5_hash}.json` |
| 进账号池 | ❌ 不进 | ✅ 进(seat 占用) |
| 状态机 | `SessionCodexAuthFlow.start()` 一次性 | `SessionCodexAuthFlow` + retry 5 次 |
| 配额管理 | N/A(母号本身) | `quota-classification.md` 五分类 |
| GRACE 状态 | N/A | `account-state-machine.md` v2.0 STATUS_DEGRADED_GRACE |

---

## §2 Call Stack(Round 10 后)

```
POST /api/admin/login/session  (api.py:1223)
  └─ ChatGPTTeamAPI.import_admin_session(email, session_token)
       └─ state.json 落地 { email, session_token, account_id, workspace_name }
  └─ if state.session_token & state.account_id 齐:
       └─ _pw_executor.run(refresh_main_auth_file)  (api.py:1254)
            └─ refresh_main_auth_file()  (codex_auth.py:1467)
                 └─ login_codex_via_session()  (codex_auth.py:1007 — Round 10 thin wrapper)
                      └─ flow = SessionCodexAuthFlow(
                             email=get_admin_email(),
                             session_token=get_admin_session_token(),
                             account_id=get_chatgpt_account_id(),
                             workspace_name=get_chatgpt_workspace_name(),
                             password="",
                             password_callback=None,
                             auth_file_callback=lambda _: "",  # 主号路径不通过 callback 写盘
                         )
                      └─ flow.start()  → _advance state machine
                           - 浏览器 chatgpt.com warm-up + Cloudflare
                           - 注 chatgpt.com 域 cookies(__Secure-next-auth.session-token + _account + oai-did)
                           - 注 auth.openai.com 域 cookies(同上 — _inject_auth_cookies 守 if self.account_id:)
                           - goto auth.openai.com/oauth/authorize?...
                           - _detect_step:
                               * email_required → _auto_fill_email(admin email) → loop  ← Round 10 关键
                               * password_required → _switch_password_to_otp + password_callback
                               * code_required → password_callback 取 OTP code
                               * completed → 退出 loop
                           - return {"step": "completed" | "..."}
                      └─ if step != "completed": return None
                      └─ flow.complete()
                           └─ _exchange_auth_code(auth_code, code_verifier)
                                POST auth.openai.com/oauth/token
                                  grant_type=authorization_code
                                  client_id=app_EMoamEEZ73f0CkXaXp7hrann
                                  code, redirect_uri, code_verifier
                                  → { access_token, refresh_token, id_token, expires_in }
                           └─ JWT id_token 解码 → email/plan_type/account_id
                           └─ return {"bundle": {...}, "auth_file": ""}
                      └─ return info["bundle"]
                      └─ finally: flow.stop()
                 └─ if bundle is None: raise RuntimeError(
                      "无法基于管理员登录态生成主号 Codex 认证文件"
                 )  ← API 层依赖此文案
                 └─ save_main_auth_file(bundle)  (codex_auth.py:1432-1442)
                      └─ AUTH_DIR / f"codex-main-{account_id}.json" 写盘
                 └─ return { email, auth_file, plan_type }
       └─ info["main_auth"] = { email, auth_file, plan_type }
  └─ return JSON to frontend
```

**Round 10 之前**(已修复):
- `login_codex_via_session()` body 是 175 行的 inline Playwright 实现
- 在 email-input 页 fallback 跳 `chatgpt.com/auth/login` 重试 — **upstream 没这条且实际无效**(只刷新 chatgpt.com 域 session,不影响 auth.openai.com 的 OAuth issuer session)
- 重试仍落 email-input 页就 `return None` → `RuntimeError`

**Round 10 之后**:
- thin wrapper(37 行)委托给 `SessionCodexAuthFlow`
- `SessionCodexAuthFlow._advance` 状态机内 `_auto_fill_email` 在 email-input 页**自动填 admin email** + 继续 → consent → callback
- 与 upstream cnitlrt/AutoTeam codex_auth.py:1017-1043 1:1 对齐

---

## §3 Auth File Schema

`accounts/codex-main-{account_id}.json`:

```json
{
  "type": "codex",
  "id_token": "<JWT>",
  "access_token": "<JWT, exp = now + 3600s>",
  "refresh_token": "<opaque 长字符串>",
  "account_id": "<chatgpt_account_id from JWT claim>",
  "email": "<admin email from JWT claim>",
  "expired": "<ISO-8601 UTC, e.g. 2026-04-28T03:53:11Z>",
  "last_refresh": "<ISO-8601 UTC>"
}
```

**字段语义**:

| 字段 | 来源 | 用途 |
|---|---|---|
| `type` | 常量 `"codex"` | 与子号文件区分(子号也是 `"codex"`,真正区分靠文件名前缀) |
| `id_token` | `auth.openai.com/oauth/token` 返回 | JWT,含 chatgpt claim(plan_type / account_id),解码取元数据 |
| `access_token` | 同上 | Bearer 调 `/backend-api/codex/responses`,exp 通常 3600s |
| `refresh_token` | 同上 | 调 `auth.openai.com/oauth/token grant_type=refresh_token` 续命 |
| `account_id` | id_token claim `chatgpt_account_id` | 文件命名 + 部分接口要求 |
| `email` | id_token claim `email` | 标识用,fallback 用 `state.email` |
| `expired` | `now + token_data.expires_in` | 决定何时 refresh,对外 ISO-8601 |
| `last_refresh` | refresh 时刻 | 监控用,无业务逻辑 |

**注**:`plan_type` **不进盘**,仅在 `refresh_main_auth_file()` 返回值中传给 manager / api 层。这是 Round 4 既有契约,Round 10 不变。

---

## §4 错误处理矩阵

| `flow.start()` 返回 | 后果 | 调用方动作 |
|---|---|---|
| `step="completed"` | flow.complete() → bundle | 保存文件,返回 dict |
| `step="email_required"` | wrapper return None | refresh_main_auth_file raise RuntimeError(原文案) |
| `step="password_required"` | wrapper return None | 同上(密码页未配置 callback,主号路径默认无密码) |
| `step="code_required"` | wrapper return None | 同上(无 password_callback,无法填 OTP) |
| `step="unsupported_password"` | wrapper return None | 同上 |
| `step="unknown"` | wrapper return None | 同上 |
| start() raise(网络/Playwright 错误)| finally 跑 flow.stop(),异常向上传 | API 层 500 |

**API 层文案契约**(不可变):

```python
raise RuntimeError("无法基于管理员登录态生成主号 Codex 认证文件")
```

`api.py` 的 `/api/admin/login/session` handler catch 此 RuntimeError 并返回 `{ok: false, message: <error>}`,前端显示 banner。

---

## §5 Cookie 注入策略(关键 — 不可改)

`SessionCodexAuthFlow._inject_auth_cookies` 在 `auth.openai.com` 域注入:

```python
# __Secure-next-auth.session-token (split if >3800 chars)
{
    "name": "__Secure-next-auth.session-token" | "__Secure-next-auth.session-token.0/.1",
    "value": session_token | session_token[:3800] | session_token[3800:],
    "domain": "auth.openai.com",
    "path": "/",
    "httpOnly": True,
    "secure": True,
    "sameSite": "Lax",
}

# _account (CRITICAL: 必须守 if self.account_id:)
if self.account_id:
    {
        "name": "_account",
        "value": self.account_id,
        "domain": "auth.openai.com",
        "path": "/",
        "secure": True,
        "sameSite": "Lax",
    }

# oai-did
{
    "name": "oai-did",
    "value": self.chatgpt.oai_device_id,  # 同 chatgpt.com 域用的 device_id
    "domain": "auth.openai.com",
    "path": "/",
    "secure": True,
    "sameSite": "Lax",
}
```

**关键约束**:
- `__Secure-next-auth.session-token` httpOnly + secure + sameSite=Lax 全 True(否则浏览器拒收)
- `_account` 值为空字符串时**不能注入**(空 `_account` 会扰乱 OAuth issuer)— `if self.account_id:` 守
- `oai-did` 必须与 chatgpt.com 域同 UUID(对应同一 device,issuer 据此识别)

**chatgpt.com 域 cookies**(在 `_inject_session` 注,先于 auth.openai.com 注入):
- 同上三个 cookie,但 domain="chatgpt.com"
- 必须先跑 chatgpt.com warm-up + Cloudflare challenge,session-token cookie 才能与 issuer ledger 关联

---

## §6 OAuth URL 与 Token 交换

**Auth URL**(`_build_auth_url`):

```
https://auth.openai.com/oauth/authorize
  ?client_id=app_EMoamEEZ73f0CkXaXp7hrann
  &response_type=code
  &redirect_uri=http://localhost:1455/auth/callback
  &scope=openid email profile offline_access
  &state=<random url-safe 16 bytes>
  &code_challenge=<S256(verifier)>
  &code_challenge_method=S256
  &prompt=consent
```

**Token Exchange**(`_exchange_auth_code`):

```python
POST https://auth.openai.com/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
&client_id=app_EMoamEEZ73f0CkXaXp7hrann
&code=<auth_code>
&redirect_uri=http://localhost:1455/auth/callback
&code_verifier=<verifier>
```

返回:`{access_token, refresh_token, id_token, expires_in}`

**Refresh**(`refresh_access_token`):

```python
POST https://auth.openai.com/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token
&client_id=app_EMoamEEZ73f0CkXaXp7hrann
&refresh_token=<refresh_token>
```

---

## §7 测试约束

`tests/unit/test_round10_master_codex_session.py` 必含 7 case:

1. wrapper happy path(step=completed → bundle)
2. wrapper email_required → None + warning
3. wrapper exception → 异常传播 + finally stop
4. refresh_main_auth_file success → save_main_auth_file 被调
5. refresh_main_auth_file None → RuntimeError 文案保留(向后兼容)
6. SessionCodexAuthFlow 必含方法存在(`_auto_fill_email` / `_advance` / `_detect_step` / `_inject_auth_cookies` / `_attach_callback_listeners` / `start` / `complete` / `stop`)
7. `_inject_auth_cookies` 静态扫描含 `if self.account_id:` 守卫

**不可降级**:case 6/7 是前置条件回归保护 — 若未来重构 `SessionCodexAuthFlow` 把这些方法删了,wrapper 立刻挂,这两 case 提早警告。

`tests/manual/test_round10_dryrun.py` 的 AC6 6 项硬指标(I1-I6)是上线前必跑,真实网络验证由用户手动跑(round10-review-report.md §6 命令)。

---

## §8 不变量(M-MA-*)

- **M-MA-1**(文案不变):`raise RuntimeError("无法基于管理员登录态生成主号 Codex 认证文件")` — API 层错误识别依赖
- **M-MA-2**(文件命名):`codex-main-{account_id}.json` 单一,旧文件清理(`save_main_auth_file` glob unlink)
- **M-MA-3**(签名稳定):`refresh_main_auth_file()` 返回 `{email, auth_file, plan_type}` — manager.py:3711 + api.py:783/1252 三个 caller 依赖
- **M-MA-4**(plan_type 不入盘):auth file 内不含 `plan_type` 字段,plan_type 在 return dict 内传递
- **M-MA-5**(`_account` cookie 守卫):`if self.account_id:` 不可丢,空 `_account` 值会扰乱 OAuth issuer
- **M-MA-6**(`oai-did` 同 device):chatgpt.com 与 auth.openai.com 两域使用同一 `oai_device_id`,否则 issuer 视为不同设备

---

## §9 Round 10 实施记录

- **入口**:`prompts/0426/prd/...`(已转 trellis 任务)
- **任务**:`.trellis/tasks/04-28-master-codex-oauth-session-fallback/`
- **PRD**:`prd.md` AC1-6
- **研究**:
  - `research/upstream-master-codex-oauth.md`(上游对照,~1600 行)
  - `research/local-impl-trace.md`(本地 6 假设排序)
- **Review**:`prompts/0426/verify/round10-review-report.md` PASS
- **Commit**:pending(用户决定时机)

**关键 commit 行号锚点**(Round 10 commit 后,`codex_auth.py` 新行号):
- `login_codex_via_session()` thin wrapper:`codex_auth.py:1007-1043`
- `SessionCodexAuthFlow.__init__`:`codex_auth.py:1083-1106`
- `SessionCodexAuthFlow._inject_auth_cookies`:`codex_auth.py:1165-1225`(`if self.account_id:` 守在 1199)
- `SessionCodexAuthFlow._auto_fill_email`:`codex_auth.py:1255-1264`
- `SessionCodexAuthFlow.start`:`codex_auth.py:1319-1339`
- `SessionCodexAuthFlow.complete`:`codex_auth.py:1366`
- `_exchange_auth_code`:`codex_auth.py:107-144`
- `_write_auth_file`:`codex_auth.py:147-166`
- `save_main_auth_file`:`codex_auth.py:1432-1442`
- `refresh_main_auth_file`:`codex_auth.py:1467-1490`

(行号可能因后续 commit 微移,以 `git blame` 为准)

---

## §10 Future Work(Round 11+)

- **F1**:dry-run 脚本接入 pytest `--run-manual` flag(P3-2 from review)
- **F2**:SessionCodexAuthFlow 真实 Playwright 集成测试(P3-1)— 风险慢 + flaky,可选
- **F3**:多母号支持时,本 spec 需扩展 `master_account_id` 维度(round 9 backlog 已记录)
- **F4**:JWT exp 监控 — 若主号 access_token < 600s 主动 refresh,而非懒加载等到 401

---

**版本变更**:
- v1.0(2026-04-28)— Round 10 首版,描述 thin wrapper + SessionCodexAuthFlow 委托链路
