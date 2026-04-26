# SPEC-1: Mail Provider 全量化 实施规范

## 0. 元数据

| 字段       | 值                                                                                  |
| ---------- | ----------------------------------------------------------------------------------- |
| 版本       | v1.1 (2026-04-26 Round 7 P2 follow-up — MailProviderCard.vue 抽组件路径明确)         |
| 日期       | 2026-04-26                                                                          |
| 主笔       | prd-mail                                                                            |
| 对应 PRD   | `prompts/0426/prd/prd-1-mail-provider.md` v1.0 + `prompts/0426/prd/prd-6-p2-followup.md` v1.0 |
| 状态       | Draft → Ready-for-Implementation                                                    |
| 关联 spec  | (无依赖 PRD-2 shared spec;`spec/shared/account-state-machine.md` 仅作弱引用)        |

---

## 1. 文件级修改清单

| 文件路径                                              | 修改类型 | 行数预估 | 涉及函数/类                                                                  |
| ----------------------------------------------------- | -------- | -------- | ---------------------------------------------------------------------------- |
| `src/autoteam/api.py`                                 | 修改     | +180     | `SetupConfig`, `_AUTH_SKIP_PATHS`, `post_setup_save`, 新增 `post_mail_provider_probe`,重构 `put_register_domain_api` |
| `src/autoteam/mail/probe.py`                          | 新增     | ~250     | `probe_fingerprint()`, `probe_credentials()`, `probe_domain_ownership()`, `ProbeError`, `ProbeErrorCode` |
| `src/autoteam/mail/cf_temp_email.py`                  | 修改     | +12 / -4 | `login()` 嗅探条件, `create_temp_email()` 嗅探条件                          |
| `src/autoteam/mail/maillab.py`                        | 修改     | +60      | `_with_login_retry`(新增), `_get`/`_post`/`_delete`/`_put`(改造调用), `MaillabAuthFailed`(新异常类) |
| `src/autoteam/setup_wizard.py`                        | 修改     | +35 / -8 | `REQUIRED_CONFIGS`(扩 4 字段), `_sniff_provider_mismatch`(强阻断), `_verify_cloudmail`(嗅探前置 + 错配即返 False) |
| `.env.example`                                        | 修改     | +5 / -3  | `MAIL_PROVIDER` 注释强化, MAILLAB_* 取消注释                                |
| `web/src/components/SetupPage.vue`                    | 重写     | ~250     | 由平铺字段重构为 `<MailProviderCard>` + 4 卡片(**v1.0 阶段实施落到 inline,Round 7 P2.2 抽出**) |
| `web/src/components/MailProviderCard.vue`             | 新增     | ~280     | 3 步 wizard 状态机 — **Round 7 抽出共享组件**(v1.0 SPEC 列出但 v1.0 实施合并到 SetupPage/Settings.vue 内联,Round 7 PRD-6 FR-P2.2 抽出去重) |
| `web/src/components/Settings.vue`                     | 修改     | +60      | 新增「邮箱后端」区块(**Round 7 P2.2 改用 `<MailProviderCard mode="settings">`**) |
| `web/src/api.js`                                      | 修改     | +6       | 新增 `probeMailProvider(payload)`                                           |
| `tests/test_mail_cf_temp_email_sniff.py`              | 新增     | ~80      | 4 个嗅探用例                                                                |
| `tests/test_mail_maillab_self_heal.py`                | 新增     | ~120     | 401 自愈 + 递归守卫                                                         |
| `tests/test_mail_provider_probe.py`                   | 新增     | ~180     | 三步 probe + error_code 全覆盖                                              |
| `tests/test_mail_probe_helper.py`                     | 新增     | ~60      | `probe_domain_ownership` 共享 helper                                        |
| `tests/test_setup_wizard_sniff_block.py`              | 新增     | ~70      | `_verify_cloudmail` 错配阻断                                                |
| `tests/integration/test_setup_save_e2e.py`            | 新增     | ~110     | maillab 字段写盘 + 重载                                                     |
| `docs/getting-started.md`                             | 修改     | +50      | §1 / §2 / §2.5 章节                                                         |
| `docs/configuration.md`                               | 修改     | +80      | MAIL_PROVIDER 必填化 + 错配排查                                             |
| `docs/mail-provider-design.md`                        | 修改     | +120     | §6 收口 + §7 API 全表                                                       |
| `docs/troubleshooting.md`                             | 修改     | +60      | 邮箱后端 / 配置保存 401                                                     |
| `docs/api.md`                                         | 修改     | +90      | `/api/mail-provider/probe` 文档化                                           |

**总计**:21 个文件,约 +1900 行 / -15 行(以 SLOC 计;不含 vue 模板的 markup 行)。

---

## 2. 数据契约

### 2.1 Pydantic 模型完整定义

> 以下代码可直接粘贴入 `src/autoteam/api.py`(SetupConfig 在原位修改;ProbeRequest/Response/ErrorCode 新增)。

```python
# ===== 修改:src/autoteam/api.py L70 =====
from typing import Literal


class SetupConfig(BaseModel):
    """`/api/setup/save` 请求体。新增 5 个 mail 相关字段。"""

    # mail provider 选择(默认 cf_temp_email,REQUIRED_CONFIGS 与之对齐)
    MAIL_PROVIDER: Literal["cf_temp_email", "cloudflare_temp_email", "maillab"] = "cf_temp_email"
    # cf_temp_email 字段
    CLOUDMAIL_BASE_URL: str = ""
    CLOUDMAIL_EMAIL: str = ""               # 已废弃,保留兼容旧前端
    CLOUDMAIL_PASSWORD: str = ""
    CLOUDMAIL_DOMAIN: str = ""
    # maillab 字段(新增)
    MAILLAB_API_URL: str = ""
    MAILLAB_USERNAME: str = ""
    MAILLAB_PASSWORD: str = ""
    MAILLAB_DOMAIN: str = ""
    # 其他既有
    CPA_URL: str = "http://127.0.0.1:8317"
    CPA_KEY: str = ""
    PLAYWRIGHT_PROXY_URL: str = ""
    PLAYWRIGHT_PROXY_BYPASS: str = ""
    API_KEY: str = ""


class ProbeErrorCode(str, Enum):
    ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"
    PROVIDER_MISMATCH = "PROVIDER_MISMATCH"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN_DOMAIN = "FORBIDDEN_DOMAIN"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    NETWORK = "NETWORK"
    TIMEOUT = "TIMEOUT"
    EMPTY_DOMAIN_LIST = "EMPTY_DOMAIN_LIST"
    UNKNOWN = "UNKNOWN"


class MailProviderProbeRequest(BaseModel):
    provider: Literal["cf_temp_email", "maillab"]
    step: Literal["fingerprint", "credentials", "domain_ownership"]
    base_url: str = Field(..., min_length=1, max_length=512)
    admin_password: str = ""              # cf_temp_email 用
    username: str = ""                    # maillab 用
    password: str = ""                    # maillab 用
    domain: str = ""                      # step=domain_ownership 必填
    bearer_token: str = ""                # 跨步 token 中转;后端不持久化

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return v

    @field_validator("domain")
    @classmethod
    def _normalize_domain(cls, v: str) -> str:
        v = (v or "").strip().lstrip("@")
        return v


class MailProviderProbeResponse(BaseModel):
    ok: bool
    step: Literal["fingerprint", "credentials", "domain_ownership"]
    error_code: ProbeErrorCode | None = None
    message: str | None = None
    hint: str | None = None
    warnings: list[str] = []
    # fingerprint
    detected_provider: Literal["cf_temp_email", "maillab", "unknown"] | None = None
    domain_list: list[str] | None = None
    add_verify_open: bool | None = None
    register_verify_open: bool | None = None
    # credentials
    is_admin: bool | None = None
    user_email: str | None = None
    token_preview: str | None = None
    # domain_ownership
    probe_email: str | None = None
    probe_account_id: int | None = None
    cleaned: bool | None = None
    leaked_probe: dict | None = None
```

### 2.2 .env 字段全表

| Key                       | 当前 .env.example 状态 | 新状态                   | 必填条件                     |
| ------------------------- | ---------------------- | ------------------------ | ---------------------------- |
| `MAIL_PROVIDER`           | 已存在,值 `cf_temp_email` | 注释强化:**强烈推荐显式设置** | 始终必填(默认 cf_temp_email) |
| `CLOUDMAIL_BASE_URL`      | 已存在                 | 注释微调                 | provider=cf_temp_email 时必填 |
| `CLOUDMAIL_PASSWORD`      | 已存在                 | 不变                     | 同上                         |
| `CLOUDMAIL_DOMAIN`        | 已存在                 | 不变                     | 同上                         |
| `CLOUDMAIL_EMAIL`         | 已存在,空值           | 保持(已废弃)            | 否                           |
| `MAILLAB_API_URL`         | 注释状态               | **取消注释,空值**         | provider=maillab 时必填      |
| `MAILLAB_USERNAME`        | 注释状态               | 取消注释,空值            | 同上                         |
| `MAILLAB_PASSWORD`        | 注释状态               | 取消注释,空值            | 同上                         |
| `MAILLAB_DOMAIN`          | 注释状态               | 取消注释,空值            | 否(缺省回落 CLOUDMAIL_DOMAIN) |

### 2.3 accounts.json / runtime_config.json 影响

**无影响**。`runtime_config.json.register_domain` 字段不变,继续被 `cf_temp_email.create_temp_email` 与 `maillab.create_temp_email` 同时读取(优先级 `显式参数 > runtime_config > 环境变量`),已在 round-2 实现并验证。

---

## 3. 函数签名规范

### 3.1 新建 `src/autoteam/mail/probe.py`

> 公开 5 个对象:3 个 helper 函数 + 1 个异常类 + 1 个 dataclass。

```python
"""mail/probe.py — `/api/mail-provider/probe` 与 `register-domain` 共享 helper。

每个 step 都有同名 helper,api.py 端点直接调用并把返回值封装为 ProbeResponse;
register-domain 复用 `probe_domain_ownership`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import requests

from autoteam.mail.base import decode_jwt_payload

PROBE_TIMEOUT = 5  # 秒;每个 HTTP 调用上限


@dataclass
class ProbeResult:
    """通用结果。step=fingerprint/credentials/domain_ownership 共用,字段按 step 填。"""

    ok: bool
    step: Literal["fingerprint", "credentials", "domain_ownership"]
    error_code: str | None = None
    message: str | None = None
    hint: str | None = None
    warnings: list[str] | None = None
    # fingerprint
    detected_provider: Literal["cf_temp_email", "maillab", "unknown"] | None = None
    domain_list: list[str] | None = None
    add_verify_open: bool | None = None
    register_verify_open: bool | None = None
    # credentials
    is_admin: bool | None = None
    user_email: str | None = None
    token_preview: str | None = None
    bearer_token: str | None = None  # 内部传给下一步 helper,api 端点不返回给前端
    # domain_ownership
    probe_email: str | None = None
    probe_account_id: int | None = None
    cleaned: bool | None = None
    leaked_probe: dict | None = None


def probe_fingerprint(base_url: str, expected_provider: str) -> ProbeResult:
    """Step 1:无凭据指纹探测。

    依次探测:
      - GET {base_url}/setting/websiteConfig (maillab 独有,且通常含 domainList)
      - GET {base_url}/admin/address         (cf_temp_email 独有,401/403/200)

    返回 detected_provider:
      - "maillab"          → /setting/websiteConfig 返回 200 + dict
      - "cf_temp_email"    → /admin/address 返回 200 (含 results) 或 401/403
      - "unknown"          → 两个都不像

    与 expected_provider 不一致时,error_code=PROVIDER_MISMATCH。
    """


def probe_credentials(
    base_url: str,
    provider: Literal["cf_temp_email", "maillab"],
    *,
    username: str = "",
    password: str = "",
    admin_password: str = "",
) -> ProbeResult:
    """Step 2:凭据校验。

    cf_temp_email:GET {base_url}/admin/address with `x-admin-auth: <admin_password>` header
    maillab:POST {base_url}/login body `{email, password}`,返回 JWT

    返回 token_preview(前 10 字符)与 is_admin(maillab 解析 JWT payload `userType==1`)。
    bearer_token 字段供下游 step 用。
    """


def probe_domain_ownership(
    base_url: str,
    provider: Literal["cf_temp_email", "maillab"],
    *,
    bearer_token: str = "",
    admin_password: str = "",
    domain: str,
) -> ProbeResult:
    """Step 3:域名归属验证。

    通用流程:
      1. 在该 domain 下创建一个 probe-{ts} 邮箱
      2. 立即删除回收(failed 时填 leaked_probe)

    cf_temp_email:用 admin_password 直接调 /admin/new_address + /admin/delete_address
    maillab:用 bearer_token 调 /account/add + /account/delete
    """


class ProbeError(Exception):
    """probe.* helper 内部抛错;api 层捕获后转成 ProbeResult(ok=False)。"""

    def __init__(self, error_code: str, message: str, hint: str | None = None):
        self.error_code = error_code
        self.message = message
        self.hint = hint
        super().__init__(message)
```

**异常映射规则**(probe 内部统一捕获):

| 触发                                      | error_code           |
| ----------------------------------------- | -------------------- |
| `requests.ConnectionError`                | `NETWORK`            |
| `requests.Timeout` 或 timeout=5 触发      | `TIMEOUT`            |
| HTTP 404 + `/setting/websiteConfig` 不存在 + `/admin/address` 不存在 | `ROUTE_NOT_FOUND`    |
| detected_provider != expected_provider    | `PROVIDER_MISMATCH`  |
| HTTP 401 / cf_temp_email admin 401        | `UNAUTHORIZED`       |
| maillab `code:401` 在 step=credentials    | `UNAUTHORIZED`       |
| maillab `code:403` 或 message 含 `domain` | `FORBIDDEN_DOMAIN`   |
| `addVerifyOpen` 或 `registerVerifyOpen` true 在 credentials 调用前 | `CAPTCHA_REQUIRED`(warning,不阻断) |
| domainList 为空数组                       | `EMPTY_DOMAIN_LIST`  |
| 兜底                                      | `UNKNOWN`            |

### 3.2 cf_temp_email.login() 嗅探收紧

**改前** (`mail/cf_temp_email.py:106-112`):

```python
if isinstance(payload, dict) and "results" not in payload and ("code" in payload or "data" in payload):
    raise Exception(...)
```

**改后**:

```python
# 正向白名单:cf_temp_email 的 /admin/address 必须返回含 `results` 字段的 dict;
# 任何缺 results 的 dict(空 {}、maillab 风格 {code,...}、null 等)都视为协议错配。
if not isinstance(payload, dict) or "results" not in payload:
    raise Exception(
        "CloudMail 登录响应不像 dreamhunter2333/cloudflare_temp_email"
        f"(响应 {type(payload).__name__} 不含 `results` 字段)。"
        f"你的 CLOUDMAIL_BASE_URL={self.base_url} 可能指向 maillab/cloud-mail 服务器或路由错。"
        "请确认 .env 中 MAIL_PROVIDER=cf_temp_email 时填的是 dreamhunter2333 后端;"
        "如果你部署的是 maillab,请改 MAIL_PROVIDER=maillab。"
    )
```

**create_temp_email** (L154) 同步:

```python
# 改前
if isinstance(data, dict) and "address" not in data and ("code" in data and "message" in data):

# 改后(任何缺 address 字段就报错)
if not isinstance(data, dict) or "address" not in data:
    raise Exception(
        f"创建邮箱响应不像 cf_temp_email(收到 {data!r})。"
        "请检查 MAIL_PROVIDER 与 base_url 是否对应。"
    )
```

### 3.3 maillab.py 401 自愈

**新增类成员 + 装饰器**:

```python
# ===== mail/maillab.py 顶部 =====
import threading
import functools

class MaillabAuthFailed(Exception):
    """maillab 401 自愈失败 — 重 login 后仍 401。业务层不再重试。"""


_LOGIN_GUARD = threading.local()


def _with_login_retry(method):
    """装饰 _get/_post/_delete/_put;若响应 code=401,自动 re-login + 重试一次。

    通过 thread-local guard `_LOGIN_GUARD.in_login` 阻止 login() 自身触发递归。
    """
    @functools.wraps(method)
    def wrapper(self, path, *args, **kwargs):
        resp = method(self, path, *args, **kwargs)
        if isinstance(resp, dict) and resp.get("code") == 401:
            if getattr(_LOGIN_GUARD, "in_login", False):
                # 我们在 login() 内部,不要递归
                return resp
            if getattr(_LOGIN_GUARD, "retried", False):
                # 上一次重试还是 401 → 抛
                raise MaillabAuthFailed(
                    f"maillab {path}: 重 login 后仍 401,请检查 MAILLAB_USERNAME/PASSWORD"
                )
            logger.warning("[maillab] %s 收到 code:401,自愈中...", path)
            self.token = None
            try:
                _LOGIN_GUARD.retried = True
                self.login()  # login 内部 _post 会被 _LOGIN_GUARD.in_login 标记保护
                resp = method(self, path, *args, **kwargs)
                logger.info("[maillab] %s token 已自愈", path)
            finally:
                _LOGIN_GUARD.retried = False
        return resp
    return wrapper
```

**改造 4 个底层方法**(改前/改后):

```python
# 改前
def _get(self, path: str, params: dict | None = None) -> dict:
    r = self.session.get(self._url(path), headers=self._headers(), params=params, timeout=30)
    return self._parse_response(r, path)

# 改后
@_with_login_retry
def _get(self, path: str, params: dict | None = None) -> dict:
    r = self.session.get(self._url(path), headers=self._headers(), params=params, timeout=30)
    return self._parse_response(r, path)
```

`_post`/`_delete`/`_put` 同样加 `@_with_login_retry`。

**login() 加守卫**:

```python
def login(self) -> str:
    _LOGIN_GUARD.in_login = True
    try:
        # ... 原有逻辑 ...
        return token
    finally:
        _LOGIN_GUARD.in_login = False
```

### 3.4 setup_wizard 嗅探强阻断

**`_sniff_provider_mismatch` 改造**(`setup_wizard.py:173`):

```python
def _sniff_provider_mismatch(provider: str) -> tuple[bool, str]:
    """探测 base_url 与 MAIL_PROVIDER 的匹配性。

    返回 (matched, reason):
      - matched=True:可继续 login
      - matched=False:reason 描述错配,_verify_cloudmail 直接 return False

    探测策略升级:
      - cf_temp_email 期望 → /admin/address 返 200 含 results / 401 / 403
                              + /setting/websiteConfig 返 404
      - maillab 期望         → /setting/websiteConfig 返 200 含 domainList
                              + /admin/address 返 404 或 maillab 风格 {code,...}
    """
    # ... 实施细节略;关键:增加 GET /setting/websiteConfig 探测
```

**`_verify_cloudmail` 流程调整**:

```python
def _verify_cloudmail():
    provider = (os.environ.get("MAIL_PROVIDER") or "cf_temp_email").strip().lower()
    # ... 原有 base_url/password/domain 完备性检查 ...

    # ★ 新位置:嗅探在所有 client 实例化之前
    if os.environ.get("AUTOTEAM_SKIP_PROVIDER_SNIFF") != "1":
        matched, reason = _sniff_provider_mismatch(provider)
        if not matched:
            logger.error("[验证] 协议错配: %s", reason)
            return False  # ← 强阻断,不再 warning

    # ... 后续 client.login() / create_temp_email() 不变 ...
```

**REQUIRED_CONFIGS 扩**(`setup_wizard.py:20`):

```python
REQUIRED_CONFIGS = [
    ("MAIL_PROVIDER", "Mail Provider(cf_temp_email | maillab)", "cf_temp_email", True),
    ("CLOUDMAIL_BASE_URL", "CloudMail API 地址(cf_temp_email)", "", False),
    ("CLOUDMAIL_PASSWORD", "CloudMail 管理员密码(cf_temp_email)", "", False),
    ("CLOUDMAIL_DOMAIN", "邮箱域名(如 @example.com)", "", False),
    # ★ 新增 4 字段(maillab 用;optional=True 由前端按 provider 切换可见性)
    ("MAILLAB_API_URL", "Maillab API 地址", "", True),
    ("MAILLAB_USERNAME", "Maillab 管理员邮箱", "", True),
    ("MAILLAB_PASSWORD", "Maillab 管理员密码", "", True),
    ("MAILLAB_DOMAIN", "Maillab 邮箱域名(缺省回落 CLOUDMAIL_DOMAIN)", "", True),
    # ... 其余不变
]
```

**注**:`/api/setup/status` 端点会按 provider 动态计算 `optional`,见 §3.6。

### 3.5 api.py 新增端点 + 鉴权策略

**`_AUTH_SKIP_PATHS` 扩展**(`api.py:31`):

```python
_AUTH_SKIP_PATHS = {
    "/api/auth/check",
    "/api/setup/status",
    "/api/setup/save",
    "/api/mail-provider/probe",  # ← 新增;条件鉴权见下
}
```

**条件鉴权说明**:`auth_middleware` 已自动跳过 `_AUTH_SKIP_PATHS` 中的路径。但 `/api/mail-provider/probe` 在面板初始化后(API_KEY 已配置)需强制鉴权,通过手工二次校验:

```python
@app.post("/api/mail-provider/probe", response_model=MailProviderProbeResponse)
def post_mail_provider_probe(req: MailProviderProbeRequest, request: Request):
    # 条件鉴权:API_KEY 已配置 → 强制 Bearer
    from autoteam.config import API_KEY as _key
    if _key:
        auth_header = request.headers.get("authorization", "")
        if not (auth_header.startswith("Bearer ") and auth_header[7:] == _key):
            raise HTTPException(status_code=401, detail="API_KEY 已配置,请提供 Bearer token")
    # 速率限制:setup 阶段(无 API_KEY)单 IP 60/min;有 API_KEY 时不限
    if not _key:
        _enforce_probe_rate_limit(request)

    # 分发
    from autoteam.mail import probe as mail_probe

    try:
        if req.step == "fingerprint":
            result = mail_probe.probe_fingerprint(req.base_url, req.provider)
        elif req.step == "credentials":
            result = mail_probe.probe_credentials(
                req.base_url, req.provider,
                username=req.username, password=req.password,
                admin_password=req.admin_password,
            )
        elif req.step == "domain_ownership":
            result = mail_probe.probe_domain_ownership(
                req.base_url, req.provider,
                bearer_token=req.bearer_token,
                admin_password=req.admin_password,
                domain=req.domain,
            )
    except mail_probe.ProbeError as exc:
        return MailProviderProbeResponse(
            ok=False, step=req.step,
            error_code=ProbeErrorCode(exc.error_code),
            message=exc.message, hint=exc.hint,
        )
    # ProbeResult → ProbeResponse(忽略 bearer_token,前端用上一步的返回)
    return MailProviderProbeResponse(**{k: v for k, v in vars(result).items() if k != "bearer_token"})
```

**`_enforce_probe_rate_limit`**(简单内存计数,生产可换 redis):

```python
_probe_rate_buckets: dict[str, list[float]] = {}
_probe_rate_lock = threading.Lock()

def _enforce_probe_rate_limit(request: Request, max_per_min: int = 60):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    with _probe_rate_lock:
        bucket = _probe_rate_buckets.setdefault(ip, [])
        bucket[:] = [t for t in bucket if now - t < 60]
        if len(bucket) >= max_per_min:
            raise HTTPException(status_code=429, detail="probe 请求过频,请稍后再试")
        bucket.append(now)
```

### 3.6 api.py /api/setup/save 新字段写盘

**`post_setup_save` 改造**(`api.py:99`):

```python
@app.post("/api/setup/save")
def post_setup_save(config: SetupConfig):
    import secrets as _secrets
    from autoteam.setup_wizard import REQUIRED_CONFIGS, _write_env

    data = config.model_dump()
    defaults = {key: default for key, _, default, _ in REQUIRED_CONFIGS}

    if not data.get("CPA_URL"):
        data["CPA_URL"] = defaults.get("CPA_URL", "http://127.0.0.1:8317")
    if not data.get("API_KEY"):
        data["API_KEY"] = _secrets.token_urlsafe(24)

    # ★ 按 provider 选择性写入:cf_temp_email 时不写 MAILLAB_*,反之亦然
    provider = data.get("MAIL_PROVIDER", "cf_temp_email")
    if provider in ("cf_temp_email", "cloudflare_temp_email"):
        skip_keys = {"MAILLAB_API_URL", "MAILLAB_USERNAME", "MAILLAB_PASSWORD", "MAILLAB_DOMAIN"}
    elif provider == "maillab":
        skip_keys = {"CLOUDMAIL_BASE_URL", "CLOUDMAIL_PASSWORD"}  # CLOUDMAIL_DOMAIN 仍可作回落,保留
    else:
        skip_keys = set()

    clearable_fields = {"PLAYWRIGHT_PROXY_URL", "PLAYWRIGHT_PROXY_BYPASS"}
    for key, value in data.items():
        if key in skip_keys:
            continue
        if value or key in clearable_fields:
            _write_env(key, value)
            os.environ[key] = value

    # ... 后续重载与 _verify_cloudmail 不变
```

**`/api/setup/status` 按 provider 标 optional**:

```python
@app.get("/api/setup/status")
def get_setup_status():
    from autoteam.setup_wizard import REQUIRED_CONFIGS, _read_env

    env = _read_env()
    provider = env.get("MAIL_PROVIDER") or os.environ.get("MAIL_PROVIDER", "cf_temp_email")

    cf_keys = {"CLOUDMAIL_BASE_URL", "CLOUDMAIL_PASSWORD", "CLOUDMAIL_DOMAIN"}
    ml_keys = {"MAILLAB_API_URL", "MAILLAB_USERNAME", "MAILLAB_PASSWORD", "MAILLAB_DOMAIN"}

    fields = []
    all_ok = True
    for key, prompt, default, optional in REQUIRED_CONFIGS:
        # 动态 optional:不属于当前 provider 的字段强制 optional=True
        if provider == "maillab" and key in cf_keys:
            optional = True
        elif provider in ("cf_temp_email", "cloudflare_temp_email") and key in ml_keys:
            optional = True
        val = env.get(key, "") or os.environ.get(key, "")
        ok = bool(val)
        if not ok and not optional:
            all_ok = False
        fields.append({"key": key, "prompt": prompt, "default": default, "optional": optional, "configured": ok})
    return {"configured": all_ok, "fields": fields, "provider": provider}
```

---

## 4. 前端契约

### 4.1 SetupPage 4 步状态机

```
                   ┌──────────────────┐
                   │ State.PROVIDER   │  用户选 cf_temp_email / maillab
                   └────────┬─────────┘
                            │ provider 选定
                            ▼
                   ┌──────────────────┐
                   │ State.CONNECTION │  填 base_url / 凭据,点击「测试连接」
                   │   probe.fingerprint
                   │   probe.credentials
                   └────────┬─────────┘
                            │ ok=true 进入下一步
                  失败──────│
                  显示       ▼
                  err     ┌──────────────────┐
                  +hint   │ State.DOMAIN     │  下拉/输入 domain,点击「验证归属」
                  灰显     │   probe.domain_ownership
                  下游     └────────┬─────────┘
                                    │ ok=true
                                    ▼
                           ┌──────────────────┐
                           │ State.SAVE       │  填 CPA / API_KEY / Proxy,点保存
                           │   /api/setup/save
                           └──────────────────┘
```

**状态转移**:任何一步失败时,State 不前进,但允许用户**修改前一步字段后重新点该步按钮**。State.PROVIDER 切换 provider 时,后续状态全部重置。

### 4.2 axios/fetch 调用契约

**新增 `web/src/api.js` 方法**:

```javascript
probeMailProvider: (payload) => request('POST', '/mail-provider/probe', payload),
```

**前端调用示例**(MailProviderCard.vue):

```javascript
async function testConnection() {
  const fp = await api.probeMailProvider({
    provider: form.provider,
    step: 'fingerprint',
    base_url: form.baseUrl,
  })
  if (!fp.ok) { showError(fp); return }
  domainList.value = fp.domain_list || []
  warnings.value = fp.warnings || []

  const cred = await api.probeMailProvider({
    provider: form.provider,
    step: 'credentials',
    base_url: form.baseUrl,
    username: form.username,
    password: form.password,
    admin_password: form.adminPassword,
  })
  if (!cred.ok) { showError(cred); return }
  state.value = 'DOMAIN'
  bearerToken.value = cred.token_preview ? null : null  // 前端实际只缓存上一步 cred 通过的 flag
}

async function verifyDomain() {
  const own = await api.probeMailProvider({
    provider: form.provider,
    step: 'domain_ownership',
    base_url: form.baseUrl,
    bearer_token: '',  // 后端再次走 login(短期 ttl 不缓存),实现简单
    admin_password: form.adminPassword,
    domain: form.domain,
  })
  if (!own.ok) { showError(own); return }
  state.value = 'SAVE'
}
```

> 注:为了让后端无状态,前端**不持有 jwt**,step=domain_ownership 时后端**重新调一次 login**(maillab 单次 login 成本低)。这把 OQ-1 决策改为「后端在 step=domain_ownership 内部 re-login」,比前端缓存 token 更简单且更安全。

### 4.3 错误展示规则表

| error_code           | 用户文案                                            | hint                                                |
| -------------------- | --------------------------------------------------- | --------------------------------------------------- |
| `ROUTE_NOT_FOUND`    | base_url 路由不可达                                 | 检查地址拼写,maillab 通常无 `/api` 前缀,cf_temp_email 通常带 `/api` |
| `PROVIDER_MISMATCH`  | 选择的 provider 与服务器类型不一致                  | 把 provider 改成「{detected_provider}」              |
| `UNAUTHORIZED`       | 凭据错误                                            | 检查用户名 / 密码                                   |
| `FORBIDDEN_DOMAIN`   | 该 domain 无写权限或不在白名单                      | 联系 maillab 管理员把该 domain 加入                  |
| `CAPTCHA_REQUIRED`   | 服务端启用了 Turnstile / addVerify                  | 到 maillab 后台 → 设置 → 安全 关闭 captcha          |
| `NETWORK`            | 网络不可达                                          | 检查 base_url、防火墙、代理                          |
| `TIMEOUT`            | 服务器响应超时(>5s)                               | 检查 base_url 可达性                                 |
| `EMPTY_DOMAIN_LIST`  | 该实例未配置任何邮箱域名                            | 到 maillab 后台 → 设置 → 域名 配置                  |
| `UNKNOWN`            | 未知错误,见 message                                 | 复制 message 反馈                                    |

---

## 5. 测试用例

### 5.1 单元测试清单

#### `tests/test_mail_cf_temp_email_sniff.py`

```python
import pytest
import responses
from autoteam.mail.cf_temp_email import CfTempEmailClient

@responses.activate
@pytest.mark.parametrize("body, should_raise", [
    ({"results": []}, False),                            # cf 正常空列表
    ({"results": [{"id": 1}]}, False),                   # cf 正常有数据
    ({}, True),                                          # 空 dict — round-3 漏判,本次必须捕获
    ({"code": 401, "message": "auth"}, True),            # maillab 风格
    ({"code": 200, "data": {}}, True),                   # maillab 风格 200
    (None, True),                                        # 非 dict
])
def test_login_sniff(body, should_raise, monkeypatch):
    monkeypatch.setenv("CLOUDMAIL_BASE_URL", "https://example.com/api")
    monkeypatch.setenv("CLOUDMAIL_PASSWORD", "secret")
    responses.add(responses.GET, "https://example.com/api/admin/address",
                  json=body, status=200)
    client = CfTempEmailClient()
    if should_raise:
        with pytest.raises(Exception, match="不像.*cloudflare_temp_email"):
            client.login()
    else:
        client.login()  # 应不抛
```

#### `tests/test_mail_maillab_self_heal.py`

```python
@responses.activate
def test_401_self_heal(monkeypatch):
    monkeypatch.setenv("MAILLAB_API_URL", "https://m.example.com")
    monkeypatch.setenv("MAILLAB_USERNAME", "admin@x.com")
    monkeypatch.setenv("MAILLAB_PASSWORD", "p")
    # 第 1 次 list 返回 401
    responses.add(responses.GET, "https://m.example.com/account/list",
                  json={"code": 401, "message": "expired"}, status=200)
    # 重 login 成功
    responses.add(responses.POST, "https://m.example.com/login",
                  json={"code": 200, "data": {"token": "new-jwt"}}, status=200)
    # 第 2 次 list 成功
    responses.add(responses.GET, "https://m.example.com/account/list",
                  json={"code": 200, "data": []}, status=200)
    from autoteam.mail.maillab import MaillabClient
    client = MaillabClient()
    client.token = "stale-jwt"
    rows = client.list_accounts()
    assert rows == []
    # 验证调用顺序:list → login → list
    assert [c.request.url for c in responses.calls] == [
        "https://m.example.com/account/list?size=30",
        "https://m.example.com/login",
        "https://m.example.com/account/list?size=30",
    ]


@responses.activate
def test_401_repeated_raises(monkeypatch):
    """重 login 后仍 401 → 抛 MaillabAuthFailed,不无限循环。"""
    # ... 类似上面,但第 2 次 list 仍 401
    from autoteam.mail.maillab import MaillabAuthFailed
    with pytest.raises(MaillabAuthFailed):
        client.list_accounts()


def test_login_internal_no_recursion(monkeypatch):
    """login() 内部 _post 即便回 401,也不应触发外层 _with_login_retry,避免无限递归。"""
    # mock /login 第一次回 {code:401},触发 wrapper 时 _LOGIN_GUARD.in_login=True 应阻止重试
    # 期望:client.login() 直接抛 "maillab 登录失败",而不是循环调 login
```

#### `tests/test_mail_provider_probe.py`

```python
@responses.activate
def test_fingerprint_detect_maillab():
    responses.add(responses.GET, "https://m.example.com/setting/websiteConfig",
                  json={"domainList": ["@a.com", "@b.com"], "addVerifyOpen": False, "registerVerifyOpen": False},
                  status=200)
    from autoteam.mail.probe import probe_fingerprint
    result = probe_fingerprint("https://m.example.com", "maillab")
    assert result.ok
    assert result.detected_provider == "maillab"
    assert result.domain_list == ["@a.com", "@b.com"]


@responses.activate
def test_fingerprint_provider_mismatch():
    """base_url 是 maillab,但 provider 选了 cf_temp_email"""
    responses.add(responses.GET, "https://m.example.com/setting/websiteConfig",
                  json={"domainList": ["@a.com"]}, status=200)
    result = probe_fingerprint("https://m.example.com", "cf_temp_email")
    assert not result.ok
    assert result.error_code == "PROVIDER_MISMATCH"
    assert result.detected_provider == "maillab"


def test_credentials_maillab_admin():
    # mock /login 返回带 userType=1 的 JWT
    # 断言 is_admin=True


def test_domain_ownership_forbidden():
    # mock /account/add 返 {code:403, message:"domain not allowed"}
    # 断言 error_code=FORBIDDEN_DOMAIN
```

#### `tests/test_setup_wizard_sniff_block.py`

```python
def test_verify_cloudmail_aborts_on_mismatch(monkeypatch, caplog):
    monkeypatch.setenv("MAIL_PROVIDER", "cf_temp_email")
    monkeypatch.setenv("CLOUDMAIL_BASE_URL", "https://m.example.com")  # maillab 服务器
    monkeypatch.setenv("CLOUDMAIL_PASSWORD", "x")
    monkeypatch.setenv("CLOUDMAIL_DOMAIN", "@a.com")
    # mock /setting/websiteConfig 返 200(maillab 指纹)、/admin/address 返 404
    # ...
    from autoteam.setup_wizard import _verify_cloudmail
    assert _verify_cloudmail() is False
    assert "协议错配" in caplog.text
    # 关键:CloudMailClient 不应被实例化
```

### 5.2 集成测试

```python
def test_setup_save_writes_maillab_fields(client, tmp_env):
    payload = {
        "MAIL_PROVIDER": "maillab",
        "MAILLAB_API_URL": "https://m.example.com",
        "MAILLAB_USERNAME": "admin@x.com",
        "MAILLAB_PASSWORD": "p",
        "MAILLAB_DOMAIN": "@x.com",
        "CLOUDMAIL_DOMAIN": "@x.com",  # 回落用
        "CPA_URL": "http://127.0.0.1:8317",
        "CPA_KEY": "k",
        "API_KEY": "test-key",
    }
    # mock _verify_cloudmail/_verify_cpa 返 True
    resp = client.post("/api/setup/save", json=payload)
    assert resp.status_code == 200
    env = read_env_file()
    assert env["MAIL_PROVIDER"] == "maillab"
    assert env["MAILLAB_API_URL"] == "https://m.example.com"
    # cf_temp_email 字段被跳过
    assert "CLOUDMAIL_BASE_URL" not in env or env["CLOUDMAIL_BASE_URL"] == ""
```

### 5.3 回归测试(避免破坏 cf_temp_email 用户)

```python
def test_existing_cf_user_not_broken(monkeypatch, mock_cf_server):
    """老用户不改 .env(无 MAIL_PROVIDER 字段)启动,行为应与之前完全一致。"""
    # 不设 MAIL_PROVIDER → 默认 cf_temp_email
    # mock_cf_server 返合法 results
    from autoteam.setup_wizard import _verify_cloudmail
    assert _verify_cloudmail() is True
```

### 5.4 测试数据(mock 样本)

**maillab `/setting/websiteConfig` 完整响应**:

```json
{
  "register": true,
  "title": "SkyMail",
  "manyEmail": true,
  "addEmail": true,
  "autoRefresh": 30,
  "addEmailVerify": false,
  "registerVerify": false,
  "send": false,
  "domainList": ["@example.com", "@another.com"],
  "siteKey": "0x4...",
  "regKey": "0x4...",
  "r2Domain": "https://r2.example.com",
  "background": "",
  "loginOpacity": 0.8,
  "regVerifyOpen": false,
  "addVerifyOpen": false,
  "noticeTitle": "",
  "noticeContent": "",
  "linuxdoSwitch": false,
  "minEmailPrefix": 3,
  "projectLink": "https://skymail.ink"
}
```

**maillab `/login` 成功响应**:

```json
{"code": 200, "data": {"token": "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VySWQiOjEsInVzZXJUeXBlIjoxLCJlbWFpbCI6ImFkbWluQHguY29tIn0.sig"}}
```

**maillab `/account/add` 403**:

```json
{"code": 403, "message": "domain @forbidden.com not in white list"}
```

**cf_temp_email `/admin/address` 正常**:

```json
{"results": [{"id": 1, "name": "test@example.com"}], "count": 1}
```

---

## 6. 文档落地清单(章节级 diff 大纲)

### `docs/getting-started.md`
- §1 「准备工作 / 1. 搭建临时邮箱」:用 admonition 强调「先决定后端,再决定填哪一组配置」
- §2 「第二步:配置 / 直接部署」:增补 maillab 完整字段示例;明确 SetupPage 已支持 provider 选择
- 新增 §2.5 「邮箱后端归属验证」:截图(可后补)+ 4 步流程文字说明

### `docs/configuration.md`
- 「`.env` 配置项」表:`MAIL_PROVIDER` 必填列由 `否` 改 `是(默认 cf_temp_email)`;MAILLAB_* 4 字段 「Web 面板可填」标记
- 「Mail Provider 切换」:**推荐顺序**改为先讲 maillab(国内 + skymail 一键部署),后讲 cf_temp_email
- 新增 「邮箱归属验证」:`/setting/websiteConfig.domainList` + addVerify 边界
- 「⚠️ 协议错配排查」:把 issue#1 截图 + 修复路径(选 provider → 测试连接)显式列

### `docs/mail-provider-design.md`
- §6 「未知项 5 项」状态全部改 ✅(login JWT header / domain 列表 API / createTime / 创建端点 / accountId 类型),引用 GitHub 源码 url
- 新增 §7 「skymail.ink API 全表」:从 issue-1-cloudmail.md §B 整理入档
- 新增 §8 「401 自愈策略」:本 SPEC §3.3 设计入档

### `docs/troubleshooting.md`
- 「Web 面板相关」:加「配置保存后 401 创建邮箱失败」 → 链接 issue#1 修复
- 新增 「邮箱后端」 章节:5 个常见错配场景 + 对应 error_code

### `docs/api.md`
- 「初始配置 API」:文档化 `/api/mail-provider/probe`(请求/响应 schema、3 个 step 示例、错误码表)
- 「域名管理 API」:`/api/config/register-domain` 添加「内部使用 `probe_domain_ownership` helper」说明

### `.env.example`
- L1-5:MAIL_PROVIDER 上方加大段注释「**强烈推荐显式设置**;不设默认 cf_temp_email,maillab 用户会撞 issue#1 错配」
- MAILLAB_* 4 行取消 `#` 注释

---

## 7. 实施顺序与依赖

### DAG

```
                ┌─────────────────────────────────────────┐
                │ Phase 1 (后端基础,无 UI 风险)            │
                │                                         │
                │  ST-101 SetupConfig 扩字段              │
                │     │                                   │
                │     ├─ ST-104 嗅探强阻断 ─┐             │
                │     │                     │             │
                │  ST-102 cf 嗅探收紧 ──────┤             │
                │  ST-103 maillab 401 自愈 ─┤             │
                │     │                     │             │
                │  ST-105 .env.example ────┤              │
                │     │                     │             │
                │  ST-107 单元测试 ─────────┘             │
                │     │                                   │
                │  ST-106 文档(部分)                     │
                └─────────────────────────────────────────┘
                            │
                            ▼
                ┌─────────────────────────────────────────┐
                │ Phase 2 (probe 端点 + UI)                │
                │                                         │
                │  ST-201 mail/probe.py ─┐                │
                │     │                  │                │
                │     ▼                  │                │
                │  ST-202 /api/mail-provider/probe        │
                │     │                  │                │
                │  ST-203 register-domain 复用 ←┘         │
                │     │                                   │
                │  ST-204 MailProviderCard.vue            │
                │  ST-205 SetupPage 重构                  │
                │  ST-206 api.js 方法                     │
                │     │                                   │
                │  ST-207 集成 + E2E                      │
                └─────────────────────────────────────────┘
                            │
                            ▼
                ┌─────────────────────────────────────────┐
                │ Phase 3 (Settings 页 UI)                 │
                │  ST-301 → ST-302 → ST-303               │
                └─────────────────────────────────────────┘
```

### Phase 边界

- **Phase 1 完整可独立合并** — 不依赖任何 UI 改动,可单独发版,issue#1 错配阻断已生效。
- **Phase 2 强依赖 Phase 1** — `/api/mail-provider/probe` 需要 SetupConfig 已扩字段才能写盘;`probe.probe_credentials` 复用 `MaillabClient` 401 自愈守卫的语义。
- **Phase 3 强依赖 Phase 2** — Settings.vue 复用 MailProviderCard.vue 组件。

---

## 8. 验收清单(Implementer Checklist)

| #   | 验收项                                                                                                  | yes/no |
| --- | ------------------------------------------------------------------------------------------------------- | ------ |
| 1   | SetupConfig 包含 MAIL_PROVIDER + MAILLAB_* 4 字段,Pydantic 模型 model_dump() 含 9 个 mail 字段        | ☐      |
| 2   | `/api/setup/save` POST `MAIL_PROVIDER=maillab` 后,`.env` 含 MAILLAB_* 4 字段且不含 CLOUDMAIL_BASE_URL | ☐      |
| 3   | `cf_temp_email.login()` 在响应 `{}` 时抛错(包含 "不像.*cloudflare_temp_email")                       | ☐      |
| 4   | `MaillabClient` 业务方法收 `code:401`,自动 re-login + 重试一次,日志含 `[maillab] token 已自愈`       | ☐      |
| 5   | `MaillabClient.login()` 内部 `/login` 自身回 401 时,**不**触发递归(thread-local guard 生效)         | ☐      |
| 6   | `_verify_cloudmail` 在 provider/base_url 错配时直接 return False,不实例化 client                    | ☐      |
| 7   | `_verify_cloudmail` 在 `AUTOTEAM_SKIP_PROVIDER_SNIFF=1` 时跳过嗅探                                     | ☐      |
| 8   | `/api/mail-provider/probe` step=fingerprint 对 maillab base_url 返 detected_provider=maillab + domain_list 非空 | ☐      |
| 9   | step=fingerprint provider 与 detected 不一致时返 error_code=PROVIDER_MISMATCH                          | ☐      |
| 10  | step=credentials 凭据错时返 UNAUTHORIZED;maillab 启用 captcha 时返 CAPTCHA_REQUIRED warning           | ☐      |
| 11  | step=domain_ownership 成功时探测邮箱已被 DELETE,leaked_probe=null                                     | ☐      |
| 12  | `PUT /api/config/register-domain` 与 step=domain_ownership 调用同一个 `probe_domain_ownership` helper | ☐      |
| 13  | `_AUTH_SKIP_PATHS` 含 `/api/mail-provider/probe`;API_KEY 已配置时强制 Bearer 鉴权                     | ☐      |
| 14  | setup 阶段 probe 端点 60 req/min 速率限制生效(超过返 429)                                            | ☐      |
| 15  | SetupPage 切 provider 时,后续状态全部重置(domain_list 清空、State 回到 PROVIDER)                    | ☐      |
| 16  | SetupPage 任一步失败时,下游卡片灰显 disabled                                                           | ☐      |
| 17  | Settings.vue 切换 mail provider 后保存,toast 显示「重启服务后生效」                                    | ☐      |
| 18  | docs/getting-started.md / configuration.md / mail-provider-design.md / troubleshooting.md / api.md 全部 sync | ☐      |
| 19  | `.env.example` MAIL_PROVIDER 上方含强引导注释,MAILLAB_* 不再注释状态                                  | ☐      |
| 20  | 19 处 `from autoteam.cloudmail import CloudMailClient` 调用零改动,业务回归测试 100% 通过              | ☐      |
| 21  | issue#1 复现场景(maillab 服务器 + cf_temp_email 选项)在 Web 面板内 step=fingerprint 即报错           | ☐      |

---

## 附:对 PRD 的批判审查与边界补完

> 实施过程中发现 PRD 未完全交代的 4 个边界,SPEC 已补:

1. **PRD §FR-004 鉴权策略未定** — PRD 仅说「setup 阶段无鉴权,进入面板后 API_KEY」,SPEC §3.5 落实为:`_AUTH_SKIP_PATHS` 加白 + 路由内手工二次校验,API_KEY 配置时强制 Bearer。
2. **PRD §OQ-1 token 跨步存储** — PRD 留作 OQ,SPEC §4.2 直接决策:**前端不持有 token**,step=domain_ownership 后端再调一次 login,降低复杂度且更安全。
3. **PRD §FR-003 login() 内部递归** — PRD 提到 thread-local guard 但未说细节,SPEC §3.3 给出双 guard:`in_login`(login 内部不触发自愈)+ `retried`(单次重试限制)。
4. **PRD §FR-001 互斥写入** — PRD 说「按 provider 跳过无关字段」,SPEC §3.6 落实为 `skip_keys` 集合,且保留 `CLOUDMAIL_DOMAIN` 作为 maillab 的 fallback(因 maillab.py 已实现该回落,删了反而破坏)。

> SPEC 已与 PRD §13 OQ-1 / OQ-3 / OQ-6 决策对齐:
> - OQ-1:不让前端持 token(SPEC §4.2)
> - OQ-3:`AUTOTEAM_SKIP_PROVIDER_SNIFF=1` 逃生口写入 SPEC §3.4
> - OQ-6:domainList 空时 error_code=`EMPTY_DOMAIN_LIST`(SPEC §3.1 异常表)

---

## 附录 A:修订记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-04-26 | 初版,21 文件清单 + Pydantic 模型 + 4 步 wizard 状态机 + probe 9 类 error_code |
| v1.1 | 2026-04-26 Round 7 P2 follow-up | §1 文件清单显式说明 `MailProviderCard.vue` 在 v1.0 阶段实施合并到 SetupPage/Settings.vue 内联(Wave 2 verify 偏差 Dev-1),Round 7 PRD-6 FR-P2.2 抽出共享组件去除 ~80 行双修代码;`MailProviderCard.vue` props `mode: 'setup' \| 'settings'` 区分父组件场景;关联 `prompts/0426/prd/prd-6-p2-followup.md` §5.2 + `prompts/0426/verify/wave1-4-integration-report.md` §4.3 Dev-1 |

