# Research: 本地 master Codex OAuth via session_token 流程追踪

- **Query**: Trace local `D:\Desktop\AutoTeam\src\autoteam\codex_auth.py` 主号 OAuth 路径,定位"主号 OAuth 先落到了登录页"根因
- **Scope**: 内部代码 + 上游对照(`research/_upstream_codex_auth.py` / round 8 oauth_workspace.py)
- **Date**: 2026-04-28

---

## §1 Local Call Stack(从用户操作到失败点)

### 1.1 触发链(`/api/admin/login/session` → 落登录页)

```
POST /api/admin/login/session  (api.py:1223-1271)
  └─ AdminSessionParams { email, session_token }
  └─ ChatGPTTeamAPI().import_admin_session(email, session_token)        ← chatgpt_api.py:1061-1160
  │     1. _launch_browser() → Playwright 起 chromium + new_context
  │     2. page.goto("https://chatgpt.com/")
  │     3. _inject_session(session_token)                                  ← chatgpt_api.py:386-411
  │           注入 cookies 到 chatgpt.com 域:
  │           - __Secure-next-auth.session-token (或 .0/.1 分片)
  │           - _account = account_id
  │           - oai-did = uuid4()
  │     4. page.goto("https://chatgpt.com/")  再次进入触发刷新
  │     5. _list_real_workspaces() → /backend-api/accounts 找 Team
  │     6. /backend-api/accounts/{id}/settings 验证 200
  │     7. update_admin_state(...) 写 state.json
  │
  └─ 若 session/account 落地成功(L1250)→ refresh_main_auth_file()        ← codex_auth.py:1601-1612
        └─ login_codex_via_session()                                       ← codex_auth.py:1003-1177
              1. _generate_pkce() / state / _build_auth_url               ← L1005-1007
              2. ChatGPTTeamAPI() → chatgpt.start()                        ← L1013-1016
                 (内部调 start_with_session(session_token, account_id, workspace_name)
                  = _launch_browser → goto chatgpt.com → _inject_session
                    → _fetch_access_token → _auto_detect_workspace)
              3. session_token = chatgpt.session_token                    ← L1017
                 (从 admin_state 读出来,内存里的是 chatgpt 域 cookie)
              4. context.add_cookies([...])                                ← L1078
                 注入到 auth.openai.com 域:
                 - __Secure-next-auth.session-token (单/分片)
                 - _account
                 - oai-did
                 ⚠️ 没有 __Host-next-auth.csrf-token / callback-url / cf_clearance
              5. page = chatgpt.context.new_page()
              6. page.goto(auth_url)  其中 auth_url 含 prompt=consent
              7. 检测 input[name="email"] 是否可见(L1107-1111)
                 ▶ visible → needs_login = True
                 ▶ 走 retry: goto chatgpt.com/auth/login → goto auth_url 重试
                 ▶ 重试还能看见 email input → log "session 无法直接用于主号 Codex OAuth,仍落在登录页"
                 ▶ return None                                            ← L1123-1125
        └─ 若 None → raise RuntimeError                                   ← L1604-1605
        └─ api.py L1259 catch → info["main_auth_error"] = ... → 返回 200 给前端
              (前端就显示"刷新主号认证文件失败")
```

### 1.2 现象到根因的因果链

| 表象(用户日志) | 推断根因 |
|---|---|
| `WARN [Codex] 主号 OAuth 先落到了登录页` | page.goto(auth_url) 返回的 DOM 含 email input,即 OAuth issuer 把请求当作"未登录" |
| `ERROR session 无法直接用于主号 Codex OAuth,仍落在登录页` | retry 通过 chatgpt.com 建立 chatgpt 域 session,但 auth.openai.com 域的 session 校验仍失败 |

**深层因果链**(技术层逐步推断):

```
auth.openai.com 收到 GET /oauth/authorize?...
  → 中间件读 __Secure-next-auth.session-token cookie
  → 验证 cookie 签名(NextAuth jwt verify with NEXTAUTH_SECRET)
  → 若 token 有效:跳到 consent 页或直接 redirect callback
  → 若 token 无效 / 过期 / **缺少同源伴随 cookie**:redirect 到 /auth/login
                                                  ↑
                                                  本地的现象
```

### 1.3 关键文件 / 行号锚点

| 文件 | 行号 | 角色 |
|---|---|---|
| `src/autoteam/api.py` | 1223-1271 | `/api/admin/login/session` endpoint(实际是 endpoint 名,**不叫** `/api/admin/session-token`) |
| `src/autoteam/api.py` | 1252-1260 | 调 `refresh_main_auth_file()` 把异常 swallow 进 `info["main_auth_error"]` |
| `src/autoteam/api.py` | 783-791 | 同款逻辑出现在 admin login completed 流程里 |
| `src/autoteam/codex_auth.py` | 1003-1177 | `login_codex_via_session()` — 失败位置 |
| `src/autoteam/codex_auth.py` | 1601-1612 | `refresh_main_auth_file()` — 入口包装 |
| `src/autoteam/codex_auth.py` | 1180-1519 | `SessionCodexAuthFlow` class — 比 1003 那个版本"更新",有 `_advance()` 状态机 |
| `src/autoteam/codex_auth.py` | 1521-1543 | `MainCodexSyncFlow` 走 `SessionCodexAuthFlow` 路径(用于 `/api/main-codex/start`) |
| `src/autoteam/chatgpt_api.py` | 386-411 | `_inject_session(session_token)` — 注入到 chatgpt.com 域(注意:**与** auth.openai.com 域不同) |
| `src/autoteam/chatgpt_api.py` | 147-179 | `_build_session_cookies(token, domain)` — 通用 cookie 构造,只产 session-token,不产 csrf-token |
| `src/autoteam/oauth_workspace.py` | 86-139 | `decode_oauth_session_cookie` — 读 oai-oauth-session cookie(验证存在的间接证据) |
| `src/autoteam/admin_state.py` | 1-150 | state.json 持久化 schema:**只存 session_token + account_id + workspace_name + email + password**,**不存** csrf / cf_clearance / oai-oauth-session |

---

## §2 Cookie Inventory(注入与所需对比表)

### 2.1 ChatGPTTeamAPI 在 chatgpt.com 域注入(`_inject_session`,chatgpt_api.py:386-411)

| Cookie 名 | 值来源 | domain | httpOnly | secure | 路径 | 是否注入 |
|---|---|---|---|---|---|---|
| `__Secure-next-auth.session-token` | session_token(< 3800 字符) | chatgpt.com | ✅ | ✅ | / | ✅ |
| `__Secure-next-auth.session-token.0` | session_token[:3800](≥ 3800) | chatgpt.com | ✅ | ✅ | / | ✅(分片版) |
| `__Secure-next-auth.session-token.1` | session_token[3800:] | chatgpt.com | ✅ | ✅ | / | ✅(分片版) |
| `_account` | account_id | chatgpt.com | ❌ | ✅ | / | ✅(条件:account_id 非空) |
| `oai-did` | uuid4() at __init__ | chatgpt.com | ❌ | ✅ | / | ✅ |

### 2.2 login_codex_via_session 在 auth.openai.com 域追加注入(codex_auth.py:1021-1078)

| Cookie 名 | 值来源 | domain | httpOnly | secure | 是否注入 | 备注 |
|---|---|---|---|---|---|---|
| `__Secure-next-auth.session-token` | chatgpt.session_token(继承自 admin_state) | auth.openai.com | ✅ | ✅ | ✅ | 与 chatgpt.com 同 token |
| `__Secure-next-auth.session-token.0/1` | 同上(分片) | auth.openai.com | ✅ | ✅ | ✅ | 同上 |
| `_account` | chatgpt.account_id | auth.openai.com | ❌ | ✅ | ✅ | |
| `oai-did` | chatgpt.oai_device_id | auth.openai.com | ❌ | ✅ | ✅ | |

### 2.3 缺失的关键 cookie(对照 NextAuth + Cloudflare + OAI 内部反爬)

| Cookie 名 | 必要性 | 说明 | 当前状态 |
|---|---|---|---|
| `__Host-next-auth.csrf-token` | **可能必需** | NextAuth 在 GET /oauth/authorize 触发的 server-side jwt 检查不依赖 csrf-token,但**signin/signout 端点会要求**。若 issuer 中间件把 missing csrf 视作"非法 session" → 重定向 /auth/login 解释了用户现象 | ❌ 不注入 |
| `__Secure-next-auth.callback-url` | 可选 | NextAuth 路由后回调记忆 | ❌ 不注入 |
| `_cfuvid` | **几乎必需** | Cloudflare 唯一访客标识。缺它的请求会被 CF 视作"陌生 fingerprint",触发 challenge | ❌ 不注入 |
| `cf_clearance` | **必需(若域有 challenge)** | 只对 chatgpt.com 域 ChatGPTTeamAPI._wait_for_cloudflare 有 implicit 处理,auth.openai.com 域**未触发过 challenge 等待**,所以 CF 状态值不存在 | ❌ 不注入 |
| `__cf_bm` | 推荐 | CF Bot Management 标记,部分接口检查它 | ❌ 不注入 |
| `oai-oauth-session` | OAuth 流程内自然下发 | issuer 在 GET /oauth/authorize 成功握手后把它 set-cookie,本地代码在 `oauth_workspace.decode_oauth_session_cookie` 里**读**它,但**首次**进 OAuth 时它不存在 | 自然产生(若 session-token 有效) |
| `oai-client-auth-session` | 备选 | 同上,gpt-auto-register 有这个名 | 自然产生 |
| `openai-sentinel-token`(请求头) | **几乎必需** | 不是 cookie 是 header,用于 `/api/accounts/workspace/select` 等内部端点;Round 8 oauth_workspace.py:155 注释明确说"不主动注入,依赖同源 cookie",意味着我们目前没主动算它 | ❌ 不计算 |
| `__stripe_mid` | 可选 | 仅 billing 相关接口要 | ❌ 不注入 |
| `intercom-*` | 可选 | 客服小部件 | ❌ 不注入 |

**结论**:本地实现注入了 NextAuth session-token + _account + oai-did 三件套,**但缺少 Cloudflare 三件套(_cfuvid / cf_clearance / __cf_bm)与 NextAuth csrf-token**。在 chatgpt.com 域因为 `_wait_for_cloudflare` 隐式过 CF challenge,CF cookie 自然落地;但 auth.openai.com 域**没有过 CF 流程**,直接 goto auth_url 必带不上 CF cookie。**这是注入路径的实际缺口**。

### 2.4 Round 4 子号 OAuth 路径的对比注入(login_codex_via_browser, codex_auth.py:266-)

子号 OAuth 路径的不同之处:

| 维度 | 主号 login_codex_via_session | 子号 login_codex_via_browser |
|---|---|---|
| step-0 ChatGPT 预登录 | ⚠️ chatgpt.start() 隐式做(login flow 在 chatgpt.com 域) | ✅ 显式走 chatgpt.com/auth/login + 邮箱+密码+OTP(L312-457) |
| 注入 _account 到 chatgpt.com | ✅ chatgpt_api.py:387-398 | ✅ codex_auth.py:315-334 |
| 注入 _account 到 auth.openai.com | ✅ codex_auth.py:1058-1066 | ✅ codex_auth.py:317-333 |
| 进入 auth.openai.com 前 wait_for_cloudflare | ❌ 没显式调用 | ❌ 也没调,**但**子号已经经过 chatgpt.com 一遍 CF,context 内 CF cookie 已落地 |
| context 是否过 CF | ✅ `chatgpt.start()` 内 `_wait_for_cloudflare` | ✅ 子号 step-0 内 `_wait_for_cloudflare` 循环 |

**深一步观察**:**两条路径表面 CF 处理一致,差异极小**。但子号 OAuth 在历史上是工作的(Round 4 落地,Round 6/7/8 持续修),所以问题大概率不在 CF cookie,而在 **session 本身的归属与时效**。

---

## §3 _build_auth_url 内容审计

### 3.1 当前实现(codex_auth.py:72-83)

```python
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_URL = "https://auth.openai.com/oauth/authorize"
CODEX_CALLBACK_PORT = 1455
CODEX_REDIRECT_URI = f"http://localhost:{CODEX_CALLBACK_PORT}/auth/callback"

def _build_auth_url(code_challenge, state):
    params = {
        "client_id": CODEX_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CODEX_REDIRECT_URI,
        "scope": "openid email profile offline_access",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
    }
    return f"{CODEX_AUTH_URL}?{urllib.parse.urlencode(params)}"
```

### 3.2 与官方 Codex CLI / 上游一致性

- `client_id` = `app_EMoamEEZ73f0CkXaXp7hrann` ✅ 与上游(`research/_upstream_codex_auth.py:32`)完全一致
- `response_type=code` ✅ 标准 PKCE 授权码模式
- `redirect_uri` = `http://localhost:1455/auth/callback` ✅ 上游同款
- `scope` = `openid email profile offline_access` ✅ 同款
- `code_challenge_method=S256` ✅ 标准
- `prompt=consent` ✅ 强制弹同意页(避免 sticky 跳过 workspace 选择)

**结论**:**`_build_auth_url` 与上游 100% 一致**,不是问题源头。

### 3.3 与 chatgpt.com 域的 callback 对比

⚠️ 注意:**目前 redirect_uri 用 `localhost:1455`**(本地回调),不用 `chatgpt.com/codex/auth/callback` 之类。这是有意为之(与 Codex CLI 一致),但 **localhost callback** 需要本地起一个 HTTP server 才能接住 `?code=xxx`。如果 OAuth issuer 没把请求当作合法的"已登录用户",根本走不到 callback 这一步,所以 callback server 的存在与否对当前 bug 无关。

---

## §4 /api/admin/login/session(用户口里的"session-token"端点)trace

### 4.1 端点签名(api.py:1223-1271)

```python
@app.post("/api/admin/login/session")
def post_admin_login_session(params: AdminSessionParams):  # AdminSessionParams = { email, session_token }
    if _admin_login_api: post_admin_login_cancel()
    if not _playwright_lock.acquire(blocking=False): raise 409
    try:
        from autoteam.chatgpt_api import ChatGPTTeamAPI

        def _do_import(email, session_token):
            api = ChatGPTTeamAPI()
            try:
                return api.import_admin_session(email, session_token)
            finally:
                api.stop()

        info = _pw_executor.run(_do_import, email, session_token)
        if info.get("session_token") and info.get("account_id"):     # ← 关键 gate
            try:
                from autoteam.codex_auth import refresh_main_auth_file
                main_auth = _pw_executor.run(refresh_main_auth_file)  # ← 失败位置
                if main_auth:
                    info["main_auth"] = main_auth
            except Exception as exc:
                info["main_auth_error"] = str(exc)                    # ← 把失败 swallow 进字段
        return {"status": "completed", "admin": _admin_status(), "info": info}
    finally:
        _playwright_lock.release()
```

### 4.2 调 refresh_main_auth_file 之前的状态

| 数据点 | 值 / 来源 | 备注 |
|---|---|---|
| ChatGPTTeamAPI.session_token | params.session_token(用户输入) | 已写入 state.json |
| ChatGPTTeamAPI.account_id | _list_real_workspaces() → 选 admin role 的 Team | 必须是合法 UUID |
| ChatGPTTeamAPI.workspace_name | 同上 chosen.name | 可空 |
| ChatGPTTeamAPI 已 stop | ✅ finally 内 stop() | 浏览器实例已关 |
| state.json 已落盘 | ✅ update_admin_state(...) | 后续 login_codex_via_session 读 |

### 4.3 refresh_main_auth_file → login_codex_via_session 内部状态

```
refresh_main_auth_file() (codex_auth.py:1601)
    bundle = login_codex_via_session()
    if not bundle: raise RuntimeError
```

`login_codex_via_session()` 第 1013-1016 行新建 `ChatGPTTeamAPI` 并 `chatgpt.start()`。这是**第二次**起浏览器(第一次 import_admin_session 时已起过、已关)。

`chatgpt.start()` (chatgpt_api.py:1162-1167) 的实现:

```python
def start(self):
    session_token = get_admin_session_token()        # 从 state.json 读
    self.account_id = get_chatgpt_account_id()
    self.workspace_name = get_chatgpt_workspace_name()
    self.start_with_session(session_token, self.account_id, self.workspace_name)
```

`start_with_session` (chatgpt_api.py:1169-1185):

```
1. _launch_browser()              ← 新 chromium + new_context
2. page.goto("https://chatgpt.com/")
3. _wait_for_cloudflare()
4. _inject_session(session_token) ← 注入到 chatgpt.com 域(L387-411)
5. _fetch_access_token()          ← 调 /api/auth/session 拿 access_token
6. _auto_detect_workspace()       ← 调 /backend-api/accounts/{id}/settings
```

**关键观察**:
- `start_with_session` 已经在 chatgpt.com 域过 CF + 拿到 access_token,说明 session_token 在 chatgpt.com 域是**有效的**。
- 然后 `login_codex_via_session` 第 1078 行**直接 add_cookies 到 auth.openai.com 域** + 第 1098 行 `page.goto(auth_url)`。
- **关键**:`auth.openai.com` 这个 page 是**第一次**访问该域,`_wait_for_cloudflare` 没有为该域调用过。

### 4.4 session_token 是新拉的还是缓存的?

- 用户从前端粘贴的 session_token 值,经过 `import_admin_session` 后写入 state.json。
- `login_codex_via_session` 在 `chatgpt.start()` 内通过 `get_admin_session_token()` 从 state.json 读出,**值与用户输入完全一致**(无 token refresh)。
- 因此 `chatgpt.session_token` 是**用户最初输入的**,是否过期取决于用户拿到 token 的时间。

### 4.5 account_id 是否正确(master vs orphan workspace)

`import_admin_session` 内部已通过 `_list_real_workspaces()` + `/backend-api/accounts/{id}/settings` 二次校验(chatgpt_api.py:1097-1134),写入的 account_id 一定是 admin role 的 Team workspace。**不是问题源头**。

---

## §5 Hypotheses Ranked by Likelihood

> 综合 §1-§4 的事实证据,以及 round 8 sticky-rejoin-mechanism.md 的 OAuth 内部机制,排列假设。

### H1(P=高,~50%)session_token 是"非主号交互场景"产物,缺 auth.openai.com 域的有效性

**证据**:
- ChatGPT NextAuth 用一个 session-token 同时给 chatgpt.com / .openai.com 子域用,但**会话本身的 token 是 issuer 单点签发**。如果用户的 session_token 是从浏览器开发者工具复制的 chatgpt.com cookie,而该 token 对应的 NextAuth session 在 auth.openai.com 服务端没有对应的 server-side session entry(NextAuth 默认是 stateless JWT,但 OpenAI 的 issuer 可能加了 server-side session ledger),那 auth.openai.com 校验就 fail。
- 子号路径(login_codex_via_browser)是先在浏览器内**完整走一遍 ChatGPT 邮箱密码登录**,**让 issuer 在 auth.openai.com 也建立 session**,所以同款 cookie 注入下子号 OAuth 工作,主号 OAuth 不工作。

**反证检查**:
- 反证就是看 auth.openai.com 收到 GET /oauth/authorize 时实际返回的 set-cookie / location。需要做 §6 的诊断命令验证。

### H2(P=高,~30%)session_token 实际有效但缺 OAuth 入口的协同 cookie(如 oai-oauth-session 必须由 issuer 自身下发)

**证据**:
- `oauth_workspace.decode_oauth_session_cookie` 期待 `oai-oauth-session` 已存在于 context,这意味着**正常 OAuth 流程**中 issuer 第一次 redirect 时 set-cookie 给 client。
- 主号路径直接 goto auth_url,issuer 看到 session-token 但**没有任何"OAuth 流程上下文"**(比如此次 OAuth 请求的来源页 referer / pre-warm 的 oai-oauth-session),issuer 可能采取保守策略 — 重定向到 /auth/login 让用户重新建立流程。

**反证检查**:
- chatgpt.start() 之后是否在 chatgpt.com 域成功拿到 access_token? **是的**(_fetch_access_token 调 /api/auth/session 200)。但 access_token 是 chatgpt.com 域的产物,与 auth.openai.com OAuth 流程的 sentinel/oauth-session 是两套机制。

### H3(P=中,~10%)Cloudflare 在 auth.openai.com 域识别不到合法访客指纹

**证据**:
- `_wait_for_cloudflare` 只在 chatgpt.com 上跑过,context 内 `_cfuvid` / `cf_clearance` 是 chatgpt.com 域的;auth.openai.com 域第一次访问没有这些。
- 现象上看到的 email input 不是 CF challenge 页(challenge 页是"Verify you are human"),所以 H3 不直接解释 email 表单的存在,但 CF 可能在某些场景下让 NextAuth 中间件无法读取 server-side session(中间被 CF 缓存劫持等)。

**反证检查**:
- CF challenge 会显示"Verify you are human",我们看到的是 `auth/login?prompt=...`,所以 H3 优先级降低。

### H4(P=低,~5%)session_token 已过期

**证据**:
- 如果是 round 8 残留 token,从用户测试时间点(2026-04-28 02:27)往前推,token 通常是 1d 有效(NextAuth 默认 30d,OpenAI 实测短一些)。
- 用户日志 `02:27:29 → 02:27:47`,说明 import_admin_session 步骤刚完成不久,token 不太可能此刻过期。

**反证检查**:
- 用 `__Secure-next-auth.session-token` 调 `/api/auth/session` 看返回。如果返回 200 + accessToken,token 没过期。

### H5(P=低,~3%)code_challenge / state 在 OAuth redirect 中丢失

**证据**:
- 当前实现 PKCE 正常,redirect_uri 也正常。
- 但若 issuer 重定向链中把 query 截断、或者 prompt=consent 与某种 issuer 状态冲突,可能让流程提前结束。

**反证检查**:
- 上游 `_upstream_codex_auth.py:1017-1042` 用相同的 `_build_auth_url`,同款 PKCE,所以这条假设几乎不成立。

### H6(P=极低,~2%)Codex client_id 已 deprecated

**证据**:
- 子号 OAuth 路径用同款 `app_EMoamEEZ73f0CkXaXp7hrann` 工作正常,client_id 没问题。

---

### 综合诊断

**最可能的链条**:

```
H1 (session 在 auth.openai.com 域无效) 或 H2 (缺 OAuth 协同 cookie)
   ↓
GET /oauth/authorize?... 携带 __Secure-next-auth.session-token
   ↓
auth.openai.com NextAuth middleware
   ↓
session 检查失败 → 302 redirect /auth/login?callbackUrl=...
   ↓
本地 page.goto 跟随重定向 → 看到 email input → return None
```

**修复方向**(主路径):仿照子号 OAuth — **先让 chatgpt.com 域完整走一遍登录(start() 已经做),再让浏览器主动在 auth.openai.com 上执行一次"前导请求"**,例如:

```
page.goto("https://auth.openai.com/auth/session")
   → 让 issuer 同步 chatgpt.com session 到自家 server-side ledger
   → 然后再 goto(auth_url) 才有效
```

或者**改用 HTTP API 路径**(类似 gpt-auto-register):
- 用 curl_cffi 模拟 Chrome 直接 POST `/oauth/authorize` 的内部 API(/api/accounts/authorize/continue)

但这超出本研究范围 — 实施由后续 implement 阶段决定。

---

## §6 Diagnostic Commands(用户可手动跑确认假设)

### 6.1 验证 session_token 在 chatgpt.com 域有效

```bash
SESSION="<state.json 里的 session_token>"

curl -s -i "https://chatgpt.com/api/auth/session" \
  -H "Cookie: __Secure-next-auth.session-token=$SESSION" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
# 期望:200 + JSON 含 accessToken
```

### 6.2 验证 session_token 在 auth.openai.com 域是否有效

```bash
curl -s -i "https://auth.openai.com/oauth/authorize?\
client_id=app_EMoamEEZ73f0CkXaXp7hrann&response_type=code&\
redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback&\
scope=openid%20email%20profile%20offline_access&state=test&\
code_challenge=test&code_challenge_method=S256&prompt=consent" \
  -H "Cookie: __Secure-next-auth.session-token=$SESSION" \
  -L --max-redirs 2
# 期望:200 + 同意页 HTML
# 实际:大概率 302 → /auth/login(印证 H1/H2)
```

### 6.3 看 NextAuth 子域 CSRF 端点是否需要预热

```bash
curl -s -i "https://auth.openai.com/api/auth/csrf" \
  -H "Cookie: __Secure-next-auth.session-token=$SESSION"
# 期望:200 + JSON {"csrfToken": "..."}
# 用这个 csrfToken 加到下一次请求的 cookie:__Host-next-auth.csrf-token=<csrfToken>|<hash>
```

### 6.4 逐步重现本地行为

```bash
# 在 Python 内
python3 -c "
from autoteam.codex_auth import login_codex_via_session
from autoteam.admin_state import get_admin_session_token, get_chatgpt_account_id
print('session_len:', len(get_admin_session_token() or ''))
print('account_id:', get_chatgpt_account_id())
bundle = login_codex_via_session()
print('bundle:', bundle)
"
# 观察 screenshots/codex_main_*.png
```

### 6.5 比对子号路径 vs 主号路径的 cookie 实际落地

```bash
# 在 chatgpt.start() 之后、login_codex_via_session 第 1078 行之前打个断点
# 或临时加日志:
python3 -c "
from playwright.sync_api import sync_playwright
from autoteam.config import get_playwright_launch_options
from autoteam.chatgpt_api import ChatGPTTeamAPI
api = ChatGPTTeamAPI()
api.start()
print('chatgpt.com cookies:')
for c in api.context.cookies('https://chatgpt.com'):
    print('  ', c['name'], '=', c['value'][:30], '...')
print('auth.openai.com cookies:')
for c in api.context.cookies('https://auth.openai.com'):
    print('  ', c['name'], '=', c['value'][:30], '...')
api.stop()
"
# 期望对照:auth.openai.com 域下 chatgpt.start() 之后是否真的没有 cookie
```

### 6.6 验证 H1 — 主动让浏览器走一次 chatgpt.com → auth.openai.com 的 SSO 中转

```bash
python3 -c "
from playwright.sync_api import sync_playwright
from autoteam.config import get_playwright_launch_options
from autoteam.chatgpt_api import ChatGPTTeamAPI
api = ChatGPTTeamAPI()
api.start()
page = api.context.new_page()
# 主动访问 chatgpt.com 设置页,该页通常会触发到 auth.openai.com 的同步
page.goto('https://chatgpt.com/#settings')
import time; time.sleep(5)
# 看 auth.openai.com 域是否多了 cookie
for c in api.context.cookies('https://auth.openai.com'):
    print(c['name'])
"
```

---

## §7 实施关注点(给后续 implement 用)

不属于本研究输出,但下游会问:

1. **首选修复方向 — SSO 中转**:在 `login_codex_via_session` 第 1098 行 `page.goto(auth_url)` 之前,先 `page.goto("https://auth.openai.com/auth/session")` 或 `page.goto("https://auth.openai.com/api/auth/session")`,让 issuer 同步建 server-side session。
2. **次选修复方向 — HTTP 流程**:仿 `gpt-auto-register/app/oauth_service.py:511-595` 用 curl_cffi 走 OAuth,不用 Playwright。需要新增依赖。
3. **诊断不足以定论时**:加 Round 9 的 `_build_auth_url + page.goto` 中间打 `await context.cookies("https://auth.openai.com")` 日志,看 cookie 落地情况。

---

## Caveats / Not Found

- 用户提到的 `/api/admin/session-token` 端点**不存在**;实际名字是 `/api/admin/login/session`(api.py:1223)。建议在 PRD 里改一下命名以免后续混淆。
- 本研究**未实测 H1**;`H1 vs H2` 的二选一需要用户跑 §6.2 / §6.6 看到证据后才能定。
- 上游 cnitlrt 已经把 `login_codex_via_session` 改成 `SessionCodexAuthFlow`-based(`_upstream_codex_auth.py:1017-1043`),但**仍然只注入相同的三件套 cookie**(L1165-1218),意味着上游同款代码也可能撞到这个 bug。我们没拿到上游的实际生产运行证据来证伪 H1。
- `__Host-next-auth.csrf-token` 在 NextAuth GET /oauth/authorize 不一定是必需的(它主要保护 POST /api/auth/signin/credentials),所以"缺 csrf-token"的解释证据偏弱。CF 三件套的解释更强。
- 没做静态深度对比 chatgpt.com vs auth.openai.com 的 NextAuth 配置(后端不开源),所以"server-side session ledger"的存在性是合理推测,无直接证据。

---

## File Paths Index

- 本地 `D:\Desktop\AutoTeam\src\autoteam\codex_auth.py` — 失败位置 + 入口包装
- 本地 `D:\Desktop\AutoTeam\src\autoteam\chatgpt_api.py` — cookie 注入与 session 读取
- 本地 `D:\Desktop\AutoTeam\src\autoteam\api.py` — `/api/admin/login/session` endpoint
- 本地 `D:\Desktop\AutoTeam\src\autoteam\oauth_workspace.py` — Round 8 oai-oauth-session 解码与 sentinel-token 注释
- 本地 `D:\Desktop\AutoTeam\src\autoteam\admin_state.py` — state.json schema(无 csrf/cf cookie 字段)
- 本地 `D:\Desktop\AutoTeam\src\autoteam\manager.py` — `_run_post_register_oauth` 子号路径(成功路径,Round 8 oauth_workspace 应用点)
- 上游 `D:\Desktop\AutoTeam\.trellis\tasks\04-28-master-codex-oauth-session-fallback\research\_upstream_codex_auth.py` — cnitlrt main 分支拷贝(2026-04-26 版本)
- Round 8 研究 `D:\Desktop\AutoTeam\.trellis\tasks\04-27-master-team-degrade-oauth-rejoin\research\sticky-rejoin-mechanism.md` — sticky-default 与 OAuth 内部机制研究
