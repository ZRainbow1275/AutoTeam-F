"""OAuth Personal Workspace 显式选择 — sticky-default 修复。

详见 SPEC `prompts/0426/spec/shared/oauth-workspace-selection.md` v1.0(2026-04-27 Round 8)。

根因:`auth.openai.com` 的 `default_workspace_id` 不随 ChatGPT DELETE user 联动,
       OAuth flow 没有显式 workspace/select 时 issuer 按 default 颁 token,personal OAuth
       拿到 plan_type=team。本模块提供:

  - decode_oauth_session_cookie    从 oai-oauth-session cookie 解出 workspaces[]
  - select_oauth_workspace         主路径 — POST /api/accounts/workspace/select
  - force_select_personal_via_ui   兜底 — Playwright 主动 goto + 点 "Personal" 按钮
  - ensure_personal_workspace_selected  顶层编排,5 次重试由外层 manager 承担

不变量:任何函数都**不抛异常**,失败转 (False, fail_category, evidence) 三元组返回。
        evidence 中**禁止**写入 access_token / refresh_token / cookie 原始值等敏感数据。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

from autoteam.register_failures import (
    OAUTH_PLAN_DRIFT_PERSISTENT,  # noqa: F401  — 仅为 W-I8 字面量校验导出
    OAUTH_WS_ENDPOINT_ERROR,
    OAUTH_WS_NO_PERSONAL,
)

logger = logging.getLogger(__name__)


# spec §2.1 — workspaces[] 项 personal 识别(三条件 OR;实施期以抓包为准)
_PERSONAL_STRUCTURE_VALUES = ("personal", "personal_v2", "personal_account")


def _is_personal_workspace(item: dict) -> bool:
    """personal 识别 — 三条件任一命中即视为 personal(spec W-I4)。"""
    if not isinstance(item, dict):
        return False
    structure = str(item.get("structure") or "").lower()
    if structure in _PERSONAL_STRUCTURE_VALUES:
        return True
    if str(item.get("plan_type") or "").lower() == "free":
        return True
    if item.get("is_personal") is True:
        return True
    return False


def _safe_b64url_decode(segment: str) -> bytes | None:
    if not segment:
        return None
    padding = 4 - (len(segment) % 4)
    if padding < 4:
        segment = segment + "=" * padding
    try:
        return base64.urlsafe_b64decode(segment)
    except Exception:
        try:
            return base64.b64decode(segment)
        except Exception:
            return None


def _redact_workspaces(workspaces: Any) -> list[dict]:
    """裁剪 workspaces[] 用于 evidence 落盘 — 只保留 id/name/structure/role/plan_type 子集(spec W-I6)。"""
    if not isinstance(workspaces, list):
        return []
    out = []
    for w in workspaces:
        if not isinstance(w, dict):
            continue
        out.append({
            "id": w.get("id"),
            "name": w.get("name") or w.get("workspace_name"),
            "structure": w.get("structure"),
            "role": w.get("role") or w.get("current_user_role"),
            "plan_type": w.get("plan_type"),
        })
    return out


def decode_oauth_session_cookie(page_or_context) -> dict | None:
    """从 Playwright page / browser_context 读 oai-oauth-session cookie 并解码。

    spec §2.2.1。返回 dict(含 workspaces[])或 None;不抛异常。

    解码策略(以抓包为准,本实施按 research/sticky-rejoin §3.1 推断):
      1. cookies = context.cookies("https://auth.openai.com")
      2. find cookie name == "oai-oauth-session"(也接受 "oai-client-auth-session"
         作为 alt — gpt-auto-register 上游用此名)
      3. 值若含 "." → JWT 三段,取首段;否则整串 base64url decode
      4. JSON parse,失败回 None
    """
    try:
        if hasattr(page_or_context, "context"):
            context = page_or_context.context
        else:
            context = page_or_context
        try:
            cookies = context.cookies("https://auth.openai.com")
        except Exception:
            cookies = context.cookies()
    except Exception:
        return None

    target = None
    for c in cookies or []:
        name = (c.get("name") or "").lower()
        if name in ("oai-oauth-session", "oai-client-auth-session"):
            target = c
            break

    if not target:
        return None

    raw = target.get("value") or ""
    if not raw:
        return None

    candidates = []
    if "." in raw:
        candidates.append(raw.split(".", 1)[0])
    candidates.append(raw)

    for seg in candidates:
        decoded = _safe_b64url_decode(seg)
        if not decoded:
            continue
        try:
            data = json.loads(decoded)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def select_oauth_workspace(
    page,
    workspace_id: str,
    *,
    consent_url: str,
    timeout: float = 15.0,
) -> tuple[bool, str | None, dict]:
    """POST https://auth.openai.com/api/accounts/workspace/select.

    spec §2.2.2。Returns (success, redirect_url_or_continue_url, evidence)。

    实施(以抓包为准):
      - 用 page.evaluate(fetch, credentials='include') 让 cookie 自动带
      - 不主动注入 sentinel-token,依赖 Playwright context 已有 session 的同源 cookie
      - 失败时 evidence 含 http_status / body_preview(各 200 字以内)
    """
    if not workspace_id:
        return False, None, {"detail": "empty_workspace_id"}

    js = """async ([wid, referer, timeoutMs]) => {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), timeoutMs);
        try {
            const resp = await fetch('https://auth.openai.com/api/accounts/workspace/select', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'Referer': referer,
                    'Accept': 'application/json',
                },
                body: JSON.stringify({ workspace_id: wid }),
                signal: ctrl.signal,
                redirect: 'manual',
            });
            const text = await resp.text().catch(() => '');
            return {
                ok: resp.ok,
                status: resp.status,
                location: resp.headers.get('Location') || resp.headers.get('location') || '',
                body: (text || '').slice(0, 500),
            };
        } catch (e) {
            return { ok: false, status: 0, error: String(e), body: '' };
        } finally {
            clearTimeout(t);
        }
    }"""

    try:
        result = page.evaluate(js, [workspace_id, consent_url, int(timeout * 1000)])
    except Exception as exc:
        return False, None, {"exception": type(exc).__name__, "detail": str(exc)[:200]}

    if not isinstance(result, dict):
        return False, None, {"detail": "invalid_evaluate_result"}

    status = int(result.get("status") or 0)
    body = (result.get("body") or "")[:200]
    location = result.get("location") or ""
    evidence = {
        "http_status": status,
        "body_preview": body,
        "location": location,
    }

    # 200/201/204 + body 含 continue_url
    if 200 <= status < 300:
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        continue_url = data.get("continue_url") if isinstance(data, dict) else None
        if continue_url:
            return True, continue_url, evidence
        # 没拿到 continue_url 但状态成功 — 视作 ok,后续 auth flow 走 callback
        return True, None, evidence

    # 302/303/307/308 — Location 头含 callback URL 或 ?code=
    if status in (302, 303, 307, 308) and location:
        return True, location, evidence

    if status in (401, 403):
        evidence["detail"] = "auth_or_sentinel_required"
    elif status >= 500:
        evidence["detail"] = "server_error"
    elif status == 0:
        evidence["detail"] = result.get("error") or "fetch_exception"

    return False, None, evidence


# spec §2.2.3 — UI fallback 候选 selector(从 cnitlrt PR#39 + 现有 codex_auth.py:643-671)
_PERSONAL_BTN_SELECTORS = (
    'text=/Personal account/i',
    'text=/Personal workspace/i',
    'text=/^Personal$/i',
    'text=/^个人$/',
    'text=/个人账户/',
    'text=/个人帐户/',
    'button:has-text("Personal")',
    'button:has-text("个人")',
    '[role="button"]:has-text("Personal")',
)

_WORKSPACE_PAGE_HINT_TEXTS = (
    "选择一个工作空间",
    "选择工作空间",
    "select a workspace",
    "choose a workspace",
    "launch a workspace",
    "personal workspace",
    "your workspaces",
)


def _is_workspace_selection_page(page) -> bool:
    try:
        url = (page.url or "").lower()
        if "/workspace" in url or "workspace/select" in url:
            return True
        if "consent" in url:
            return True
        # body 文案多语言匹配
        text = ""
        try:
            text = page.inner_text("body", timeout=1500)
        except Exception:
            try:
                text = page.content()[:3000]
            except Exception:
                text = ""
        text_low = (text or "").lower()
        for hint in _WORKSPACE_PAGE_HINT_TEXTS:
            if hint in text_low or hint in text:
                return True
    except Exception:
        return False
    return False


def force_select_personal_via_ui(
    page,
    *,
    timeout_per_step: float = 8.0,
) -> tuple[bool, dict]:
    """fallback — 主动 goto auth.openai.com/workspace,DOM 找 Personal 按钮点击。

    spec §2.2.3。Returns (success, evidence)。
    """
    evidence: dict = {"phase": "navigate", "ts_ms": int(time.time() * 1000)}
    try:
        page.goto(
            "https://auth.openai.com/workspace",
            wait_until="domcontentloaded",
            timeout=int(timeout_per_step * 1000),
        )
    except Exception as exc:
        evidence["exception"] = type(exc).__name__
        evidence["detail"] = "goto_failed"
        return False, evidence

    # 等 DOM 渲染
    time.sleep(min(timeout_per_step / 4, 3))
    evidence["url_after_goto"] = (page.url or "")[:200]

    if not _is_workspace_selection_page(page):
        evidence["phase"] = "not_selection_page"
        try:
            evidence["page_title"] = (page.title() or "")[:200]
        except Exception:
            evidence["page_title"] = ""
        return False, evidence

    # 尝试每个候选 selector
    for sel in _PERSONAL_BTN_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=int(timeout_per_step * 1000 / len(_PERSONAL_BTN_SELECTORS))):
                try:
                    text_label = loc.inner_text(timeout=1500)
                except Exception:
                    text_label = sel
                loc.click(force=True, timeout=int(timeout_per_step * 1000))
                time.sleep(1)
                # 试着点 "继续 / Continue"(若有确认页)
                try:
                    cont = page.locator('button:has-text("继续"), button:has-text("Continue")').first
                    if cont.is_visible(timeout=2000):
                        cont.click(force=True, timeout=3000)
                        time.sleep(1)
                except Exception:
                    pass
                evidence.update({
                    "phase": "clicked",
                    "selector": sel,
                    "clicked_text": (text_label or "")[:80],
                })
                return True, evidence
        except Exception:
            continue

    evidence["phase"] = "no_personal_button"
    return False, evidence


def ensure_personal_workspace_selected(
    page,
    *,
    consent_url: str,
    max_retries: int = 5,
) -> tuple[bool, str, dict]:
    """Personal OAuth 主流程 — 三层兜底(W-I1 / W-I2)。

    spec §2.2.4。Returns (success, fail_category, evidence)。

    success=True ⇒ fail_category=""(空串),OAuth 流程可继续走 callback。
    success=False ⇒ fail_category ∈ §2.3 三个枚举之一。

    流程:
      1. decode_oauth_session_cookie → 拿 workspaces[]
      2. 找 personal item → 没找到 → return OAUTH_WS_NO_PERSONAL
      3. select_oauth_workspace 主路径 → 200/302 → success=True
      4. 主路径失败 → force_select_personal_via_ui → 点 Personal → success=True
      5. 都失败 → return OAUTH_WS_ENDPOINT_ERROR

    备注:5 次 OAuth retry 由外层 _run_post_register_oauth 承担,本函数单次调用即返回结论。
          OAUTH_PLAN_DRIFT_PERSISTENT 由外层在 5 次后写入,本函数从不返回。
    """
    evidence: dict = {"max_retries_hint": max_retries}

    # 1. 解 cookie
    try:
        session = decode_oauth_session_cookie(page)
    except Exception as exc:
        session = None
        evidence["decode_exception"] = type(exc).__name__

    if not session:
        # 解码失败 — 直接走 fallback,不假设 cookie 一定存在
        logger.warning("[oauth_ws] 无法解码 oai-oauth-session cookie,走 UI fallback")
        try:
            ok, fb_ev = force_select_personal_via_ui(page)
        except Exception as exc:
            ok, fb_ev = False, {"exception": type(exc).__name__}
        evidence["primary"] = {"phase": "skipped_decode_failed"}
        evidence["fallback"] = fb_ev
        if ok:
            return True, "", evidence
        return False, OAUTH_WS_ENDPOINT_ERROR, evidence

    workspaces = session.get("workspaces") if isinstance(session, dict) else None
    evidence["workspaces_redacted"] = _redact_workspaces(workspaces)

    if not isinstance(workspaces, list) or not workspaces:
        # session 解出来了但 workspaces 空 — fallback
        logger.warning("[oauth_ws] oai-oauth-session 不含 workspaces[],走 UI fallback")
        try:
            ok, fb_ev = force_select_personal_via_ui(page)
        except Exception as exc:
            ok, fb_ev = False, {"exception": type(exc).__name__}
        evidence["primary"] = {"phase": "skipped_empty_workspaces"}
        evidence["fallback"] = fb_ev
        if ok:
            return True, "", evidence
        return False, OAUTH_WS_ENDPOINT_ERROR, evidence

    # 2. 找 personal
    personal = next((w for w in workspaces if _is_personal_workspace(w)), None)
    if not personal:
        # spec W-I4 + W-I2 — 不重试
        logger.warning("[oauth_ws] workspaces[] 中找不到 personal 项,fail-fast")
        return False, OAUTH_WS_NO_PERSONAL, evidence

    pid = personal.get("id")
    if not pid:
        logger.warning("[oauth_ws] personal workspace 缺 id 字段,走 UI fallback")
        try:
            ok, fb_ev = force_select_personal_via_ui(page)
        except Exception as exc:
            ok, fb_ev = False, {"exception": type(exc).__name__}
        evidence["primary"] = {"phase": "skipped_personal_no_id"}
        evidence["fallback"] = fb_ev
        if ok:
            return True, "", evidence
        return False, OAUTH_WS_ENDPOINT_ERROR, evidence

    # 3. 主路径
    try:
        ok, redirect, primary_ev = select_oauth_workspace(
            page, pid, consent_url=consent_url,
        )
    except Exception as exc:
        ok, redirect, primary_ev = False, None, {"exception": type(exc).__name__}

    evidence["primary"] = primary_ev
    if ok:
        # 跟随 redirect / continue_url(如果有)— Playwright 在主流程会继续走 callback
        if redirect:
            try:
                page.goto(redirect, wait_until="domcontentloaded", timeout=15000)
            except Exception as exc:
                logger.warning("[oauth_ws] 主路径 200 但 redirect %s 跳转失败: %s",
                               redirect, exc)
        return True, "", evidence

    # 4. fallback
    logger.warning("[oauth_ws] 主路径失败 status=%s,走 UI fallback",
                   primary_ev.get("http_status"))
    try:
        fb_ok, fb_ev = force_select_personal_via_ui(page)
    except Exception as exc:
        fb_ok, fb_ev = False, {"exception": type(exc).__name__}
    evidence["fallback"] = fb_ev
    evidence["primary_failed"] = True
    if fb_ok:
        return True, "", evidence

    # 5. 全失败
    return False, OAUTH_WS_ENDPOINT_ERROR, evidence
