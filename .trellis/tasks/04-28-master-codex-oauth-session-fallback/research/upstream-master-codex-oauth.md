# Research: Upstream `cnitlrt/AutoTeam` Master-Account Codex OAuth Flow

- **Query**: How does upstream generate `codex-main-*.json` from an admin `__Secure-next-auth.session-token`, and why does our local fork land on the login page when navigating to `auth.openai.com/oauth/authorize`?
- **Scope**: External (upstream `cnitlrt/AutoTeam` repo, default branch `main`)
- **Date**: 2026-04-28
- **Upstream source files retrieved** (saved as siblings in this directory for reference):
  - `_upstream_codex_auth.py` (`src/autoteam/codex_auth.py`, 1648 lines)
  - `_upstream_chatgpt_api.py` (`src/autoteam/chatgpt_api.py`, ~1700 lines, only relevant slices read)
  - `_upstream_auth_storage.py` (`src/autoteam/auth_storage.py`, 36 lines)
  - `_upstream_api.py` (`src/autoteam/api.py`, only `_start_main_codex_flow` slice read)
  - `_test_api_main_codex.py` (`tests/unit/test_api_main_codex_after_admin.py`)
  - `/tmp/test_codex_auth_session.py` (`tests/unit/test_codex_auth_session.py`)

---

## §1 Entry Point — How the Master-Account Codex Auth File is Generated

Upstream **does not** have a dedicated "import session_token → produce codex-main-*.json" function. The session_token is imported **earlier** (admin login step), and master-codex generation reuses that already-saved session.

### 1.1 Two-stage flow

```
Stage A — Admin login (one-time, persists session_token to state.json)
  POST /api/admin/login/start            -> ChatGPTTeamAPI.begin_admin_login(email)  ┐
  POST /api/admin/login/session          -> ChatGPTTeamAPI.import_admin_session(...)  │ writes state.json:
  POST /api/admin/login/password         -> submit_password / submit_code             │   { email, session_token,
  POST /api/admin/login/code                                                          │     account_id, workspace_name }
                                                                                      ┘

Stage B — Master Codex login (called whenever you need codex-main-*.json)
  POST /api/main-codex/start { action: "sync" | "login" }
    └─ api._start_main_codex_flow(action)               # _upstream_api.py:1235-1252
         └─ MainCodexSyncFlow / MainCodexLoginFlow      # _upstream_codex_auth.py:1389-1421
              └─ inherits SessionCodexAuthFlow.start()  # _upstream_codex_auth.py:1319-1339
                  └─ ChatGPTTeamAPI.start_with_session(session, account_id,
                                                       workspace, require_browser=True)
                      └─ _start_browser_session(session_token)         # chatgpt.com/* + Cloudflare wait
                          └─ _inject_session(session_token)            # cookies on chatgpt.com
                  └─ self._inject_auth_cookies()                       # cookies on auth.openai.com
                  └─ self.page.goto(self.auth_url)                     # auth.openai.com/oauth/authorize?...
                  └─ self._advance() / self.complete()
                       └─ _exchange_auth_code(...) -> bundle
                       └─ save_main_auth_file(bundle) -> auths/codex-main-{account_id}.json
```

### 1.2 Key call-site references

| Step | File:line | What |
|---|---|---|
| FastAPI route | `src/autoteam/api.py:1504-1515` | `@app.post("/api/main-codex/start")` -> `post_main_codex_start()` |
| Flow factory | `src/autoteam/api.py:1235-1252` | `_start_main_codex_flow(action)` picks `MainCodexSyncFlow` (action="sync") or `MainCodexLoginFlow` (action="login") |
| Flow base class | `src/autoteam/codex_auth.py:1046-1386` | `class SessionCodexAuthFlow` |
| Login subclass | `src/autoteam/codex_auth.py:1389-1407` | `class MainCodexLoginFlow(SessionCodexAuthFlow)` — `auth_file_callback=save_main_auth_file` |
| Sync subclass | `src/autoteam/codex_auth.py:1410-1421` | `class MainCodexSyncFlow(MainCodexLoginFlow)` — also runs `sync_main_codex_to_configured_targets` |
| Legacy wrapper | `src/autoteam/codex_auth.py:1017-1043` | `def login_codex_via_session()` — **delegates** to `SessionCodexAuthFlow`, returns `bundle` |
| Master file writer | `src/autoteam/codex_auth.py:1448-1457` | `def save_main_auth_file(bundle)` — produces `auths/codex-main-{account_id}.json` |
| Auth file shape writer | `src/autoteam/codex_auth.py:147-166` | `def _write_auth_file(filepath, bundle)` |
| Refresh entry | `src/autoteam/codex_auth.py:1479-1490` | `def refresh_main_auth_file()` — bundles login + save |

> **Important**: there is **no separate "set_session" or "/codex/token" endpoint**. The session_token comes from `state.json` which was written during Stage A. Master codex login only ever **reads** `get_admin_session_token()` (see `_upstream_codex_auth.py:1391-1393`).

---

## §2 Auth Flow Diagram (Stage B detailed)

### 2.1 The chosen approach: Playwright UI on a real Chromium context, NO HTTP-only path

Upstream uses **Playwright UI** end-to-end for `auth.openai.com/oauth/authorize`. There is no pure-HTTP OAuth path. The session_token replaces the user's password/email step; PKCE is still done; an `auth_code` is captured from the localhost callback, then exchanged via plain `requests.post` to `auth.openai.com/oauth/token`.

```
+---------------------------------------------------------------+
|  ChatGPTTeamAPI.start_with_session(require_browser=True)      |
|  -----------------------------------------------------------  |
|  1. _launch_browser()             # creates Playwright ctx     |
|  2. page.goto("https://chatgpt.com/")                          |
|  3. _wait_for_cloudflare()        # solves CF challenge        |
|  4. _inject_session(session_token)                             |
|       -> cookies on **chatgpt.com**:                           |
|            __Secure-next-auth.session-token (split if >3800)   |
|            _account = <account_id>                             |
|            oai-did   = <random uuid>                           |
|  5. _fetch_access_token()                                      |
|       -> page.evaluate fetch("/api/auth/session") on           |
|          chatgpt.com -> {accessToken: "eyJ..."}                |
|       -> stores self.access_token (Bearer for backend API)    |
|  6. _auto_detect_workspace()                                   |
+---------------------------------------------------------------+
                              |
                              v
+---------------------------------------------------------------+
|  SessionCodexAuthFlow.start()  (codex_auth.py:1319)            |
|  -----------------------------------------------------------  |
|  page = chatgpt.context.new_page()                             |
|  _attach_callback_listeners()    # listen for                  |
|     localhost:1455/auth/callback?code=...                      |
|  _inject_auth_cookies()                                        |
|       -> cookies on **auth.openai.com**:                       |
|            __Secure-next-auth.session-token (split if >3800)   |
|            _account = <account_id>                             |
|            oai-did   = chatgpt.oai_device_id  (SAME UUID!)     |
|  page.goto("https://auth.openai.com/oauth/authorize?...")      |
|  time.sleep(3)                                                 |
|  _advance()                                                    |
|     |- _detect_step():                                          |
|     |    if callback URL hit         -> "completed"            |
|     |    elif code input visible     -> "code_required"        |
|     |    elif password input visible -> "password_required"    |
|     |    elif email input visible    -> "email_required"       |
|     |- if email_required: _auto_fill_email() then loop         |
|     |- if password_required: try _switch_password_to_otp()     |
|     |- otherwise: _click_workspace_or_consent() + sleep 1s     |
+---------------------------------------------------------------+
                              |
                              v
+---------------------------------------------------------------+
|  Browser visits auth.openai.com/oauth/authorize?               |
|     client_id=app_EMoamEEZ73f0CkXaXp7hrann                     |
|     response_type=code                                         |
|     redirect_uri=http://localhost:1455/auth/callback           |
|     scope=openid email profile offline_access                  |
|     state=<random>                                             |
|     code_challenge=<S256(verifier)>                            |
|     code_challenge_method=S256                                 |
|     prompt=consent                                             |
|                                                                |
|  With session-token cookie set on auth.openai.com:             |
|    -> server recognizes existing OpenAI account session        |
|    -> MAY auto-redirect through workspace-select/consent/      |
|       301 to localhost:1455/auth/callback?code=...&state=...   |
|    -> page.on("request"/"response") captures the code         |
+---------------------------------------------------------------+
                              |
                              v
+---------------------------------------------------------------+
|  flow.complete() (codex_auth.py:1366)                          |
|     bundle = _exchange_auth_code(auth_code, code_verifier)     |
|        POST https://auth.openai.com/oauth/token                |
|          form: grant_type=authorization_code                   |
|                client_id=app_EMoamEEZ73f0CkXaXp7hrann          |
|                code=<auth_code>                                |
|                redirect_uri=http://localhost:1455/auth/callback|
|                code_verifier=<verifier>                        |
|     -> { access_token, refresh_token, id_token, expires_in }   |
|     -> JWT id_token decoded:                                   |
|          chatgpt_account_id, chatgpt_plan_type, email          |
|     filepath = save_main_auth_file(bundle)                     |
|        -> auths/codex-main-{account_id}.json                   |
+---------------------------------------------------------------+
```

### 2.2 Endpoints actually hit (in order)

| # | Method | URL | Purpose |
|---|---|---|---|
| 1 | GET (Playwright) | `https://chatgpt.com/` | warm-up + Cloudflare cookie acquisition |
| 2 | (cookie inject) | `chatgpt.com` domain | `__Secure-next-auth.session-token`, `_account`, `oai-did` |
| 3 | GET (in-page fetch) | `https://chatgpt.com/api/auth/session` | extract `accessToken` (Bearer) — used for subsequent ChatGPT backend calls (NOT for Codex OAuth) |
| 4 | GET (Playwright, optional) | `https://chatgpt.com/backend-api/accounts/{account_id}/settings` | confirm workspace_name |
| 5 | (cookie inject) | `auth.openai.com` domain | **same** `__Secure-next-auth.session-token`, `_account`, `oai-did` |
| 6 | GET (Playwright) | `https://auth.openai.com/oauth/authorize?client_id=...&prompt=consent` | the login-bypass attempt |
| 7 | (server-driven 30x chain) | `auth.openai.com` -> potentially `auth.openai.com/workspace` -> `localhost:1455/auth/callback?code=...` | OAuth code redirect |
| 8 | POST | `https://auth.openai.com/oauth/token` | exchange `code` for `access_token`/`refresh_token`/`id_token` |

> Note: step 3 (`/api/auth/session` -> `accessToken`) is **never** used as the Codex `Authorization: Bearer` directly. It is collected for ChatGPT backend admin/invite calls. Codex master OAuth still goes through step 6-8.

---

## §3 Code Excerpts (with upstream file:line)

### 3.1 PKCE generation — identical to local

```python
# src/autoteam/codex_auth.py:32-43
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CALLBACK_PORT = 1455
CODEX_REDIRECT_URI = f"http://localhost:{CODEX_CALLBACK_PORT}/auth/callback"


def _generate_pkce():
    """生成 PKCE code_verifier 和 code_challenge"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge
```

### 3.2 Build the OAuth URL — identical to local

```python
# src/autoteam/codex_auth.py:93-104
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

### 3.3 The current `login_codex_via_session()` — wrapper only

```python
# src/autoteam/codex_auth.py:1017-1043
def login_codex_via_session():
    """使用管理员 session 复用统一流程完成主号 Codex OAuth 登录。"""
    logger.info("[Codex] 开始使用 session 登录主号 Codex...")

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
        step = result.get("step")
        detail = result.get("detail")
        logger.info("[Codex] 主号 session OAuth 初始结果: step=%s detail=%s", step, detail)
        if step != "completed":
            logger.warning("[Codex] 主号 session OAuth 未直接完成: step=%s detail=%s", step, detail)
            return None

        info = flow.complete()
        return info.get("bundle")
    finally:
        flow.stop()
```

### 3.4 `SessionCodexAuthFlow.__init__` — note `auth_url` is built ONCE here

```python
# src/autoteam/codex_auth.py:1083-1106
def __init__(
    self,
    *,
    email,
    session_token,
    account_id,
    workspace_name="",
    password="",
    password_callback=None,
    auth_file_callback=None,
):
    self.email = email or ""
    self.password = password or ""
    self.workspace_name = workspace_name or ""
    self.account_id = account_id or ""
    self.session_token = session_token or ""
    self.password_callback = password_callback
    self.auth_file_callback = auth_file_callback or save_auth_file
    self.code_verifier, code_challenge = _generate_pkce()
    self.state = secrets.token_urlsafe(16)
    self.auth_url = _build_auth_url(code_challenge, self.state)
    self.auth_code = None
    self.chatgpt = None
    self.page = None
```

### 3.5 The crucial `start()` — `require_browser=True` is THE KEY DIFFERENCE

```python
# src/autoteam/codex_auth.py:1319-1339
def start(self):
    if not self.session_token:
        raise RuntimeError("缺少登录 session")
    if not self.email:
        raise RuntimeError("缺少登录邮箱")

    from autoteam.chatgpt_api import ChatGPTTeamAPI

    self.chatgpt = ChatGPTTeamAPI()
    self.chatgpt.start_with_session(
        self.session_token,
        self.account_id,
        self.workspace_name,
        require_browser=True,                  # <<< FORCES Playwright path
    )                                          # <<< populates chatgpt.com cookies + access_token
    self.page = self.chatgpt.context.new_page()
    self._attach_callback_listeners()
    self._inject_auth_cookies()                # <<< now puts cookies on auth.openai.com
    self.page.goto(self.auth_url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)
    return self._advance()
```

### 3.6 `_inject_auth_cookies` — auth.openai.com cookies

```python
# src/autoteam/codex_auth.py:1165-1225
def _inject_auth_cookies(self):
    cookies = []
    if len(self.session_token) > 3800:
        cookies.extend(
            [
                {
                    "name": "__Secure-next-auth.session-token.0",
                    "value": self.session_token[:3800],
                    "domain": "auth.openai.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "__Secure-next-auth.session-token.1",
                    "value": self.session_token[3800:],
                    "domain": "auth.openai.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
            ]
        )
    else:
        cookies.append(
            {
                "name": "__Secure-next-auth.session-token",
                "value": self.session_token,
                "domain": "auth.openai.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        )

    if self.account_id:
        cookies.append(
            {
                "name": "_account",
                "value": self.account_id,
                "domain": "auth.openai.com",
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            }
        )

    cookies.append(
        {
            "name": "oai-did",
            "value": self.chatgpt.oai_device_id,
            "domain": "auth.openai.com",
            "path": "/",
            "secure": True,
            "sameSite": "Lax",
        }
    )
    self.chatgpt.context.add_cookies(cookies)
```

### 3.7 The upstream `start_with_session(..., require_browser=True)` path — note ORDER

```python
# src/autoteam/chatgpt_api.py:1387-1410
def start_with_session(self, session_token, account_id, workspace_name="", require_browser=False):
    """用指定的 session/account 启动浏览器上下文。"""
    if not session_token:
        raise FileNotFoundError("缺少会话信息")
    self.stop()                                     # <-- defensive cleanup
    self.account_id = account_id or ""
    self.workspace_name = workspace_name or ""
    self.session_token = session_token
    self.access_token = None
    if not self.account_id:
        raise RuntimeError("缺少 workspace/account ID")

    if not require_browser and self._start_transport_session(session_token):
        token_source = self._fetch_access_token_via_transport()
        if token_source:
            self._auto_detect_workspace_via_transport()
            return                                  # <-- HTTP-only path (NOT used for codex master OAuth)
        logger.warning("[ChatGPT] curl_cffi 未能直接获取 access token，回退 Playwright transport")
        if self.http_transport:
            self.http_transport.close()
        self.http_transport = None
        self.transport_name = None

    self._start_browser_session(session_token)      # <-- full browser path required for OAuth UI
```

### 3.8 `_start_browser_session` — pre-warms `chatgpt.com` and gets `access_token`

```python
# src/autoteam/chatgpt_api.py:658-666
def _start_browser_session(self, session_token):
    self._launch_browser()
    logger.info("[ChatGPT] 访问 chatgpt.com 过 Cloudflare...")
    self.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)
    self._wait_for_cloudflare()
    self._inject_session(session_token)             # cookies on chatgpt.com
    self._fetch_access_token()                      # in-page fetch /api/auth/session
    self._auto_detect_workspace()
```

### 3.9 `_inject_session` — chatgpt.com cookies (note: `_account` and `oai-did` here too)

```python
# src/autoteam/chatgpt_api.py:438-463
def _inject_session(self, session_token):
    cookies = self._build_session_cookies(session_token, "chatgpt.com")
    if self.account_id:
        cookies.append(
            {
                "name": "_account",
                "value": self.account_id,
                "domain": "chatgpt.com",
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            }
        )
    cookies.append(
        {
            "name": "oai-did",
            "value": self.oai_device_id,
            "domain": "chatgpt.com",
            "path": "/",
            "secure": True,
            "sameSite": "Lax",
        }
    )
    self.context.add_cookies(cookies)
    self.session_token = session_token
    logger.info("[ChatGPT] 已注入 session cookies")
```

### 3.10 `_build_session_cookies` — single vs split

```python
# src/autoteam/chatgpt_api.py:199-238 (excerpt)
def _build_session_cookies(self, session_token, domain):
    if len(session_token) > 3800:
        return [
            {
                "name": "__Secure-next-auth.session-token.0",
                "value": session_token[:3800],
                "domain": domain,
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
            {
                "name": "__Secure-next-auth.session-token.1",
                "value": session_token[3800:],
                "domain": domain,
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
        ]
    return [
        {
            "name": "__Secure-next-auth.session-token",
            "value": session_token,
            "domain": domain,
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ]
```

### 3.11 `_fetch_access_token` — the in-page fetch

```python
# src/autoteam/chatgpt_api.py:1454-1500
def _fetch_access_token(self, allow_bearer_file=True):
    result = self.page.evaluate("""async () => {
        try {
            const resp = await fetch("/api/auth/session");
            const data = await resp.json();
            return { ok: true, data: data };
        } catch(e) {
            return { ok: false, error: e.message };
        }
    }""")

    if result.get("ok") and "accessToken" in result.get("data", {}):
        self.access_token = result["data"]["accessToken"]
        logger.info("[ChatGPT] 已获取 access token")
        return "session"

    if allow_bearer_file:
        bearer_file = BASE_DIR / "bearer_token"
        if bearer_file.exists():
            self.access_token = read_text(bearer_file).strip()
            logger.info("[ChatGPT] 从 bearer_token 文件加载 access token")
            return "file"

    logger.info("[ChatGPT] 尝试通过页面获取 access token...")
    self.page.goto("https://chatgpt.com/", wait_until="networkidle", timeout=60000)
    time.sleep(10)
    # ... try localStorage as last resort
```

> This `access_token` is for ChatGPT backend (invite, settings, accounts list) — **not** for Codex OAuth.

### 3.12 `_advance` and `_detect_step` — UI state machine

```python
# src/autoteam/codex_auth.py:1127-1145
def _detect_step(self):
    if self.auth_code:
        return "completed", None

    cur = self.page.url if self.page else ""
    if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in cur:
        parsed = urllib.parse.urlparse(cur)
        qs = urllib.parse.parse_qs(parsed.query)
        self.auth_code = qs.get("code", [None])[0]
        if self.auth_code:
            return "completed", None

    if self._visible_locator(self.CODE_SELECTORS, timeout_ms=800):
        return "code_required", None
    if self._visible_locator(self.PASSWORD_SELECTORS, timeout_ms=800):
        return "password_required", None
    if self._visible_locator(self.EMAIL_SELECTORS, timeout_ms=800):
        return "email_required", None
    return "unknown", cur
```

```python
# src/autoteam/codex_auth.py:1291-1317
def _advance(self, attempts=12):
    for _ in range(attempts):
        step, detail = self._detect_step()
        if step == "completed":
            return {"step": "completed", "detail": detail}
        if step == "code_required":
            return {"step": "code_required", "detail": detail}
        if step == "password_required":
            if self._switch_password_to_otp():
                continue
            return {
                "step": "unsupported_password",
                "detail": "主号 Codex 当前停留在密码页，且未找到一次性验证码入口",
            }

        if step == "email_required":
            if self._auto_fill_email():
                continue
            return {"step": "email_required", "detail": detail}

        if self._click_workspace_or_consent():
            continue

        time.sleep(1)

    final_step, detail = self._detect_step()
    return {"step": final_step, "detail": detail}
```

### 3.13 Token exchange (no auth.openai.com cookies needed for this step)

```python
# src/autoteam/codex_auth.py:107-144
def _exchange_auth_code(auth_code, code_verifier, fallback_email=None):
    logger.info("[Codex] 获取到 auth code，交换 token...")

    import requests

    resp = requests.post(
        CODEX_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CODEX_CLIENT_ID,
            "code": auth_code,
            "redirect_uri": CODEX_REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if resp.status_code != 200:
        logger.error("[Codex] Token 交换失败: %d %s", resp.status_code, resp.text[:200])
        return None

    token_data = resp.json()
    id_token = token_data.get("id_token", "")
    claims = _parse_jwt_payload(id_token)
    auth_claims = claims.get("https://api.openai.com/auth", {})

    bundle = {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "id_token": id_token,
        "account_id": auth_claims.get("chatgpt_account_id", ""),
        "email": claims.get("email", fallback_email or ""),
        "plan_type": auth_claims.get("chatgpt_plan_type", "unknown"),
        "expired": time.time() + token_data.get("expires_in", 3600),
    }

    logger.info("[Codex] 登录成功: %s (plan: %s)", bundle["email"], bundle["plan_type"])
    return bundle
```

### 3.14 Save master file shape

```python
# src/autoteam/codex_auth.py:147-166
def _write_auth_file(filepath, bundle):
    filepath = Path(filepath)
    ensure_auth_dir()
    filepath.parent.mkdir(exist_ok=True)

    auth_data = {
        "type": "codex",
        "id_token": bundle.get("id_token", ""),
        "access_token": bundle.get("access_token", ""),
        "refresh_token": bundle.get("refresh_token", ""),
        "account_id": bundle.get("account_id", ""),
        "email": bundle.get("email", ""),
        "expired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(bundle.get("expired", 0))),
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    write_text(filepath, json.dumps(auth_data, indent=2))
    ensure_auth_file_permissions(filepath)
    logger.info("[Codex] 认证文件已保存: %s", filepath)
    return str(filepath)
```

```python
# src/autoteam/codex_auth.py:1448-1457
def save_main_auth_file(bundle):
    """保存主号 Codex 认证文件，不进入账号池。"""
    account_id = bundle.get("account_id") or hashlib.md5(bundle.get("email", "main").encode()).hexdigest()[:8]

    for old in AUTH_DIR.glob("codex-main-*.json"):
        old.unlink()
        logger.info("[Codex] 清理旧主号文件: %s", old.name)

    filepath = AUTH_DIR / f"codex-main-{account_id}.json"
    return _write_auth_file(filepath, bundle)
```

### 3.15 The MainCodexLoginFlow / MainCodexSyncFlow subclasses

```python
# src/autoteam/codex_auth.py:1389-1421
class MainCodexLoginFlow(SessionCodexAuthFlow):
    def __init__(self):
        super().__init__(
            email=get_admin_email(),
            session_token=get_admin_session_token(),
            account_id=get_chatgpt_account_id(),
            workspace_name=get_chatgpt_workspace_name(),
            password="",
            password_callback=None,
            auth_file_callback=save_main_auth_file,
        )

    def complete(self):
        info = super().complete()
        return {
            "email": info.get("email"),
            "auth_file": info.get("auth_file"),
            "plan_type": info.get("plan_type"),
        }


class MainCodexSyncFlow(MainCodexLoginFlow):
    def complete(self):
        info = super().complete()

        from autoteam.sync_targets import sync_main_codex_to_configured_targets

        sync_main_codex_to_configured_targets(info["auth_file"])
        return {
            "email": info.get("email"),
            "auth_file": info.get("auth_file"),
            "plan_type": info.get("plan_type"),
        }
```

### 3.16 API entry — what `_start_main_codex_flow` does

```python
# src/autoteam/api.py:1235-1252
def _start_main_codex_flow(action="sync"):
    from autoteam.codex_auth import MainCodexLoginFlow, MainCodexSyncFlow

    flow_cls = MainCodexSyncFlow if action == "sync" else MainCodexLoginFlow

    def _do_start():
        return _run_playwright_start(flow_cls, lambda flow: flow.start())

    flow, result = _pw_executor.run(_do_start)
    step = result["step"]
    if step == "completed":
        _set_pending_main_codex_flow(flow, step, action)
        return step, _finish_main_codex_flow()
    if step in ("password_required", "code_required"):
        return step, _set_pending_main_codex_flow(flow, step, action)

    _pw_executor.run(flow.stop)
    raise RuntimeError(result.get("detail") or "无法识别主号 Codex 登录步骤")
```

---

## §4 Comparison Table — Upstream vs Local at `src/autoteam/codex_auth.py:1003-1177`

The user's pointer (`src/autoteam/codex_auth.py:1003-1177`) is the **legacy** `login_codex_via_session()` body in our local fork. It still contains its own end-to-end implementation, instead of delegating to `SessionCodexAuthFlow`. Below compares that legacy local body to the upstream behavior.

| Aspect | Upstream (current) | Local fork (legacy `login_codex_via_session` 1003-1177) | Match? |
|---|---|---|---|
| Entry function name | `login_codex_via_session()` is **a thin wrapper** that delegates to `SessionCodexAuthFlow` (codex_auth.py:1017-1043) | Uses an inline implementation; does NOT delegate to `SessionCodexAuthFlow` (`codex_auth.py:1003-1177` reimplements the flow) | ❌ DIVERGES |
| Where session_token comes from | `get_admin_session_token()` straight from `state.json` (codex_auth.py:1393) | `chatgpt.start()` -> `session_token = chatgpt.session_token` (local 1016-1017) — first calls `start()` which goes via `start_with_session` and re-reads session from state.json into the chatgpt instance | Roughly equivalent |
| `start_with_session` signature | `(session_token, account_id, workspace_name, require_browser=False)` — has `require_browser` flag (chatgpt_api.py:1387) | `(session_token, account_id, workspace_name)` — **NO** `require_browser` flag (local chatgpt_api.py:1169) | ❌ DIVERGES (but local is browser-only anyway, so functionally OK) |
| `start_with_session` always launches Chromium | Only when `require_browser=True` or curl_cffi transport fails | Always launches Chromium | Functionally equivalent for OAuth path |
| `chatgpt.com` warm-up before injecting cookies | `start_with_session(require_browser=True)` -> `_start_browser_session` -> `page.goto("chatgpt.com")` -> `_wait_for_cloudflare` -> `_inject_session` -> `_fetch_access_token` -> `_auto_detect_workspace` (chatgpt_api.py:658-666) | Local `chatgpt.start()` -> internally same `_start_browser_session` chain | ✅ Equivalent |
| `__Secure-next-auth.session-token` cookie on `auth.openai.com` | Yes, split if >3800 chars, `httpOnly=True secure=True sameSite=Lax` (codex_auth.py:1166-1201) | Yes, **identical** structure (local 1022-1056) | ✅ Equivalent |
| `_account` cookie on `auth.openai.com` | Yes (codex_auth.py:1203-1213) | Yes (local 1058-1067) | ✅ Equivalent |
| `oai-did` cookie on `auth.openai.com` | Yes, value = `self.chatgpt.oai_device_id` (codex_auth.py:1215-1224) — same UUID also used for `chatgpt.com` cookie injected earlier | Yes, value = `chatgpt.oai_device_id` — same UUID also used on `chatgpt.com` (local 1068-1075) | ✅ Equivalent |
| `__Host-next-auth.csrf-token` / `__Secure-next-auth.callback-url` cookies | **NOT injected** anywhere | **NOT injected** anywhere | ✅ Equivalent (neither side uses these) |
| Cookie injection ORDER vs `chatgpt.com` warm-up | Warm-up `chatgpt.com` first (incl. CF challenge + access_token fetch + workspace detect), THEN `_inject_auth_cookies()` for `auth.openai.com`, THEN `goto(auth_url)` (codex_auth.py:1334-1338) | Local 1078-1104: Same ordering — `chatgpt.start()` runs first (which warms up `chatgpt.com`), then injects `auth.openai.com` cookies on lines 1078, then `goto(auth_url)` on 1098-1104 | ✅ Equivalent |
| `page.goto` to OAuth URL | `wait_until="domcontentloaded", timeout=60000` then `time.sleep(3)` (codex_auth.py:1337-1338) | `wait_until="domcontentloaded", timeout=60000` then `time.sleep(3)` (local 1098-1099) | ✅ Equivalent |
| Detection that we landed on login page | `_advance()` -> `_detect_step()` returns `email_required` if `EMAIL_SELECTORS` is visible. Then it auto-fills email (using admin email!) and loops — i.e. it DOES NOT bail out on landing on login page (codex_auth.py:1306-1309) | Local 1106-1127: detects email input, logs warning, **navigates to `https://chatgpt.com/auth/login`** and re-attempts OAuth url. If still login page -> `return None` | ❌ **DIVERGES** — Local has a "bootstrap retry via chatgpt.com/auth/login" that upstream does NOT have, and upstream auto-fills email instead |
| Workspace selection | `_click_workspace_or_consent()` uses `_is_workspace_selection_page` + `_select_team_workspace` (sophisticated, label-based) (codex_auth.py:1227-1250) | Local 1133-1143: simple `'workspace' in page.url and click text=workspace_name` | ❌ DIVERGES (local less robust) |
| Consent click | `_click_workspace_or_consent` clicks `继续/Continue/Allow` (codex_auth.py:1238-1248) | Local 1145-1155 — same selector | ✅ Equivalent |
| PKCE | Yes — `_generate_pkce`, exchange via `code_verifier` (codex_auth.py:107-144) | Yes — identical (local `_exchange_auth_code` is shared) | ✅ Equivalent |
| Token exchange endpoint | POST `auth.openai.com/oauth/token` form: `grant_type=authorization_code, client_id, code, redirect_uri, code_verifier` (codex_auth.py:107-144) | Same | ✅ Equivalent |
| Auth file fields | `type, id_token, access_token, refresh_token, account_id, email, expired, last_refresh` (codex_auth.py:147-166) | Same | ✅ Equivalent |
| Auth file naming | `auths/codex-main-{account_id}.json` (codex_auth.py:1448-1457). If `account_id` is missing, uses `md5(email)[:8]` | Local has same `save_main_auth_file` | ✅ Equivalent |
| `OPENAI_API_KEY` field | **NOT** in auth file | **NOT** in auth file | ✅ Equivalent |
| `plan_type` field | NOT written into auth file (only returned in `bundle`/in-memory) | Same | ✅ Equivalent |
| `prompt=consent` in auth url | Yes (`_build_auth_url`) | Yes | ✅ Equivalent |
| Listening for `localhost:1455/auth/callback` | `page.on("request"/"response")` (codex_auth.py:1147-1163) | Same (local 1081-1095) | ✅ Equivalent |

### 4.1 Key behavioral divergences (the only ones that matter for the bug)

1. **Local fork's bootstrap-retry path** (lines 1113-1127) is **upstream-incompatible**. When upstream lands on the email-input page, it does **not** navigate to `chatgpt.com/auth/login` to re-bootstrap; instead it calls `_auto_fill_email()` with the admin email and re-runs `_advance()`. The implication: **upstream tolerates a session-token that doesn't fully bypass the login page**, by treating the email step as "just type the admin email and let the session-token cookie finish the rest of the consent flow."

2. **Upstream uses the new `SessionCodexAuthFlow`**, the legacy local `login_codex_via_session` (1003-1177) is dead code in upstream — it has been replaced by the wrapper at upstream `codex_auth.py:1017-1043`. The local fork still keeps the legacy in-place implementation and presumably calls it, while also having a near-duplicate `SessionCodexAuthFlow` at the bottom of the same file (`codex_auth.py:1180+`).

3. **`require_browser=True`** is the upstream contract for OAuth flows. Upstream's `start_with_session` will silently use curl_cffi transport (HTTP-only, no Playwright context) if the curl_cffi transport works. Without `require_browser=True`, `chatgpt.context` would be `None` (no Playwright browser launched), and `_inject_auth_cookies` / `new_page()` would crash. Local doesn't expose `require_browser` because the local `start_with_session` is unconditionally browser-based, so this divergence is **not** functionally problematic for the local code, but matters for any future curl_cffi transport adoption.

### 4.2 Why local lands on login page (root cause hypothesis)

The cookie set is identical between upstream and local. The difference is purely in **how upstream handles the case when the session cookie alone fails to bypass the login form**:

- Upstream: assume cookie was valid but UI requires email step; auto-fill admin email; let consent + workspace selection proceed.
- Local: treat email-input visible as "session is invalid", attempt a `chatgpt.com/auth/login` bootstrap, retry OAuth url; if still email-input visible -> bail out (return None).

The local `chatgpt.com/auth/login` bootstrap is **harmful** rather than helpful — visiting `chatgpt.com/auth/login` doesn't add anything beyond what `_start_browser_session` already did (which already navigates to `https://chatgpt.com/`). It probably even disturbs the existing session by clicking the login button and possibly clearing session state.

**The reason the user observes "lands on login page" is most likely**:

- The session cookie alone produces a transient login-page render, especially on `prompt=consent` (which forces the consent UI). Upstream's response is to fill in the email and continue. Local's response is to bail and re-bootstrap, which does not solve the issue.

A second possibility (less likely given identical cookies):

- The local fork's call sequence forgets the `_account` cookie when `chatgpt.account_id` is empty (local code at 1058-1077 unconditionally appends the `_account` cookie even if `chatgpt.account_id` is empty/`None`). Upstream guards with `if self.account_id:` (codex_auth.py:1203). An empty `_account` cookie value could confuse the OAuth server.

---

## §5 Recommended Fix Direction (suggestions only — for the main agent to evaluate)

> The agent receiving this research should choose between options based on whether the local code already maintains the `SessionCodexAuthFlow` class.

### Option A (preferred, minimal divergence): replace the legacy body

Replace the entire body of `login_codex_via_session()` at `src/autoteam/codex_auth.py:1003-1177` with the upstream wrapper:

```python
def login_codex_via_session():
    """使用管理员 session 复用统一流程完成主号 Codex OAuth 登录。"""
    logger.info("[Codex] 开始使用 session 登录主号 Codex...")

    flow = SessionCodexAuthFlow(
        email=get_admin_email(),
        session_token=get_admin_session_token(),
        account_id=get_chatgpt_account_id(),
        workspace_name=get_chatgpt_workspace_name(),
        password="",
        password_callback=None,
        auth_file_callback=lambda _bundle: "",  # caller will call save_main_auth_file
    )

    try:
        result = flow.start()
        if result.get("step") != "completed":
            logger.warning("[Codex] 主号 session OAuth 未直接完成: step=%s detail=%s",
                           result.get("step"), result.get("detail"))
            return None
        info = flow.complete()
        return info.get("bundle")
    finally:
        flow.stop()
```

**Prerequisites for option A**:
1. Local `SessionCodexAuthFlow` (codex_auth.py:1180+) must exist and be functionally equivalent to upstream (cross-check `_inject_auth_cookies`, `_advance`, `_detect_step`, etc.).
2. Local `start_with_session` either accepts `require_browser=True` (preferred — backport from upstream chatgpt_api.py:1387) **or** is unconditionally browser-only (already the case — then the `require_browser=True` argument can simply be dropped from `SessionCodexAuthFlow.start()` in local).

### Option B (in-place fix, keep current SessionCodexAuthFlow untouched)

Inside the legacy `login_codex_via_session` body, rather than re-bootstrapping via `chatgpt.com/auth/login`, **auto-fill the admin email** when the email input is visible. This mirrors `_auto_fill_email`:

```python
# Replace lines 1106-1127 (the needs_login bootstrap) with:
for _ in range(2):
    email_input = page.locator(
        'input[name="email"], input[id="email-input"], input[id="email"]'
    ).first
    if not email_input.is_visible(timeout=3000):
        break
    admin_email = get_admin_email()
    if not admin_email:
        logger.error("[Codex] OAuth 落到了登录页，且没有 admin email 可填")
        return None
    logger.info("[Codex] OAuth 落在 email-input 页，自动填入 admin email: %s", admin_email)
    email_input.fill(admin_email)
    time.sleep(0.5)
    _click_primary_auth_button(page, email_input, ["Continue", "继续", "Log in"])
    time.sleep(3)
```

Also remove the unconditional `_account` cookie when `chatgpt.account_id` is empty:

```python
# Lines 1058-1077 — guard `_account` cookie:
if chatgpt.account_id:
    cookies.append(
        {
            "name": "_account",
            "value": chatgpt.account_id,
            "domain": "auth.openai.com",
            "path": "/",
            "secure": True,
            "sameSite": "Lax",
        }
    )
cookies.append(
    {
        "name": "oai-did",
        "value": chatgpt.oai_device_id,
        "domain": "auth.openai.com",
        "path": "/",
        "secure": True,
        "sameSite": "Lax",
    }
)
```

### Option C (deeper fix, brings parity with upstream)

1. Backport the `require_browser=False` parameter into `start_with_session` (chatgpt_api.py:1169) — even if the local code never uses HTTP-only path today, it future-proofs against transport changes.
2. Delete the legacy `login_codex_via_session` body entirely; replace with the wrapper from §5 Option A.
3. Cross-verify the local `SessionCodexAuthFlow` against the upstream source we saved at `_upstream_codex_auth.py:1046-1407` line by line.

### What NOT to do

- Do **not** add `__Host-next-auth.csrf-token` or `__Secure-next-auth.callback-url` cookies — upstream does not, and OpenAI's OAuth server does not require them when `__Secure-next-auth.session-token` is present.
- Do **not** try to skip PKCE / use `Authorization: Bearer <accessToken>` directly — the upstream `accessToken` from `chatgpt.com/api/auth/session` is **not accepted** by `auth.openai.com/oauth/token` for `grant_type=authorization_code`. PKCE is mandatory.
- Do **not** change the OAuth client_id (`app_EMoamEEZ73f0CkXaXp7hrann`) or scopes (`openid email profile offline_access`) — they are upstream-stable.

---

## Caveats / Not Found

- Upstream README and `docs/architecture.md` do not describe the master Codex flow at the API level. The behavioral contract is **inferred from code + tests** rather than documentation.
- I did not exhaustively diff every helper in upstream `chatgpt_api.py` against local; only the OAuth-relevant slice (`start_with_session`, `_inject_session`, `_fetch_access_token`, `_build_session_cookies`, `_start_browser_session`).
- I did not verify whether upstream still maintains the legacy `login_codex_via_session` body anywhere — based on the upstream codex_auth.py read, it has been **fully replaced** by the wrapper at lines 1017-1043.
- The hypothesis "session cookie alone triggers an email-input page" matches upstream's `_advance()` behavior (which auto-fills email), but I cannot reproduce the live failure without a real `auth.openai.com` session.
- Tests `tests/unit/test_codex_auth_session.py` confirm the contract for `login_codex_via_session()`: it must (a) construct `SessionCodexAuthFlow` with `email/session_token/account_id/workspace_name/auth_file_callback`, (b) call `start()` then `complete()` then `stop()` in that order, (c) return `bundle` dict on success or `None` if `start()` returned non-`completed` step.
