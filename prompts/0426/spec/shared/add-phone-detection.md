# Shared SPEC: OAuth add-phone 探针

## 0. 元数据 + 引用方

| 字段 | 内容 |
|---|---|
| 名称 | OAuth 流程 add-phone 探针接入与异常分类 |
| 版本 | v1.0 (2026-04-26) |
| 主题归属 | `login_codex_via_browser` 中的 4 个探针接入点 + 5 个调用方分类处置 |
| 引用方 | PRD-2 / spec-2-account-lifecycle.md / FR-C1~C5 |
| 共因 | synthesis §1 共因 C;Issue#4 + Issue#6 |
| 不在范围 | invite 注册阶段的 add-phone 检测(已实现,见 `invite.py:106-145`)、SMS pool 自动绑定(违反 ToS,非目标) |

---

## 1. 概念定义

| 术语 | 定义 |
|---|---|
| `add-phone` | OpenAI 在 OAuth 流程中触发的强制手机号验证页(URL 含 `add-phone` / `verify-phone` 等),命中后用户必须绑定手机才能继续 |
| `RegisterBlocked` | 现有异常类(`invite.py:53-66`),用于注册流程被风控阻断时上抛;含 `is_phone` / `is_duplicate` / `step` / `reason` 字段 |
| `assert_not_blocked` | 现有探针函数(`invite.py:138-145`),命中 add-phone 或 duplicate 立刻 raise |
| `detect_phone_verification` | 现有检测函数(`invite.py:106-126`),URL 强信号 + 文本+输入框组合信号 |
| `oauth_phone_blocked` | 新增 register_failures category,与注册阶段的 `phone_blocked` 区分(便于统计 OAuth vs 注册的命中率) |

---

## 2. 完整数据契约

### 2.1 现有契约(直接复用,不改动)

```python
# src/autoteam/invite.py L53-66 已存在
class RegisterBlocked(Exception):
    """
    注册流程被风控或确定性错误阻断时抛出;调用方按 reason 做分流处理:
    - is_phone=True: OpenAI 要求手机验证,当前账号放弃(用户明确不绕过)
    - is_duplicate=True: 邮箱已被占用,当前账号放弃,换邮箱重来
    - 其他: 单步逻辑错误,按现有 retry 流程处理
    """
    def __init__(self, step: str, reason: str, *, is_phone: bool = False, is_duplicate: bool = False):
        super().__init__(f"[{step}] {reason}")
        self.step: str = step
        self.reason: str = reason
        self.is_phone: bool = is_phone
        self.is_duplicate: bool = is_duplicate


# src/autoteam/invite.py L106 已存在
def detect_phone_verification(page) -> bool:
    """若当前页面要求手机验证返回 True。URL 命中优先;文本命中需配合电话输入框。"""
    ...


# src/autoteam/invite.py L138 已存在
def assert_not_blocked(page, step: str) -> None:
    """任何步骤后调用,检测到阻断项立刻 raise RegisterBlocked。"""
    ...
```

### 2.2 新增契约(本 spec 引入)

```python
# src/autoteam/register_failures.py 文档化扩 category(只是注释,不改代码逻辑)
def record_failure(email: str, category: str, reason: str, **extra) -> None:
    """
    category: 'phone_blocked'         注册阶段命中 add-phone(invite.register_with_invite)
              'oauth_phone_blocked'   OAuth 阶段命中 add-phone(login_codex_via_browser)  ★新增
              'duplicate_exhausted'   邮箱重复
              'register_failed'       通用注册失败
              'oauth_failed'          OAuth 通用失败(无 phone 子分类时)
              'kick_failed'           kick 失败
              'team_oauth_failed'     Team 阶段 OAuth 失败
              'exception'             浏览器/网络异常
              'plan_unsupported'      plan_type 不在白名单(见 plan-type-whitelist.md)  ★新增
              'no_quota_assigned'     wham/usage 返回 no_quota(见 quota-classification.md)  ★新增
              'plan_drift'            reinvite 拿到非 team plan(见 spec-2 §3.3)  ★新增
              'auth_error_at_oauth'   注册收尾 wham 401/403  ★新增
              'quota_probe_network_error'  注册收尾 wham 网络异常  ★新增
    """
```

### 2.3 OAuth 域定制规则(差异于注册阶段)

```python
# src/autoteam/codex_auth.py 顶部新增(本地常量,不动 invite.py)
# OAuth consent 页可能含 "phone" 帮助链接 / footer 文字,但**没有**可见 input[type=tel]。
# 对 OAuth 域采用更严格规则:URL 命中是强信号(直接判 True);文本命中**必须**配合
# 可见 tel input,且 tel input 不能在 footer / aside 等次要区域。
_OAUTH_PHONE_URL_HINTS = ("verify-phone", "add-phone", "/phone", "phone_verification", "phone-number")
_OAUTH_PHONE_TEXT_HINTS = (  # 严格子集,移除 "phone verification" 这种容易误命中 consent 页解释文案的
    "verify your phone",
    "add your phone",
    "verification code to your phone",
    "add a phone number",
    "add a phone",
    "enter your phone",
    "we'll text you",
    "请输入手机号",
    "手机号码",
    "验证手机",
    "添加手机",
)
```

---

## 3. 行为契约

### 3.1 函数签名(所有现有,本 spec 仅约束调用方)

```python
def detect_phone_verification(page) -> bool: ...
def assert_not_blocked(page, step: str) -> None:
    """命中 add-phone → raise RegisterBlocked(step, "add-phone 手机验证", is_phone=True)
       命中 duplicate → raise RegisterBlocked(step, "duplicate email", is_duplicate=True)
       未命中 → 返回 None
    """
```

### 3.2 前置条件

- `page` 是已加载的 Playwright Page 对象(可读 `.url` / `.inner_text` / `.locator`)
- `step` 是非空字符串,表示触发探针的位点名称(用于异常 `step` 字段 + 失败截图命名)
- 探针函数本身**不抛**业务异常;Playwright 异常被内部 try/except 吞掉(注释中已说明)

### 3.3 后置条件

- `assert_not_blocked` 返回 None 表示页面未阻塞,主流程可继续
- 抛出 `RegisterBlocked` 后,调用方必须按 `is_phone` / `is_duplicate` 分类处置(见 §5)
- 命中后由调用方负责截图(`_screenshot(page, f"codex_phone_blocked_{step}.png")`)

### 3.4 异常传播

```
detect_phone_verification(page)
  └─ 任何 Playwright 异常 → logger.debug + return False(不阻塞)
  └─ 命中 → return True

assert_not_blocked(page, step)
  └─ detect_phone_verification(page) == True → raise RegisterBlocked(step, ..., is_phone=True)
  └─ detect_duplicate_email(page) == True   → raise RegisterBlocked(step, ..., is_duplicate=True)
  └─ 其他 → return None
```

---

## 4. 4 个探针接入点(精确位置)

### 4.1 接入位置图

```
login_codex_via_browser(email, password, mail_client, *, use_personal)  # codex_auth.py:250
  │
  ├─ step-0  ChatGPT 预登录 + _account cookie 注入(L295-440)
  │           Cloudflare 等待 / 邮箱+OTP / workspace 选择
  │
  ├─ step-1  goto auth_url → 邮箱表单(L443-488)
  │           Google redirect 检测(已存在,不变)
  │
  ├─ step-2  邮箱 + 密码 + OTP(L489-562)
  │
  ├─ ★ C-P1: assert_not_blocked(page, "oauth_about_you")    [L568 about-you 入口前]
  │           插入位置:`if "about-you" in page.url:` 这行**之前**
  │           理由:about-you 提交后 OpenAI 经常拉一次 add-phone,要在页面切到 about-you 前先确认
  │
  ├─ step-3  about-you 填表 + 提交(L568-610)
  │
  ├─ ★ C-P2: assert_not_blocked(page, f"oauth_consent_{step}")  [L612 consent 循环每轮开头]
  │           插入位置:`for step in range(10):` 内,每次 try 块的**第一行**
  │           理由:consent 循环每一步都可能跳到 add-phone,只看 workspace/Continue 按钮看不到
  │
  ├─ step-4  consent 10 次循环(L612-882)
  │
  ├─ ★ C-P3: assert_not_blocked(page, "oauth_callback_wait")    [L884 等 callback 前]
  │           插入位置:`for _ in range(30):` 这行**之前**
  │           理由:add-phone 页就是"callback 永远不来"的根因,等之前先拦
  │
  ├─ step-5  等 30s callback(L884-906)
  │
  ├─ ★ C-P4: assert_not_blocked(page, "oauth_personal_check")   [L920 personal 拒收 bundle 之前]
  │           插入位置:`if use_personal:` 这行**之前**
  │           理由:防御性 — 通常 callback 前已拦截,但作为最后一道关卡
  │
  └─ _exchange_auth_code(auth_code) → bundle
```

### 4.2 实施代码片段

```python
# 位点 C-P1(在 codex_auth.py:568 之前)
from autoteam.invite import assert_not_blocked, RegisterBlocked  # 文件顶部 import

# === 处理 about-you 页面（可能出现在 OAuth 流程中）===
assert_not_blocked(page, "oauth_about_you")  # ★新增
if "about-you" in page.url:
    logger.info("[Codex] 检测到 about-you 页面，填写个人信息...")
    ...

# 位点 C-P2(在 codex_auth.py:612 后的 for 循环每轮开头)
for consent_step in range(10):
    try:
        assert_not_blocked(page, f"oauth_consent_{consent_step}")  # ★新增,作为 try 块第一行
        # ... 原有 consent 循环代码 ...
    except RegisterBlocked:
        raise  # 不在内层吞,让外层调用方处理
    except Exception as e:
        logger.debug(...)

# 位点 C-P3(在 codex_auth.py:884 之前)
assert_not_blocked(page, "oauth_callback_wait")  # ★新增
# 等待 redirect callback 获取 auth code
for _ in range(30):
    if auth_code:
        break
    ...

# 位点 C-P4(在 codex_auth.py:920 之前,personal 模式校验 plan_type 之前)
assert_not_blocked(page, "oauth_personal_check")  # ★新增,防御性
if use_personal:
    plan = (bundle.get("plan_type") or "").lower()
    ...
```

### 4.3 位点选择理由(摘自 issue#4 D.1.1)

| 位点 | 选择理由 |
|---|---|
| C-P1 (about-you 前) | OpenAI 在新账号注册的 about-you 页之后**最频繁**拉 add-phone。在切到 about-you 之前先拦,可在最早时间发现风控 |
| C-P2 (consent 循环每轮) | consent 循环 10 次,每一步都可能跳 add-phone;不在循环里拦,会被当作"workspace 没选好"反复重试 |
| C-P3 (callback 等待前) | add-phone 阻塞会让 callback 永远不来,30s 等待白白耗费;在等之前先判 |
| C-P4 (personal 拒收前) | 防御性:正常情况下前 3 个位点已拦下;万一漏过(consent 循环之外的页面),最后兜底 |

---

## 5. 调用方处置规范(5 个 login_codex_via_browser 调用点)

### 5.1 调用点清单

| # | 文件:行号 | 上下文 | use_personal |
|---|---|---|---|
| 1 | `manager.py:1057` | `_check_pending_invites` 补登录 | False |
| 2 | `manager.py:1431` | `_run_post_register_oauth(leave_workspace=True)` 个人模式 | True |
| 3 | `manager.py:1463` | `_run_post_register_oauth` Team 注册收尾 | False |
| 4 | `manager.py:2466` | `reinvite_account` standby 复用 | False |
| 5 | `api.py:1479` | `/api/accounts/{email}/login` 用户触发补登录 | 视 acc.status 而定 |

### 5.2 5 个调用方处置矩阵

| # | catch RegisterBlocked(is_phone=True) 后 | record_failure 字段 |
|---|---|---|
| 1 | `delete_account(email)` + `record_failure(...)` | category="oauth_phone_blocked", stage="check_pending_invites" |
| 2 | `delete_account(email)` + `record_failure(...)` | category="oauth_phone_blocked", stage="run_post_register_oauth_personal" |
| 3 | `update_account(email, status=STATUS_AUTH_INVALID)` + `record_failure(...)` + 不删账号(已在 Team) | category="oauth_phone_blocked", stage="run_post_register_oauth_team" |
| 4 | `_cleanup_team_leftover("oauth_phone_blocked")` + `update_account(email, status=STATUS_AUTH_INVALID, auth_file=None)` | category="oauth_phone_blocked", stage="reinvite_account" |
| 5 | 转 HTTP 409 + body `{"error": "phone_required", "step": ..., "reason": ...}` + record_failure | category="oauth_phone_blocked", stage="api_login" |

### 5.3 通用 try/except 模板

```python
from autoteam.invite import RegisterBlocked

try:
    bundle = login_codex_via_browser(email, password, mail_client=mail_client, use_personal=...)
except RegisterBlocked as blocked:
    if blocked.is_phone:
        record_failure(
            email,
            category="oauth_phone_blocked",
            reason=f"OAuth 阶段触发 add-phone (step={blocked.step})",
            step=blocked.step,
            stage=<调用点名>,
            url=getattr(blocked, "url", None),
        )
        # 调用点特定处置(见 §5.2 矩阵)
        ...
        return None
    elif blocked.is_duplicate:
        # OAuth 阶段不应触发 duplicate(账号已注册成功);记 exception 兜底
        record_failure(email, category="exception", reason=f"OAuth 阶段意外 duplicate: {blocked.reason}")
        return None
    raise  # 其他 RegisterBlocked 子分类暂不可能,raise 让上层兜底
```

### 5.4 主号路径(不在范围)

- `login_codex_via_session` / `SessionCodexAuthFlow` / `MainCodexSyncFlow`(`codex_auth.py` 中段)用于主号 session 复用
- 主号通常已绑定手机,极罕见撞 add-phone
- **本 spec 不要求**给主号路径加探针(节省工作量);如未来需要,按 §4.2 模板独立扩展

---

## 6. 单元测试 fixture 与样本数据

### 6.1 mock 页面 fixture

```python
# tests/fixtures/oauth_phone_pages.py
from unittest.mock import MagicMock


def make_phone_page(url="https://auth.openai.com/add-phone",
                    body_text="please add your phone number to continue",
                    has_tel_input=True):
    page = MagicMock()
    page.url = url
    page.inner_text.return_value = body_text
    tel_input = MagicMock()
    tel_input.is_visible.return_value = has_tel_input
    page.locator.return_value.first = tel_input
    return page


def make_consent_page(url="https://auth.openai.com/authorize/consent",
                      body_text="continue with your team workspace"):
    return make_phone_page(url=url, body_text=body_text, has_tel_input=False)


def make_about_you_page(url="https://auth.openai.com/about-you",
                        body_text="tell us about yourself, full name, age"):
    return make_phone_page(url=url, body_text=body_text, has_tel_input=False)


# 误报测试:consent 页含 "phone number" 帮助文字 + footer tel input,但应判 False
def make_consent_with_phone_link():
    page = MagicMock()
    page.url = "https://auth.openai.com/authorize/consent"
    page.inner_text.return_value = "if you have issues, call our phone support number"
    tel_input = MagicMock()
    tel_input.is_visible.return_value = False  # 实际页上没有可见 tel input
    page.locator.return_value.first = tel_input
    return page
```

### 6.2 单测代码

```python
# tests/unit/test_oauth_phone_detection.py
import pytest
from autoteam.invite import RegisterBlocked, assert_not_blocked, detect_phone_verification
from tests.fixtures.oauth_phone_pages import (
    make_phone_page, make_consent_page, make_about_you_page, make_consent_with_phone_link,
)


def test_url_hint_strong_signal():
    """URL 含 add-phone → 立即判 True,不需要 tel input"""
    page = make_phone_page(url="https://auth.openai.com/verify-phone", body_text="", has_tel_input=False)
    assert detect_phone_verification(page) is True


def test_text_hint_requires_tel_input():
    """文本命中但无 tel input → 不阻塞(避免帮助文字误报)"""
    page = make_consent_with_phone_link()
    assert detect_phone_verification(page) is False


def test_text_hint_with_tel_input():
    """文本命中 + tel input 可见 → 判 True"""
    page = make_phone_page(
        url="https://auth.openai.com/authorize",
        body_text="add your phone number to continue",
        has_tel_input=True,
    )
    assert detect_phone_verification(page) is True


def test_assert_not_blocked_raises_on_phone():
    page = make_phone_page()
    with pytest.raises(RegisterBlocked) as exc_info:
        assert_not_blocked(page, "oauth_about_you")
    assert exc_info.value.is_phone is True
    assert exc_info.value.step == "oauth_about_you"


def test_assert_not_blocked_passes_on_consent():
    page = make_consent_page()
    assert_not_blocked(page, "oauth_consent_0")  # 不抛


def test_about_you_page_does_not_trigger():
    """about-you 页应当通过(避免在过早位点误报)"""
    page = make_about_you_page()
    assert detect_phone_verification(page) is False
```

### 6.3 集成测试样本

```python
# tests/integration/test_oauth_phone_blocked_flow.py
from unittest.mock import patch
from autoteam.invite import RegisterBlocked
from autoteam.manager import _run_post_register_oauth


def test_post_register_oauth_team_phone_blocked():
    """Team 模式 OAuth 命中 add-phone → 标 STATUS_AUTH_INVALID + record_failure"""
    with patch("autoteam.manager.login_codex_via_browser") as mock_login:
        mock_login.side_effect = RegisterBlocked("oauth_consent_2", "add-phone", is_phone=True)
        with patch("autoteam.manager.record_failure") as mock_rec:
            _run_post_register_oauth("test@example.com", "pwd", mail_client=None, leave_workspace=False)
            mock_rec.assert_called_with(
                "test@example.com",
                category="oauth_phone_blocked",
                reason=pytest.approx("OAuth 阶段触发 add-phone (step=oauth_consent_2)"),
                step="oauth_consent_2",
                stage="run_post_register_oauth_team",
                url=None,
            )


def test_reinvite_account_phone_blocked_kicks_team_leftover():
    """reinvite 命中 add-phone → _cleanup_team_leftover + STATUS_AUTH_INVALID(不进 STANDBY)"""
    # 略,见 spec-2-account-lifecycle.md §5
```

---

## 7. 不变量(Invariants)

- **I1**:`assert_not_blocked` 在 `login_codex_via_browser` 内被调用时,任何已有的 `try/except Exception` 块**禁止**吞掉 `RegisterBlocked`(必须显式 `except RegisterBlocked: raise`)
- **I2**:5 个 login_codex_via_browser 调用方**必须**显式 `except RegisterBlocked`,不允许让异常裸奔到 cmd_fill 等顶层
- **I3**:`record_failure` 中 `category="oauth_phone_blocked"` 的记录**必须**携带 `step` 和 `stage` 字段(用于事后统计)
- **I4**:add-phone 命中后,调用方**必须**先调 `_screenshot(page, f"codex_phone_blocked_{step}.png")`(在 raise 之前),否则页面 close 后无法回放
- **I5**:`detect_phone_verification` 不能改成"无 tel input 也判 True":会让 OAuth consent 页含 phone 帮助链接的场景大量误报
- **I6**:OAuth 流程的 RegisterBlocked 不重试 — 命中即放弃账号(用户硬要求"不要脱离原本流程太多");任何"撞了再试一次"的逻辑都属于范围外
- **I7**:`STATUS_PHONE_REQUIRED` 不新增到 `accounts.py`;复用 `STATUS_AUTH_INVALID` + `register_failures.category` 区分

---

## 附录 A:误报回放与运营校准

每次 `oauth_phone_blocked` 命中后:
1. 截图保存 `screenshots/codex_phone_blocked_{step}_{ts}.png`
2. `register_failures.json` 记录 `step` / `stage` / `url` / `email`
3. 运营定期(每周)抽样回放,确认未发生误报
4. 如发现误报集中在某 step:补充 `_OAUTH_PHONE_TEXT_HINTS` 排除规则,而非全局放宽

---

**文档结束。** 工程师据此可直接编写 4 处 `assert_not_blocked` 接入 + 5 处调用方 try/except + 单元/集成测试,无需额外决策。
