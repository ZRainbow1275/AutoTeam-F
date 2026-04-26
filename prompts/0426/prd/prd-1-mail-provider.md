# PRD-1: Mail Provider 全量化

## 0. 元数据

| 字段     | 值                                                                                          |
| -------- | ------------------------------------------------------------------------------------------- |
| 版本     | v1.0                                                                                        |
| 日期     | 2026-04-26                                                                                  |
| 主笔     | prd-mail (AutoTeam team `autoteam-prd-0426`)                                                |
| 评审人   | team-lead                                                                                   |
| 状态     | Draft                                                                                       |
| 输入文档 | `prompts/0426/research/issue-1-cloudmail.md` (613 行) / `prompts/0426/synthesis.md`         |
| 关联     | Issue#1 「setup_wizard 创建邮箱失败 (401)」、`docs/mail-provider-design.md` §6 待确认项     |

---

## 1. 背景与问题陈述

AutoTeam 已在 2026-04 完成 mail provider 抽象重构(`src/autoteam/mail/{base,cf_temp_email,maillab}.py`),但**配置层与 UI 层未同步迁移**,导致用户在以下三种场景持续踩坑:

1. **半成功假象** — 用户填了 maillab 服务器地址但没设 `MAIL_PROVIDER=maillab`,默认走 cf_temp_email 分支。`/admin/address` 在 maillab 的 catch-all 路由下可能误回 200,login 假成功,直到 `/admin/new_address` 才暴露 `{code:401, message:"身份认证失效"}`(issue#1 截图)。
2. **Web 面板配置黑洞** — `api.py:70-79` 的 `SetupConfig` Pydantic 模型只声明 `CLOUDMAIL_*`,Pydantic 默认丢弃未声明字段。即便前端塞 `MAIL_PROVIDER` / `MAILLAB_*`,`/api/setup/save` 也不会写入 `.env`,maillab 用户被锁死在 cf_temp_email 模式。
3. **盲填域名** — 用户手敲 `CLOUDMAIL_DOMAIN`,只能等启动验证或 `register-domain PUT` 试探时才知道是否被服务端接受;maillab 实例自带 `/setting/websiteConfig.domainList`,用户体验差距明显。

经核验(`src/autoteam/setup_wizard.py:20-35` REQUIRED_CONFIGS 已含 `MAIL_PROVIDER`,但 SetupConfig 没有同步),现状是**底层 ABC 已完整,中层配置 / 上层 UI 仍为单后端思维**,需要补齐贯通。

---

## 2. 目标(SMART)

| ID  | 目标                                                                                                                  | 衡量                                                                            |
| --- | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| G1  | Web 面板 SetupPage / Settings 支持 cf_temp_email / maillab 双 provider 字段填写并持久化                              | `/api/setup/save` 写入 `.env` 后 `MAIL_PROVIDER` / `MAILLAB_*` 全部可被读到     |
| G2  | 用户在保存前完成"指纹 → 凭据 → 域名归属"3 步在线验证,失败给出可读的 error_code + hint                                | 错配场景(maillab 服务器配 cf_temp_email 字段)在 step=fingerprint 即报错       |
| G3  | cf_temp_email 协议错配嗅探零漏判                                                                                      | `/admin/address` 返回 `200 + {}` 空响应也被识别为非 cf_temp_email,login 即抛错 |
| G4  | maillab 401 token 失效时客户端自愈                                                                                    | `MaillabClient` 任意业务方法收 `code:401` 后自动 `_ensure_login()` 重试一次     |
| G5  | 文档(getting-started / configuration / mail-provider-design / troubleshooting / api / .env.example)同步更新       | 文档清单 §11 全部状态从 P0 → Done                                              |

**MVP 完成标准** — Issue#1 截图复现的"管理员鉴权通过 → 创建邮箱失败 401"流程,在 Web 面板里能在 step=fingerprint 阶段(填完 base_url 即点测试连接时)就拿到"建议切 MAIL_PROVIDER=maillab"的 hint。

---

## 3. 非目标(明确不做的)

- **不实现** maillab 管理员功能(`/user/*` `/role/*` `/allEmail/*` `/regKey/*` 等 30+ 路由) — 属于 maillab 自身后台,业务上不需要。
- **不实现** `/email/send`(发邮件) — AutoTeam 只读验证码。
- **不实现** maillab Turnstile / addVerify 兼容 — 用户需到 maillab 后台关闭。后续如有需求再立 PRD。
- **不实现** 多 maillab 实例切换 — 当前一个 base_url 即可,prod/staging 由用户 .env 切换。
- **不实现** linuxdo OAuth 自动绑定 — 当前不在范围。
- **不实现** `/public/genToken` 集成 — 现 `/login` 路径已够用,且 `/public/genToken` 仅 `c.env.admin` 可调,适用面更窄。
- **不动**业务调用层(`manager.py` / `invite.py` / `codex_auth.py` / `account_ops.py`)对 `CloudMailClient` 的调用,通过 `from autoteam.cloudmail import CloudMailClient` 兼容别名继续工作。

---

## 4. 用户故事

| ID     | 故事                                                                                                                                                             |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| US-001 | 作为首次部署 AutoTeam 的管理员,我**应该**在 SetupPage 选 cf_temp_email / maillab 后**只看到对应字段**,无关字段不出现,避免我把 maillab 服务器填到 cf 字段里。 |
| US-002 | 作为管理员,我**应该**在填完 base_url 后立即看到"这是 cf_temp_email / maillab / 未识别"的指纹结果与 hint,而不是等启动后才发现错配。                              |
| US-003 | 作为 maillab 用户,我**应该**在保存前看到该实例支持的 domainList 下拉框,且选中的 domain **已被 `/account/add` 试创建一次邮箱并立即回收**确认归属。                |
| US-004 | 作为重启后 token 过期的用户,我**期望**任意 mail 调用收 401 时客户端自愈,而不是直接业务失败。                                                                    |
| US-005 | 作为 cf_temp_email 用户,我**期望**当我填错 base_url(指向 maillab 服务器)时立即报错,而不是 login 假成功。                                                       |
| US-006 | 作为通过 Settings 页修改 mail provider 的管理员,我**期望**复用 SetupPage 的同一套验证 UX(指纹 / 凭据 / 域名),不需要二次踩坑。                                  |

---

## 5. 功能需求

> 每条 FR 对应 §8 验收标准的一条 AC。

### FR-001 SetupConfig 扩字段

- 在 `src/autoteam/api.py` 的 `SetupConfig` 模型中新增字段:`MAIL_PROVIDER: str = "cf_temp_email"`、`MAILLAB_API_URL: str = ""`、`MAILLAB_USERNAME: str = ""`、`MAILLAB_PASSWORD: str = ""`、`MAILLAB_DOMAIN: str = ""`。
- `post_setup_save` 写入 `.env` 时,**当 MAIL_PROVIDER == "cf_temp_email" 时跳过 MAILLAB_* 写入,反之亦然**(避免无关字段污染 .env)。
- `GET /api/setup/status` 已经按 REQUIRED_CONFIGS 输出字段,需要新增 maillab 4 字段进 REQUIRED_CONFIGS,且在 `provider != maillab` 时把这 4 项的 `optional` 标为 `True`。

### FR-002 cf_temp_email 嗅探零漏判

- `src/autoteam/mail/cf_temp_email.py:login()` L106 的判定从 `("code" in payload or "data" in payload)` 改为**正向白名单**:**响应 dict 不含 `results` key 一律抛错**(无论是否含 code/data,空 `{}` 也命中)。
- 错误信息保留原有的"看起来是 maillab,请改 MAIL_PROVIDER=maillab"提示。
- `create_temp_email` L154 同步收紧:`"address" not in data` 即报错(不再要求同时 `code` + `message`)。

### FR-003 maillab 401 自愈

- `src/autoteam/mail/maillab.py` 的 `_get / _post / _delete / _put` 在拿到响应后:若 `resp.get("code") == 401`,**且** `_login_recursion_guard` 未置位,则清空 `self.token` → `self.login()` → 用新 token **重试一次**。
- 重试仍 401 时抛 `Exception("maillab 鉴权失败,请检查 MAILLAB_USERNAME/PASSWORD")`。
- 用 thread-local guard 防止 `login()` 自身触发递归(login 调 `_post` 会再触发 401 处理)。

### FR-004 新增 `/api/mail-provider/probe` 端点

- 新增 POST 端点,**3 步可分步调用**,Pydantic 模型见 §7.2。
- 鉴权:setup 阶段可无鉴权(与 `/api/setup/save` 一致);进入面板后(`API_KEY` 已配置)必须带 `Authorization: Bearer <key>`。
- `step=fingerprint`:无需凭据,GET `{base_url}/setting/websiteConfig` 一次即可拿到 `domainList / addVerifyOpen / registerVerifyOpen`,响应缺 `domainList` 但含 `results` 时识别为 cf_temp_email,均缺时识别为 unknown。
- `step=credentials`:用填好的 username/password 调 `{base_url}/login`,解析 JWT payload 反馈 `is_admin / user_email / token_preview`。token **不持久化到磁盘**,只在响应中作展示。
- `step=domain_ownership`:用上一步的 token 调 `{base_url}/account/add` body `{email: "probe-{ts}@{domain}"}`,成功立即 `DELETE /account/delete?accountId=N`。失败(403 / message 含 "domain")归类为 `FORBIDDEN_DOMAIN`。

### FR-005 register-domain 复用共享 helper

- 把 `api.py:1631-1673` 的 PUT register-domain 探测逻辑(create_temp_email + delete_account)**抽到 `src/autoteam/mail/probe.py` 的 `probe_domain_ownership(client, domain) -> ProbeResult`**。
- `/api/mail-provider/probe` step=domain_ownership 与 `PUT /api/config/register-domain` 共享此 helper,**确保两条路径的语义、错误归类、回收逻辑完全一致**。

### FR-006 setup_wizard 嗅探前置

- `_verify_cloudmail()` 中的 `_sniff_provider_mismatch(provider)` 调用从"login 失败前的无害 warning"改为"启动 / Web save 流程的强阻断":**指纹错配时直接 `return False`,不再继续走 login**。
- Web `post_setup_save` 自动复用此判定。
- `_sniff_provider_mismatch` 内部增强:除了 admin/login 路由探测,加一次 GET `/setting/websiteConfig`(maillab 独有 + 含 domainList 即明确 maillab),让 cf_temp_email 服务器也能被识别(它没有 `/setting/websiteConfig`)。

### FR-007 SetupPage 分组卡片 UX

- `web/src/components/SetupPage.vue` 由现有平铺 `v-for="field"` 改为**分组卡片**:
  - **卡片 1** Mail Provider 选择(单选 cf_temp_email / maillab,默认 cf_temp_email)
  - **卡片 2** base_url + 凭据 + 「测试连接」按钮(顺序调 step=fingerprint → step=credentials)
  - **卡片 3** domain 下拉框(从 fingerprint 拿 domainList) + 「验证归属」按钮(step=domain_ownership)
  - **卡片 4** CPA / API_KEY / Playwright 代理(原样保留)
- 步骤未通过的卡片下游灰显;每步失败显示 error_code + 原始 message + hint。
- domainList 缺失(cf_temp_email 后端 / maillab 老版)时降级为自由输入框,`/api/mail-provider/probe?step=domain_ownership` 仍执行。

### FR-008 Settings 页加 mail provider 切换

- `web/src/components/Settings.vue` 当前**完全无 mail provider 切换 UI**(经核验仅含管理员登录块)。新增「邮箱后端」区块,沿用 SetupPage 同款 3 步验证 + 写入 `.env`(后端走同一条 `/api/setup/save` 路径)。
- 修改成功后弹出 toast 提示「重启服务后生效」(`MAIL_PROVIDER` 是模块级常量,需重启)。

### FR-009 .env.example 注释加固

- `.env.example` L5 在 `MAIL_PROVIDER=cf_temp_email` 上方加注释:**强烈推荐显式设置;不设默认 cf_temp_email 可能与你的 maillab 部署错配**。
- maillab 字段块从注释状态改为正常字段状态(默认值留空),与 cf_temp_email 字段同等可见性。

### FR-010 文档同步

详见 §11 文档影响清单。

---

## 6. 非功能需求(NFR)

| 类别       | 要求                                                                                                                                          |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| 性能       | `/api/mail-provider/probe` 任意 step P95 ≤ 5s(含网络);domainList 探测开 timeout=5s;step=domain_ownership 含创建 + 回收两次往返,P95 ≤ 8s |
| 可靠性     | maillab 401 自愈最多 1 次重试,避免无限循环;register-domain 探测失败时探测邮箱泄漏须以 `leaked_probe` 字段透传给前端                          |
| 安全性     | username / password 仅作请求 body 转发到用户填的 base_url,**后端不持久化**(由 `/api/setup/save` 单独写 .env);`token_preview` 只截取前 10 字符 |
| 可观测性   | `/api/mail-provider/probe` 每步打 INFO 日志含 `step / detected_provider / ok`;失败打 WARNING 含 `error_code`                                  |
| 国际化     | 全部错误 message 中文(沿用项目现状)                                                                                                          |
| 兼容性     | 不破坏 `from autoteam.cloudmail import CloudMailClient` 兼容别名;19 处实例化 / 22 处方法调用零改动                                            |
| 速率限制   | `/api/mail-provider/probe` 端点,setup 阶段(无鉴权)单 IP 60 req/min(防扫描);进入面板后无限制                                                |

---

## 7. 技术方案

### 7.1 架构图(ASCII)

```
                         ┌─────────────────────────────────┐
                         │  SetupPage / Settings (Vue)     │
                         │   分组卡片 + 3 步验证流          │
                         └─────────────┬───────────────────┘
                                       │ POST /api/mail-provider/probe
                                       │ POST /api/setup/save
                                       ▼
                         ┌─────────────────────────────────┐
                         │  api.py (FastAPI)               │
                         │   + SetupConfig (扩字段)        │
                         │   + post_mail_provider_probe()  │
                         │   + put_register_domain()共享   │
                         └─────────────┬───────────────────┘
                                       │
                  ┌────────────────────┼────────────────────┐
                  ▼                    ▼                    ▼
        ┌────────────────┐  ┌──────────────────┐  ┌────────────────┐
        │ setup_wizard   │  │ mail/probe.py    │  │ runtime_config │
        │ _verify_cloud  │  │ probe_domain_    │  │ register_domain│
        │ + _sniff_前置  │  │ ownership(共享)  │  │ 持久化         │
        └────────┬───────┘  └────────┬─────────┘  └────────────────┘
                 │                   │
                 ▼                   ▼
       ┌──────────────────────────────────────┐
       │  mail/__init__.py — get_mail_client()│
       │  按 MAIL_PROVIDER 分发                │
       └─────────┬──────────────────┬─────────┘
                 │                  │
                 ▼                  ▼
       ┌──────────────────┐  ┌──────────────────┐
       │ cf_temp_email.py │  │ maillab.py       │
       │ + 嗅探零漏判     │  │ + 401 自愈守卫   │
       └──────────────────┘  └──────────────────┘
```

### 7.2 关键 API 契约

#### 7.2.1 `/api/mail-provider/probe` 请求体(Pydantic)

```python
from typing import Literal
from pydantic import BaseModel, Field, field_validator

class MailProviderProbeRequest(BaseModel):
    provider: Literal["cf_temp_email", "maillab"]
    step: Literal["fingerprint", "credentials", "domain_ownership"]
    base_url: str = Field(..., min_length=1, max_length=512)
    # cf_temp_email 字段
    admin_password: str = ""              # cf_temp_email 的 admin password
    # maillab 字段
    username: str = ""                    # maillab 登录邮箱
    password: str = ""                    # maillab 登录密码
    # 共用
    domain: str = ""                      # step=domain_ownership 必填
    # step=credentials/domain_ownership 时,前端把上一步拿到的 token 回传,后端不持久化
    bearer_token: str = ""

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return v
```

#### 7.2.2 `/api/mail-provider/probe` 响应体

```python
class MailProviderProbeResponse(BaseModel):
    ok: bool
    step: Literal["fingerprint", "credentials", "domain_ownership"]
    # 通用
    error_code: str | None = None         # ROUTE_NOT_FOUND/UNAUTHORIZED/FORBIDDEN_DOMAIN/PROVIDER_MISMATCH/NETWORK/TIMEOUT/UNKNOWN
    message: str | None = None
    hint: str | None = None
    warnings: list[str] = []
    # step=fingerprint 专属
    detected_provider: Literal["cf_temp_email", "maillab", "unknown"] | None = None
    domain_list: list[str] | None = None
    add_verify_open: bool | None = None
    register_verify_open: bool | None = None
    # step=credentials 专属
    is_admin: bool | None = None
    user_email: str | None = None
    token_preview: str | None = None      # 仅前 10 字符,展示用
    # step=domain_ownership 专属
    probe_email: str | None = None
    probe_account_id: int | None = None
    cleaned: bool | None = None           # 探测邮箱是否回收成功
    leaked_probe: dict | None = None      # cleaned=False 时给出 {email, acct_id, error}
```

#### 7.2.3 错误码枚举

| error_code           | 触发条件                                                                             | 默认 hint                                      |
| -------------------- | ------------------------------------------------------------------------------------ | ---------------------------------------------- |
| `ROUTE_NOT_FOUND`    | step=fingerprint 时 base_url 既无 `/setting/websiteConfig` 也无 `/admin/address`     | 检查 base_url 是否拼错(应含 /api 前缀等)     |
| `PROVIDER_MISMATCH`  | provider 与 detected_provider 不一致                                                 | 把 MAIL_PROVIDER 改为 detected_provider        |
| `UNAUTHORIZED`       | step=credentials 时 maillab 返回 code:401,或 cf_temp_email 返回 401/403              | 检查用户名/密码                                |
| `FORBIDDEN_DOMAIN`   | step=domain_ownership 时返回 403,或 message 含 "domain"                             | 联系 maillab 管理员把该 domain 加入白名单      |
| `CAPTCHA_REQUIRED`   | step=credentials 时 maillab 提示 turnstile / addVerify                               | 在 maillab 后台关闭 captcha                    |
| `NETWORK`            | 连接被拒 / DNS 解析失败                                                              | 检查 base_url 与防火墙                         |
| `TIMEOUT`            | 单步 5s 内无响应                                                                     | 检查 base_url 可达性                           |

### 7.3 数据模型变更

无数据库变更。仅:

- `.env` 新增/启用 5 字段(`MAIL_PROVIDER` `MAILLAB_API_URL` `MAILLAB_USERNAME` `MAILLAB_PASSWORD` `MAILLAB_DOMAIN`),5 字段已存在 `.env.example` 注释中,本次取消注释 + 默认空。
- `runtime_config.json` 不变,继续用 `register_domain` 字段(同时被 cf_temp_email 与 maillab 读取,优先级:显式参数 > runtime_config > 环境变量)。

### 7.4 配置项变更

| 配置                | 当前状态                                       | 变更                                      |
| ------------------- | ---------------------------------------------- | ----------------------------------------- |
| `MAIL_PROVIDER`     | `setup_wizard.REQUIRED_CONFIGS` 已有,但 SetupConfig 没有 | 加入 SetupConfig;Optional → Required(默认 cf_temp_email) |
| `MAILLAB_API_URL`   | 仅 `.env.example` 注释                         | 加入 SetupConfig + REQUIRED_CONFIGS;maillab 时必填        |
| `MAILLAB_USERNAME`  | 仅 `.env.example` 注释                         | 同上                                                       |
| `MAILLAB_PASSWORD`  | 仅 `.env.example` 注释                         | 同上                                                       |
| `MAILLAB_DOMAIN`    | 仅 `.env.example` 注释                         | 同上(可选,缺省回落 CLOUDMAIL_DOMAIN)                    |
| `CLOUDMAIL_EMAIL`   | 已废弃但仍在 `.env.example` L12 留兼容空字段     | 保持现状(与本 PRD 无关)                                  |

### 7.5 前端变更

| 文件                         | 变更                                                                                                                |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `web/src/api.js`             | 新增 `probeMailProvider(payload)` 方法,POST `/api/mail-provider/probe`                                              |
| `web/src/components/SetupPage.vue` | 重构为分组卡片(FR-007);从 `<input v-for="field">` 改为 `<MailProviderCard>` + `<CredentialCard>` + `<DomainCard>` |
| `web/src/components/MailProviderCard.vue` | **新增**;封装 provider 选择 + base_url + 凭据 + 域名 3 步验证                                                       |
| `web/src/components/Settings.vue`  | 新增「邮箱后端」区块(FR-008),复用 `<MailProviderCard>`                                                              |

---

## 8. 验收标准

| AC ID  | FR    | 步骤                                                                                                                                       | 通过判据                                                                       |
| ------ | ----- | ------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| AC-001 | FR-001 | Web 面板 SetupPage 选 maillab,填全 4 字段 + 点保存;查 `.env`                                                                              | `MAIL_PROVIDER=maillab` 与 `MAILLAB_*` 4 字段全部落盘                          |
| AC-002 | FR-002 | mock 一个 base_url 对 `/admin/address` 回 `200 + {}`;调 `cf_temp_email.login()`                                                            | 抛 `CloudMail 登录响应不像 dreamhunter2333/...`                                |
| AC-003 | FR-003 | 启动后等 maillab token 失效,触发 `client.list_accounts()`                                                                                  | 内部自动 login 一次,业务方法返回正确数据,日志含 `[maillab] token 已自愈`     |
| AC-004 | FR-004 | 调 `/api/mail-provider/probe` step=fingerprint,base_url 指向 maillab                                                                       | 返回 `detected_provider=maillab` + 非空 `domain_list`                          |
| AC-005 | FR-004 | 同上,base_url 指向 cf_temp_email                                                                                                           | 返回 `detected_provider=cf_temp_email`                                         |
| AC-006 | FR-004 | step=fingerprint,但 provider=cf_temp_email、base_url 是 maillab                                                                            | `ok=false, error_code=PROVIDER_MISMATCH, hint="把 MAIL_PROVIDER 改为 maillab"` |
| AC-007 | FR-004 | step=domain_ownership,domain 不在 maillab 白名单                                                                                          | `ok=false, error_code=FORBIDDEN_DOMAIN`                                        |
| AC-008 | FR-005 | `PUT /api/config/register-domain` 与 `/api/mail-provider/probe?step=domain_ownership` 命中同一个 domain                                    | 两端返回 `probe_email` 一致前缀(`probe{ts}`),探测邮箱回收语义一致           |
| AC-009 | FR-006 | 启动时 `MAIL_PROVIDER=cf_temp_email` 但 `CLOUDMAIL_BASE_URL` 指向 maillab                                                                 | `_verify_cloudmail` 不调 `client.login()` 即 `return False`,日志 `[验证] 协议错配...` |
| AC-010 | FR-007 | 在 SetupPage 选 cf_temp_email                                                                                                              | maillab 4 字段隐藏,只看到 `CLOUDMAIL_*`                                        |
| AC-011 | FR-007 | step=credentials 失败,点击下游域名卡片                                                                                                    | 域名卡片灰显,无法操作                                                          |
| AC-012 | FR-008 | Settings 页修改 mail provider 后保存                                                                                                       | toast 显示「重启服务后生效」,`.env` 已更新                                    |
| AC-013 | FR-009 | 全新部署用户复制 `.env.example` 后启动                                                                                                     | 用户能看到 maillab 注释突出引导                                                 |
| AC-014 | 全部  | issue#1 截图复现:用户在 SetupPage 填 maillab 服务器地址但选 cf_temp_email                                                                  | 测试连接按钮显示 `PROVIDER_MISMATCH` + hint,**不**进 step=credentials         |

---

## 9. 测试计划

### 9.1 单元测试

| 测试模块                                           | 用例                                                                                                          |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `tests/test_mail_cf_temp_email_sniff.py`           | login() 响应:`{"results":[]}` ✓;`{}` ✗;`{"code":401}` ✗;`{"code":200,"data":{}}` ✗(全部 ✗ 应 raise) |
| `tests/test_mail_maillab_self_heal.py`             | 模拟首次 401 后 login 成功 + 重试拿数据;login 仍 401 时抛错;login 内部递归不再触发自愈                      |
| `tests/test_mail_provider_probe.py`                | step=fingerprint 三种 detected_provider(cf/maillab/unknown);step=credentials 401/200;step=domain_ownership 403/200 |
| `tests/test_mail_probe_helper.py`                  | `probe_domain_ownership` 共享函数:成功 + cleaned=True;成功 + 删除失败 → leaked_probe 透传                  |
| `tests/test_setup_wizard_sniff_block.py`           | `_verify_cloudmail` 在指纹错配时直接 return False,不创建 client                                              |

### 9.2 集成测试

- `tests/integration/test_setup_save_e2e.py`:POST `/api/setup/save`(maillab 全字段 + 模拟 maillab server)→ `.env` 含 5 个新字段,服务重载后 `MAIL_PROVIDER=maillab` 生效
- `tests/integration/test_register_domain_shared.py`:`PUT /api/config/register-domain` 与 `POST /api/mail-provider/probe?step=domain_ownership` 对同 domain 的输出一致

### 9.3 E2E

- Playwright:SetupPage 4 步流(provider 选 maillab → fingerprint → credentials → domain_ownership → save)全绿;Settings 页同款流(已部署后切换)

### 9.4 手测清单

| 场景                                                                | 期望                                                                  |
| ------------------------------------------------------------------- | --------------------------------------------------------------------- |
| 全新 docker 部署,首次进 SetupPage 选 cf_temp_email                | 仅看到 cf 字段,maillab 字段不渲染                                     |
| 同上,选 maillab 后填 skymail.ink demo                              | fingerprint 拉到 ≥1 个 domain,credentials 显示 is_admin              |
| 已部署用户在 Settings 切 maillab → cf_temp_email                    | 保存后 toast 提示重启;重启后业务正常                                  |
| 故意把 maillab base_url 填到 cf_temp_email 字段                      | 测试连接按钮显示 PROVIDER_MISMATCH(issue#1 复现)                     |
| 让 maillab 实例 token 过期(关 worker 30 分钟后再点 sync_accounts) | 业务正常,日志显示 token 自愈                                          |

---

## 10. 灰度/回滚策略

- **灰度**:本 PRD 无远程 feature flag(配置类变更天然分发到每个用户实例),通过分阶段 PR 控制风险:
  - **Phase 1**(P0,无 UI 风险):FR-001(SetupConfig 扩字段)+ FR-002(嗅探零漏判)+ FR-003(401 自愈)+ FR-006(嗅探前置)+ FR-009(.env.example)+ FR-010 文档
  - **Phase 2**(P0,UI 风险):FR-004(probe 端点)+ FR-005(register-domain 复用)+ FR-007(SetupPage 改造)
  - **Phase 3**(P1):FR-008(Settings 页切换 UI)
- **回滚**:每个 Phase 独立 PR,失败即 `git revert`。Phase 1 不影响业务路径,可即时回滚;Phase 2 / 3 涉及前端构建,需 `web/dist` 重新打包。
- **数据回滚**:`.env` 字段写入由用户掌控,本身可手动改回。

---

## 11. 文档影响清单

| 文档                              | 章节                                                | 改动类型 | 状态(P0=必做) |
| --------------------------------- | --------------------------------------------------- | -------- | --------------- |
| `docs/getting-started.md`         | 准备工作 / 1. 搭建临时邮箱                          | 改写     | P0              |
| `docs/getting-started.md`         | 第二步:配置 / 直接部署                              | 改写     | P0              |
| `docs/getting-started.md`         | (新增)第二点五步:验证邮箱后端归属                  | 新增     | P0              |
| `docs/configuration.md`           | `.env` 配置项表(MAIL_PROVIDER 必填化)              | 改写     | P0              |
| `docs/configuration.md`           | Mail Provider 切换(顺序与推荐反转)                 | 改写     | P0              |
| `docs/configuration.md`           | (新增)邮箱归属验证                                  | 新增     | P1              |
| `docs/configuration.md`           | ⚠️ 协议错配排查(链接 SetupPage)                    | 改写     | P0              |
| `docs/mail-provider-design.md`    | §6「未知项 5 项」标记为已验证                        | 改写     | P0              |
| `docs/mail-provider-design.md`    | (新增)§7 skymail.ink API 全表                       | 新增     | P1              |
| `docs/troubleshooting.md`         | Web 面板相关 / 配置保存 401                          | 增补     | P0              |
| `docs/troubleshooting.md`         | (新增)邮箱后端                                      | 新增     | P1              |
| `docs/api.md`                     | 初始配置 API / 文档化 `/api/mail-provider/probe`    | 增补     | P0              |
| `docs/api.md`                     | 域名管理 API / 复用共享 helper 说明                  | 改写     | P0              |
| `README.md`                       | Features 增补 maillab 全量集成                       | 增补     | P1              |
| `.env.example`                    | 注释突出 MAIL_PROVIDER 必填,maillab 字段取消注释   | 改写     | P0              |

---

## 12. 风险登记册

| Risk ID | 风险描述                                                                                       | Owner       | 严重度 | Mitigation                                                                                       |
| ------- | ---------------------------------------------------------------------------------------------- | ----------- | ------ | ------------------------------------------------------------------------------------------------ |
| R-001   | maillab v2.x 之前 `/setting/websiteConfig` 不返回 `domainList`(skymail.ink 当前 v2.8.0 才有) | impl-1      | 中     | step=fingerprint 缺 domainList 时降级:不阻断,返回 `domain_list=null`,前端切自由输入框           |
| R-002   | maillab 启用 Turnstile/addVerify 时 step=credentials 失败                                       | impl-1      | 中     | 错误归类 `CAPTCHA_REQUIRED`,hint「请到 maillab 后台关闭 captcha」;不实现 captcha bypass         |
| R-003   | `/api/mail-provider/probe` setup 阶段无鉴权,被外部当端口扫描器                                  | impl-2      | 中     | NFR 速率限制 60 req/min;面板初始化后此端点强制 API_KEY 鉴权                                       |
| R-004   | cf_temp_email 嗅探收紧后,误伤第三方 fork(如 unsendapp 等响应不带 results 字段)             | impl-1      | 低     | 错误信息提示 `AUTOTEAM_SKIP_VERIFY=1` 绕过(已存在);记录到 troubleshooting.md                    |
| R-005   | maillab 401 自愈遇到密码本身错误时,会陷入 login 失败 → throw → 上层重试 → login 失败循环   | impl-1      | 中     | 用 thread-local guard,login() 函数内部不触发自愈;login 失败抛 `MAILLAB_AUTH_FAILED`,业务层不重试 |
| R-006   | SetupPage 重构破坏现有 e2e 测试                                                                 | impl-2      | 中     | Phase 2 单独 PR;先添加新 testid,旧 testid 兼容保留 1 个版本                                     |
| R-007   | Settings 页 mail provider 切换后,正在跑的任务用旧 client 引用                                  | impl-2      | 高     | toast 明确「重启服务后生效」;不在运行时 hot-swap client                                          |
| R-008   | `MAIL_PROVIDER` 在 `setup_wizard.REQUIRED_CONFIGS` 默认值 `cf_temp_email`,但 SetupConfig 默认值如果不一致会引起冲突 | impl-1 | 低 | SetupConfig 默认值与 REQUIRED_CONFIGS 同步,加 unit test 校验默认值一致性                         |

---

## 13. 未决问题(Open Questions)

| OQ ID | 问题                                                                                                                                        | 决策建议(待 team-lead 确认)                                                                  |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| OQ-1  | step=credentials 通过后,是否要在前端把 token 暂存于 vue 内存以便 step=domain_ownership 直接调,还是后端 session 持有?                       | **建议前端暂存**,后端无状态;后端只校验签名,这样后端不增加 session 复杂度                    |
| OQ-2  | `is_admin=false`(非管理员账号)是否应阻断保存?                                                                                              | **建议警告但允许保存**,因为 `/account/add` 普通用户也能调;`is_admin=false` 时给 hint 即可    |
| OQ-3  | `_sniff_provider_mismatch` 改为强阻断后,如果用户用的是某种 fork(比如自部署改了 `/admin/address` 为非标响应),会被误伤                      | **建议加 `AUTOTEAM_SKIP_PROVIDER_SNIFF=1` 环境变量逃生口**                                    |
| OQ-4  | maillab 401 自愈是否应记录到 SQLite/runtime_config,统计 token 失效频率以便后续做 keep-alive?                                              | **暂不做**,Phase 1 先 just-in-time 自愈;有 telemetry 需求时再立 PRD                           |
| OQ-5  | `/api/mail-provider/probe` 是否要支持 GET(仅 step=fingerprint,可被书签化)?                                                              | **不做**,统一 POST;GET 容易被浏览器历史记录泄露 base_url                                     |
| OQ-6  | 如果 maillab 实例返回的 `domainList` 是空数组(部署没配 domain),前端是否仍允许进 step=domain_ownership?                                  | **不允许**,直接报错「该实例未配置任何邮箱域名,请先在 maillab 后台 → 设置 → 域名 配置」     |

---

## 14. 实施 Story Map(可拆 sub-task 列表)

> 按照 §10 的 Phase 分组;每个 sub-task 对应一个独立可 review 的 PR。

### Phase 1 — 后端基础(无 UI 风险)

| Sub-task ID | 任务                                                            | 涉及文件                                                                                | 估算 |
| ----------- | --------------------------------------------------------------- | --------------------------------------------------------------------------------------- | ---- |
| ST-101      | SetupConfig 扩字段 + REQUIRED_CONFIGS 同步                      | `src/autoteam/api.py`、`src/autoteam/setup_wizard.py`                                   | 0.5d |
| ST-102      | `cf_temp_email.login()` 嗅探零漏判 + create_temp_email 同步收紧 | `src/autoteam/mail/cf_temp_email.py`                                                    | 0.5d |
| ST-103      | `MaillabClient` 401 自愈守卫                                    | `src/autoteam/mail/maillab.py`                                                          | 0.5d |
| ST-104      | `_sniff_provider_mismatch` 改强阻断 + 增 `/setting/websiteConfig` 探测 | `src/autoteam/setup_wizard.py`                                                          | 0.5d |
| ST-105      | `.env.example` 注释加固                                         | `.env.example`                                                                          | 0.1d |
| ST-106      | Phase 1 文档同步(getting-started / configuration / mail-provider-design / troubleshooting / api / .env.example) | `docs/*.md` 8 文件                                                                       | 1d   |
| ST-107      | Phase 1 单元测试                                                | `tests/test_mail_*.py` 4 文件                                                            | 0.5d |

### Phase 2 — Probe 端点 + SetupPage(P0)

| Sub-task ID | 任务                                                       | 涉及文件                                                                                | 估算 |
| ----------- | ---------------------------------------------------------- | --------------------------------------------------------------------------------------- | ---- |
| ST-201      | 新建 `src/autoteam/mail/probe.py` 共享 helper              | `src/autoteam/mail/probe.py`(新建)                                                       | 0.5d |
| ST-202      | `/api/mail-provider/probe` 端点实现 + 错误码归类           | `src/autoteam/api.py`                                                                   | 1d   |
| ST-203      | `register-domain` PUT 复用 `probe_domain_ownership`        | `src/autoteam/api.py`                                                                   | 0.3d |
| ST-204      | 前端 `MailProviderCard.vue` 新组件                         | `web/src/components/MailProviderCard.vue`(新建)                                          | 1d   |
| ST-205      | SetupPage.vue 重构为分组卡片                               | `web/src/components/SetupPage.vue`                                                      | 1d   |
| ST-206      | api.js 新增 `probeMailProvider`                            | `web/src/api.js`                                                                        | 0.1d |
| ST-207      | Phase 2 集成 + E2E 测试                                    | `tests/integration/*.py`、`tests/e2e/*.spec.ts`                                          | 1d   |

### Phase 3 — Settings 页切换 UI(P1)

| Sub-task ID | 任务                                            | 涉及文件                                          | 估算 |
| ----------- | ----------------------------------------------- | ------------------------------------------------- | ---- |
| ST-301      | Settings.vue 增「邮箱后端」区块,复用 MailProviderCard | `web/src/components/Settings.vue`                  | 0.5d |
| ST-302      | 切换后 toast「重启服务后生效」                    | `web/src/components/Settings.vue`                 | 0.1d |
| ST-303      | Phase 3 手测                                    | —                                                 | 0.3d |

**总估算**:Phase 1 ≈ 3.6d / Phase 2 ≈ 4.9d / Phase 3 ≈ 0.9d,合计 ≈ 9.4d(单人)

---

## 附:核验记录(批判性审查)

> 调研报告 §A.3 提到「`REQUIRED_CONFIGS` 与 Web 面板 SetupPage 没有收 maillab 字段」,经直接读取 `setup_wizard.py:20-35` 发现 **REQUIRED_CONFIGS 已含 `MAIL_PROVIDER`**(2026-04 重构后已加),但 `MAILLAB_*` 4 字段仍缺。**推翻调研结论的一半**:不是「全部缺失」,而是「provider 选择项已加,maillab 详细字段仍缺 + SetupConfig 端不接收任何 mail 字段」。

> 调研报告 §D.2 描述的 401 协议错配,**经过核验代码已完成 round-2 修复**:
> - `cf_temp_email.py:106` 已加嗅探(2026-04-25 commit `de7cad3`)
> - `setup_wizard.py:259` 已在 `_verify_cloudmail` 第一步调 `_sniff_provider_mismatch`
> - `_sniff_provider_mismatch` 当前是 warning 级别,未阻断
>
> 因此 PRD 重点不是「补齐嗅探」(已有),而是「**收紧条件 + 改强阻断**」。

> 调研报告 §F.2-7 提到「register-domain 探测路径与 §C step=domain_ownership 是否合并」,经核验 `api.py:1631-1673` 已含完整 create+delete 逻辑,**应当抽 helper 合并**(FR-005)。

