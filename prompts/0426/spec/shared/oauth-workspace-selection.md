# Shared SPEC: OAuth Workspace 显式选择(personal sticky-rejoin 修复)

## 0. 元数据 + 引用方

| 字段 | 内容 |
|---|---|
| 名称 | OAuth flow 内 personal workspace 显式选择(HTTP `/api/accounts/workspace/select` 主路径 + Playwright UI fallback + 5 次重试) |
| 版本 | v1.0 (2026-04-27 Round 8 — sticky-default unset 根因修复) |
| 主题归属 | `login_codex_via_browser(use_personal=True)` 流程内 workspace 主动选择 + cookie 解码契约 + 失败分类 + sleep(8) 删除依据 |
| 引用方 | PRD-7(Round 8 master-team-degrade-oauth-rejoin) / spec-2-account-lifecycle.md v1.5 §3.4.6 / FR-W1~W5(待 PRD-7 落地)|
| 共因 | Round 8 PRD §1 代码根因 — `auth.openai.com` 的 `default_workspace_id` 不随 ChatGPT DELETE user 联动,issuer 默认按 default 颁 token,personal OAuth 拿到 `plan_type=team` |
| 不在范围 | Master 订阅健康度探针(见 [`./master-subscription-health.md`](./master-subscription-health.md)) / `_account` cookie 注入(已存在,Team 路径用,本 spec 不动) / `oai-oauth-session` 之外的 sentinel-token 等 OpenAI 私有反爬细节(由 patch-implementer 抓包确定) |

---

## 1. 概念定义

| 术语 | 定义 |
|---|---|
| `sticky-rejoin` 误用 | PRD 旧叙事 — "OpenAI 偷偷把用户加回 workspace";research 已澄清:**真相是 default_workspace_id 不随 DELETE user 自动 unset**(research/sticky-rejoin-mechanism.md §1.2-1.3) |
| `default_workspace_id` | `auth.openai.com` 后端为每个 user 维护的 last-used workspace 标记;OAuth flow 没显式 `workspace/select` 时 issuer 用它颁 token |
| `oai-oauth-session` | `auth.openai.com` 域 cookie,JWT-like 结构,base64 解码后 JSON 含 `workspaces[]` 数组 — research/oauth-personal-selection.md §2.3 + research/sticky-rejoin-mechanism.md §3.1 |
| `workspace/select` | `POST https://auth.openai.com/api/accounts/workspace/select`,接收 `{workspace_id}` 直接定向,绕过 UI 选择页;返回 `continue_url` 或 302 含 `?code=...&state=...` |
| `Playwright UI fallback` | 主路径(HTTP)失败时的兜底:Playwright 主动 goto `auth.openai.com/workspace`,DOM 找 "Personal/个人" 按钮点击 — cnitlrt PR#39 已实证 |
| `5 次重试` | 同一 user 多次 OAuth retry 触发后端最终一致性 — `openai/codex#1977` ejntaylor 实证 5 次成功;本 spec 作为终极保险 |

---

## 2. 完整数据契约

### 2.1 cookie 解码契约(`oai-oauth-session`)

**结构假设**(research/sticky-rejoin §3.1 + research/oauth-personal-selection §2.3 反推,**实施期 patch-implementer 必须抓包验证**):

```python
# cookie 名:oai-oauth-session
# 域:auth.openai.com
# HttpOnly: 视情况(可能 false,Playwright context.cookies() 可读)
# 值结构:base64url(JSON) 或 三段 JWT(header.payload.signature),取 [0] 段 base64url decode

# 解码后 JSON schema(实施期以抓包为准,本 spec 为目标契约)
{
  "user_id": "<auth0 user uuid>",
  "workspaces": [
    {
      "id": "<workspace_account_id uuid>",
      "name": "Master Team",
      "structure": "workspace",          # 或 "personal"
      "role": "account-owner",            # owner / admin / user
      "plan_type": "team"                  # 部分实现含,可能缺
    },
    {
      "id": "<personal_account_id uuid>",
      "name": "Personal",
      "structure": "personal",
      "role": "account-owner"
    }
  ],
  "default_workspace_id": "<sticky 指向>",
  "issued_at": 1777699200
}
```

**personal 项识别规则**(优先级从高到低,实施期任一命中即视为 personal):

```python
def _is_personal_workspace(item: dict) -> bool:
    """personal 识别 — 字段名以 OpenAI 后端实际下发为准."""
    if str(item.get("structure") or "").lower() == "personal":
        return True
    if str(item.get("plan_type") or "").lower() == "free":
        return True
    if item.get("is_personal") is True:        # 部分实现的 boolean 标记
        return True
    return False
```

**workspaces 数组为空 / 无 personal 项的语义**:

| 场景 | 含义 | 处置 |
|---|---|---|
| `workspaces == []` | OAuth session cookie 异常 / 解码失败 | 走 Playwright UI fallback |
| `workspaces` 非空但无 personal | user 在后端确实**只属于 Team**(personal workspace 未恢复) | fail-fast,失败分类 `oauth_workspace_select_no_personal`,不重试 |
| `workspaces` 含 personal | 主路径可走 | POST `workspace/select` |

### 2.2 函数签名(实施期目标)

#### 2.2.1 cookie 解码工具

```python
def decode_oauth_session_cookie(
    page_or_context,
) -> Optional[dict]:
    """从 Playwright page / browser_context 读 oai-oauth-session cookie 并解码.

    实施位置:`src/autoteam/chatgpt_api.py` 末尾或新文件 `oauth_workspace.py`

    返回:
      解码后的 JSON dict(含 workspaces[]);失败时返回 None(不抛异常)

    实施细节(以抓包为准):
      1. context.cookies("https://auth.openai.com") 获取 cookie 列表
      2. find name == "oai-oauth-session"
      3. 若值含 ".",取首段;否则整串 base64url decode
      4. JSON parse,失败回 None
    """
```

#### 2.2.2 workspace/select 主路径

```python
def select_oauth_workspace(
    page,
    workspace_id: str,
    *,
    consent_url: str,
    timeout: float = 15.0,
) -> tuple[bool, Optional[str], dict]:
    """POST https://auth.openai.com/api/accounts/workspace/select.

    Returns:
      (success, continue_url_or_redirect, evidence)
      success=True 表示 endpoint 200/302 + 拿到 continue_url 或 ?code= redirect
      success=False 表示 4xx/5xx/异常,evidence 含失败原因

    实施细节:
      - 用 page.evaluate() 发 fetch 调用,credentials='include' 让 cookie 自动带
      - body: {"workspace_id": <uuid>}
      - headers: {"Content-Type": "application/json", "Referer": consent_url}
      - 不主动添加 sentinel-token —— Playwright context 已注入,fetch 走相同上下文
      - 失败时 evidence["http_status"] / evidence["body_preview"] 各 200 字以内,供事后排查
    """
```

#### 2.2.3 Playwright UI fallback

```python
def force_select_personal_via_ui(
    page,
    *,
    timeout_per_step: float = 8.0,
) -> tuple[bool, dict]:
    """fallback — 主动 goto auth.openai.com/workspace,DOM 找 Personal 按钮点击.

    源自 cnitlrt PR#39 `_ensure_workspace_target_session` + `_select_workspace_target`
    (research/sticky-rejoin-mechanism.md §3.2)

    流程:
      1. page.goto("https://auth.openai.com/workspace", wait_until="domcontentloaded")
      2. 校验是否在 workspace 选择页 (URL 或 标题文案)
      3. locator("text=/个人|Personal/i") + locator("button:has-text('Personal')") 等候选,first visible 点击
      4. 点 "继续/Continue" 按钮(可选,部分版本无确认页)
      5. 返回 (success, evidence)

    Returns:
      (True,  {url, clicked_text, ts_ms}) 命中 Personal 按钮且点击成功
      (False, {url, page_title, snapshot_path}) 未在选择页 / 找不到按钮 / 点击异常
    """
```

#### 2.2.4 顶层编排函数

```python
def ensure_personal_workspace_selected(
    page,
    *,
    consent_url: str,
    max_retries: int = 5,
) -> tuple[bool, str, dict]:
    """Personal OAuth 主流程 — 三层兜底.

    Returns:
      (success, fail_category, evidence)
      success=True ⇒ fail_category=""(空串),OAuth 流程可继续走 callback
      success=False 时 fail_category ∈ {
          "oauth_workspace_select_no_personal",
          "oauth_workspace_select_endpoint_error",
          "oauth_plan_drift_persistent",   # 重试 5 次仍 plan_type=team
      }

    流程(伪代码):
      session = decode_oauth_session_cookie(page.context)
      if session is None: → 走 fallback (UI)
      personal = next(w for w in session["workspaces"] if _is_personal_workspace(w), None)
      if personal is None:
          return False, "oauth_workspace_select_no_personal", {workspaces: [...]}
      ok, redirect_url, ev = select_oauth_workspace(page, personal["id"], consent_url=consent_url)
      if not ok:
          fb_ok, fb_ev = force_select_personal_via_ui(page)
          if fb_ok:
              return True, "", {primary_failed: True, fallback: fb_ev}
          return False, "oauth_workspace_select_endpoint_error", {primary: ev, fallback: fb_ev}
      return True, "", {primary: ev}
    """
```

### 2.3 失败分类常量

```python
# src/autoteam/register_failures.py 文档化(spec-2 v1.5 RegisterFailureRecord enum 扩)
OAUTH_WS_NO_PERSONAL = "oauth_workspace_select_no_personal"
"""workspaces[] 中找不到 personal 项 — user 在后端事实上只属于 Team
   (sticky 根因之一)。fail-fast,不重试。"""

OAUTH_WS_ENDPOINT_ERROR = "oauth_workspace_select_endpoint_error"
"""POST /api/accounts/workspace/select 返回 4xx/5xx 或网络异常,且 UI fallback 也失败。
   通常为端点变更 / sentinel-token 反爬 / Playwright DOM 漂移。"""

OAUTH_PLAN_DRIFT_PERSISTENT = "oauth_plan_drift_persistent"
"""workspace/select 成功但 5 次 OAuth retry 后 bundle.plan_type 仍非 free。
   罕见 — 后端最终一致性失败,与 register_failures 已有 plan_drift 区分:
   plan_drift 是单次拒收;persistent 是 5 次重试都拒收。"""
```

---

## 3. 行为契约

### 3.1 前置条件

- `page` 是 Playwright Page 对象,且已完成 `step-0` ChatGPT 预登录 + 邮箱+密码+OTP(`codex_auth.py:295-562`)
- `consent_url` 是从 `auth.openai.com/sign-in-with-chatgpt/codex/consent` 形态的 referer URL(用于 select 端点的 Referer 头)
- 调用前 `is_master_subscription_healthy()` 必已返回 healthy(否则按 [`./master-subscription-health.md`](./master-subscription-health.md) M-T1 fail-fast,根本不进本流程)
- 调用前 OAuth callback **尚未**发生(显式选择必须在 issuer 颁 token 之前)

### 3.2 后置条件

- 任何函数调用都不抛业务异常(Playwright / requests 异常被内部 try/except 吞为 evidence,顶层编排返回 `(False, fail_category, evidence)`)
- success=True 时:OAuth 流程继续到 callback,后续 `_exchange_auth_code` 拿到 bundle 应**预期**为 `plan_type=free`;但若依旧 `plan_type=team`,由 §3.4 重试逻辑承担
- success=False 时:必有 `fail_category` ∈ §2.3 三个枚举之一;evidence 必含足够信息供事后排查(URL / status / 解码后 workspaces[] 子集)

### 3.3 异常类型

- 解码 cookie / fetch / DOM 操作的所有 Playwright 异常 → 内部 try/except 吞掉,evidence["exception"] = type name
- 不传播 Exception 到 `login_codex_via_browser` 主流程 — 主流程只看 (success, fail_category, evidence) 三元组
- **唯一**例外:`assert_not_blocked` 抛 `RegisterBlocked(is_phone=True)` 必须传播(对齐 [`./add-phone-detection.md`](./add-phone-detection.md) §5.2 模板)

### 3.4 5 次重试策略(指数退避)

```
触发条件:select_oauth_workspace 成功(endpoint 200) 但 callback 拿到 bundle.plan_type != "free"

重试位点:由 _run_post_register_oauth(personal) 的外层重试循环承担,本 spec 提供策略参数:

| 次数 | 单次预算 | 累计 | 退避 |
|---|---|---|---|
| 1 | ~30s OAuth 全流程 | 30s | 立即 |
| 2 | ~30s             | 60s | sleep 5s  |
| 3 | ~30s             | 95s | sleep 10s |
| 4 | ~30s             | 145s | sleep 20s |
| 5 | ~30s             | 215s | sleep 30s |

总时长上限:~4 分钟 (215s + 单次最大 35s tolerance)
退避抖动:每次 sleep 加 ±20% jitter (rng,避免多账号并发同步重试 → 风控)

每次重试都会重新调用 ensure_personal_workspace_selected,因为 workspace/select 在新 OAuth state
上必须重新发起 (旧 state 已被 callback 消费或过期)
```

**理由**:research/oauth-personal-selection.md §3.1 / `openai/codex#1977` ejntaylor 实证 5 次后端最终一致 (分钟量级)。本 spec 不超过 5 次以避免触发 OpenAI 风控 (research/sticky-rejoin-mechanism.md §6.1 风险 1 + 风险 2)。

---

## 4. 与既有 OAuth 流程的整合

### 4.1 在 `login_codex_via_browser(use_personal=True)` 内的接入位置

**目标位置**:`src/autoteam/codex_auth.py:280-450` 区(use_personal 分支)

```
login_codex_via_browser(email, password, mail_client, *, use_personal=True)
  │
  ├─ step-0 跳过 _account cookie 注入 (旧路径 L311-312,保留)
  ├─ step-0 跳过 ChatGPT 预登录 (与 Team 路径不同,保留)
  │
  ├─ goto auth_url → 邮箱 + 密码 + OTP (L443-562)
  │
  ├─ ★ C-P1: assert_not_blocked(page, "oauth_about_you")
  ├─ step-3 about-you 填表 (L568-610)
  ├─ ★ C-P2: assert_not_blocked(page, f"oauth_consent_{step}")
  ├─ step-4 consent 循环 (L612-882)
  │
  ├─ ★★★ NEW (本 spec):ensure_personal_workspace_selected(page, consent_url=...)
  │       插入位置:consent 循环结束后,callback 等待之前
  │       仅当 use_personal=True 才调用;Team 路径完全跳过
  │       命中 fail_category 立即退出(配合外层重试)
  │
  ├─ ★ C-P3: assert_not_blocked(page, "oauth_callback_wait")
  ├─ step-5 等 callback (L884-906)
  ├─ ★ C-P4: assert_not_blocked(page, "oauth_personal_check")
  └─ _exchange_auth_code → bundle
```

**与既有探针的关系**:

- C-P1~C-P4 add-phone 探针**保留**,本 spec 不替代它们(语义不同 — phone vs workspace)
- C-P3 / C-P4 命中 add-phone 时优先抛 RegisterBlocked,本 spec 退出
- 本 spec 的失败 → fail_category 由编排函数返回,**不**抛异常到 login_codex_via_browser 顶层

### 4.2 与 use_personal=False (Team 路径) 的关系

**Team 路径不调用本 spec 的 select** — 因为:

- Team 路径希望默认 workspace == Team(已是 default_workspace_id 指向),没动机切换
- Team 路径已有 `_account` cookie 注入(`codex_auth.py:316-335`)间接锁定 Team workspace

**Team 路径仍需要 master health probe** — 见 [`./master-subscription-health.md`](./master-subscription-health.md) M-T2:即使是 Team 路径,母号降级时 invite 也会拿 free,所以 fill 入口仍需调 master probe fail-fast。

### 4.3 sleep(8) 删除依据(`manager.py:1554-1556`)

**原代码**(已实测无效):

```python
result = chatgpt_api._api_fetch("DELETE", delete_path)
if result["status"] in (200, 204):
    logger.info("[Team] 已将 %s 移出 Team", email)
    time.sleep(8)  # 等 OAI 后端同步 sticky-default
```

**Round 8 删除决定 + 理由**(research/sticky-rejoin-mechanism.md §1.2-1.3):

- DELETE user **真生效**,ChatGPT member 列表确实清掉(reconcile 验证过)
- 但 `auth.openai.com.session.default_workspace_id` 不随 DELETE 联动 — 等 8s / 80s / 800s 都没用
- sticky 根因不是"OpenAI 同步延迟",而是"default 不会自动 unset"
- 真正的解法是本 spec 的显式 `workspace/select`,8s sleep 是**纯无意义等待**

**Round 8 实施期动作**:删除 `time.sleep(8)`(无注释保留)。删除位置:`src/autoteam/manager.py:1554-1556`(以实施期实际行号为准)。删除后 personal 流程时长降低 8s。

**Out of Scope**:不引入 longer sleep / probe loop(60s+5 retry)等 Approach C 路径。本 spec 的 §3.4 5 次 OAuth retry 已替代该兜底。

---

## 5. 安全 / 风控注意事项

| 项 | 内容 |
|---|---|
| 私有 API 风险 | `/api/accounts/workspace/select` 未公开,字段名 / sentinel-token 算法 / JA3 指纹要求都可能变。Playwright UI fallback 是**强制**保留的兜底,不可省略 |
| 风控触发 | 多账号同时重试可能触发 OpenAI rate-limit。本 spec §3.4 的 5 次重试 + ±20% jitter 是上限;不允许加大 |
| Token 不落盘 | evidence 中**禁止**包含 access_token / id_token / refresh_token / `oai-oauth-session` 原始 cookie 值;decode 后的 workspaces[] 也只保留 id/name/structure/role 子集(类比 master health §2.3 裁剪规则) |
| 截图脱敏 | force_select_personal_via_ui 失败时截图存 `screenshots/oauth_workspace_select_failed_{ts}.png`,但不存 cookie/local-storage 转储 |
| 单 Playwright 锁 | 本 spec 在既有"全局 Playwright 锁"(PRD §5 已知约束)内运行,不引入新并发原语 |

---

## 6. 不变量(Invariants)

- **W-I1**:`ensure_personal_workspace_selected` / `select_oauth_workspace` / `decode_oauth_session_cookie` / `force_select_personal_via_ui` **永不抛异常**;任何 Exception 转为 (False, fail_category, evidence) 三元组返回
- **W-I2**:三个失败分类**互斥不重叠**:no_personal(workspaces 中确认无 personal)/ endpoint_error(主路径 + fallback 都失败)/ plan_drift_persistent(5 次重试后仍 team) — 每条 register_failures 记录的 fail_category 字段必为这三个之一
- **W-I3**:本流程**只**在 `use_personal=True` 时执行;Team 路径调用 `ensure_personal_workspace_selected` 视为 bug
- **W-I4**:解码后 workspaces[] 中查找 personal 必须严格使用 §2.1 `_is_personal_workspace` 三条件之一,**禁止** 仅靠 `workspaces[0]` 默认取首项(gpt-auto-register 上游用 [0],但本工程 sticky 场景下 [0] 可能是 Team)
- **W-I5**:5 次重试上限**硬编码上限**;允许通过 `runtime_config.oauth_workspace_select_max_retries` 调小(1~5),不允许调大 — 风控考虑
- **W-I6**:落盘 evidence **不含**敏感字段(access/refresh/id token / cookie 原始值 / `chatgpt-account-id` header 等)
- **W-I7**:`time.sleep(8)` 在 `_run_post_register_oauth(personal)` 中**已删除** — 任何"加回 sleep 等 sticky 同步"的代码视为对本 spec 的回归
- **W-I8**:`fail_category` 字符串字面量与 `register_failures.json` schema(spec-2 v1.5 RegisterFailureRecord enum)一致 — 任何不在枚举内的字面量视为 schema 违规
- **W-I9**:`workspace/select` 主路径成功(endpoint 200) 但 callback 拿到 plan!=free 时,**不**立即记 fail_category,而是进入外层重试;只有 5 次后仍失败才记 `oauth_plan_drift_persistent`
- **W-I10**:本 spec 的接入点(§4.1 NEW 位置)**不**复用 add-phone 探针的 `assert_not_blocked` — 探针语义不同,且本 spec 失败需要外层重试,不应抛 RegisterBlocked

---

## 7. 单元测试 fixture 与样本

### 7.1 `oai-oauth-session` cookie 解码样本

```json
// tests/fixtures/oauth_session_cookies.json
{
  "session_with_personal": {
    "user_id": "user-aaaa",
    "workspaces": [
      {
        "id": "team-uuid-1111",
        "name": "Master Team",
        "structure": "workspace",
        "role": "user",
        "plan_type": "team"
      },
      {
        "id": "personal-uuid-2222",
        "name": "Personal",
        "structure": "personal",
        "role": "account-owner"
      }
    ],
    "default_workspace_id": "team-uuid-1111"
  },
  "session_no_personal_sticky": {
    "user_id": "user-aaaa",
    "workspaces": [
      {
        "id": "team-uuid-1111",
        "name": "Master Team",
        "structure": "workspace",
        "role": "user"
      }
    ],
    "default_workspace_id": "team-uuid-1111"
  },
  "session_empty_workspaces": {
    "user_id": "user-aaaa",
    "workspaces": []
  },
  "session_personal_via_plan_type_free": {
    "user_id": "user-aaaa",
    "workspaces": [
      {"id": "ws-aaaa", "structure": "workspace", "role": "user", "plan_type": "team"},
      {"id": "ws-bbbb", "structure": "personal_v2", "role": "owner", "plan_type": "free"}
    ]
  },
  "session_personal_via_is_personal_flag": {
    "user_id": "user-aaaa",
    "workspaces": [
      {"id": "ws-aaaa", "structure": "team_workspace"},
      {"id": "ws-bbbb", "is_personal": true, "name": "Personal account"}
    ]
  }
}
```

### 7.2 推荐单测代码

```python
# tests/unit/test_oauth_workspace_select.py
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from autoteam.chatgpt_api import (
    decode_oauth_session_cookie,
    select_oauth_workspace,
    force_select_personal_via_ui,
    ensure_personal_workspace_selected,
    _is_personal_workspace,
)
from autoteam.register_failures import (
    OAUTH_WS_NO_PERSONAL,
    OAUTH_WS_ENDPOINT_ERROR,
    OAUTH_PLAN_DRIFT_PERSISTENT,
)

FIXTURE = json.loads(Path("tests/fixtures/oauth_session_cookies.json").read_text())


@pytest.mark.parametrize("name,expected_personal_id", [
    ("session_with_personal", "personal-uuid-2222"),
    ("session_personal_via_plan_type_free", "ws-bbbb"),
    ("session_personal_via_is_personal_flag", "ws-bbbb"),
    ("session_no_personal_sticky", None),
    ("session_empty_workspaces", None),
])
def test_personal_detection(name, expected_personal_id):
    workspaces = FIXTURE[name]["workspaces"]
    found = next((w for w in workspaces if _is_personal_workspace(w)), None)
    if expected_personal_id is None:
        assert found is None
    else:
        assert found is not None and found["id"] == expected_personal_id


def test_no_personal_returns_no_personal_category():
    """W-I4 + 失败分类 — workspaces 无 personal 时必返 OAUTH_WS_NO_PERSONAL."""
    page = MagicMock()
    page.context.cookies.return_value = []
    with patch(
        "autoteam.chatgpt_api.decode_oauth_session_cookie",
        return_value=FIXTURE["session_no_personal_sticky"],
    ):
        ok, category, ev = ensure_personal_workspace_selected(
            page, consent_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )
    assert ok is False
    assert category == OAUTH_WS_NO_PERSONAL


def test_select_endpoint_500_falls_back_to_ui(monkeypatch):
    """主路径 endpoint 500 时走 fallback;fallback 成功 → success=True."""
    page = MagicMock()

    def fake_decode(*args, **kwargs):
        return FIXTURE["session_with_personal"]

    def fake_select(*args, **kwargs):
        return False, None, {"http_status": 500, "body_preview": "Internal Server Error"}

    def fake_fallback(*args, **kwargs):
        return True, {"clicked_text": "Personal", "ts_ms": 1234}

    monkeypatch.setattr("autoteam.chatgpt_api.decode_oauth_session_cookie", fake_decode)
    monkeypatch.setattr("autoteam.chatgpt_api.select_oauth_workspace", fake_select)
    monkeypatch.setattr("autoteam.chatgpt_api.force_select_personal_via_ui", fake_fallback)

    ok, category, ev = ensure_personal_workspace_selected(
        page, consent_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
    )
    assert ok is True
    assert category == ""
    assert ev.get("primary_failed") is True
    assert ev.get("fallback") is not None


def test_select_endpoint_500_and_ui_failure_returns_endpoint_error(monkeypatch):
    """主路径 + UI fallback 都失败 → OAUTH_WS_ENDPOINT_ERROR."""
    page = MagicMock()
    monkeypatch.setattr(
        "autoteam.chatgpt_api.decode_oauth_session_cookie",
        lambda *a, **kw: FIXTURE["session_with_personal"],
    )
    monkeypatch.setattr(
        "autoteam.chatgpt_api.select_oauth_workspace",
        lambda *a, **kw: (False, None, {"http_status": 500}),
    )
    monkeypatch.setattr(
        "autoteam.chatgpt_api.force_select_personal_via_ui",
        lambda *a, **kw: (False, {"page_title": "Sign in"}),
    )
    ok, category, ev = ensure_personal_workspace_selected(
        page, consent_url="...",
    )
    assert ok is False
    assert category == OAUTH_WS_ENDPOINT_ERROR


def test_no_exception_propagates(monkeypatch):
    """W-I1 — 内部任何 Exception 转 (False, ...)."""
    page = MagicMock()
    def boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr("autoteam.chatgpt_api.decode_oauth_session_cookie", boom)
    ok, category, ev = ensure_personal_workspace_selected(page, consent_url="...")
    assert ok is False
    assert category in (
        OAUTH_WS_NO_PERSONAL,
        OAUTH_WS_ENDPOINT_ERROR,
        OAUTH_PLAN_DRIFT_PERSISTENT,
    )


def test_evidence_no_token_leak():
    """W-I6 — evidence 不含 access_token / refresh_token / cookie 原始值."""
    sample_evidence = {
        "primary": {"http_status": 200, "body_preview": "{...continue_url...}"},
        "fallback": None,
    }
    serialized = json.dumps(sample_evidence)
    for token_kw in ("access_token", "refresh_token", "id_token", "session_token"):
        assert token_kw not in serialized
```

### 7.3 抓包验证 checklist(patch-implementer Stage 2 必须做)

| # | 验证点 | 通过标准 |
|---|---|---|
| V1 | `oai-oauth-session` cookie 是否仍可 base64url decode 成 JSON | 解码后 JSON 含 `workspaces` 数组 |
| V2 | workspaces[] 项含 `structure` 字段且取值为 `"personal" / "workspace"` 之一 | 至少一个 sticky 场景下能区分 |
| V3 | 子号刚被 DELETE 后,workspaces[] 是否含 personal 项 | 含 → 主路径可走;不含 → 走 OAUTH_WS_NO_PERSONAL |
| V4 | `POST /api/accounts/workspace/select` 不带 sentinel-token 是否仍 200 | 若 401 → 实施期需补 sentinel-token 提取(不在本 spec) |
| V5 | UI fallback 的"Personal" / "个人" 按钮 selector 是否有效 | Playwright 实测命中可见元素 |

---

## 8. 与既有 spec / FR 的关系

| 关系对象 | 说明 |
|---|---|
| `spec-2 v1.5 §3.4.6` | 引用本 spec — 定义 personal OAuth 内的 workspace/select 接入点;Team 路径不动 |
| [`./master-subscription-health.md`](./master-subscription-health.md) | 互补 — master health 决定能否进 OAuth;workspace/select 决定 OAuth 颁哪个 token。两者前后串联 |
| [`./add-phone-detection.md`](./add-phone-detection.md) | 共存 — 4 处 add-phone 探针保留,本 spec 在 consent 循环之后 / callback 之前接入 |
| [`./plan-type-whitelist.md`](./plan-type-whitelist.md) | 下游消费 — workspace/select 成功后 bundle.plan_type 应为 `free`,由 `is_supported_plan` 判定;本 spec 不复制 plan 校验 |
| [`./quota-classification.md`](./quota-classification.md) | 下游消费 — personal OAuth 拿到 free token 后仍要 wham/usage 探测配额 |
| `register_failures.json schema` | 新增 3 个 category(`oauth_workspace_select_no_personal` / `oauth_workspace_select_endpoint_error` / `oauth_plan_drift_persistent`),在 spec-2 v1.5 RegisterFailureRecord enum 同步 |
| `manager.py:1554-1556 sleep(8)` | 本 spec 删除 — 见 §4.3 删除依据 |

---

## 9. 参考资料

### 9.1 内部研究(Round 8 task research/)

- `.trellis/tasks/04-27-master-team-degrade-oauth-rejoin/research/oauth-personal-selection.md`
  - §1.1-1.6 OAuth URL hint 全集 + `allowed_workspace_id` 是 allow-list 不是 selector
  - §2.1-2.3 UI 选择页 + `_account` cookie + `accounts/workspace/select` 端点
  - §3 业内方案对比(gpt-auto-register / cnitlrt PR#39 / opencode-openai-codex-auth)
  - §4 Approach A/B/C(本 spec 主路径源自 Approach B,fallback 源自 Approach C)
  - §5 风险与未决(对应本 spec §5)

- `.trellis/tasks/04-27-master-team-degrade-oauth-rejoin/research/sticky-rejoin-mechanism.md`
  - §1.1-1.3 sticky-default 真相(non-sticky-rejoin)
  - §1.4 本工程 codex_auth.py:963-973 的"软兜底"位置
  - §2 Hard kick / Deactivate API 调研(确认无关)
  - §3 Recovery 策略 a-f 对比 — 本 spec 选 b(主) + c(兜底)
  - §3.1 gpt-auto-register `_submit_workspace_and_org` 完整伪代码
  - §3.2 cnitlrt PR#39 `_ensure_workspace_target_session` Playwright 实现
  - §5 推荐 Recovery 路径(本 spec §3 / §4 直接落地)
  - §6 风险与未决(对应本 spec §5 + §7.3 抓包 checklist)

### 9.2 内部代码引用(实施期目标位置)

- `src/autoteam/codex_auth.py:266-975` — `login_codex_via_browser` 全流程,本 spec 在 use_personal=True 分支注入
- `src/autoteam/codex_auth.py:643-671` — Personal workspace UI 点击逻辑(已存在,本 spec 升级为带 fallback 的完整流程)
- `src/autoteam/codex_auth.py:963-973` — Personal 强校验 plan_type=free 的拒收逻辑(保留,作为 5 次重试的判据)
- `src/autoteam/manager.py:1513-1655` — `_run_post_register_oauth(leave_workspace=True)` 全流程(本 spec 5 次重试在此外层)
- `src/autoteam/manager.py:1549-1556` — kick 后 sleep 8s(本 spec §4.3 删除依据)

### 9.3 外部参考

- `TongjiRabbit/gpt-auto-register/app/oauth_service.py:511-595` `_submit_workspace_and_org` — 主路径参考实现
- `cnitlrt/AutoTeam` PR #39 (closed unmerged) — Playwright fallback 完整代码(`_ensure_workspace_target_session` / `_select_workspace_target`)
- `openai/codex#1977` `mrairdon-midmark` 评论 — 多 workspace JWT claim 反推证据
- `openai/codex#1977` `ejntaylor` 评论 — 5 次 OAuth retry 触发后端最终一致性的实证
- `openai/codex codex-rs/login/src/server.rs:468-503` — `build_authorize_url`(`allowed_workspace_id` 是 allowlist 不是 selector)

---

**文档结束。** 工程师据此可直接编写 `decode_oauth_session_cookie` / `select_oauth_workspace` / `force_select_personal_via_ui` / `ensure_personal_workspace_selected` 函数 + 接入 + 单测,无需额外决策。抓包验证 checklist 见 §7.3。

---

## 附录 A:修订记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-04-27 Round 8 | 初版 — 三函数契约(decode / select / fallback / 编排)+ 5 次重试 + 3 失败分类(no_personal / endpoint_error / plan_drift_persistent)+ 10 不变量(W-I1~I10)+ sleep(8) 删除依据 + 抓包验证 checklist。源自 `.trellis/tasks/04-27-master-team-degrade-oauth-rejoin/research/oauth-personal-selection.md` §3-§5 + `research/sticky-rejoin-mechanism.md` §3-§5。配套 PRD-7 Approach A R3 落地。 |
