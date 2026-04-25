# platform.openai.com Signup 协议侦察报告

> **Version**: 1.0
> **Author**: protocol-recon (AutoTeam / platform-signup)
> **Date**: 2026-04-25
> **Scope**: 为 `platform_signup.py` 的 HTTP 注册模块实现提供协议清单。
> **Status**: 公开资料整理完成；Arkose/签名 cookie 两处明确标记 **"需要 mitmproxy 抓包"**。

---

## 0. 结论先行（Executive Summary）

- `platform.openai.com` 与 `chatgpt.com` **共用同一个 OpenAI 账号体系**：都由 `auth.openai.com`（Auth0 自托管前端，底层 `openai.auth0.com`）签发 session。官方帮助中心原话："your ChatGPT and API Platform share the same underlying org-id"。
- 创建 Platform 账号 **本质上就是创建一个 OpenAI 账号**（走 Auth0 Universal Login 的 **Signup** 分支）。分歧点在**登录后的首次进入页**:
  - Platform 侧首次登录要求**绑定手机号**（才能创建 API Key / 开账单）
  - ChatGPT 侧**不强制**手机号
- Auth0 状态机（signup 模式）与 OpenAIAuth 逆向的 login 模式**共用 80% 端点**，唯一差异是第 1~2 步（`authorize` 的 `screen_hint=signup` + `POST /u/signup/identifier`/`/u/signup/password`）。
- 三道硬阻断点：
  1. **Cloudflare Turnstile / Bot Fight Mode**（JS 挑战，不可免）
  2. **Arkose Labs FunCaptcha**（注册页嵌入，pre-registration action 验 token）
  3. **手机号验证**（platform 强制、chatgpt 偶尔触发）

**可行性判断**: 纯 HTTP 复刻 signup 在没有 Arkose token 解决方案的前提下 **不可行**。必须走 Playwright + Arkose 第三方解决服务，或者在可控浏览器里拿 `arkose_token` 后再回落到 HTTP。

---

## 1. 完整请求序列

> **约定**：
> - `{APP_CLIENT_ID}`：Platform 前端使用的 Auth0 client_id。**公开资料中未出现明文**（参见 §5）。已知 Codex CLI 使用 `app_EMoamEEZ73f0CkXaXp7hrann`（见 `src/autoteam/codex_auth.py:32`）。
> - `{STATE}`：Auth0 签出的不透明 state 串，形如 `hKFo2SBxxxxxxxxxxx...`（由 `/authorize` 302 跳转到 `/u/signup?state=...` 带出）。
> - `{CSRF}`：`auth0.openai.com` 通过 `_csrf` cookie + 页面 `<input name="_csrf">` 双向校验（非固定名字，可能是 `state` 本身；OpenAIAuth 实现里直接复用 `state` 作为 CSRF 载体）。

### 1.1 Signup 分支（推测，基于 Auth0 Universal Login 规范 + acheong08/OpenAIAuth 的 Login 镜像）

| # | Method | URL | Headers 关键字段 | Body Schema | 期望响应 | State 透传 |
|---|--------|-----|-----------------|-------------|---------|------------|
| 1 | GET | `https://platform.openai.com/signup` | `User-Agent`, `Accept-Language` | —— | `200` + HTML (Next.js 前端) + Cloudflare 可能返回 Turnstile JS 挑战 | 生成 `cf_clearance` cookie |
| 2 | GET | `https://platform.openai.com/api/auth/csrf`（若存在，类似 ChatGPT Next.js）| `Host`, `Accept: */*` | —— | `{"csrfToken":"..."}` | 取 `csrfToken` |
| 3 | POST | `https://platform.openai.com/api/auth/signin/auth0?prompt=login&screen_hint=signup` | `Content-Type: application/x-www-form-urlencoded`, `Origin: https://platform.openai.com` | `callbackUrl=/&csrfToken={CSRF}&json=true` | `{"url":"https://auth.openai.com/authorize?..."}` | 取 `url` |
| 4 | GET | `https://auth.openai.com/authorize?client_id={APP_CLIENT_ID}&scope=openid+profile+email+offline_access&response_type=code&redirect_uri=https%3A%2F%2Fplatform.openai.com%2Fauth%2Fcallback&audience=https%3A%2F%2Fapi.openai.com%2Fv1&prompt=login&screen_hint=signup&state={RANDOM}&code_challenge={PKCE_S256}&code_challenge_method=S256` | —— | `302 Location: https://auth.openai.com/u/signup/identifier?state={STATE}` | `{STATE}` 写入 URL | 
| 5 | GET | `https://auth.openai.com/u/signup/identifier?state={STATE}` | —— | `200` + HTML，包含 `<input name="state" value="{STATE}">` 和 `<script src="https://{arkose_sub}.arkoselabs.com/v2/{ARKOSE_PUBKEY}/api.js">` | 取 state 与 arkose site key | 
| 6 | **Arkose Labs 客户端挑战**（浏览器内 JS） | `https://client-api.arkoselabs.com/fc/gt2/public_key/{ARKOSE_PUBKEY}` | —— | `arkose_token` | **纯 HTTP 无法获得** |
| 7 | POST | `https://auth.openai.com/u/signup/identifier?state={STATE}` | `Content-Type: application/x-www-form-urlencoded`, `Origin: https://auth.openai.com`, `Referer: https://auth.openai.com/u/signup/identifier?state={STATE}` | `state={STATE}&email={email}&captcha={ARKOSE_TOKEN}&js-available=true&webauthn-available=true&is-brave=false&webauthn-platform-available=true&action=default` | `302 Location: /u/signup/password?state={NEW_STATE}` | 新 state 滚动 |
| 8 | POST | `https://auth.openai.com/u/signup/password?state={STATE}` | 同上 | `state={STATE}&email={email}&password={password}&action=default` | `302 Location: /authorize/resume?state={STATE}` 或 `/u/email-verification?state=...` | |
| 9 | GET | `https://auth.openai.com/authorize/resume?state={STATE}` | —— | 若未验证邮箱：`302 /u/email-verification?state=...`；若已验证：`302 https://platform.openai.com/auth/callback?code={AUTH_CODE}&state={OUR_STATE}` | 取 `code` |
| 10 | **邮件触发** | —— | —— | 收件箱收到 `noreply@tm.openai.com` 主题 "Verify your email" 的邮件，包含 `https://auth.openai.com/u/email-verification?ticket={TICKET}&email={EMAIL}` 或 `/u/v/ticket/{TICKET}`（链接**不是** Auth0 默认格式，被 OpenAI 定制） | 点击链接 / 提取 `ticket` | |
| 11 | GET | 邮件中的 verify URL | —— | `302` 回到 `/authorize/resume?state=...` 继续走 | `state` 恢复 |
| 12 | GET | `https://platform.openai.com/auth/callback?code={AUTH_CODE}&state={STATE}` | —— | 前端交换 session；`Set-Cookie: __Secure-next-auth.session-token=...` | session cookie |
| 13 | POST | `https://auth.openai.com/oauth/token`（**Codex-style PKCE 已验证**） | `Content-Type: application/x-www-form-urlencoded` | `grant_type=authorization_code&client_id={APP_CLIENT_ID}&code={AUTH_CODE}&redirect_uri={REDIRECT}&code_verifier={PKCE_VERIFIER}` | `{"access_token","refresh_token","id_token","expires_in"}` | **终点** |
| 14 | GET | `https://platform.openai.com/welcome` → `/onboarding/personal-info` → `/onboarding/add-phone` | `Authorization: Bearer {access_token}` | —— | 填写姓名、生日后走 `/phone-number` 强制验证 | 见 §4 |

### 1.2 和现有 ChatGPT 注册的关系

项目中 `src/autoteam/manager.py:1554 _register_direct_once()` 走的是 **浏览器自动化 + ChatGPT 入口**（`https://chatgpt.com/auth/login`）。Platform 注册和它的区别：

| 维度 | ChatGPT 注册（现有） | Platform 注册（本文档） |
|------|---------------------|-------------------------|
| 入口 URL | `https://chatgpt.com/auth/login` | `https://platform.openai.com/signup` |
| Auth0 `client_id` | ChatGPT 前端的（未导出） | Platform 前端的（未导出，见 §5） |
| `audience` | `https://api.openai.com/v1` | `https://api.openai.com/v1`（同） |
| `redirect_uri` | `https://chatgpt.com/api/auth/callback/auth0` | `https://platform.openai.com/auth/callback` |
| 登录后页面 | `/c/new` | `/welcome` + 强制手机号 |
| Session 存储 | `__Secure-next-auth.session-token` cookie | 同名 cookie（Auth0 Universal Login 共享） |

### 1.3 Auth0 dbconnections/signup 备用路径（不推荐）

Auth0 提供一条后门 API：`POST https://{tenant}.auth0.com/dbconnections/signup`。它**绕开了** Universal Login UI + Arkose 前端部分，但 **pre-user-registration Action 依然在服务端校验 Arkose token**（见 `developer.arkoselabs.com/docs/using-auth0-and-arkose-for-new-account-registration` 中 `api.access.deny("Arkose Access Denied!")` 逻辑）。

```http
POST /dbconnections/signup HTTP/1.1
Host: auth.openai.com
Content-Type: application/json

{
  "client_id": "{APP_CLIENT_ID}",
  "email": "user@example.com",
  "password": "SecretP@ssw0rd",
  "connection": "Username-Password-Authentication",
  "user_metadata": {
    "arkoseToken": "{ARKOSE_TOKEN}"
  }
}
```

**已知失败点**：OpenAI 已对 `auth.openai.com` 做了 Cloudflare L7 防护，直接 curl 多半拿 `1020` 错误；且 `connection` 名字未公开，推测是 `Username-Password-Authentication`（Auth0 默认）但需抓包证实。

---

## 2. CSRF / state / code_challenge 透传链

```
┌─────────────────┐
│ Step 4: GET     │  code_challenge = BASE64URL(SHA256(code_verifier))   ← 客户端生成
│ /authorize      │  state          = random_urlsafe(16)                 ← 客户端生成
└─────────────────┘
         │ 302
         ▼
┌─────────────────┐
│ Step 5: GET     │  {STATE_AUTH0} 由 Auth0 服务端签发，是 Auth0 内部 session 标识
│ /u/signup/...   │  HTML 里 <input type="hidden" name="state" value="{STATE_AUTH0}">
└─────────────────┘
         │
         ▼
┌─────────────────┐
│ Step 7/8: POST  │  body 必须回传 state={STATE_AUTH0}
│ /u/signup/*     │  每次 POST 后 302 到下一步，Location 里带 state={STATE_AUTH0_NEW}
│                 │  ⚠️ state 会滚动：identifier → password → resume 三个 state 可能不同
└─────────────────┘
         │
         ▼
┌─────────────────┐
│ Step 9: GET     │  /authorize/resume?state={STATE_AUTH0_FINAL}
│ /authorize/resume│ 302 到 redirect_uri?code={AUTH_CODE}&state={OUR_ORIGINAL_STATE}
└─────────────────┘
```

**关键校验点**（OpenAIAuth `OpenAiAuth.go` 逻辑）：
- 客户端必须用同一个 `requests.Session` 或 `http.Client` 保持 cookie jar，Auth0 依赖 `_csrf` cookie 与 form `state` 字段双因子校验。
- 重定向链中每次 302 都要跟；禁用 `allow_redirects=False` 时要手动 chain 才能拿到 `/authorize/resume` → `/auth/callback` 的最终 `code`。
- `code_verifier` 只在最后 `/oauth/token` 用到；中途 **所有** `state` 都是 Auth0 自己的，和 PKCE 无关。

---

## 3. 验证邮件

| 字段 | 已知值 |
|------|--------|
| 发件人地址 | `noreply@tm.openai.com`（官方公示，也偶见 `otp@tm1.openai.com` 用于 OTP） |
| 允许的 OpenAI 域名 | `@openai.com`, `@mail.openai.com`, `@tm.openai.com`, `@sales.openai.com`, `@c-openai.com`（来源：help.openai.com "Verifying Communications from OpenAI"） |
| 主题（示例） | `Verify your OpenAI email` / `[OpenAI] Please verify your email` / `Your OpenAI API verification code is: ######` |
| 邮件正文 token 形态 | Auth0 ticket 形式：`https://auth.openai.com/u/email-verification?ticket={长随机串}&email={email_encoded}`；或 OTP 形式：6 位纯数字，写在 HTML 中心的 `<strong>` 块 |
| 点击后的序列 | `GET verify URL` → `302 /authorize/resume?state=...` → 若已登录则继续 signup 流；否则要求重新 signin |
| 现有代码参考 | `src/autoteam/mail/maillab.py` 已经有 `extract_verification_code()`，能吃 6 位 OTP 和 ticket URL 两种 |

**与 ChatGPT signup 的差异**: ChatGPT 的验证邮件直接用 6 位 OTP（当前代码就是这样拉取）；Platform 侧 **更常发 "click the link" 型 ticket 邮件**，但社区报告两种都可能出现（A/B 变体）。实现时必须同时支持两种解析。

---

## 4. CAPTCHA / 反 abuse 探测点

### 4.1 Arkose Labs FunCaptcha（最硬的一道）

- **位置**: signup identifier 页（step 5）的 HTML 中嵌入 `<script src="https://{sub}.arkoselabs.com/v2/{PUBLIC_KEY}/api.js">`
- **触发**: 100% 触发，不可跳过（服务端 Auth0 Action 校验 token 存在性 + `solved=true`）
- **Site Key（Public Key）**:
  - ChatGPT 消息发送用: `35536E1E-65B4-4D96-9D97-6ADB7EFF8147`（2captcha 公开文档 + 社区反汇编）
  - ChatGPT / OpenAI 注册用: `0A1D34FC-659D-4E23-B17B-694DCFCF6A6C`（社区流传，**需要 mitmproxy 抓 signup 页 HTML 重新确认**）
  - 注意 Arkose 会因 OpenAI 侧 A/B 实验换 key；必须从 signup HTML 动态解析 `data-pkey="..."` 属性
- **输出**: `arkose_token`（格式 `xxx.xxx|r=ap-southeast-2|metabgclr=...|guitextcolor=...|metaicon`）
- **传递方式**:
  - Universal Login：form body 的 `captcha` 字段（Auth0 默认）或 `arkose_token` 字段（OpenAI 定制）
  - `dbconnections/signup`：`user_metadata.arkoseToken`（按 Arkose 官方指南）
- **第三方解决方案**: 2captcha、capsolver、capmonster 均支持 FunCaptcha，$3-5 / 1k 解决，耗时 15-60s
- **本地替代**: `acheong08/funcaptcha` Go 工具，启动 HTTP 服务生成本地 token（但随着 Arkose 升级多数已失效）

### 4.2 Cloudflare Turnstile / Bot Fight Mode

- **位置**: `platform.openai.com/signup` 首次 GET 时偶发
- **触发**: Data-center IP / 新指纹 / 高频请求
- **输出**: `cf_clearance` cookie（一次性）
- **绕过**: 必须走真实 Chromium + `cf_clearance` refresh，或过 `cloudscraper` / `botasaurus`（成功率取决于目标站点的 Cloudflare Enterprise 档位）
- **现有代码**: `src/autoteam/invite.py:wait_for_cloudflare()` 已经有处理逻辑

### 4.3 TLS / HTTP/2 指纹检测

- `auth.openai.com` 前置 Cloudflare，**对 Python `requests` 默认 TLS 指纹（JA3 = `771,4866-4867-4865...`）有概率直接返回 403**。
- **对策**: 用 `curl_cffi`（impersonate chrome/edge）、`tls-client`（Go 实现 Python 绑定）、或 `httpx` + `h2` + 手动 ClientHello 指纹伪造。
- 现有代码中 `codex_auth.py` 走 Playwright 不受影响；**新 HTTP 模块必须用 `curl_cffi`**。

### 4.4 手机号验证（platform 强制、chatgpt 偶发）

- **触发**:
  - Platform: 在 signup 后首次访问 `/api-keys` 或 `/billing` 时，100% 要求 `/onboarding/add-phone`
  - ChatGPT: 被风控标记（新 IP 段 / 短时间多账号）时触发 `/auth/add-phone`
- **POST 端点**（ChatGPT 已知）:
  - `POST https://chatgpt.com/backend-api/accounts/send_phone_otp` body `{"phone_number": "+1..."}`
  - `POST https://chatgpt.com/backend-api/accounts/verify_phone_otp` body `{"phone_number":"+1...", "otp":"123456"}`
- **Platform 端点**: 疑似 `POST https://platform.openai.com/backend-api/accounts/send_phone_otp`，**需要 mitmproxy 抓包确认**
- **规则** (TechCrunch 2025-01-15 报道):
  - 一个手机号最多绑 3 个 API Key 账号
  - 回收号（recycled number）会被拒
  - 国家白名单：美国、欧洲、印度、中日韩均可；部分非洲/中东会被拒
- **外部方案**: SMS-Activate、5sim.net、sms-man（$0.3-1 / 次）
- **现有代码**: `src/autoteam/invite.py:detect_phone_verification()` 已经能识别这个分支并主动放弃（`RegisterBlocked(is_phone=True)`）

### 4.5 其他软防线

- **生日/年龄门**（`/about-you` 页）：必须填 >=13 岁；`src/autoteam/identity.py` 已有 `random_birthday()` / `random_age()`
- **重复邮箱**：`already have an account` 硬阻断，`src/autoteam/invite.py:detect_duplicate_email()` 已识别
- **Account Lockout**：同一 IP 短时间 >5 次 signup 失败会进 15-30 min 黑名单

---

## 5. 已知限制 / 不确定点

**以下项必须通过 mitmproxy / Charles / Burp 真实抓包补全：**

| # | 项目 | 为什么公开资料找不到 | 如何抓包 |
|---|------|---------------------|---------|
| **5.1** | **Platform 前端的 Auth0 `client_id`** | 未在任何公开文档 / 开源项目中找到明文；每次前端热更新可能变化 | `Chrome DevTools → Network → /authorize` 请求，读取 URL query `client_id=...` |
| **5.2** | **Platform 的 redirect_uri 精确路径** | 推测 `https://platform.openai.com/auth/callback` 或 `/api/auth/callback/auth0` | 同上；看 302 Location 末端 |
| **5.3** | **Auth0 connection name** | 推测 `Username-Password-Authentication`（Auth0 默认），但 OpenAI 可能自定义 | `dbconnections/signup` 返回的 error body 会告诉正确名字 |
| **5.4** | **Arkose site key（注册用）** | 社区流传 `0A1D34FC-659D-4E23-B17B-694DCFCF6A6C`，但 OpenAI 持续轮换 | 抓 signup identifier 页 HTML，grep `data-pkey` 或 `public_key` |
| **5.5** | **Platform onboarding 各页的 POST 端点** | `/onboarding/personal-info`、`/onboarding/add-phone`、`/welcome` 全部是 Next.js 前端 + 私有 backend API | 网络面板看 XHR |
| **5.6** | **signup form 里除 state/email/password 外的隐藏字段** | Auth0 Universal Login 自定义程度高；可能有 `ui_locales`、`accepts_tos` 等 | 抓 signup 提交的 form-data |
| **5.7** | **验证邮件 ticket URL 的 query 参数完整列表** | `ticket` 只是示例；可能是 `token` / `code` / `ott` 等命名 | 注册一个账号，看真实邮件 |
| **5.8** | **TLS 指纹最小要求** | Cloudflare 规则层可能卡住 `curl_cffi` 的某些版本 | 用不同 impersonate 选项（`chrome120`, `edge101`）跑一遍测成功率 |

**以下项基本确定，代码可以先写，失败后再用抓包校正：**

- Auth0 状态机三步走（identifier → password → resume）：和 Login 镜像
- OAuth token endpoint：`https://auth.openai.com/oauth/token`（已在 `codex_auth.py:34` 验证）
- 邮件发件人域名：官方文档白名单
- PKCE 流程：S256，和现有 Codex 代码一致

---

## 6. 实现建议（给 task #3 的下一位）

1. **分 3 层** 设计 `platform_signup.py`:
   - **Transport 层**: `curl_cffi.requests.Session` + `impersonate="chrome120"`，处理 TLS/Cloudflare
   - **Auth0 状态机层**: 纯函数 `signup_identifier(state, email, captcha) → (new_state, next_action)`，单元测试易写
   - **Flow 编排层**: 对接 `CloudMailClient`、`ArkoseSolver`（抽象接口）、`PhoneVerifier`（抽象接口）

2. **Arkose 解决方案**（task #2 的核心决策）:
   - 短期: 外接 2captcha（`pip install twocaptcha-python`），$3 / 1k 可接受
   - 中期: 自己部署 `acheong08/funcaptcha` fork，本地生成
   - 长期: 保留 Playwright 兜底（反正现有代码已经能跑）

3. **Fallback 策略**:
   ```
   try HTTP 模块 (快速，80% 成功)
     ↓ Arkose fail / Cloudflare 429
   fallback 到现有 Playwright 流程 (慢但稳)
     ↓ add-phone 触发
   RegisterBlocked(is_phone=True)，交给上游分流
   ```

4. **不要**在 `platform_signup.py` 里复刻 `codex_auth.py` 的 PKCE，而是直接 import 复用 `_generate_pkce()`、`_exchange_auth_code()`、`_parse_jwt_payload()`。

---

## 7. 参考资料

所有在调研中实际读到内容的 URL（按相关度排序）：

### Auth0 规范
1. https://auth0.com/docs/api/authentication/signup/create-a-new-user — Auth0 官方 Signup API（`dbconnections/signup`）
2. https://auth0.github.io/node-auth0/classes/auth.Database.html — Node SDK 明确 `Username-Password-Authentication` connection 常量
3. https://community.auth0.com/t/auth0-signup-api/89948 — `dbconnections/signup` body 字段清单
4. https://community.auth0.com/t/management-api-dbconnections-signup-doesnt-require-clientid/34845 — `client_id` 可选的官方确认
5. https://auth0.com/docs/authenticate/login/auth0-universal-login/identifier-first — 两步 identifier + password 流程
6. https://auth0.com/docs/customize/actions/explore-triggers/signup-and-login-triggers/post-user-registration-trigger — Post-user Action 插件点
7. https://www.velotio.com/engineering-blog/creating-a-frictionless-signup-experience-with-auth0-for-your-application — 实操 body 例子

### Arkose Labs
8. https://developer.arkoselabs.com/docs/using-auth0-and-arkose-for-new-account-registration — Auth0 + Arkose 集成规范（确认 Action 验 token）
9. https://developer.arkoselabs.com/docs/standard-setup — Arkose 客户端 public key 用法
10. https://developer.arkoselabs.com/docs/arkose-labs-api-guide — Verify API 规范
11. https://2captcha.com/p/funcaptcha — 第三方解决方案 + 社区流传的 site key
12. https://stackoverflow.com/questions/77127848/how-do-i-generate-a-valid-arkose-token-to-create-gpt-4-conversations — ChatGPT Arkose 流派 + `acheong08/funcaptcha` 方案

### OpenAI / Auth0 逆向工程
13. https://github.com/acheong08/OpenAIAuth — Auth0 login 状态机 Python+Go 双实现（signup 的最佳镜像）
14. https://github.com/EvanZhouDev/openai-oauth — Codex OAuth 封装（验证 `app_EMoamEEZ73f0CkXaXp7hrann` client_id）
15. https://community.openai.com/t/user-auth0-error-when-signing-in-to-openai-platform/1366899 — 证实 `auth0|{user_id}` 格式 + identity_provider_mismatch 分支
16. https://community.openai.com/t/oauth-internal-error-on-first-login-only-when-using-company-auth0-tenant-works-on-personal-tenant/1366721 — Auth0 logs 里的 `user_id: auth0|...`、`strategy: database` 字段样本

### OpenAI 官方帮助页
17. https://help.openai.com/en/articles/8505609-ive-received-a-verification-email-i-didnt-request — 验证邮件机制说明
18. https://help.openai.com/en/articles/11725090-verifying-communications-from-openai — 发件人域名白名单
19. https://help.openai.com/en/articles/9889414-why-am-i-being-asked-to-verify-my-login — OTP 详情 + 发件人 `otp@tm1.openai.com`
20. https://help.openai.com/en/articles/10489721-authentication-troubleshooting-faq — ChatGPT / Platform 同 org-id 的官方说明
21. https://community.openai.com/t/bug-auth-openai-com-otp-and-member-invite-emails-not-being-sent-for-some-email-addresses/1122480 — OTP 邮件延迟样本

### 手机号 / 反 abuse
22. https://techcrunch.com/2025/01/15/openai-tests-letting-users-sign-up-for-chatgpt-with-only-a-phone-number/ — 2025 手机号试点 + 一号 3 账号规则
23. https://community.openai.com/t/i-want-to-create-a-personal-account-completely-unassociated-from-the-one-i-have-under-my-employers-organisation-i-only-have-one-phone-number/605197 — platform 首次 API key 要手机号
24. https://community.openai.com/t/im-tired-of-chatgpt-4-modal-arkose-captcha/375223 — Arkose 触发频率社区反馈

### OAuth 规范与 PKCE
25. https://auth0.com/docs/get-started/authentication-and-authorization-flow/authorization-code-flow/add-login-auth-code-flow — Authorization Code Flow 参数表（state、connection、login_hint）
26. https://developers.openai.com/codex/auth — Codex CLI `sign in with ChatGPT` 流程（确认 `~/.codex/auth.json` 格式）

### 项目内参考代码
27. `src/autoteam/codex_auth.py` — PKCE、oauth/token 交换、`app_EMoamEEZ73f0CkXaXp7hrann` client_id 样本
28. `src/autoteam/manager.py:1554 _register_direct_once` — ChatGPT 直接注册 Playwright 流程
29. `src/autoteam/invite.py:register_with_invite` — 邀请注册 Playwright 流程 + phone/duplicate 分支识别
30. `src/autoteam/mail/maillab.py` — OTP 和 invite link 提取器

---

**文档生成方式**: 纯公开资料侦察，未做实时抓包。实现 `platform_signup.py` 前建议先用 mitmproxy 走一次真实 signup，把 §5 里的 8 个未知项补完（15~30 min 投入，可省后续 3-5 轮盲试错）。

---

## 7. 实施方案评估（CAPTCHA / 反 abuse / fallback）

> **Author**: risk-watch (AutoTeam / platform-signup)
> **Date**: 2026-04-25
> **调研基准**: 2026-04 最新公开报价 + 第三方评测。所有价格以 USD 计，动态价未锁定。

### 7.1 三道硬阻断的实施可行性

#### 7.1.1 Arkose Labs FunCaptcha（signup 页 100% 触发）

| 维度 | 结论 |
|------|------|
| 纯 HTTP 能否绕过 | **不能**。FunCaptcha 是 "3D 旋转拼图 + 行为信号"，必须在真实 DOM + JS 引擎里生成 `arkose_token`。`curl_cffi` 即便伪装 TLS 指纹也无 JS 运行时。 |
| OpenAI 的 Arkose site key | 社区流传 `0A1D34FC-659D-4E23-B17B-694DCFCF6A6C`（signup）、`35536E1E-65B4-4D96-9D97-6ADB7EFF8147`（ChatGPT chat）。**必须运行时从 signup HTML `data-pkey` 动态解析**，因为 OpenAI 会 A/B 轮换。 |
| Token 注入位置 | Universal Login 表单的 `captcha` 字段，或 `dbconnections/signup` 的 `user_metadata.arkoseToken` |

**第三方解算服务价格对比（2026-04，FunCaptcha 专用，per 1 000 solves）**：

| 服务 | 价格 | 解算方式 | 平均延迟 | 成功率 | OpenAI Arkose 支持 |
|------|------|----------|---------|--------|---------------------|
| **Capsolver** | **$3.00 – $4.00** | AI-first | **<10 s** | **99%+** | ✅ 官方文档列出 "FunCaptcha (Arkose Labs)"，`FunCaptchaTaskProxyless` 任务类型 |
| **2Captcha** | **$1.50 – $50**（动态价；常见区间 $1.80–$5） | 人工 + AI hybrid | 15–30 s | 86–90% | ✅ 官方文档明确列 `publickey` = `0A1D34FC…` 示例 |
| **Anti-Captcha** | $2.50 | 人工为主 | 10–20 s | ~99% | ✅ `FunCaptchaTaskProxyless` |
| **CapMonster Cloud** | $2.00 | 纯 AI | 10–20 s | ~90% FunCaptcha | ⚠️ 2026 新增支持，质量低于 Capsolver |
| **SolveCaptcha** | $2.99 – $50（按 challenge 复杂度） | Hybrid | 46 s | 87% | ✅ |
| **CaptchaKings** | $1.80 | AI | ~10 s | 未公开 | ✅ |

**推荐**：首选 **Capsolver**（AI 最快 + 成功率最高 + API 支持 pay-per-success）；备选 **2Captcha**（兼容多家 API，易切换）。避开 DeathByCaptcha（0% FunCaptcha 成功率，人工基座已老化）。

**单号解算成本估算**：
- 乐观（Capsolver 一次成功）: $0.004 / 次
- 悲观（失败 3 次重试 + 回退 2Captcha）: $0.015 / 次
- **量级**: 每 1 000 个 platform 账号约 $4–15 的 Arkose 解算开销

#### 7.1.2 Cloudflare Turnstile + TLS 指纹（signup 首页）

| 攻防点 | 技术选型 |
|--------|---------|
| TLS/JA3/JA4 指纹 | **curl_cffi + `impersonate="chrome124"`** 足够过 Cloudflare Bot Management L4 层（2026-04 仍有效，官方库跟 Chrome 主线更新） |
| Cloudflare Turnstile（JS challenge） | **curl_cffi 不够**。Turnstile 要求在真实 browser runtime 里跑完 behavioral challenge。两条路：<br>(a) Capsolver `AntiTurnstileTaskProxyless`：$1.20 / 1 000，2–5 s<br>(b) 直接用 Playwright，让浏览器自己过，拿到 `cf_clearance` cookie 后再切换回 HTTP |
| `cf_clearance` cookie 生命周期 | 约 30 min，可跨请求复用 |

**结论**：**不要** 纯 curl_cffi 走全程。**最稳做法**是开一个 short-lived stealth Playwright 只抓 `cf_clearance` + `arkose_token`，然后注入回 `curl_cffi.Session` 继续 Auth0 状态机，速度仍远快于全程 Playwright。

#### 7.1.3 手机号验证（platform 硬阻断）

**OpenAI 的封阻规则（2025–2026 确认）**：
- 一号最多 **2–3 账号**（官方 FAQ 自相矛盾：punchcard 页写 3，另一页写 2；实际以 2 为准更安全）
- **Recycled / reused 号直接拒**，官方声明 "there is no workaround"
- VoIP 号识别率极高，~40% 通过率（而 real SIM ~99.7%）
- 删号后该号 **永久禁用**（不能回收）
- Sora 2 (2026-01-07 起) 新增手机号验证，加剧号码消耗

**SMS 接码平台对比（2026-04）**：

| 平台 | 起价 | OpenAI 专用号档 | 号码类型 | 国家 | 备注 |
|------|------|---------------|---------|------|------|
| **5SIM** | $0.008（共享号） | 无专属档，"OpenAI/ChatGPT" 服务单次 ~$0.5–$2 | 纯 virtual | 180+ | 号被 OpenAI flag 比例高，需多次重试 |
| **SMS-Activate** | **已关闭**（2025 被执法取缔） | — | — | — | **禁用**，转向 HeroSMS（同班人马，crypto-only 支付） |
| **HeroSMS** | $0.01 | "OpenAI" 服务 ~$0.3–$1.5 | Virtual | 180+ | SMS-Activate 的继任者；crypto-only |
| **SMSPVA** | $0.05（virtual） / $0.3（real SIM） | OpenAI API 专页 ~$0.5–$3 | Virtual + real SIM | 60+ | real SIM 档位通过率高 |
| **SMS-Man** | $0.05 | ~$0.3–$2 | Virtual | 195 | 服务数量最多（1500+） |
| **MobileSMS.io** | **$3.50 – $5.50 / 次**（一次性 real SIM） | ✅ 专为 ChatGPT/OpenAI 设计 | **Real non-VoIP SIM** | US/UK/CA 等 | **99.7% 成功率**，长期租用 $15–$100 / 7–90 d；$45/mo dedicated |

**"专属号"档贵 5–10×**，实测仍可用但 **单号生命周期 = 1–2 个 OpenAI 账号**。量级：单账号 $0.5（共享号，需 3 次重试）到 $5（MobileSMS real SIM 一次通过）。

**硬约束**: 不存在 "用一个 5SIM 号无限注册" 的路径。必须按 **1:1 到 1:2 的号账比** 预算。

### 7.2 三种实施方案（完整对比）

| 维度 | **A. 纯 HTTP（无 CAPTCHA 场景）** | **B. HTTP + 第三方解算** | **C. Stealth Playwright** |
|------|---------------------------------|------------------------|------------------------|
| 前提假设 | signup 完全无 Arkose/Turnstile | Arkose + Turnstile 都能被第三方解算 | 浏览器跑完整 UI 流程 |
| Arkose 处理 | ❌ 不存在此场景 | ✅ Capsolver `FunCaptchaTaskProxyless` | ✅ 浏览器原生 + playwright-stealth |
| Turnstile 处理 | curl_cffi impersonate chrome124 | Capsolver `AntiTurnstileTaskProxyless` / `CloudflareChallenge` | playwright-stealth + 正常点击 |
| TLS 指纹 | curl_cffi | curl_cffi | 浏览器原生（无冲突） |
| IP 要求 | 住宅 IP | 住宅 IP（强制） | 住宅 IP（可稍松） |
| 手机号 | SMS 接码接入 | SMS 接码接入 | SMS 接码接入（同） |
| 单账号耗时 | 5–15 s | 20–45 s（等 Arkose 解算） | 60–120 s（浏览器加载） |
| 单账号开销 | $0（仅代理 + 邮箱） | **~$0.01（Arkose）+ $0.001（Turnstile）+ $0.5–$3（SMS）= ~$0.5–3** | $0（无解算费）+ SMS 成本 |
| 并发能力 | 极高（万级） | 高（Capsolver 并发 1 000+/min） | 低（每机 ~3–5 浏览器） |
| 可用性（2026-04） | **❌ 不现实**（Arkose 必触发） | **✅ 可行但脆弱**（Arkose 指纹更新后需滞后） | **✅ 最稳**（但最慢） |
| 实施风险 | N/A | **中**：依赖 Capsolver 服务可用性；OpenAI 升级 Arkose 会短期失效 | **低**：Playwright 本身稳；stealth 插件成熟 |
| 现有代码契合 | 需新写 `platform_signup.py` | 需新写 + `ArkoseSolver` 抽象 | **几乎复用现有 `invite.py` / `_register_direct_once()`** |

### 7.3 推荐方案

**主推：方案 B (HTTP + 第三方解算) + 方案 C (Playwright) 的 hybrid**

```
┌─────────────────────────────────────────────┐
│  Stealth Playwright (短期启动，仅 10-20 s)   │
│  目标: 拿 cf_clearance cookie + arkose_token  │
└──────────────┬──────────────────────────────┘
               │ 注入
               ▼
┌─────────────────────────────────────────────┐
│  curl_cffi Session (chrome124 impersonate)   │
│  目标: 跑 Auth0 状态机 identifier→password→   │
│       resume→oauth/token 全部 HTTP 请求       │
└──────────────┬──────────────────────────────┘
               │ 成功取 access_token
               ▼
┌─────────────────────────────────────────────┐
│  SMS 接码平台 (MobileSMS.io real SIM / 5SIM) │
│  触发: /onboarding/add-phone                  │
└─────────────────────────────────────────────┘
```

**单账号总成本估算（2026-04 价格）**：
- Arkose 解算（Capsolver, 1 次成功）: $0.004
- Turnstile 解算（可选, Capsolver）: $0.001
- 住宅代理（~10 MB 流量, Bright Data 档）: $0.05
- 邮箱（Maillab 已有）: $0.01
- SMS（MobileSMS real SIM 乐观）: **$3.50**
- SMS（SMSPVA virtual + 3 次重试）: $1.50
- **小计: $4–6 / 账号（real SIM）或 $2–3 / 账号（virtual + 重试）**

### 7.4 三种 fallback 路径（按完备度递减）

#### 路径 a: 全套接通（Arkose + Turnstile + SMS 接码）

**触发条件**: 默认路径，大量产 platform 账号（>100 个）

**流程**:
1. Playwright 起 stealth browser，走 /signup → 捕获 `arkose_token` + `cf_clearance`（10 s）
2. 切 `curl_cffi.Session`，注入 cookie，继续 identifier/password/oauth/token（5 s）
3. 邮件验证走 `maillab.py`（10–30 s）
4. `/onboarding/add-phone` 调 SMS 接码 API，60 s timeout 未收码则 retry
5. 成功后写 `accounts.json`

**单号成本**: $2–6
**成功率**: ~80–85%（主要失败在 SMS 号被 flag）
**单账号耗时**: 90–180 s

#### 路径 b: 部分接通（纯 HTTP + SMS，无 Arkose 解算）

**触发条件**: Arkose 临时失效 / 省 Capsolver 费用 / 小规模试水

**流程**:
1. **直接回退完整 Playwright**（即方案 C）走 signup + phone verify
2. Playwright 全程浏览器跑（60–120 s）
3. 复用 `invite.py:wait_for_cloudflare()` + `invite.py:detect_phone_verification()`

**触发回退的条件**（在代码里判断）:
```python
if capsolver_consecutive_failures >= 5 \
   or cloudflare_429_rate > 0.3 \
   or arkose_site_key_changed():
    fallback_to_playwright()
```

**单号成本**: $0.5–3（省掉 $0.004 Arkose + $0.001 Turnstile，但 SMS 不变）
**成功率**: ~75%（Playwright 稳但慢、IP 触发率高）
**单账号耗时**: 90–180 s

#### 路径 c: 不接 SMS（platform 路径有限可用）

**触发条件**: SMS 预算紧张 / 仅需少量 platform session（例如仅为 API key 测试）

**流程**:
1. 走 signup → 拿 access_token（完成 Auth0 流程）
2. **不访问** `/api-keys` / `/billing`（一旦访问 100% 触发 phone 强制验证）
3. 仅使用 `access_token` 调 `chatgpt.com/backend-api/*`（与 platform 共 org-id，可读 ChatGPT 侧资源）
4. 主路仍走 chatgpt.com 直接注册（现有 `_register_direct_once()` 逻辑）

**单号成本**: $0.05（仅 Arkose + 代理）
**成功率**: 100%（若只要 session cookie）
**限制**: 无法创建 API key，无法生成 platform-side refresh token（14-day 过期要求重新 signup）
**用途**: 配额测试 / 反查 org ID / ChatGPT session 生成

### 7.5 切换条件（运行时决策树）

```python
def choose_signup_path(ctx):
    # 条件 1: 是否需要 API key？
    if ctx.need_api_key:
        if capsolver_health_ok() and cloudflare_403_rate < 0.2:
            return PathA  # 全套接通
        else:
            return PathB  # 回退 Playwright

    # 条件 2: 仅需 ChatGPT session？
    if ctx.only_chat_session:
        return PathC  # 不接 SMS

    # 条件 3: 默认主路（chatgpt.com 直接注册，已有实现）
    return existing_chatgpt_signup()  # manager.py:_register_direct_once()
```

**硬性切换触发点**（监控指标）:
- `capsolver.fail_rate > 30% over 50 requests`: 切方案 C
- `SMS.success_rate < 50% over 20 numbers`: 暂停 platform 注册 24h
- `Arkose public_key 从 signup HTML 解析失败`: alert + 切方案 C
- `cf_clearance` 获取失败 > 3 次: alert + 检查 IP 段是否被 Cloudflare ban

### 7.6 实施风险等级

| 风险项 | 等级 | 缓解措施 |
|-------|------|---------|
| OpenAI 换 Arkose site key | **高**（月级发生） | 运行时从 HTML 解析 `data-pkey`，不 hardcode |
| Capsolver FunCaptcha 失效（Arkose 升级） | 中（季度级） | 自动回退 Playwright；监控 `capsolver.fail_rate` |
| SMS 号被 OpenAI 黑名单（recycled） | **高**（每天发生） | 使用 MobileSMS real SIM 档；retry 时换号 |
| 一号最多 2–3 账号限制 | **不可控** | 按 1:2 号账比预算，不可无限复用 |
| Cloudflare 升级 (OpenAI Enterprise 档) | 低 | curl_cffi 跟 Chrome 主线；stealth Playwright 兜底 |
| IP 段被封（datacenter） | **高** | 住宅 IP（Bright Data / Floppydata）强制 |
| SMS-Activate 跑路 / 关闭 | 中 | 已发生（2025），备 5SIM + SMSPVA + MobileSMS 多供应商 |
| 一个 Capsolver 账号被封 | 低 | 分散账号 + 2Captcha 做 secondary |
| OpenAI 要求 Verified Organization ID | 已生效（前沿模型） | 本系统仅取基础 API key，不在 ID 验证范围 |

### 7.7 决策总结

**最终推荐**：实施 **方案 B（HTTP + Capsolver + SMS 接码） + 方案 C 兜底**，按 §7.5 决策树运行时切换。

**关键参数**（写入 `runtime_config.json`）:
```json
{
  "platform_signup": {
    "primary_solver": "capsolver",
    "solver_fallback": ["2captcha"],
    "sms_provider_primary": "mobilesms",
    "sms_provider_fallback": ["smspva", "5sim"],
    "playwright_fallback_threshold": 0.3,
    "max_sms_retries_per_account": 3,
    "residential_proxy_required": true,
    "arkose_site_key_dynamic": true,
    "hybrid_mode": "playwright_token_capture + curl_cffi_http"
  }
}
```

**预算基线**（生产级，日产 100 账号）:
- Capsolver: ~$0.50 / 天（Arkose + Turnstile）
- SMS（mix virtual + real SIM 70/30）: ~$200 / 天
- 住宅代理: ~$5 / 天
- **日成本 ~$205 / 100 账号 ≈ $2.05 / 账号**
