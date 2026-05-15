"""Codex 认证管理 - OAuth 登录、token 管理、保存 CPA 兼容认证文件"""

import base64
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

import autoteam.display  # noqa: F401
from autoteam.accounts import is_supported_plan, normalize_plan_type
from autoteam.admin_state import (
    get_admin_email,
    get_admin_session_token,
    get_chatgpt_account_id,
    get_chatgpt_workspace_name,
)
from autoteam.auth_storage import AUTH_DIR, ensure_auth_dir, ensure_auth_file_permissions
from autoteam.config import get_playwright_launch_options
from autoteam.invite import assert_not_blocked  # SPEC-2 shared/add-phone-detection §3 — OAuth 流程复用
from autoteam.textio import write_text

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
SCREENSHOT_DIR = PROJECT_ROOT / "screenshots"

# Codex OAuth 配置
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CALLBACK_PORT = 1455
CODEX_REDIRECT_URI = f"http://localhost:{CODEX_CALLBACK_PORT}/auth/callback"

# SPEC-2 shared/quota-classification §4.4 I5 — Codex backend 最小推理端点(用于 uninitialized_seat 二次验证)
_CODEX_SMOKE_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
# quota 关键词:codex backend 返回 4xx 时若 body 含这些词同样视为 auth_invalid(可能是配额相关 API 错误)
_CODEX_SMOKE_QUOTA_HINTS = ("quota", "no_quota", "rate_limit", "billing", "exceeded")
# Round 11 — codex backend 拒绝 model 时的 body 关键词(4xx),用于触发 fallback model chain
_CODEX_SMOKE_MODEL_NOT_SUPPORTED_HINTS = (
    "not supported",
    "model_not_supported",
    "is not supported",
    "invalid model",
    "unknown model",
)
# Round 11 — cheap_codex_smoke 默认 instructions(短回复 + 最小 token 消耗)
_CODEX_SMOKE_DEFAULT_INSTRUCTIONS = "You are a concise assistant. Reply with one short word."


def _generate_pkce():
    """生成 PKCE code_verifier 和 code_challenge"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _parse_jwt_payload(token):
    """解析 JWT payload（不验证签名）"""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    # 补齐 base64 padding
    payload += "=" * (4 - len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _screenshot(page, name):
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(SCREENSHOT_DIR / name), full_page=True)


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

    raw_plan = auth_claims.get("chatgpt_plan_type", "unknown")
    bundle = {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "id_token": id_token,
        "account_id": auth_claims.get("chatgpt_account_id", ""),
        "email": claims.get("email", fallback_email or ""),
        # SPEC-2 shared/plan-type-whitelist §2.3:plan_type 已归一化为小写;
        # plan_type_raw 保留 OpenAI 原始字面量便于事后排查;
        # plan_supported 是白名单判定结果,下游消费方应只读该字段不再自己 .lower() 比对。
        "plan_type": normalize_plan_type(raw_plan),
        "plan_type_raw": raw_plan,
        "plan_supported": is_supported_plan(raw_plan),
        "expired": time.time() + token_data.get("expires_in", 3600),
    }

    logger.info(
        "[Codex] 登录成功: %s (plan: %s, supported: %s)",
        bundle["email"],
        bundle["plan_type"],
        bundle["plan_supported"],
    )
    return bundle


def _password_grant_access_token(email, password):
    """Round 11 V8 fast-path(已弃用,保留作未来 hook)— OAuth 2.0 ROPC password grant.

    现状(V11 实证 2026-04-30):OAuth 2.1 已移除 ROPC,OpenAI auth.openai.com/oauth/token
    后端 30 次组合(5 账号 × 2 client_id × 3 scope)全员 HTTP 400
    `unknown_parameter:'username'` — 完全没部署 password grant,本 helper 永远返回 None。
    完整证据见 `research/v11-password-grant-probe-report.md`。

    实际使用 `fetch_nextauth_backend_access_token` 替代(NextAuth /api/auth/session.accessToken
    路径,V10 jshook trace 已实证 chatgpt.com /api/auth/session 响应含 accessToken 字段
    可作 Bearer 调 backend-api/*)。

    保留 helper 仅作未来 OpenAI 重启 ROPC 时的 hook;现有单测继续覆盖契约语义(空参 / 200 /
    401 / 网络异常 / 非 JSON / 缺字段),实现行为保持不变。

    Args:
        email: 子号邮箱,作为 `username` 字段。
        password: 注册时设的密码。

    Returns:
        access_token 字符串(200 OK + token_type=Bearer 时);否则 None。永不抛(M-I1 风格)。
        当前实测永远返回 None(OpenAI 后端 unknown_parameter:'username')。
    """
    if not email or not password:
        return None

    import requests

    try:
        resp = requests.post(
            CODEX_TOKEN_URL,
            data={
                "grant_type": "password",
                "client_id": CODEX_CLIENT_ID,
                "username": email,
                "password": password,
                "scope": "openid profile email offline_access",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Codex] _password_grant_access_token 请求异常: %s", exc)
        return None

    if resp.status_code != 200:
        logger.warning(
            "[Codex] _password_grant_access_token 返回 %d: %s",
            resp.status_code,
            (resp.text or "")[:200],
        )
        return None

    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Codex] _password_grant_access_token 响应非 JSON: %s", exc)
        return None

    access_token = data.get("access_token")
    if not access_token:
        logger.warning(
            "[Codex] _password_grant_access_token 响应缺 access_token: %s",
            str(data)[:200],
        )
        return None

    logger.info("[Codex] _password_grant_access_token 成功 (token_type=%s)", data.get("token_type"))
    return access_token


def fetch_nextauth_backend_access_token(page):
    """Round 11 V12 P0.1 — 从已加载 chatgpt.com cookie 的浏览器 page 提取 NextAuth `accessToken`。

    背景:V11 探活实证 OpenAI `auth.openai.com/oauth/token` 已撤 ROPC password grant
    (30 个 client_id/scope/audience 变体全员 HTTP 400 `unknown_parameter: 'username'`),
    `_password_grant_access_token` 在新号场景永远返回 None。

    替代路径:V10 jshook trace 已实证 chatgpt.com `/api/auth/session` 响应含 `accessToken`
    字段(2080 字符),等价于 SPA 调 `/backend-api/*` 用的 Bearer token。在 register 成功
    后浏览器 context 已落 chatgpt.com NextAuth session cookie,直接 page.evaluate 调
    /api/auth/session credentials:include 即可拿到。

    Args:
        page: Playwright Page 对象,必须已加载 chatgpt.com 域(cookie 在该上下文)。

    Returns:
        access_token 字符串(200 OK + accessToken 字段非空时);否则 None。永不抛(M-I1 风格)。
    """
    if page is None:
        return None

    try:
        result = page.evaluate(
            """
            async () => {
                try {
                    const r = await fetch('/api/auth/session', {
                        credentials: 'include',
                        cache: 'no-store',
                        headers: { 'Accept': 'application/json' },
                    });
                    if (r.status !== 200) return { status: r.status, accessToken: null };
                    const ct = r.headers.get('content-type') || '';
                    if (!ct.includes('application/json')) return { status: r.status, accessToken: null, raw: 'non-json' };
                    const data = await r.json();
                    return { status: r.status, accessToken: data && data.accessToken ? data.accessToken : null };
                } catch (e) {
                    return { status: 0, accessToken: null, error: String(e) };
                }
            }
            """
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Codex] fetch_nextauth_backend_access_token page.evaluate 异常: %s", exc)
        return None

    if not isinstance(result, dict):
        logger.warning("[Codex] fetch_nextauth_backend_access_token 异常返回: %s", str(result)[:200])
        return None

    status = result.get("status")
    access_token = result.get("accessToken")
    if status != 200:
        logger.warning(
            "[Codex] fetch_nextauth_backend_access_token /api/auth/session 返回 %s (accessToken=%s)",
            status,
            "yes" if access_token else "no",
        )
        return None
    if not access_token:
        logger.warning(
            "[Codex] fetch_nextauth_backend_access_token /api/auth/session 200 但 accessToken 字段缺失/空"
        )
        return None

    logger.info(
        "[Codex] fetch_nextauth_backend_access_token 成功 (len=%d)",
        len(access_token),
    )
    return access_token


def fetch_personal_uuid(access_token):
    """Round 11 V8 — POST https://chatgpt.com/backend-api/accounts/personal idempotent getOrCreate.

    Master 控制实验(`research/v7c-master-replay.json`)证实该 endpoint 是 idempotent — 用 Bearer
    access_token 调一次,200 OK + 返回 `{id: "<personal_uuid>", structure: "personal", created: false}`,
    用户在 register 阶段已经被自动创建 personal workspace。此 UUID 下游用作 OAuth `allowed_workspace_id`
    query 参数,绕过 default_workspace_id sticky-Team 死锁。

    ⚠️ Round 11 V13 实证(2026-04-30):用 `requests` 直打该 endpoint 会被 Cloudflare 403 拦
    (无 cf_clearance / 真实 chrome UA / TLS fingerprint)。生产路径必须用
    `fetch_personal_uuid_via_page(page, access_token)` 在浏览器内 page.evaluate 调用,绕开 cf
    bot challenge。本 helper 仅保留作单测 / 离线 CTF 复现脚本用途。

    Args:
        access_token: 子号刚完成 password login 拿到的临时 Bearer token。

    Returns:
        UUID 字符串 / None(401 / 403 / 网络异常)。永不抛(M-I1 风格)。
    """
    if not access_token:
        return None

    import requests

    try:
        resp = requests.post(
            "https://chatgpt.com/backend-api/accounts/personal",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Origin": "https://chatgpt.com",
            },
            json={},
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 — 任何 transport 异常都不抛
        logger.warning("[Codex] fetch_personal_uuid 请求异常: %s", exc)
        return None

    if resp.status_code != 200:
        logger.warning(
            "[Codex] fetch_personal_uuid 返回 %d: %s",
            resp.status_code,
            (resp.text or "")[:200],
        )
        return None

    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Codex] fetch_personal_uuid 响应非 JSON: %s", exc)
        return None

    uuid = data.get("id")
    if not uuid:
        logger.warning("[Codex] fetch_personal_uuid 响应缺少 id 字段: %s", str(data)[:200])
        return None

    logger.info(
        "[Codex] fetch_personal_uuid 成功: %s (created=%s)",
        uuid,
        data.get("created"),
    )
    return uuid


def fetch_personal_uuid_via_page(page, access_token=None):
    """Round 11 V13 P0.2 — 浏览器内调用 POST /backend-api/accounts/personal,绕过 Cloudflare。

    背景:V12 探活实证 `requests` 直打 `chatgpt.com/backend-api/accounts/personal` 全员被 Cloudflare
    403 拦(无 cf_clearance / 真实 chrome TLS fingerprint / 真实 UA)。但浏览器在 register 阶段已
    通过 cf challenge,context 里有 cf_clearance cookie + 完整 chrome 指纹,page.evaluate 在
    chatgpt.com origin 内 fetch 不会被拦。

    Args:
        page: Playwright Page 对象,必须已加载 chatgpt.com 域(cookie + cf_clearance 在该上下文)。
        access_token: 显式 Bearer token(从 `fetch_nextauth_backend_access_token` 拿到);
            None 时走 `credentials: 'include'` 用浏览器 NextAuth cookie session(SPA 默认行为)。

    Returns:
        UUID 字符串 / None(page=None / 401 / 403 / 缺 id / 异常)。永不抛(M-I1 风格)。
    """
    if page is None:
        return None

    js = """
    async (token) => {
        try {
            const headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            };
            if (token) headers['Authorization'] = 'Bearer ' + token;
            const r = await fetch('/backend-api/accounts/personal', {
                method: 'POST',
                credentials: 'include',
                headers: headers,
                body: '{}',
            });
            if (r.status !== 200) {
                let body = '';
                try { body = (await r.text()).slice(0, 200); } catch (e) {}
                return { status: r.status, id: null, body: body };
            }
            const ct = r.headers.get('content-type') || '';
            if (!ct.includes('application/json')) {
                return { status: r.status, id: null, raw: 'non-json' };
            }
            const data = await r.json();
            return { status: r.status, id: data && data.id ? data.id : null, created: data ? data.created : null };
        } catch (e) {
            return { status: 0, id: null, error: String(e) };
        }
    }
    """

    try:
        result = page.evaluate(js, access_token or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Codex] fetch_personal_uuid_via_page page.evaluate 异常: %s", exc)
        return None

    if not isinstance(result, dict):
        logger.warning(
            "[Codex] fetch_personal_uuid_via_page 异常返回: %s", str(result)[:200]
        )
        return None

    status = result.get("status")
    uuid = result.get("id")
    if status != 200:
        logger.warning(
            "[Codex] fetch_personal_uuid_via_page /accounts/personal 返回 %s (id=%s, body=%s)",
            status,
            "yes" if uuid else "no",
            (result.get("body") or "")[:120],
        )
        return None
    if not uuid:
        logger.warning(
            "[Codex] fetch_personal_uuid_via_page 200 但缺 id 字段: %s", str(result)[:200]
        )
        return None

    logger.info(
        "[Codex] fetch_personal_uuid_via_page 成功: %s (created=%s)",
        uuid,
        result.get("created"),
    )
    return uuid


def is_token_pair_invalidated(auth_path):
    """Round 11 V7 — 双失效探测:access_token + refresh_token 是否被 server-side invalidate。

    研究报告 v7-v10-personal-uuid-exploitation.md §V7 确认 OpenAI 在 user kick 时同步废
    access_token(`token_invalidated` 401)+ refresh_token(`refresh_token_invalidated` 401)。
    任意一个仍可用 → 视为可救活,仅 access_token 死可走 refresh_access_token 重生;两个都死才
    判定 auth_invalid 触发外层清账(避免无谓重登)。

    Args:
        auth_path: 子号 auth_file 路径(JSON)。

    Returns:
        True 当且仅当 GET /backend-api/me 返回 401 + POST /oauth/token (refresh_token grant)
        也返回 401。任何其它结果(网络异常 / 200 / 非 401 错误)→ False(保守不动状态)。
    """
    if not auth_path:
        return False
    p = Path(auth_path)
    if not p.exists():
        return False

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Codex] is_token_pair_invalidated 读取 %s 异常: %s", auth_path, exc)
        return False

    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    if not access_token or not refresh_token:
        return False

    import requests

    try:
        me_resp = requests.get(
            "https://chatgpt.com/backend-api/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Codex] is_token_pair_invalidated GET /me 异常: %s", exc)
        return False

    if me_resp.status_code != 401:
        return False

    try:
        rt_resp = requests.post(
            CODEX_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": CODEX_CLIENT_ID,
                "refresh_token": refresh_token,
                "scope": "openid profile email",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Codex] is_token_pair_invalidated refresh 异常: %s", exc)
        return False

    if rt_resp.status_code != 401:
        return False

    logger.warning(
        "[Codex] is_token_pair_invalidated 命中:access_token + refresh_token 同时被 server-side invalidate (auth_path=%s)",
        auth_path,
    )
    return True


def _build_auth_url_with_allowed_workspace(code_challenge, state, allowed_workspace_id):
    """Round 11 V8 — 在 _build_auth_url 基础上拼 `&allowed_workspace_id=<personal_uuid>` query 参数。

    OAuth issuer 静默接受该参数(实测 v8-result.md:不报 unknown_parameter),让 issuer 在颁
    token 时优先选 personal workspace 而非 default_workspace_id 指向的 Team。
    """
    base = _build_auth_url(code_challenge, state)
    if not allowed_workspace_id:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}allowed_workspace_id={urllib.parse.quote(allowed_workspace_id, safe='')}"


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


def _inject_personal_session_cookies(context, session_token):
    """
    把注册阶段的 chatgpt.com __Secure-next-auth.session-token 注入到
    chatgpt.com + auth.openai.com 双域,让 personal OAuth 跳过 /log-in 表单。

    Round 11 四轮:
    - 单注入 auth.openai.com 域不够 —— NextAuth 跨域 issuer 校验严格,
      /oauth/authorize 不认 chatgpt.com 颁发的 token。
    - 必须双域同时注入,然后先 goto chatgpt.com 让服务端 next-auth API 校验
      session 并写齐配套 cookies,再 goto auth_url 进入 OAuth。

    切片规则与 SessionCodexAuthFlow._inject_auth_cookies / chatgpt_api._build_session_cookies
    保持一致:>3800 字节切两段 .0/.1,否则单个 cookie。
    """
    if not session_token:
        return

    def _build(domain):
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

    cookies = _build("chatgpt.com") + _build("auth.openai.com")
    context.add_cookies(cookies)


def _click_primary_auth_button(page, field, labels):
    """
    只点击当前输入框所在表单的主按钮，避免误点 Continue with Google/Apple/Microsoft。
    """
    label_re = re.compile(rf"^(?:{'|'.join(re.escape(label) for label in labels)})$", re.I)

    try:
        form = field.locator("xpath=ancestor::form[1]").first
        btn = form.get_by_role("button", name=label_re).first
        if btn.is_visible(timeout=2000):
            btn.click()
            return True
    except Exception:
        pass

    try:
        form = field.locator("xpath=ancestor::form[1]").first
        btn = form.locator('button[type="submit"], input[type="submit"]').first
        if btn.is_visible(timeout=2000):
            btn.click()
            return True
    except Exception:
        pass

    try:
        btn = page.get_by_role("button", name=label_re).last
        if btn.is_visible(timeout=2000):
            btn.click()
            return True
    except Exception:
        pass

    try:
        field.press("Enter")
        return True
    except Exception:
        return False


def _is_google_redirect(page):
    url = (page.url or "").lower()
    if "accounts.google.com" in url:
        return True

    try:
        text = page.locator("body").inner_text(timeout=1000).lower()
        return "sign in with google" in text[:300]
    except Exception:
        return False


_OTP_INPUT_SELECTORS = (
    'input[name="code"], input[inputmode="numeric"], input[autocomplete="one-time-code"], '
    'input[placeholder*="验证码"], input[placeholder*="code" i]'
)
_OTP_INVALID_HINTS = (
    "invalid code",
    "incorrect code",
    "wrong code",
    "expired code",
    "check the code and try again",
    "验证码无效",
    "验证码错误",
    "验证码已过期",
)


def _is_otp_input_visible(page, timeout=500):
    try:
        return page.locator(_OTP_INPUT_SELECTORS).first.is_visible(timeout=timeout)
    except Exception:
        return False


def _detect_otp_error(page):
    try:
        body = page.locator("body").inner_text(timeout=1500).lower().replace("\n", " ")
    except Exception:
        return None

    for hint in _OTP_INVALID_HINTS:
        if hint in body:
            return hint
    return None


def _wait_for_otp_submit_result(page, timeout=12):
    """
    等待验证码提交结果：
    - accepted: 验证码输入框已消失 / 页面已前进
    - invalid: 页面明确提示验证码错误
    - pending: 既没报错也没明显前进（常见于页面较慢或状态未稳定）
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        err = _detect_otp_error(page)
        if err:
            return "invalid", err
        if not _is_otp_input_visible(page, timeout=250):
            return "accepted", None
        time.sleep(0.5)

    err = _detect_otp_error(page)
    if err:
        return "invalid", err
    return "pending", None


def _typewrite_credential(page, locator, value, *, delay_ms=50, post_sleep=1.0):
    """逐字符 keyboard.type 填入 credential,触发 React onChange 事件链.

    Round 11 五轮 — 阶段 2 fresh re-login 必备:
    OpenAI auth /log-in 页 React 表单对 Playwright fill() 不友好 —— fill 一次性
    setValue,不触发 input 事件 → React internal state 不更新 → Continue 按钮变灰禁用。
    keyboard.type 逐字符模拟真用户击键,触发 onChange/onInput 完整事件链。

    注:注册流的 /create-account/password 页用 fill() 是 OK 的(不同 React form impl);
    本 helper 仅用于 fresh login (/log-in) 阶段。
    """
    try:
        locator.click()
        time.sleep(0.3)
        # 清空可能预填的内容
        try:
            locator.press("Control+A")
            locator.press("Delete")
        except Exception:
            pass
        time.sleep(0.2)
        page.keyboard.type(value, delay=delay_ms)
        time.sleep(post_sleep)
        return True
    except Exception as exc:
        logger.warning("[Codex] _typewrite_credential 失败: %s", exc)
        return False


def _perform_fresh_relogin_in_context(context, email, password, mail_client, *, used_email_ids):
    """阶段 2 — fresh chatgpt.com login.

    Round 11 五轮 Option A:
    阶段 1(silent step-0 + cookie 注入)拿到 plan_type=team 或 bundle=None 时,
    说明 chatgpt.com session_token 内嵌的 user identity 已被锁死在原 Team workspace,
    NextAuth refresh 不能切。唯一兜底:清空 OAuth context 所有 cookies,做一次完整的
    chatgpt.com 登录(email + password),拿到 Personal-bound 全新 session,再走 OAuth。

    流程:
    1. context.clear_cookies() — 清空 stale 的 chatgpt.com / auth.openai.com 双域 session
    2. goto chatgpt.com/auth/login,过 Cloudflare
    3. 用 _typewrite_credential(keyboard.type)填 email + password — 不用 fill() 避免灰按钮
    4. 处理 OTP(若 mail_client 可用)
    5. 等 chatgpt.com 登录完成

    Returns:
        bool — True 表示 fresh login 成功(context 现持有新 session),False 表示失败
    """
    logger.info("[Codex] 阶段 2 fresh re-login 开始: 清空 context cookies → 重新登录 chatgpt.com")

    try:
        context.clear_cookies()
    except Exception as exc:
        logger.warning("[Codex] 清空 context cookies 失败: %s", exc)

    # 重新刷邮箱 ID snapshot,fresh login 阶段不复用阶段 1 的旧 snapshot
    fresh_email_id_before = 0
    if mail_client:
        try:
            _pre = mail_client.search_emails_by_recipient(email, size=1)
            if _pre:
                fresh_email_id_before = _pre[0].get("emailId", 0)
        except Exception:
            pass

    page = context.new_page()
    try:
        page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        # 过 Cloudflare(若有)
        for _i in range(12):
            try:
                if "verify you are human" not in page.content()[:2000].lower():
                    break
            except Exception:
                break
            logger.info("[Codex] fresh re-login 等待 Cloudflare...(%ds)", _i * 5)
            time.sleep(5)

        # 点击 "登录" / "Log in" 按钮(若 chatgpt.com 首页落在欢迎页)
        try:
            login_btn = page.locator(
                'button:has-text("登录"), button:has-text("Log in"), a:has-text("Log in"), a:has-text("登录")'
            ).first
            if login_btn.is_visible(timeout=3000):
                login_btn.click()
                time.sleep(3)
        except Exception:
            pass

        _screenshot(page, "codex_relogin_01_login_page.png")

        # === Email 步骤 ===
        try:
            ei = page.locator(
                'input[name="email"], input[id="email-input"], input[id="email"], input[type="email"]'
            ).first
            if ei.is_visible(timeout=8000):
                logger.info("[Codex] fresh re-login: 用 keyboard.type 填入 email...")
                if not _typewrite_credential(page, ei, email):
                    logger.warning("[Codex] fresh re-login: keyboard.type email 失败")
                    return False
                _click_primary_auth_button(page, ei, ["Continue", "继续"])
                time.sleep(3)
                _screenshot(page, "codex_relogin_02_after_email.png")
            else:
                logger.warning("[Codex] fresh re-login: email input 不可见")
        except Exception as exc:
            logger.warning("[Codex] fresh re-login email 步骤异常: %s", exc)

        # === Password 步骤(关键:/log-in 页) ===
        try:
            pi = page.locator('input[name="password"], input[type="password"]').first
            if pi.is_visible(timeout=8000):
                if password:
                    logger.info("[Codex] fresh re-login: 用 keyboard.type 填入 password...")
                    if not _typewrite_credential(page, pi, password):
                        logger.warning("[Codex] fresh re-login: keyboard.type password 失败")
                        return False
                    _click_primary_auth_button(page, pi, ["Continue", "继续", "Log in"])
                    time.sleep(5)
                else:
                    # 无密码 → 一次性验证码登录
                    otp_btn = page.locator(
                        'button:has-text("一次性验证码"), button:has-text("one-time"), button:has-text("email login")'
                    ).first
                    if otp_btn.is_visible(timeout=3000):
                        logger.info("[Codex] fresh re-login: 无密码,点击一次性验证码登录")
                        otp_btn.click()
                        time.sleep(3)
                _screenshot(page, "codex_relogin_03_after_password.png")
        except Exception as exc:
            logger.warning("[Codex] fresh re-login password 步骤异常: %s", exc)

        # === 可能 OTP ===
        try:
            ci = page.locator(_OTP_INPUT_SELECTORS).first
            if ci.is_visible(timeout=5000) and mail_client:
                logger.info("[Codex] fresh re-login: 需要 OTP,等待 emailId > %d 的新邮件...", fresh_email_id_before)
                otp = None
                otp_email_id = 0
                t0 = time.time()
                while time.time() - t0 < 120:
                    for em in mail_client.search_emails_by_recipient(email, size=5):
                        eid = em.get("emailId", 0)
                        if eid <= fresh_email_id_before or eid in used_email_ids:
                            continue
                        sender = (em.get("sendEmail") or "").lower()
                        if "openai" not in sender and "chatgpt" not in sender:
                            continue
                        subj = (em.get("subject") or "").lower()
                        if "invited" in subj or "invitation" in subj:
                            continue
                        otp = mail_client.extract_verification_code(em)
                        if otp:
                            otp_email_id = eid
                            break
                    if otp:
                        break
                    time.sleep(3)
                if otp:
                    used_email_ids.add(otp_email_id)
                    logger.info("[Codex] fresh re-login: 获取到 OTP %s", otp)
                    ci.fill(otp)
                    time.sleep(0.5)
                    page.locator(
                        'button[type="submit"], button:has-text("Continue"), button:has-text("继续")'
                    ).first.click()
                    time.sleep(5)
                    _screenshot(page, "codex_relogin_04_after_otp.png")
                else:
                    logger.warning("[Codex] fresh re-login: 未获取到 OTP")
        except Exception:
            pass

        # === 等 chatgpt.com 登录完成 ===
        # 成功条件:URL 不再含 /auth/login,且页面可正常渲染
        for _i in range(15):
            cur = page.url or ""
            if "auth/login" not in cur and "/log-in" not in cur:
                logger.info("[Codex] fresh re-login: 登录完成,当前 URL: %s", cur[:120])
                _screenshot(page, "codex_relogin_05_logged_in.png")
                return True
            time.sleep(2)

        # 还停在 login 页 — 失败
        logger.warning("[Codex] fresh re-login: 等待登录完成超时,当前 URL: %s", page.url[:120])
        _screenshot(page, "codex_relogin_05_timeout.png")
        return False
    except Exception as exc:
        logger.warning("[Codex] fresh re-login 异常: %s", exc)
        return False
    finally:
        try:
            page.close()
        except Exception:
            pass


def login_codex_via_browser(
    email,
    password,
    mail_client=None,
    *,
    use_personal=False,
    chatgpt_session_token=None,
    prefetched_personal_uuid=None,
):
    """
    通过 Playwright 自动完成 Codex OAuth 登录。
    mail_client: CloudMailClient 实例，用于自动读取登录验证码。
    use_personal: 若为 True，则走"个人账号"流程 —— 不注入 Team _account cookie，
                  workspace 选择时跳过 Team 直接用 Personal。用于已退出 Team 的子账号生成 free plan 的 rt/at。
    chatgpt_session_token: 注册阶段从 chatgpt.com 抽出的 __Secure-next-auth.session-token,
                           在 use_personal=True 时注入 auth.openai.com 跳过 /log-in 表单。
                           沿用 SessionCodexAuthFlow._inject_auth_cookies 的注入模式(主号专用扩展给子号)。
    prefetched_personal_uuid: Round 11 V8 — accounts.json 持久化的 personal_workspace_id;
                              非空时直接拼到 OAuth `auth_url` 的 allowed_workspace_id 参数,
                              省一次 silent step-0 内的 POST /accounts/personal。
    返回 auth bundle: {access_token, refresh_token, id_token, account_id, email, plan_type,
                       personal_workspace_id}

    Round 11 五轮 Option A — 两阶段 personal OAuth:
      阶段 1(快路径):有 chatgpt_session_token → silent step-0 双域注入 + NextAuth refresh,
                       直接 goto auth_url。注册→未踢出场景大部分用得上。
                       拿到 plan_type=free 直接返回。
      阶段 2(fresh re-login fallback):阶段 1 拿到 plan != free 或 bundle=None,
                       说明 session_token 内嵌 user identity 锁死原 Team。清空 OAuth context
                       所有 cookies,做一次完整 chatgpt.com 登录(keyboard.type 逐字符,
                       绕过 React 灰按钮),拿 Personal-bound 全新 session,再走 OAuth → plan=free。
    """
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)
    _used_email_ids: set[int] = set()  # 记录已尝试过的邮件，避免重复提交同一封验证码邮件

    # personal 模式下不引导到 Team workspace
    chatgpt_account_id = "" if use_personal else get_chatgpt_account_id()

    auth_url = _build_auth_url(code_challenge, state)

    logger.info(
        "[Codex] 开始 OAuth 登录: %s (use_personal=%s, session_token=%s)",
        email,
        use_personal,
        "yes" if chatgpt_session_token else "no",
    )

    auth_code = None
    # Round 11 V8 — personal UUID 在 silent step-0 / stage2 re-login 完成后通过
    # POST /backend-api/accounts/personal getOrCreate 拿到,之后用 allowed_workspace_id
    # 注入 OAuth /authorize URL,绕过 default_workspace_id sticky-Team 死锁。
    # 注入既可来自调用方 prefetched_personal_uuid(accounts.json 持久化字段),
    # 也可来自 silent step-0 现场 fetch。
    personal_uuid = prefetched_personal_uuid

    # Round 11 V8 fast-path(verifier #2 P0)+ V12 P0.1 deprecation —
    # `_password_grant_access_token` 已被 V11 探活实证不可用:OpenAI auth.openai.com/oauth/token
    # 30 个 client_id/scope/audience 组合全员 HTTP 400 `unknown_parameter: 'username'`,
    # ROPC password grant 已撤(OAuth 2.1 demand 移除)。保留此 fast-path 仅作为未来
    # OpenAI 重启 ROPC 的 hook,实际效果在新号场景永远进 logger.warning 分支。
    # 真正落 personal_uuid 的路径切到 silent step-0 内的 fetch_nextauth_backend_access_token
    # → fetch_personal_uuid Bearer 链(V12 P0.1)。
    if use_personal and not personal_uuid and email and password:
        try:
            tmp_access = _password_grant_access_token(email, password)
            if tmp_access:
                fetched_uuid = fetch_personal_uuid(tmp_access)
                if fetched_uuid:
                    personal_uuid = fetched_uuid
                    logger.info(
                        "[Codex] fast-path Bearer 拿到 personal_workspace_id=%s",
                        personal_uuid,
                    )
                else:
                    logger.warning(
                        "[Codex] fast-path password grant 成功但 fetch_personal_uuid 返回 None,等 silent step-0 NextAuth fallback"
                    )
            else:
                logger.warning(
                    "[Codex] fast-path password grant 失败(V11 已知 ROPC 不可用),等 silent step-0 NextAuth fallback"
                )
        except Exception as exc:  # noqa: BLE001 — fast-path 失败不应阻断 OAuth 主流程
            logger.warning("[Codex] fast-path 异常,等 silent step-0 NextAuth fallback: %s", exc)

    with sync_playwright() as p:
        browser = p.chromium.launch(**get_playwright_launch_options())
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        )

        # Round 11 四轮 — Personal 模式 session_token 注入(silent step-0):
        # 实测刚踢出 Team 的新号在 OAuth /log-in 页 fill email 后 Continue 按钮变灰禁用,
        # login flow 永远卡在 email 步骤,bundle=None。根因疑似 Playwright fill() 的
        # input 事件被 OpenAI auth /log-in React 表单的 anti-bot 检测识别。
        # 单纯把 session_token 灌到 auth.openai.com 不够 —— NextAuth 对加密 token
        # 跨域 issuer 校验严格,/oauth/authorize 不认 chatgpt.com 颁发的 token。
        # 必须做"silent step-0":把 token 灌到 chatgpt.com 域,先 goto chatgpt.com
        # 让服务端 _next-auth API 校验 session 并写一套配套 cookies(oai-did /
        # __cflb / cf_clearance / _puid 等),然后再 goto auth_url。OpenAI auth
        # backend 看到来自 chatgpt.com 的有效会话引荐 → 直接跳过 /log-in 进 consent。
        # 同时 auth.openai.com 域也注入一份(冗余兜底),双域同步 SessionCodexAuthFlow。
        if use_personal and chatgpt_session_token:
            _inject_personal_session_cookies(context, chatgpt_session_token)
            logger.info(
                "[Codex] personal 模式注入 __Secure-next-auth.session-token (len=%d) 到 chatgpt.com + auth.openai.com",
                len(chatgpt_session_token),
            )

        # === Step 0: 先登录 ChatGPT 并切换到 Team workspace ===
        # Team 模式:登录前注入 _account cookie 引导登录进入 Team workspace。
        #
        # Round 11 三轮 — Personal 模式也需要 step-0:
        # 历史注释说"personal 模式跳过 step-0 因为 chatgpt_account_id="" 无 cookie 可注入"。
        # 但实测发现刚踢出 Team 的新号在 OpenAI auth backend 端 oai-oauth-session.workspaces=[]
        # (server-side 状态),直接走 auth_url 时 /oauth/authorize → /log-in → 永远循环,
        # 即便 consent loop 点 10 次 Continue,URL 仍卡在 /log-in 没有 auth_code。
        #
        # 根因:OpenAI 只在用户在 chatgpt.com 实际登录后才在 OAuth session 端 populate workspaces。
        # 新号注册流走 chatgpt.com 是另一个 browser context;OAuth 这个新 context 第一次访问
        # auth.openai.com,session 端没有任何 workspace 关联 → /oauth/authorize 拒绝颁 token。
        #
        # 修复:personal 模式也走 step-0,先 chatgpt.com 登录建立 Personal workspace 上下文,
        # auth.openai.com session 端 workspaces[] 被 populate 后,auth_url 即可正常 consent。
        # 区别:不注入 _account cookie(没有 Team workspace_id),让 chatgpt.com 自动用 Personal。

        # 在登录开始前记录当前最新邮件 ID,后续只接受比这个更新的
        _email_id_before_login = 0
        if mail_client:
            try:
                _pre = mail_client.search_emails_by_recipient(email, size=1)
                if _pre:
                    _email_id_before_login = _pre[0].get("emailId", 0)
            except Exception:
                pass

        if use_personal:
            if chatgpt_session_token:
                # Round 11 四轮 — silent step-0:cookie 已注入双域,只需 goto chatgpt.com
                # 让服务端 next-auth API 校验 session 并写齐配套 cookies(oai-did / _puid /
                # __cflb 等),OpenAI auth backend 看到来自 chatgpt.com 的有效会话引荐 →
                # /oauth/authorize 不再走 /log-in 表单,直接 consent。
                #
                # 进一步:踢出 Team 后,session_token 内 user.workspace 字段还指向 Team,
                # OAuth 仍拿到 plan_type=team。call /api/auth/session 强制 NextAuth 刷新
                # session,把 user.workspace 切到 Personal。然后再走 OAuth 才能拿 plan=free。
                logger.info("[Codex] personal 模式 silent step-0: cookie 注入后访问 chatgpt.com 验证 session...")
                _silent_page = context.new_page()
                try:
                    _silent_page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
                    time.sleep(5)
                    # 过 Cloudflare(若有)
                    for _i in range(8):
                        html_lower = _silent_page.content()[:2000].lower()
                        if "verify you are human" not in html_lower and "challenge" not in (_silent_page.url or "").lower():
                            break
                        logger.info("[Codex] silent step-0 等待 Cloudflare... (%ds)", _i * 5)
                        time.sleep(5)

                    # 强制 NextAuth session refresh:踢出 Team 后必须刷新 user.workspace
                    # 才能让 OAuth 拿 plan=free 而非缓存的 plan=team。
                    try:
                        refresh_resp = _silent_page.evaluate(
                            """
                            async () => {
                                const r = await fetch('/api/auth/session?update', { credentials: 'include', cache: 'no-store' });
                                const ct = r.headers.get('content-type') || '';
                                if (!ct.includes('application/json')) return { ok: r.ok, status: r.status, raw: 'non-json' };
                                const data = await r.json();
                                return { ok: r.ok, status: r.status, hasUser: !!data?.user, plan: data?.user?.plan ?? null };
                            }
                            """
                        )
                        logger.info("[Codex] silent step-0 NextAuth session refresh 结果: %s", refresh_resp)
                    except Exception as refresh_exc:
                        logger.warning("[Codex] silent step-0 NextAuth refresh 异常(忽略): %s", refresh_exc)

                    # 再调一次 backend-api/accounts/check,触发 server-side workspace 重新判定
                    try:
                        accounts_resp = _silent_page.evaluate(
                            """
                            async () => {
                                const r = await fetch('/backend-api/accounts/check', { credentials: 'include', cache: 'no-store' });
                                return { status: r.status };
                            }
                            """
                        )
                        logger.info("[Codex] silent step-0 accounts/check 结果: %s", accounts_resp)
                    except Exception as ck_exc:
                        logger.debug("[Codex] silent step-0 accounts/check 异常(忽略): %s", ck_exc)

                    # Round 11 V13 P0.2 — 浏览器内 fetch_personal_uuid_via_page 绕 Cloudflare。
                    # 已有 prefetched_personal_uuid 则跳过重复 fetch。否则:
                    #   primary: /api/auth/session 提 accessToken → page.evaluate Bearer
                    #            fetch_personal_uuid_via_page(避开 cf bot 拦截)
                    #   fallback: 原 in-browser POST /accounts/personal cookie 路径(新号场景已知 401)
                    if not personal_uuid:
                        try:
                            nextauth_token = fetch_nextauth_backend_access_token(_silent_page)
                            if nextauth_token:
                                fetched_uuid = fetch_personal_uuid_via_page(
                                    _silent_page, nextauth_token
                                )
                                if fetched_uuid:
                                    personal_uuid = fetched_uuid
                                    logger.info(
                                        "[Codex] silent step-0 NextAuth Bearer 路径(浏览器内)拿到 personal_workspace_id=%s",
                                        personal_uuid,
                                    )
                                else:
                                    logger.warning(
                                        "[Codex] silent step-0 NextAuth accessToken 拿到但 fetch_personal_uuid_via_page 返回 None,降级 cookie fallback"
                                    )
                        except Exception as nx_exc:
                            logger.warning(
                                "[Codex] silent step-0 NextAuth route 异常,降级 cookie fallback: %s",
                                nx_exc,
                            )

                    if not personal_uuid:
                        try:
                            personal_resp = _silent_page.evaluate(
                                """
                                async () => {
                                    const r = await fetch('/backend-api/accounts/personal', {
                                        method: 'POST',
                                        credentials: 'include',
                                        headers: {'Content-Type': 'application/json'},
                                        body: '{}',
                                        cache: 'no-store',
                                    });
                                    let data = null;
                                    try { data = await r.json(); } catch (_) {}
                                    return { status: r.status, id: data && data.id, created: data && data.created };
                                }
                                """
                            )
                            logger.info(
                                "[Codex] silent step-0 cookie fallback /accounts/personal 结果: %s",
                                personal_resp,
                            )
                            if isinstance(personal_resp, dict) and personal_resp.get("status") == 200:
                                fetched_uuid = personal_resp.get("id")
                                if fetched_uuid:
                                    personal_uuid = fetched_uuid
                                    logger.info(
                                        "[Codex] silent step-0 cookie fallback 拿到 personal_workspace_id=%s (created=%s)",
                                        personal_uuid,
                                        personal_resp.get("created"),
                                    )
                        except Exception as pu_exc:
                            logger.warning(
                                "[Codex] silent step-0 cookie fallback /accounts/personal fetch 异常(继续走 OAuth): %s",
                                pu_exc,
                            )

                    cur = _silent_page.url or ""
                    logger.info("[Codex] silent step-0 完成 URL: %s", cur[:120])
                    _screenshot(_silent_page, "codex_00b_silent_session_validate.png")
                except Exception as exc:
                    logger.warning("[Codex] silent step-0 异常(继续走 OAuth): %s", exc)
                finally:
                    try:
                        _silent_page.close()
                    except Exception:
                        pass
            else:
                logger.info("[Codex] personal 模式: 无 session_token,跳过 step-0,直接走 auth_url")
        else:
            if chatgpt_account_id:
                context.add_cookies(
                    [
                        {
                            "name": "_account",
                            "value": chatgpt_account_id,
                            "domain": "chatgpt.com",
                            "path": "/",
                            "secure": True,
                            "sameSite": "Lax",
                        },
                        {
                            "name": "_account",
                            "value": chatgpt_account_id,
                            "domain": "auth.openai.com",
                            "path": "/",
                            "secure": True,
                            "sameSite": "Lax",
                        },
                    ]
                )
                logger.debug("[Codex] 登录前已注入 _account cookie = %s", chatgpt_account_id)

            logger.info("[Codex] 先登录 ChatGPT 选择 Team workspace...")
            _page = context.new_page()
            _page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)

            # Cloudflare
            for _i in range(12):
                if "verify you are human" not in _page.content()[:2000].lower():
                    break
                time.sleep(5)

            # 点击登录
            try:
                _page.locator('button:has-text("登录"), button:has-text("Log in")').first.click()
                time.sleep(3)
            except Exception:
                pass

            # 输入邮箱（避免误点 Google/Microsoft 第三方登录按钮）
            try:
                ei = _page.locator('input[name="email"], input[id="email-input"], input[id="email"]').first
                if ei.is_visible(timeout=5000):
                    ei.fill(email)
                    time.sleep(0.5)
                    _click_primary_auth_button(_page, ei, ["Continue", "继续"])
                    time.sleep(3)
            except Exception:
                pass

            # 输入密码 / 点击一次性验证码登录
            try:
                pi = _page.locator('input[type="password"]').first
                if pi.is_visible(timeout=5000):
                    if password:
                        pi.fill(password)
                        time.sleep(0.5)
                        _click_primary_auth_button(_page, pi, ["Continue", "继续", "Log in"])
                    else:
                        # 没有密码，点击"使用一次性验证码登录"
                        otp_btn = _page.locator(
                            'button:has-text("一次性验证码"), button:has-text("one-time"), button:has-text("email login")'
                        ).first
                        if otp_btn.is_visible(timeout=3000):
                            logger.info("[Codex] 无密码，点击一次性验证码登录")
                            otp_btn.click()
                        else:
                            # fallback: 提交空密码让页面报错，然后找验证码按钮
                            _click_primary_auth_button(_page, pi, ["Continue", "继续", "Log in"])
                    time.sleep(8)
            except Exception:
                pass

            # 可能需要邮箱验证码
            try:
                ci = _page.locator('input[name="code"]').first
                if ci.is_visible(timeout=5000) and mail_client:
                    logger.info("[Codex] ChatGPT 登录需要验证码，等待 emailId > %d 的新邮件...", _email_id_before_login)
                    otp = None
                    otp_email_id = 0
                    t0 = time.time()
                    while time.time() - t0 < 120:
                        for em in mail_client.search_emails_by_recipient(email, size=5):
                            email_id = em.get("emailId", 0)
                            if email_id <= _email_id_before_login or email_id in _used_email_ids:
                                continue
                            otp = mail_client.extract_verification_code(em)
                            if otp:
                                otp_email_id = email_id
                                break
                        if otp:
                            break
                        time.sleep(3)
                    if otp:
                        _used_email_ids.add(otp_email_id)
                        ci.fill(otp)
                        time.sleep(0.5)
                        _page.locator('button[type="submit"]').first.click()
                        time.sleep(5)
            except Exception:
                pass

            _screenshot(_page, "codex_00_chatgpt_login.png")
            logger.info("[Codex] ChatGPT 登录后 URL: %s", _page.url)

            # 如果是 workspace 选择页面，Team 模式选配置的 workspace
            if "workspace" in _page.url:
                workspace_name = get_chatgpt_workspace_name()
                logger.info("[Codex] 检测到 workspace 选择页面...")
                try:
                    ws_btn = _page.locator(f'text="{workspace_name}"').first
                    if workspace_name and ws_btn.is_visible(timeout=3000):
                        logger.info("[Codex] 选择 workspace: %s", workspace_name)
                        ws_btn.click()
                        time.sleep(5)
                    else:
                        # fallback: 选第二个选项（第一个通常是"个人"）
                        options = _page.locator('a, button, [role="button"]').all()
                        for opt in options:
                            try:
                                text = opt.inner_text(timeout=1000).strip()
                                if (
                                    text
                                    and "个人" not in text
                                    and "Personal" not in text
                                    and text not in ("ChatGPT", "")
                                ):
                                    logger.info("[Codex] 选择 workspace: %s", text)
                                    opt.click()
                                    time.sleep(5)
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass
                _screenshot(_page, "codex_00_after_workspace.png")
                logger.info("[Codex] 选择 workspace 后 URL: %s", _page.url)

            # _account cookie 已在登录前注入

            # 关闭 ChatGPT 页面但保留 context
            _page.close()

        # 通过监听请求来捕获 OAuth callback redirect
        def on_request(request):
            nonlocal auth_code
            url = request.url
            if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in url:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                auth_code = qs.get("code", [None])[0]
                if auth_code:
                    logger.info("[Codex] 捕获到 auth code!")

        # 也监听 response/framenavigated 来捕获 redirect URL
        def on_response(response):
            nonlocal auth_code
            url = response.url
            if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in url and not auth_code:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                auth_code = qs.get("code", [None])[0]
                if auth_code:
                    logger.info("[Codex] 从 response 捕获到 auth code!")

        # Round 11 V8 — 拿到 personal UUID 后,把 OAuth /authorize URL 拼上 allowed_workspace_id,
        # 让 issuer 在颁 token 时优先选 personal workspace 而非 default_workspace_id 指向的 Team。
        if use_personal and personal_uuid:
            auth_url = _build_auth_url_with_allowed_workspace(code_challenge, state, personal_uuid)
            logger.info(
                "[Codex] OAuth /authorize 注入 allowed_workspace_id=%s",
                personal_uuid,
            )

        page = context.new_page()
        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        _screenshot(page, "codex_01_auth_page.png")

        # 输入邮箱（注意避免点到 Google/Microsoft/Apple 第三方登录按钮）
        try:
            for attempt in range(2):
                email_input = page.locator('input[name="email"], input[id="email-input"], input[id="email"]').first
                if not email_input.is_visible(timeout=5000):
                    break

                email_input.fill(email)
                time.sleep(0.5)
                _click_primary_auth_button(page, email_input, ["Continue", "继续"])
                time.sleep(3)

                if not _is_google_redirect(page):
                    break

                _screenshot(page, f"codex_02_google_redirect_attempt{attempt + 1}.png")
                logger.warning("[Codex] 邮箱步骤误跳转到 Google 登录，返回重试... (attempt %d)", attempt + 1)
                page.go_back(wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
            _screenshot(page, "codex_02_after_email.png")
        except Exception:
            _screenshot(page, "codex_02_no_email.png")

        # 输入密码
        try:
            for attempt in range(2):
                pwd_input = page.locator('input[name="password"], input[type="password"]').first
                if not pwd_input.is_visible(timeout=5000):
                    break

                pwd_input.fill(password)
                time.sleep(0.5)
                _click_primary_auth_button(page, pwd_input, ["Continue", "继续", "Log in"])
                time.sleep(5)

                if not _is_google_redirect(page):
                    break

                _screenshot(page, f"codex_03_google_redirect_attempt{attempt + 1}.png")
                logger.warning("[Codex] 密码步骤误跳转到 Google 登录，返回重试... (attempt %d)", attempt + 1)
                page.go_back(wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
            _screenshot(page, "codex_03_after_password.png")
        except Exception:
            _screenshot(page, "codex_03_no_password.png")

        # 可能需要邮箱登录验证码
        _screenshot(page, "codex_03b_check_otp.png")
        code_input = None
        try:
            code_input = page.locator(
                'input[name="code"], input[placeholder*="验证码"], input[placeholder*="code" i]'
            ).first
            if not code_input.is_visible(timeout=5000):
                code_input = None
        except Exception:
            code_input = None

        if code_input and mail_client:
            logger.info("[Codex] 需要登录验证码，等待 emailId > %d 的新邮件...", _email_id_before_login)

            start_t = time.time()
            otp_code = None
            otp_email_id = 0
            while time.time() - start_t < 120:
                emails = mail_client.search_emails_by_recipient(email, size=5)
                for em in emails:
                    email_id = em.get("emailId", 0)
                    if email_id <= _email_id_before_login or email_id in _used_email_ids:
                        continue
                    subj = em.get("subject", "").lower()
                    if "invited" in subj or "invitation" in subj:
                        continue
                    otp_code = mail_client.extract_verification_code(em)
                    if otp_code:
                        otp_email_id = email_id
                        break
                if otp_code:
                    break
                time.sleep(3)

            if otp_code:
                _used_email_ids.add(otp_email_id)
                logger.info("[Codex] 获取到验证码: %s", otp_code)
                code_input.fill(otp_code)
                time.sleep(0.5)
                page.locator(
                    'button:has-text("Continue"), button:has-text("继续"), button[type="submit"]'
                ).first.click()
                time.sleep(5)
                _screenshot(page, "codex_03c_after_otp.png")
            else:
                logger.warning("[Codex] 未获取到验证码")
        elif code_input:
            logger.warning("[Codex] 需要验证码但无 mail_client，无法自动获取")

        # SPEC-2 shared/add-phone-detection §4 (位点 C-P1):about-you 入口前先探针。
        # 注册流程提交 OTP 后 OpenAI 经常把账号引到 add-phone,等切到 about-you 再发现就太晚。
        assert_not_blocked(page, "oauth_about_you")

        # 处理 about-you 页面（可能出现在 OAuth 流程中）
        if "about-you" in page.url:
            logger.info("[Codex] 检测到 about-you 页面，填写个人信息...")
            try:
                name_input = page.locator('input[name="name"]').first
                if name_input.is_visible(timeout=3000):
                    name_input.fill("User")

                # 自适应：生日日期（spinbutton）或年龄（普通 input）
                spinbuttons = page.locator('[role="spinbutton"]').all()
                if len(spinbuttons) >= 3:
                    # 类型 A：React Aria DateField
                    try:
                        page.locator("text=生日日期").click()
                        time.sleep(0.5)
                    except Exception:
                        pass
                    for sb, val in zip(spinbuttons[:3], ["1995", "06", "15"]):
                        sb.click(force=True)
                        time.sleep(0.2)
                        page.keyboard.type(val, delay=80)
                        time.sleep(0.3)
                    logger.info("[Codex] 填入生日: 1995/06/15 (spinbutton)")
                else:
                    # 类型 B：普通年龄数字输入框
                    age_input = page.locator('input[name="age"], input[placeholder*="年龄"]').first
                    try:
                        if age_input.is_visible(timeout=3000):
                            age_input.fill("25")
                            logger.info("[Codex] 填入年龄: 25")
                    except Exception:
                        logger.warning("[Codex] 未找到年龄/生日输入框")

                time.sleep(0.5)
                page.locator(
                    'button:has-text("继续"), button:has-text("Continue"), button:has-text("完成帐户创建"), button[type="submit"]'
                ).first.click()
                time.sleep(5)
                _screenshot(page, "codex_03d_after_aboutyou.png")
                logger.info("[Codex] about-you 完成，当前 URL: %s", page.url)
            except Exception as e:
                logger.error("[Codex] about-you 处理失败: %s", e)

        # Round 11 三轮 — Personal 模式: pre-consent workspace_select.
        # 历史:
        #   Round 8 加 explicit workspace_select 是因为"default 不会自动 unset"研究结论,
        #   想强制把 default 切到 Personal 再 consent。
        #   Round 11 二轮把 workspace_select 前置到 consent loop 之前,绕过"consent loop 1-2
        #   步抓到 auth_code → workspace_select 永远不被调用"的 bug。
        #
        # Round 11 三轮发现:刚踢出 Team 的新号在 OpenAI auth backend 端
        # `oai-oauth-session.workspaces=[]`(server-side 状态),/workspace UI 显示
        # "Workspaces not found in client auth session" 错误。force_select_personal_via_ui
        # 把浏览器 goto /workspace,落在错误页 → consent loop 找不到 consent button →
        # 永远 bundle=None,5 次外层重试全失败。
        #
        # 修复:把 pre-consent workspace_select 改成"尽力而为"——
        #   1. 如果 workspaces[] 非空,正常 POST /api/accounts/workspace/select 切 personal
        #   2. 如果 workspaces[] 空(刚踢出场景),不再 goto /workspace UI(肯定错),而是直接
        #      跳过 → 让 consent loop 在 auth_url 上自然运行。OAuth backend 用 default
        #      workspace 颁 token,如果 plan!=free,外层 5 次重试 + bundle plan_type 校验
        #      会拦下来,等后端最终一致性同步(回归 Round 4/e760be9 8s sleep 行为)
        #   3. 任何情况 finally 强制 goto auth_url,确保 consent loop 入口正确
        if use_personal:
            try:
                from autoteam.oauth_workspace import (
                    ensure_personal_workspace_selected as _pre_consent_ws_select,
                )

                # 用当前页面状态(login + about-you 完成,session cookie 已建立)调 workspace/select.
                # auth_url 作为 fallback 的 base 用,正常成功不会用到.
                pre_ws_ok, pre_ws_fail, pre_ws_ev = _pre_consent_ws_select(
                    page,
                    consent_url=auth_url,
                    skip_ui_fallback_on_empty=True,  # Round 11 三轮 — 空 workspaces[] 不再 goto /workspace UI
                )
                if pre_ws_ok:
                    logger.info(
                        "[Codex] Personal mode pre-consent workspace_select 成功 — "
                        "后续 consent 应颁 plan=free token"
                    )
                else:
                    logger.warning(
                        "[Codex] Personal mode pre-consent workspace_select 失败 "
                        "fail_category=%s evidence=%s,继续走 consent loop(由外层重试兜底)",
                        pre_ws_fail,
                        json.dumps(pre_ws_ev, ensure_ascii=False)[:300],
                    )
                _screenshot(page, "codex_03e_pre_consent_workspace_select.png")
            except Exception as exc:
                logger.warning(
                    "[Codex] Personal mode pre-consent workspace_select 异常: %s,"
                    "继续 consent loop",
                    exc,
                )

            # Round 11 三轮 — 仅在浏览器停留在 /workspace 错误页时导航回 auth_url。
            # /workspace 路径出错(force_select_personal_via_ui 走该 URL 但 server 返
            # "Workspaces not found in client auth session"),consent loop 找不到按钮永远 bundle=None。
            # 但若浏览器在 /log-in / /password / about-you 等正常 OAuth 流程页,goto(auth_url) 会
            # **重置** login 表单状态(email/password 输入清空),consent loop 再也跑不通。
            # 所以只针对已知的 /workspace 错误页恢复,其他保持 page 当前状态让流程自然跑完。
            try:
                current_url = (page.url or "")
                if "/workspace" in current_url:
                    logger.info(
                        "[Codex] pre-consent 后浏览器停在 /workspace 错误页,导航回 auth_url 恢复 consent flow (current_url=%s)",
                        current_url[:120],
                    )
                    page.goto(auth_url, wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2)
            except Exception as exc:
                logger.warning("[Codex] 导航回 auth_url 失败: %s,consent loop 仍尝试", exc)

        # 处理多个授权/同意页面（可能有多步）
        for step in range(10):
            if auth_code:
                break

            # SPEC-2 shared/add-phone-detection §4 (位点 C-P2):consent 循环每轮开头探针。
            # 不在循环里拦,会被当作"workspace 没选好"反复重试,30s callback 等待白白耗费。
            assert_not_blocked(page, f"oauth_consent_{step}")

            _screenshot(page, f"codex_04_step{step + 1}_before.png")

            # 在任何页面中，如果有 workspace/组织选择，先选 Team（personal 模式下选个人）
            try:
                # Round 11 — 与 cnitlrt/AutoTeam upstream codex_auth.py:772-815 对齐:
                # consent loop 每个 step 起始先用 upstream-style 健壮检测,避免页面变 workspace
                # 选择页时被当成 "consent button 不可见" → break → 30s callback 等待白白消耗。
                # Team 模式优先用 upstream helper;personal 模式留给 ensure_personal_workspace_selected
                # 在 consent loop 之后兜底(L916+)。
                if not use_personal:
                    from autoteam.oauth_workspace import (
                        _is_workspace_selection_page as _ws_is_selection_page,
                    )
                    from autoteam.oauth_workspace import (
                        _select_team_workspace as _ws_select_team,
                    )

                    workspace_name_upstream = get_chatgpt_workspace_name()
                    if _ws_is_selection_page(page):
                        _screenshot(page, f"codex_04_workspace_{step + 1}_before.png")
                        logger.info(
                            "[Codex] 检测到工作空间选择页 (step %d, upstream),尝试选择: %s",
                            step + 1,
                            workspace_name_upstream,
                        )
                        upstream_selected = _ws_select_team(page, workspace_name_upstream)
                        _screenshot(page, f"codex_04_workspace_{step + 1}_after.png")
                        if upstream_selected:
                            # 选完 workspace 后点"继续"按钮提交
                            try:
                                cont_btn = page.locator(
                                    'button:has-text("继续"), button:has-text("Continue")'
                                ).first
                                if cont_btn.is_visible(timeout=3000):
                                    cont_btn.click()
                                    time.sleep(3)
                                    logger.info("[Codex] 已点击继续 (step %d, upstream)", step + 1)
                            except Exception:
                                pass
                            continue
                        else:
                            logger.warning(
                                "[Codex] upstream-style 无法选择 workspace '%s' (step %d),回退现有 JS/locator 路径",
                                workspace_name_upstream,
                                step + 1,
                            )

                page_text = page.inner_text("body")[:1000]

                # personal 模式：检测到工作空间选择页时，直接选 Personal
                if use_personal and (
                    "选择一个工作空间" in page_text or "Select a workspace" in page_text or "选择工作空间" in page_text
                ):
                    _screenshot(page, f"codex_04_personal_ws_{step + 1}_before.png")
                    logger.info("[Codex] 检测到工作空间选择页 (step %d, personal 模式)", step + 1)
                    personal_selected = False
                    try:
                        personal_btn = page.locator("text=/个人|Personal/").first
                        if personal_btn.is_visible(timeout=2000):
                            personal_btn.click(force=True)
                            time.sleep(1)
                            personal_selected = True
                            logger.info("[Codex] 已选择 Personal workspace (step %d)", step + 1)
                    except Exception as e:
                        logger.warning("[Codex] 选择 Personal 失败: %s", e)
                    _screenshot(page, f"codex_04_personal_ws_{step + 1}_after.png")
                    if personal_selected:
                        try:
                            cont_btn = page.locator('button:has-text("继续"), button:has-text("Continue")').first
                            if cont_btn.is_visible(timeout=3000):
                                cont_btn.click()
                                time.sleep(3)
                        except Exception:
                            pass
                        continue

                # 选择 Team workspace（用配置的名称精确匹配）— upstream-style 检测失败后的 JS/locator 兜底
                workspace_name = "" if use_personal else get_chatgpt_workspace_name()
                # 检测"选择一个工作空间"页面，点击 Team workspace
                if workspace_name and (
                    "选择一个工作空间" in page_text or "Select a workspace" in page_text or "选择工作空间" in page_text
                ):
                    selected = False
                    _screenshot(page, f"codex_04_workspace_{step + 1}_before.png")
                    logger.info("[Codex] 检测到工作空间选择页 (step %d)，尝试选择: %s", step + 1, workspace_name)

                    # 用 JS 直接点击包含 workspace 名称的元素（最可靠）
                    try:
                        clicked = page.evaluate(
                            """(name) => {
                            const els = document.querySelectorAll('*');
                            for (const el of els) {
                                const text = (el.textContent || '').trim();
                                if (text === name && !text.includes('个人') && !text.includes('Personal')) {
                                    // 找到最近的可点击父元素
                                    let target = el;
                                    while (target && target.tagName !== 'BODY') {
                                        const tag = target.tagName.toLowerCase();
                                        if (['button', 'a', 'li', 'label'].includes(tag)
                                            || target.getAttribute('role')
                                            || target.onclick
                                            || target.classList.length > 0) {
                                            target.click();
                                            return true;
                                        }
                                        target = target.parentElement;
                                    }
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }""",
                            workspace_name,
                        )
                        if clicked:
                            time.sleep(1)
                            selected = True
                            logger.info("[Codex] 已选择 workspace (JS): %s (step %d)", workspace_name, step + 1)
                    except Exception as e:
                        logger.warning("[Codex] JS 选择 workspace 失败: %s", e)

                    if not selected:
                        # fallback: Playwright 选择器
                        try:
                            ws_el = page.locator(f"text={workspace_name}").first
                            if ws_el.is_visible(timeout=2000):
                                ws_el.click(force=True)
                                time.sleep(1)
                                selected = True
                                logger.info(
                                    "[Codex] 已选择 workspace (force click): %s (step %d)", workspace_name, step + 1
                                )
                        except Exception:
                            pass

                    _screenshot(page, f"codex_04_workspace_{step + 1}_after.png")
                    if selected:
                        # 选完 workspace 后点"继续"按钮提交
                        try:
                            cont_btn = page.locator('button:has-text("继续"), button:has-text("Continue")').first
                            if cont_btn.is_visible(timeout=3000):
                                cont_btn.click()
                                time.sleep(3)
                                logger.info("[Codex] 已点击继续 (step %d)", step + 1)
                        except Exception:
                            pass
                        continue
                    else:
                        logger.warning("[Codex] 无法选择 workspace '%s' (step %d)", workspace_name, step + 1)

                elif workspace_name:
                    # 非工作空间选择页，但可能有 workspace 文本（如 organization 页）
                    try:
                        ws_btn = page.locator(f'text="{workspace_name}"').first
                        if ws_btn.is_visible(timeout=1000):
                            ws_btn.click()
                            time.sleep(1)
                            logger.info("[Codex] 已选择 workspace: %s (step %d)", workspace_name, step + 1)
                    except Exception:
                        pass

                # Organization 页面的下拉选择 — 与 upstream codex_auth.py:798-813 对齐
                if "organization" in page.url:
                    dropdown = page.locator("[aria-expanded], [aria-haspopup]").first
                    if dropdown.is_visible(timeout=2000):
                        dropdown.click()
                        time.sleep(1)
                        options = page.locator('[role="option"]').all()
                        for opt in options:
                            text = opt.inner_text(timeout=1000).strip()
                            if text and "新组织" not in text and "New" not in text:
                                opt.click()
                                logger.info("[Codex] 选择已有组织: %s", text)
                                break
                        else:
                            if options:
                                options[0].click()
                        time.sleep(1)
            except Exception:
                pass

            # 处理密码页面（可能在 consent 流程中出现）
            try:
                pwd_field = page.locator('input[name="password"], input[type="password"]').first
                if pwd_field.is_visible(timeout=2000):
                    if password:
                        logger.info("[Codex] 需要重新输入密码 (step %d)...", step + 1)
                        pwd_field.fill(password)
                        time.sleep(0.5)
                        _click_primary_auth_button(page, pwd_field, ["Continue", "继续", "Log in"])
                    else:
                        # 没密码，点"使用一次性验证码登录"
                        otp_btn = page.locator(
                            'button:has-text("一次性验证码"), button:has-text("one-time"), button:has-text("email login")'
                        ).first
                        if otp_btn.is_visible(timeout=3000):
                            logger.info("[Codex] 无密码，点击一次性验证码登录 (step %d)", step + 1)
                            otp_btn.click()
                        else:
                            _click_primary_auth_button(page, pwd_field, ["Continue", "继续", "Log in"])
                    time.sleep(5)
                    _screenshot(page, f"codex_04_password_{step + 1}.png")
                    continue
            except Exception:
                pass

            # 处理邮箱验证码页面（可能在 consent 流程中出现）
            try:
                otp_input = page.locator(_OTP_INPUT_SELECTORS).first
                if otp_input.is_visible(timeout=2000) and mail_client:
                    logger.info(
                        "[Codex] 需要邮箱验证码 (step %d)，等待 emailId > %d 的新邮件...",
                        step + 1,
                        _email_id_before_login,
                    )
                    otp = None
                    otp_email_id = 0
                    page_left_code = False
                    t0 = time.time()
                    while time.time() - t0 < 120:
                        if not _is_otp_input_visible(page, timeout=300):
                            page_left_code = True
                            logger.info("[Codex] 验证码页已退出，继续后续授权流程")
                            break
                        for em in mail_client.search_emails_by_recipient(email, size=5):
                            # 只接受比快照更新的邮件
                            email_id = em.get("emailId", 0)
                            if email_id <= _email_id_before_login or email_id in _used_email_ids:
                                continue
                            sender = (em.get("sendEmail") or "").lower()
                            if "openai" not in sender and "chatgpt" not in sender:
                                continue
                            subj = (em.get("subject") or "").lower()
                            if "invited" in subj or "invitation" in subj:
                                continue
                            otp = mail_client.extract_verification_code(em)
                            if otp:
                                otp_email_id = email_id
                                break
                        if otp:
                            break
                        time.sleep(3)
                    if otp:
                        submit_ok = False
                        for submit_attempt in range(1, 3):
                            otp_input = page.locator(_OTP_INPUT_SELECTORS).first
                            if not otp_input.is_visible(timeout=2000):
                                submit_ok = True
                                break

                            otp_input.fill(otp)
                            time.sleep(0.5)
                            page.locator(
                                'button[type="submit"], button:has-text("Continue"), button:has-text("继续")'
                            ).first.click()
                            logger.info("[Codex] 已输入验证码: %s", otp)

                            submit_status, submit_detail = _wait_for_otp_submit_result(page, timeout=12)
                            if submit_status == "accepted":
                                submit_ok = True
                                break
                            if submit_status == "invalid":
                                _used_email_ids.add(otp_email_id)
                                detail_suffix = f"，命中提示: {submit_detail}" if submit_detail else ""
                                logger.warning(
                                    "[Codex] 验证码邮件 %s（code=%s）被页面判定无效%s，标记并跳过该邮件",
                                    otp_email_id,
                                    otp,
                                    detail_suffix,
                                )
                                break

                            if submit_attempt < 2:
                                logger.warning(
                                    "[Codex] 验证码邮件 %s（code=%s）提交后未确认成功，准备重试第 %d/2 次",
                                    otp_email_id,
                                    otp,
                                    submit_attempt + 1,
                                )
                                time.sleep(2)
                            else:
                                _used_email_ids.add(otp_email_id)
                                logger.warning(
                                    "[Codex] 验证码邮件 %s（code=%s）提交后仍未确认成功，标记并跳过该邮件",
                                    otp_email_id,
                                    otp,
                                )

                        if submit_ok:
                            _used_email_ids.add(otp_email_id)
                        continue
                    if page_left_code:
                        continue
            except Exception:
                pass

            try:
                consent_btn = page.locator(
                    'button:has-text("继续"), button:has-text("Continue"), button:has-text("Allow")'
                ).first
                if consent_btn.is_visible(timeout=5000):
                    logger.info("[Codex] 点击同意/继续按钮 (step %d)...", step + 1)
                    consent_btn.click()
                    time.sleep(5)
                    _screenshot(page, f"codex_04_consent_{step + 1}.png")
                else:
                    break
            except Exception:
                break

        # Round 11 二轮 — pre-consent workspace_select 已前置(line 632+);此处作为兜底:
        # consent loop 自然结束(auth_code 未抓到,可能 workspace_select 仍未触发后端 default 切换)
        # 时再调一次 workspace_select。
        # Round 8 原始动机:personal OAuth 需要在 callback 之前**主动**选 personal workspace,
        # 否则 issuer 按 default_workspace_id(sticky 指向 Team)颁 token,拿到 plan_type=team。
        # 三层兜底:HTTP /api/accounts/workspace/select(主路径)→ Playwright UI fallback →
        # 失败则返回 fail_category 由外层 5 次重试承担。
        # Team 路径(use_personal=False)完全跳过 — 默认 default_workspace_id 已指向 Team。
        if use_personal and not auth_code:
            try:
                from autoteam.oauth_workspace import ensure_personal_workspace_selected

                consent_url_for_select = page.url or "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
                ws_ok, ws_fail_category, ws_evidence = ensure_personal_workspace_selected(
                    page,
                    consent_url=consent_url_for_select,
                    skip_ui_fallback_on_empty=True,  # Round 11 三轮 — 同 pre-consent
                )
                if ws_ok:
                    logger.info("[Codex] personal workspace 选择成功,继续等 callback")
                else:
                    logger.warning(
                        "[Codex] personal workspace 选择失败 fail_category=%s evidence=%s",
                        ws_fail_category,
                        json.dumps(ws_evidence, ensure_ascii=False)[:300],
                    )
                _screenshot(page, "codex_04b_after_workspace_select.png")
            except Exception as exc:
                logger.warning("[Codex] ensure_personal_workspace_selected 异常: %s", exc)

        # SPEC-2 shared/add-phone-detection §4 (位点 C-P3):等 callback 前探针。
        # add-phone 阻塞 = "callback 永远不来"的根因,30s 等待白白浪费。
        assert_not_blocked(page, "oauth_callback_wait")

        # 等待 redirect callback 获取 auth code
        for _ in range(30):
            if auth_code:
                break
            # 也从当前 URL 尝试提取（CPA 可能接收了回调）
            try:
                cur = page.url
                if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in cur:
                    parsed = urllib.parse.urlparse(cur)
                    qs = urllib.parse.parse_qs(parsed.query)
                    auth_code = qs.get("code", [None])[0]
                    if auth_code:
                        logger.info("[Codex] 从 URL 捕获到 auth code!")
                        break
            except Exception:
                pass
            time.sleep(1)

        if not auth_code:
            _screenshot(page, "codex_05_no_callback.png")
            logger.warning("[Codex] 未获取到 auth code，当前 URL: %s", page.url)

        # SPEC-2 §3.4.5 (位点 C-P4) + Round 6 PRD-5 FR-P1.1:personal 拒收 bundle 之前的最后一道关卡。
        # 防御性 — 通常 C-P1~C-P3 已拦下,但 add-phone 可能在 callback 阶段后晚到一两秒;
        # 此处在 browser.close() 之前做最后一次探针,确保 page 仍可读。命中即抛 RegisterBlocked,
        # 上游 5 个调用方按 add-phone-detection.md §5.2 矩阵分类处置(personal/team/reinvite/api 各异)。
        try:
            assert_not_blocked(page, "oauth_personal_check")
        except Exception:
            # assert_not_blocked 命中 add-phone 会抛 RegisterBlocked — 必须传播给上层
            # 但要保证 browser 资源被释放
            try:
                browser.close()
            except Exception:
                pass
            raise

        # Round 11 五轮 Option A — 阶段 1 完成,先尝试 exchange + plan 校验
        # 校验在 with sync_playwright() 块内做,失败时可以直接 fallback 到阶段 2
        # (使用同一 context,清空 cookies 后重新登录)而不需要重新启动 Playwright。
        stage1_bundle = None
        if auth_code:
            stage1_bundle = _exchange_auth_code(auth_code, code_verifier, fallback_email=email)
            if stage1_bundle:
                stage1_plan = (stage1_bundle.get("plan_type") or "").lower()
                logger.info(
                    "[Codex] 阶段 1 OAuth 完成: plan_type=%s account_id=%s",
                    stage1_plan or "unknown",
                    stage1_bundle.get("account_id"),
                )

        # 阶段 1 直接成功的判断:Team 路径不校验 plan,有 bundle 即可;
        # personal 路径仅 plan == "free" 算阶段 1 真成功。
        stage1_ok = bool(stage1_bundle) and (
            (not use_personal) or (stage1_bundle.get("plan_type") or "").lower() == "free"
        )

        # 阶段 2 触发条件 — 仅 personal 模式 + 阶段 1 失败(bundle=None 或 plan != free)
        # 触发后:context.clear_cookies() + fresh chatgpt.com login + 重新走 OAuth
        if (not stage1_ok) and use_personal:
            stage1_plan = (
                (stage1_bundle.get("plan_type") or "").lower() if stage1_bundle else "none"
            )
            logger.warning(
                "[Codex] 阶段 1 拿到 plan=%s(期望 free)bundle=%s,触发阶段 2 fresh re-login fallback",
                stage1_plan,
                "yes" if stage1_bundle else "no",
            )

            # === 阶段 2 入口 ===
            # 1) 清空 cookies + fresh chatgpt.com login(用 keyboard.type 绕过灰按钮)
            relogin_ok = _perform_fresh_relogin_in_context(
                context,
                email,
                password,
                mail_client,
                used_email_ids=_used_email_ids,
            )
            if not relogin_ok:
                logger.warning("[Codex] 阶段 2 fresh re-login 未成功,放弃")
                browser.close()
                return None

            # 2) 重新生成 PKCE + state(原 auth_code 已 used,新 OAuth 必须用新 code_verifier)
            stage2_code_verifier, stage2_code_challenge = _generate_pkce()
            stage2_state = secrets.token_urlsafe(16)

            # Round 11 V8 — fresh re-login 已清空 cookies,personal_uuid 之前在 silent step-0
            # 拿到的值仍可复用(personal workspace UUID 是稳定标识,不会因 re-login 重置)。
            # 若 stage 1 没拿到(prefetched_personal_uuid 也是 None),这里再尝试一次:
            #   V13 primary: NextAuth /api/auth/session accessToken → page.evaluate Bearer
            #                fetch_personal_uuid_via_page(浏览器内,避 cf 拦截)
            #   fallback: 原 in-browser POST /accounts/personal cookie 路径
            if use_personal and not personal_uuid:
                fetch_page = None
                try:
                    fetch_page = context.new_page()
                    fetch_page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=30000)

                    nextauth_token = fetch_nextauth_backend_access_token(fetch_page)
                    if nextauth_token:
                        fetched_uuid = fetch_personal_uuid_via_page(
                            fetch_page, nextauth_token
                        )
                        if fetched_uuid:
                            personal_uuid = fetched_uuid
                            logger.info(
                                "[Codex] 阶段 2 NextAuth Bearer 路径(浏览器内)拿到 personal_workspace_id=%s",
                                personal_uuid,
                            )

                    if not personal_uuid:
                        fetched_uuid = fetch_page.evaluate(
                            """
                            async () => {
                              try {
                                const resp = await fetch('/backend-api/accounts/personal', {
                                  method: 'POST',
                                  credentials: 'include',
                                  headers: {'Accept': 'application/json'},
                                });
                                if (!resp.ok) return null;
                                const j = await resp.json();
                                return j && j.id ? j.id : null;
                              } catch (e) { return null; }
                            }
                            """
                        )
                        if fetched_uuid:
                            personal_uuid = fetched_uuid
                            logger.info(
                                "[Codex] 阶段 2 cookie fallback 拿到 personal_workspace_id=%s",
                                personal_uuid,
                            )
                except Exception as exc:
                    logger.warning("[Codex] 阶段 2 fetch personal uuid 失败: %s", exc)
                finally:
                    if fetch_page is not None:
                        try:
                            fetch_page.close()
                        except Exception:
                            pass

            if use_personal and personal_uuid:
                stage2_auth_url = _build_auth_url_with_allowed_workspace(
                    stage2_code_challenge, stage2_state, personal_uuid
                )
                logger.info(
                    "[Codex] 阶段 2 OAuth /authorize 注入 allowed_workspace_id=%s",
                    personal_uuid,
                )
            else:
                stage2_auth_url = _build_auth_url(stage2_code_challenge, stage2_state)

            # 3) 重新走 OAuth — 在同一 context 内开新 page 监听 callback
            stage2_auth_code = None

            def _on_request_stage2(request):
                nonlocal stage2_auth_code
                url = request.url
                if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in url:
                    parsed = urllib.parse.urlparse(url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    stage2_auth_code = qs.get("code", [None])[0]
                    if stage2_auth_code:
                        logger.info("[Codex] 阶段 2 捕获到 auth code!")

            def _on_response_stage2(response):
                nonlocal stage2_auth_code
                url = response.url
                if (
                    f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in url
                    and not stage2_auth_code
                ):
                    parsed = urllib.parse.urlparse(url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    stage2_auth_code = qs.get("code", [None])[0]
                    if stage2_auth_code:
                        logger.info("[Codex] 阶段 2 从 response 捕获到 auth code!")

            stage2_page = context.new_page()
            stage2_page.on("request", _on_request_stage2)
            stage2_page.on("response", _on_response_stage2)
            try:
                stage2_page.goto(stage2_auth_url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(3)
                _screenshot(stage2_page, "codex_relogin_06_oauth_start.png")

                # 因为 context 现在持有 fresh Personal-bound session,
                # /oauth/authorize 通常直接进 consent。
                # 简化的 consent loop(沿用现有 page 的 consent 处理逻辑)
                for s2_step in range(10):
                    if stage2_auth_code:
                        break

                    # add-phone 探针
                    try:
                        assert_not_blocked(stage2_page, f"oauth_consent_stage2_{s2_step}")
                    except Exception:
                        try:
                            browser.close()
                        except Exception:
                            pass
                        raise

                    _screenshot(stage2_page, f"codex_relogin_07_consent_step{s2_step + 1}.png")

                    # workspace 选择页(personal 路径)
                    try:
                        page_text = stage2_page.inner_text("body")[:1000]
                        if (
                            "选择一个工作空间" in page_text
                            or "Select a workspace" in page_text
                            or "选择工作空间" in page_text
                        ):
                            logger.info("[Codex] 阶段 2 检测到 workspace 选择页 (step %d)", s2_step + 1)
                            try:
                                personal_btn = stage2_page.locator("text=/个人|Personal/").first
                                if personal_btn.is_visible(timeout=2000):
                                    personal_btn.click(force=True)
                                    time.sleep(1)
                                    cont_btn = stage2_page.locator(
                                        'button:has-text("继续"), button:has-text("Continue")'
                                    ).first
                                    if cont_btn.is_visible(timeout=3000):
                                        cont_btn.click()
                                        time.sleep(3)
                                    continue
                            except Exception as exc:
                                logger.warning("[Codex] 阶段 2 选 Personal 失败: %s", exc)
                    except Exception:
                        pass

                    # 同意/继续按钮
                    try:
                        consent_btn = stage2_page.locator(
                            'button:has-text("继续"), button:has-text("Continue"), button:has-text("Allow")'
                        ).first
                        if consent_btn.is_visible(timeout=5000):
                            logger.info("[Codex] 阶段 2 点击同意按钮 (step %d)", s2_step + 1)
                            consent_btn.click()
                            time.sleep(5)
                        else:
                            break
                    except Exception:
                        break

                # 等 callback
                for _ in range(30):
                    if stage2_auth_code:
                        break
                    try:
                        cur2 = stage2_page.url
                        if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in cur2:
                            parsed = urllib.parse.urlparse(cur2)
                            qs = urllib.parse.parse_qs(parsed.query)
                            stage2_auth_code = qs.get("code", [None])[0]
                            if stage2_auth_code:
                                logger.info("[Codex] 阶段 2 从 URL 捕获到 auth code!")
                                break
                    except Exception:
                        pass
                    time.sleep(1)

                if not stage2_auth_code:
                    _screenshot(stage2_page, "codex_relogin_08_no_callback.png")
                    logger.warning(
                        "[Codex] 阶段 2 未获取到 auth code,当前 URL: %s",
                        (stage2_page.url or "")[:120],
                    )
            finally:
                try:
                    stage2_page.close()
                except Exception:
                    pass

            browser.close()

            if not stage2_auth_code:
                logger.warning("[Codex] 阶段 2 OAuth 失败,放弃")
                return None

            stage2_bundle = _exchange_auth_code(
                stage2_auth_code, stage2_code_verifier, fallback_email=email
            )
            if not stage2_bundle:
                logger.warning("[Codex] 阶段 2 _exchange_auth_code 失败")
                return None

            stage2_plan = (stage2_bundle.get("plan_type") or "").lower()
            logger.info(
                "[Codex] 阶段 2 OAuth 完成: plan_type=%s account_id=%s",
                stage2_plan or "unknown",
                stage2_bundle.get("account_id"),
            )

            if stage2_plan != "free":
                logger.warning(
                    "[Codex] 阶段 2 仍拿到 plan_type=%s(期望 free),返回 None 由外层 5 次重试兜底",
                    stage2_plan or "unknown",
                )
                return None

            if personal_uuid:
                stage2_bundle["personal_workspace_id"] = personal_uuid
            return stage2_bundle

        # 阶段 1 通过(非 personal 或 plan == free) — 走原退出路径
        browser.close()

    if not auth_code:
        logger.error("[Codex] OAuth 登录失败: 未获取到 authorization code")
        return None

    if not stage1_bundle:
        return None

    # Personal 模式强校验 plan_type:当子号还挂在 Team workspace(OpenAI 后端 kick 同步延迟 /
    # default workspace 为 Team)时,auth.openai.com 会默认选 Team 颁发 token,拿到 plan_type=team
    # 的 bundle —— 这个 token 绑在 Team account_id 上,一旦子号离开 Team 就作废(refresh 401),
    # 本地却标成 PERSONAL,用户看到的是"可用免费号"但 Codex 跑不动。必须在这里拒收。
    #
    # Round 11 hotfix:本函数单次拒收返回 None,由 manager._run_post_register_oauth 外层 5 次重试承担
    # (W-I9 spec — workspace/select 主路径成功但 callback 拿到 plan!=free 时,需进入外层重试触发
    # 后端最终一致性,而非立即 fail-fast)。日志级别从 ERROR 调整为 WARNING,因为这不是真正的错误
    # 而是预期的 retry 触发器。
    #
    # Round 11 五轮:阶段 1 plan != free 已在 with 块内被 stage2 fallback 拦截,
    # 走到这里说明阶段 1 就是 plan == free(或非 personal 路径,无校验)。
    if use_personal:
        plan = (stage1_bundle.get("plan_type") or "").lower()
        if plan != "free":
            logger.warning(
                "[Codex] personal 模式拿到 plan_type=%s(期望 free),account_id=%s。"
                "本次拒收(返回 None),由外层 5 次重试触发后端最终一致性同步。",
                plan or "unknown",
                stage1_bundle.get("account_id"),
            )
            return None

    if personal_uuid:
        stage1_bundle["personal_workspace_id"] = personal_uuid
    return stage1_bundle


def login_codex_via_session():
    """使用管理员 session 复用统一流程完成主号 Codex OAuth 登录。

    Round 10 重构(2026-04-28):删除 1003-1177 行的遗留 inline 实现,改为 thin wrapper
    委托给 SessionCodexAuthFlow(对齐 upstream cnitlrt/AutoTeam:1017-1043)。
    遗留实现的 chatgpt.com/auth/login 重试 fallback 实测无效(只刷新 chatgpt.com 域,
    不影响 auth.openai.com session),且漏了"落 email-input 页时自动填 admin email"这步,
    导致主号 OAuth 必失败。

    SessionCodexAuthFlow 内 _advance → _auto_fill_email 会在 email-input 页自动填
    admin email 并继续 consent 流程,这是 upstream 已经实证的解法。
    """
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


class SessionCodexAuthFlow:
    EMAIL_SELECTORS = [
        'input[name="email"]',
        'input[id="email-input"]',
        'input[id="email"]',
        'input[type="email"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="邮箱"]',
        'input[autocomplete="email"]',
        'input[autocomplete="username"]',
    ]
    PASSWORD_SELECTORS = [
        'input[name="password"]',
        'input[type="password"]',
    ]
    CODE_SELECTORS = [
        'input[name="code"]',
        'input[placeholder*="验证码"]',
        'input[placeholder*="code" i]',
        'input[inputmode="numeric"]',
        'input[autocomplete="one-time-code"]',
    ]
    OTP_OPTION_SELECTORS = [
        'button:has-text("一次性验证码")',
        'button:has-text("邮箱验证码")',
        'button:has-text("Email login")',
        'button:has-text("email login")',
        'button:has-text("one-time")',
        'button:has-text("One-time")',
        'button:has-text("email code")',
        'button:has-text("Email code")',
        'a:has-text("一次性验证码")',
        'a:has-text("邮箱验证码")',
        'a:has-text("Email login")',
        'a:has-text("one-time")',
    ]

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

    def _visible_locator(self, selectors, timeout_ms=5000):
        if not self.page:
            return None

        selector = ", ".join(selectors)
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            frames = [self.page.main_frame]
            frames.extend(frame for frame in self.page.frames if frame != self.page.main_frame)
            for frame in frames:
                try:
                    locator = frame.locator(selector).first
                    if locator.is_visible(timeout=250):
                        return locator
                except Exception:
                    pass
            time.sleep(0.2)
        return None

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

    def _attach_callback_listeners(self):
        def on_request(request):
            if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in request.url:
                parsed = urllib.parse.urlparse(request.url)
                qs = urllib.parse.parse_qs(parsed.query)
                self.auth_code = qs.get("code", [None])[0]

        def on_response(response):
            if self.auth_code:
                return
            if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" in response.url:
                parsed = urllib.parse.urlparse(response.url)
                qs = urllib.parse.parse_qs(parsed.query)
                self.auth_code = qs.get("code", [None])[0]

        self.page.on("request", on_request)
        self.page.on("response", on_response)

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

    def _click_workspace_or_consent(self):
        acted = False

        try:
            if "workspace" in self.page.url and self.workspace_name:
                ws_btn = self.page.locator(f'text="{self.workspace_name}"').first
                if ws_btn.is_visible(timeout=1000):
                    ws_btn.click()
                    logger.info("[Codex] 主号选择 workspace: %s", self.workspace_name)
                    time.sleep(2)
                    acted = True
        except Exception:
            pass

        try:
            consent_btn = self.page.locator(
                'button:has-text("继续"), button:has-text("Continue"), button:has-text("Allow")'
            ).first
            if consent_btn.is_visible(timeout=1000):
                consent_btn.click()
                logger.info("[Codex] 主号点击继续/授权")
                time.sleep(3)
                acted = True
        except Exception:
            pass

        return acted

    def _auto_fill_email(self):
        email_input = self._visible_locator(self.EMAIL_SELECTORS, timeout_ms=1000)
        if not email_input or not self.email:
            return False

        email_input.fill(self.email)
        time.sleep(0.5)
        _click_primary_auth_button(self.page, email_input, ["Continue", "继续", "Log in"])
        time.sleep(3)
        return True

    def _auto_fill_password(self):
        password_input = self._visible_locator(self.PASSWORD_SELECTORS, timeout_ms=1000)
        if not password_input or not self.password:
            return False

        password_input.fill(self.password)
        time.sleep(0.5)
        _click_primary_auth_button(self.page, password_input, ["Continue", "继续", "Log in"])
        time.sleep(5)
        return True

    def _switch_password_to_otp(self):
        otp_entry = self._visible_locator(self.OTP_OPTION_SELECTORS, timeout_ms=1500)
        if not otp_entry:
            return False

        try:
            otp_entry.click()
        except Exception:
            try:
                otp_entry.click(force=True)
            except Exception:
                return False

        logger.info("[Codex] 主号流程检测到密码页，自动切换到一次性验证码登录")
        time.sleep(3)
        return True

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
            require_browser=True,
        )
        self.page = self.chatgpt.context.new_page()
        self._attach_callback_listeners()
        self._inject_auth_cookies()
        self.page.goto(self.auth_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        return self._advance()

    def submit_password(self, password):
        self.password = password
        if self.password_callback:
            self.password_callback(password)
        password_input = self._visible_locator(self.PASSWORD_SELECTORS, timeout_ms=5000)
        if not password_input:
            raise RuntimeError("当前 Codex 登录不是密码输入步骤")

        password_input.fill(password)
        time.sleep(0.5)
        _click_primary_auth_button(self.page, password_input, ["Continue", "继续", "Log in"])
        time.sleep(5)
        return self._advance()

    def submit_code(self, code):
        code_input = self._visible_locator(self.CODE_SELECTORS, timeout_ms=5000)
        if not code_input:
            raise RuntimeError("当前 Codex 登录不是验证码输入步骤")

        code_input.fill(code)
        time.sleep(0.5)
        _click_primary_auth_button(self.page, code_input, ["Continue", "继续", "Verify"])
        time.sleep(5)
        return self._advance()

    def complete(self):
        if not self.auth_code:
            raise RuntimeError("未获取到 Codex authorization code")

        bundle = _exchange_auth_code(self.auth_code, self.code_verifier, fallback_email=self.email)
        if not bundle:
            raise RuntimeError("Codex token 交换失败")

        filepath = self.auth_file_callback(bundle)
        return {
            "email": bundle.get("email"),
            "auth_file": filepath,
            "plan_type": bundle.get("plan_type"),
            "bundle": bundle,
        }

    def stop(self):
        if self.chatgpt:
            self.chatgpt.stop()
        self.chatgpt = None
        self.page = None


class MainCodexSyncFlow(SessionCodexAuthFlow):
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

        from autoteam.cpa_sync import sync_main_codex_to_cpa

        sync_main_codex_to_cpa(info["auth_file"])
        return {
            "email": info.get("email"),
            "auth_file": info.get("auth_file"),
            "plan_type": info.get("plan_type"),
        }


def login_main_codex():
    """主号 Codex 登录：使用已保存的管理员 session。"""
    return login_codex_via_session()


def save_auth_file(bundle):
    """保存 CPA 兼容的认证文件。同一邮箱只保留一个文件，优先 team。"""
    ensure_auth_dir()

    email = bundle["email"]
    plan_type = bundle.get("plan_type", "unknown")
    account_id = bundle.get("account_id", "")
    hash_id = hashlib.md5(account_id.encode()).hexdigest()[:8]

    # 清理同一邮箱的旧文件（避免 free/team 并存）
    for old in AUTH_DIR.glob(f"codex-{email}-*.json"):
        old.unlink()
        logger.info("[Codex] 清理旧文件: %s", old.name)

    filename = f"codex-{email}-{plan_type}-{hash_id}.json"
    filepath = AUTH_DIR / filename
    return _write_auth_file(filepath, bundle)


def save_main_auth_file(bundle):
    """保存主号 Codex 认证文件，不进入账号池。"""
    account_id = bundle.get("account_id") or hashlib.md5(bundle.get("email", "main").encode()).hexdigest()[:8]

    for old in AUTH_DIR.glob("codex-main-*.json"):
        old.unlink()
        logger.info("[Codex] 清理旧主号文件: %s", old.name)

    filepath = AUTH_DIR / f"codex-main-{account_id}.json"
    return _write_auth_file(filepath, bundle)


def get_saved_main_auth_file():
    """获取本地已保存的主号 Codex 认证文件路径。"""
    candidates = []
    for path in AUTH_DIR.glob("codex-main-*.json"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except Exception:
            continue
        candidates.append((stat.st_mtime, path.name, path))

    if not candidates:
        return ""

    candidates.sort(reverse=True)
    return str(candidates[0][2].resolve())


def refresh_main_auth_file():
    """基于已保存的管理员登录态，刷新并保存主号 Codex 认证文件。"""
    bundle = login_codex_via_session()
    if not bundle:
        raise RuntimeError("无法基于管理员登录态生成主号 Codex 认证文件")

    auth_file = save_main_auth_file(bundle)
    return {
        "email": bundle.get("email"),
        "auth_file": auth_file,
        "plan_type": bundle.get("plan_type"),
    }


def quota_result_quota_info(info):
    """从 check_codex_quota 返回值中提取额度快照。"""
    if not isinstance(info, dict):
        return None
    quota_info = info.get("quota_info")
    if isinstance(quota_info, dict):
        return quota_info
    if "primary_pct" in info or "weekly_pct" in info:
        return info
    return None


def quota_result_resets_at(info):
    """从 check_codex_quota 返回值中提取恢复时间。"""
    if isinstance(info, dict):
        value = info.get("resets_at")
    else:
        value = info

    try:
        return int(value or 0)
    except Exception:
        return 0


def get_quota_exhausted_info(quota_info, *, limit_reached=False):
    """根据额度快照判断是否已耗尽，并返回耗尽详情。

    SPEC-2 shared/quota-classification §4.2:no_quota 优先级**高于** exhausted。
    `primary_total == 0` / `reset_at == 0 + used_pct == 0` 表示 workspace 未分配配额,
    返回 window="no_quota" 形态,resets_at 给 24h 占位(不应被用作重试依据)。
    """
    if not isinstance(quota_info, dict):
        return None

    primary_pct = int(quota_info.get("primary_pct", 0) or 0)
    weekly_pct = int(quota_info.get("weekly_pct", 0) or 0)
    primary_reset = int(quota_info.get("primary_resets_at", 0) or 0)
    weekly_reset = int(quota_info.get("weekly_resets_at", 0) or 0)
    primary_total = quota_info.get("primary_total")
    primary_remaining = quota_info.get("primary_remaining")

    # SPEC-2 shared/quota-classification I4 — no_quota 短路必须先于 exhausted 判定
    no_quota_signals = []
    if primary_total == 0:
        no_quota_signals.append("primary_total==0")
    elif primary_total is None and primary_pct == 0 and primary_reset == 0 and not limit_reached:
        # rate_limit 字段缺失 / primary_window 缺失走这一支(上游 quota_info 取值都是 0)
        no_quota_signals.append("rate_limit_empty")
    if primary_remaining == 0 and (primary_total == 0 or primary_total is None) and primary_pct == 0:
        no_quota_signals.append("remaining==0+total==0")

    if no_quota_signals and not limit_reached and primary_pct < 100 and weekly_pct < 100:
        return {
            "window": "no_quota",
            "resets_at": int(time.time() + 86400),  # 24h 占位
            "quota_info": quota_info,
            "limit_reached": False,
            "no_quota_signals": no_quota_signals,
        }

    # SPEC-2 shared/quota-classification §4.4 I5 (Round 6 PRD-5 FR-P0) — uninitialized_seat 形态。
    # OpenAI fresh seat 懒初始化:wham 给了占位 reset_at>0 但 total/remaining=null,且 pct=0。
    # 此形态在 wham 层无法与"真无配额"区分,**不**直接判 no_quota,而是返回 window=
    # "uninitialized_seat" + needs_codex_smoke=True,要求上游调 cheap_codex_smoke 二次验证。
    if (
        primary_total is None
        and primary_remaining is None
        and primary_pct == 0
        and weekly_pct == 0
        and primary_reset > 0
        and not limit_reached
    ):
        return {
            "window": "uninitialized_seat",
            "resets_at": int(time.time() + 86400),
            "quota_info": quota_info,
            "limit_reached": False,
            "needs_codex_smoke": True,
            "no_quota_signals": ["workspace_uninitialized"],
        }

    primary_exhausted = primary_pct >= 100
    weekly_exhausted = weekly_pct >= 100
    if not (limit_reached or primary_exhausted or weekly_exhausted):
        return None

    reset_candidates = []
    if primary_exhausted and primary_reset:
        reset_candidates.append(primary_reset)
    if weekly_exhausted and weekly_reset:
        reset_candidates.append(weekly_reset)

    if not reset_candidates:
        if primary_reset:
            reset_candidates.append(primary_reset)
        if weekly_reset:
            reset_candidates.append(weekly_reset)

    resets_at = max(reset_candidates) if reset_candidates else int(time.time() + 18000)

    if primary_exhausted and weekly_exhausted:
        window = "combined"
    elif weekly_exhausted:
        window = "weekly"
    elif primary_exhausted:
        window = "primary"
    else:
        window = "limit"

    return {
        "window": window,
        "resets_at": resets_at,
        "quota_info": quota_info,
        "limit_reached": bool(limit_reached),
    }


# Round 7 FR-D6 — manager 24h 去重 cheap_codex_smoke。
# 同一 account_id 在 24h 内重复调用 cheap_codex_smoke 时,直接返回上次落盘的结果,
# 不再走网络。R2 风险缓解(fresh seat 命中率 > 5% 时 smoke 调用密度爆炸)。
_CODEX_SMOKE_DEDUP_SECONDS = 86400


def _read_codex_smoke_cache(account_id):
    """从 accounts.json 读 last_codex_smoke_at + last_smoke_result。

    匹配规则:account_id 优先按 workspace_account_id 比对,其次 email 比对。
    返回 (epoch_at, result_str) 或 None(无 account_id / 无记录 / 字段缺失)。
    """
    if not account_id:
        return None
    try:
        from autoteam.accounts import load_accounts

        accounts = load_accounts()
    except Exception:
        return None
    target = str(account_id)
    for acc in accounts:
        if acc.get("workspace_account_id") == target or acc.get("email") == target:
            ts = acc.get("last_codex_smoke_at")
            res = acc.get("last_smoke_result")
            if ts and res:
                try:
                    return (float(ts), str(res))
                except (TypeError, ValueError):
                    return None
    return None


def _write_codex_smoke_cache(account_id, result):
    """落盘 last_codex_smoke_at + last_smoke_result,用于 24h 去重。

    匹配规则同 _read_codex_smoke_cache。result 取值 alive/auth_invalid/uncertain。
    任何异常静默吞,cache 失败不阻塞主流程。
    """
    if not account_id or not result:
        return
    try:
        from autoteam.accounts import load_accounts, update_account
    except Exception:
        return
    target = str(account_id)
    try:
        accounts = load_accounts()
    except Exception:
        return
    for acc in accounts:
        if acc.get("workspace_account_id") == target or acc.get("email") == target:
            try:
                update_account(
                    acc["email"],
                    last_codex_smoke_at=time.time(),
                    last_smoke_result=str(result),
                )
            except Exception as exc:
                logger.debug("[Codex smoke] cache 落盘失败(忽略): %s", exc)
            return


def cheap_codex_smoke(
    access_token,
    account_id=None,
    *,
    model="gpt-5.5",
    fallback_models=None,
    instructions=None,
    max_output_tokens=64,
    timeout=15.0,
    force=False,
):
    """SPEC-2 shared/quota-classification §4.4 — uninitialized_seat 二次验证。
    Round 11 升级(v2 — codex backend payload schema 改版):
      - 默认 model 升 gpt-5.5(team-only,主路径)
      - fallback_models 默认 ["gpt-5.4"](通用模型,team + free 都能用),主 model 撞 model_not_supported 时自动 fallback
      - payload 严格按最新 codex backend schema:
          * instructions 必填(默认 "You are a concise assistant. Reply with one short word.")
          * input 改为 list 格式 [{type:message, role:user, content:[{type:input_text, text:"ping"}]}]
          * 新增 store: false(必填)
          * 删除 max_output_tokens(后端不再支持,函数签名保留供未来扩展)

    PRD-Round11 Q4:gpt-5.5 是 team-only 模型(只 team 号能用),gpt-5.4 是通用模型
    (free + team 都能用)。所以 team 号默认走 gpt-5.5,model_not_supported 时
    fallback 到 gpt-5.4(free 号在主 model 立即 fallback)。

    Round 7 FR-D6:24h 去重 cache。account_id 在 24h 内已有 cache 时直接返回,不走网络;
    传 force=True 可绕过 cache(用于强制刷新场景)。

    返回 (result, detail):
        ("alive", {model, response_text, raw_event, tokens?})  — HTTP 200 + response.completed → 真活号
                                                                detail.model 反映最终成功用的 model(可能是 fallback)
        ("alive", None) (cache hit only)                       — 24h cache 命中
        ("auth_invalid", reason_str)  — HTTP 401/403/429 / 4xx 含 quota 关键词 → token/seat 真失效
        ("uncertain", reason_str)     — HTTP 5xx / network / timeout / 解析异常 / 主+fallback 全 model_not_supported
        cache 命中时 detail 为 "cache_hit_<原 result>"

    向后兼容:max_output_tokens 参数保留但**不再传给后端**(spike 实测后端拒收);
    instructions=None 时用模块级默认。
    """
    if not access_token:
        return "auth_invalid", "empty_access_token"

    if not account_id:
        try:
            account_id = get_chatgpt_account_id()
        except Exception:
            account_id = None

    # Round 7 FR-D6 — 24h 去重 cache 命中直接返回(force=True 时绕过)
    if not force and account_id:
        cached = _read_codex_smoke_cache(account_id)
        if cached:
            cached_at, cached_result = cached
            if (time.time() - cached_at) < _CODEX_SMOKE_DEDUP_SECONDS:
                logger.debug(
                    "[Codex smoke] 24h cache 命中 account_id=%s result=%s age=%.0fs",
                    account_id,
                    cached_result,
                    time.time() - cached_at,
                )
                return cached_result, f"cache_hit_{cached_result}"

    # 构造 model 尝试链:主 model + fallback chain(去重保序)
    if fallback_models is None:
        fallback_models = ["gpt-5.4"]
    elif not isinstance(fallback_models, (list, tuple)):
        fallback_models = [fallback_models]
    model_chain: list[str] = []
    seen: set[str] = set()
    for m in [model] + list(fallback_models):
        if not m:
            continue
        if m in seen:
            continue
        seen.add(m)
        model_chain.append(m)

    last_result: tuple[str, object] = ("uncertain", "no_models_attempted")
    for idx, candidate in enumerate(model_chain):
        result, detail = _cheap_codex_smoke_network(
            access_token,
            account_id,
            model=candidate,
            instructions=instructions,
            max_output_tokens=max_output_tokens,
            timeout=timeout,
        )
        # alive / auth_invalid → 直接返回(终态)
        if result == "alive" or result == "auth_invalid":
            _write_codex_smoke_cache(account_id, result)
            return result, detail
        # uncertain 路径下:仅当 detail 是 model_not_supported 才继续尝试 fallback
        last_result = (result, detail)
        is_model_not_supported = (
            isinstance(detail, str) and detail.startswith("model_not_supported")
        )
        if not is_model_not_supported:
            break  # 非 model 问题(网络 / 5xx / 解析)— 不重试
        # else:模型不被支持,继续下一个 fallback
        if idx + 1 < len(model_chain):
            logger.info(
                "[Codex smoke] model=%s 不被支持,fallback → %s",
                candidate,
                model_chain[idx + 1],
            )

    result, detail = last_result
    _write_codex_smoke_cache(account_id, result)
    return result, detail


def _cheap_codex_smoke_network(
    access_token,
    account_id,
    *,
    model="gpt-5.5",
    instructions=None,
    max_output_tokens=64,  # 保留参数供未来扩展;后端拒收此字段,不再写入 payload
    timeout=15.0,
):
    """实际走网络的 cheap_codex_smoke 内部函数(Round 7 FR-D6 拆出 + Round 11 v2 schema 升级)。

    与 cheap_codex_smoke 不同点:不查也不写 cache,直接调 codex backend。
    Round 11:读完整 SSE 帧累积 output_text,见 response.completed 时返回 dict 含 response_text。
    Round 11 v2:payload schema 严格按 codex backend 最新版(spike 实测验证):
      - instructions 必填(默认 _CODEX_SMOKE_DEFAULT_INSTRUCTIONS)
      - input 是 list 格式 [{type:message, role:user, content:[{type:input_text, text}]}]
      - 必含 store: false
      - **不再**写 max_output_tokens(后端拒收"Unsupported parameter")

    返回值语义:
      alive 路径 detail = dict {"model", "response_text", "raw_event", "tokens"}
      auth_invalid 路径 detail = str(http_<code>[_quota_hint])
      uncertain 路径 detail = str
        - "model_not_supported_<code>":4xx 且 body 含 not supported 关键字 → 上层触发 fallback
        - "http_<code>" / "no_response_created_frame" / "stream:..." / "network:..." 等
    """
    import requests

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id

    payload = {
        "model": model,
        "instructions": instructions if instructions else _CODEX_SMOKE_DEFAULT_INSTRUCTIONS,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "ping"}],
            }
        ],
        "stream": True,
        "store": False,
        "reasoning": {"effort": "none"},
    }

    try:
        resp = requests.post(
            _CODEX_SMOKE_ENDPOINT,
            headers=headers,
            json=payload,
            stream=True,
            timeout=timeout,
        )
    except (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.SSLError,
    ) as exc:
        logger.warning("[Codex smoke] 网络异常(uncertain): %s", exc)
        return "uncertain", f"network:{type(exc).__name__}"
    except Exception as exc:
        logger.warning("[Codex smoke] 未知异常(uncertain): %s", exc)
        return "uncertain", f"exception:{type(exc).__name__}"

    try:
        status_code = resp.status_code
        if status_code in (401, 403, 429):
            try:
                resp.close()
            except Exception:
                pass
            return "auth_invalid", f"http_{status_code}"

        if 500 <= status_code < 600:
            try:
                resp.close()
            except Exception:
                pass
            return "uncertain", f"http_{status_code}"

        if status_code != 200:
            # 4xx 非 401/403/429:
            #   1) body 含 model_not_supported 关键词 → uncertain + sentinel,触发上层 fallback
            #   2) body 含 quota 关键词 → auth_invalid
            #   3) 其他 → uncertain + http_<code>
            try:
                body_preview = (resp.text or "").lower()[:1500]
            except Exception:
                body_preview = ""
            try:
                resp.close()
            except Exception:
                pass
            if any(hint in body_preview for hint in _CODEX_SMOKE_MODEL_NOT_SUPPORTED_HINTS):
                return "uncertain", f"model_not_supported_{status_code}"
            if any(hint in body_preview for hint in _CODEX_SMOKE_QUOTA_HINTS):
                return "auth_invalid", f"http_{status_code}_quota_hint"
            return "uncertain", f"http_{status_code}"

        # HTTP 200 — 读完整 SSE 帧累积 output_text(Round 11)
        response_text_parts = []
        seen_created = False
        completed_event = False
        output_tokens = None
        frames_read = 0
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                frames_read += 1
                # SSE 行通常为 "data: {...}" 或 "event: response.created" 等
                # 先剥离 "data: " 前缀,失败按原文匹配
                payload_text = line[6:].strip() if line.startswith("data: ") else line.strip()
                if "response.created" in line:
                    seen_created = True
                if "response.completed" in line:
                    # 见 completed 帧结束读流
                    completed_event = True
                    # 尝试解析 token 数(usage.output_tokens)
                    try:
                        ev_obj = json.loads(payload_text)
                        usage = (ev_obj.get("response") or {}).get("usage") or {}
                        output_tokens = usage.get("output_tokens")
                    except Exception:
                        pass
                    break
                # 累积 output_text.delta 帧文本
                if "response.output_text.delta" in line or "output_text.delta" in line:
                    try:
                        ev_obj = json.loads(payload_text)
                        delta_str = ev_obj.get("delta")
                        if isinstance(delta_str, str) and delta_str:
                            response_text_parts.append(delta_str)
                    except Exception:
                        pass
                # 安全兜底:30 帧仍未见 completed,跳出当作 alive(已 reach model)
                if frames_read >= 30:
                    break
        except Exception as exc:
            logger.debug("[Codex smoke] iter_lines 异常(uncertain): %s", exc)
            return "uncertain", f"stream:{type(exc).__name__}"
        finally:
            try:
                resp.close()
            except Exception:
                pass

        # 8 行内仍没拿到 response.created 视为 uncertain
        if not seen_created and frames_read < 8:
            return "uncertain", "no_response_created_frame"
        if not seen_created:
            return "uncertain", "no_response_created_frame"

        response_text = "".join(response_text_parts)
        raw_event = "response.completed" if completed_event else "no_completed_within_30_frames"
        detail = {
            "model": model,
            "response_text": response_text,
            "raw_event": raw_event,
        }
        if output_tokens is not None:
            detail["tokens"] = output_tokens
        return "alive", detail
    finally:
        try:
            resp.close()
        except Exception:
            pass


def check_codex_quota(access_token, account_id=None):
    """
    通过 /backend-api/wham/usage 查询 Codex 额度状态，不消耗额度。
    返回:
        ("ok", quota_info)         — HTTP 200 + 成功解析,额度未触发上限
        ("exhausted", info)        — HTTP 200 + quota 用尽(get_quota_exhausted_info 命中)
        ("auth_error", None)       — **仅** HTTP 401/403,token/seat 真失效
        ("network_error", None)    — DNS/timeout/SSL/连接异常 / 5xx / 429 / json 解析失败 / 其他临时错误

    auth_error 与 network_error 必须严格区分:auth_error 会触发"标记 AUTH_INVALID/重登"等
    破坏性流程,网络抖动绝不能落入该分支(否则一次网络故障可能批量误删账号)。
    quota_info = {"primary_pct": int, "primary_resets_at": int, "weekly_pct": int, "weekly_resets_at": int}
    """
    import requests

    if not account_id:
        account_id = get_chatgpt_account_id()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id

    try:
        resp = requests.get(
            "https://chatgpt.com/backend-api/wham/usage",
            headers=headers,
            timeout=30,
        )
    except (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.SSLError,
    ) as e:
        logger.warning("[Codex] 网络异常(归类 network_error): %s", e)
        return "network_error", None
    except requests.exceptions.RequestException as e:
        # 其他 requests 异常(ChunkedEncodingError 等)同样归为 network_error,而不是 auth_error
        logger.warning("[Codex] requests 异常(归类 network_error): %s", e)
        return "network_error", None
    except Exception as e:
        # 兜底:未知异常宁可归 network_error,避免因为一次网络抖动批量误标 AUTH_INVALID
        logger.warning("[Codex] 未知异常(归类 network_error,保守处理): %s", e)
        return "network_error", None

    if resp.status_code in (401, 403):
        return "auth_error", None

    # 429 限流 / 5xx 服务端错误 → 临时性故障,归为 network_error,不动账号 status
    if resp.status_code == 429 or 500 <= resp.status_code < 600:
        logger.warning("[Codex] wham/usage 临时错误 %d(归类 network_error): %s", resp.status_code, resp.text[:200])
        return "network_error", None

    if resp.status_code != 200:
        # 4xx(非 401/403/429) 也归为 network_error:可能是 OpenAI 接口在调整,
        # 不能因为一次接口变更把全部账号误判 token 失效
        logger.warning("[Codex] wham/usage 非预期状态 %d(归类 network_error): %s", resp.status_code, resp.text[:200])
        return "network_error", None

    try:
        data = resp.json()
    except Exception as e:
        logger.warning("[Codex] wham/usage 响应 JSON 解析失败(归类 network_error): %s", e)
        return "network_error", None

    rate_limit = data.get("rate_limit") or {}
    primary = rate_limit.get("primary_window") or {}
    secondary = rate_limit.get("secondary_window") or {}

    # SPEC-2 shared/quota-classification §2.2 — 扩 primary_total / primary_remaining,
    # 让 get_quota_exhausted_info 能区分 no_quota(workspace 未分配)与 exhausted(已用完)
    primary_total_raw = primary.get("limit", primary.get("total"))
    primary_remaining_raw = primary.get("remaining")
    quota_info = {
        "primary_pct": primary.get("used_percent", 0),
        "primary_resets_at": primary.get("reset_at", 0),
        "primary_total": primary_total_raw if isinstance(primary_total_raw, (int, float)) else None,
        "primary_remaining": primary_remaining_raw if isinstance(primary_remaining_raw, (int, float)) else None,
        "weekly_pct": secondary.get("used_percent", 0),
        "weekly_resets_at": secondary.get("reset_at", 0),
        # Round 7 P2.5:把 wham/usage 的原始 rate_limit + primary_window 子树注入 quota_info,
        # 让 manager 在 record_failure(no_quota_assigned) 时能附 raw_rate_limit 用于事后排查。
        "raw_rate_limit": rate_limit,
        "primary_window": primary,
    }

    # SPEC-2 shared/quota-classification §4.2 — no_quota 单独分支:rate_limit 字段
    # 完全缺失 / primary_window 缺失也归 no_quota(空载也是无配额信号)
    rate_limit_missing = (not rate_limit) or (not primary)
    if rate_limit_missing:
        return "no_quota", {
            "window": "no_quota",
            "resets_at": int(time.time() + 86400),
            "quota_info": quota_info,
            "limit_reached": False,
            "no_quota_signals": ["rate_limit_or_primary_missing"],
            "raw_rate_limit": rate_limit,
        }

    exhausted_info = get_quota_exhausted_info(quota_info, limit_reached=bool(rate_limit.get("limit_reached")))
    if exhausted_info:
        # window="no_quota" 是 get_quota_exhausted_info 内部短路出的形态;独立分类返回
        if exhausted_info.get("window") == "no_quota":
            return "no_quota", exhausted_info
        # window="uninitialized_seat"(I5)— Round 6 PRD-5 FR-P0:必须用 cheap_codex_smoke 二次验证
        if exhausted_info.get("window") == "uninitialized_seat":
            smoke_result, smoke_detail = cheap_codex_smoke(access_token, account_id=account_id)
            exhausted_info["last_smoke_result"] = smoke_result
            if smoke_detail is not None:
                exhausted_info["last_smoke_detail"] = smoke_detail
            if smoke_result == "alive":
                # fresh seat 真活,token 有效 → 维持 ok,但带 smoke_verified 标记
                quota_info_verified = dict(quota_info)
                quota_info_verified["smoke_verified"] = True
                quota_info_verified["last_smoke_result"] = "alive"
                return "ok", quota_info_verified
            if smoke_result == "auth_invalid":
                # codex backend 401/403/quota → 当作真 auth_error,触发 reconcile 重登
                return "auth_error", None
            # uncertain (5xx/network/timeout) → 保持原状态等下轮,归 network_error
            return "network_error", None
        return "exhausted", exhausted_info

    return "ok", quota_info


def refresh_access_token(refresh_token):
    """刷新 access token"""
    import requests

    resp = requests.post(
        CODEX_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CODEX_CLIENT_ID,
            "refresh_token": refresh_token,
            "scope": "openid profile email",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if resp.status_code != 200:
        logger.error("[Codex] Token 刷新失败: %d", resp.status_code)
        return None

    data = resp.json()
    return {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token", refresh_token),
        "id_token": data.get("id_token", ""),
        "expires_in": data.get("expires_in", 3600),
    }
