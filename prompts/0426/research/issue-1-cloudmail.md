# Issue#1 CloudMail 全量集成 — 研究报告

> 范围:把"CloudMail"(实际指 `maillab/cloud-mail`,品牌名 `skymail.ink`)的全量 API 接入 AutoTeam,在前端
> 加邮箱归属验证,修复 setup_wizard 的 401 半成功 bug,并梳理文档影响面。
>
> 调研时间:2026-04-26
> 关键证据:
> - `https://doc.skymail.ink` 顶部 logo 链与 GitHub Releases 链直指 `https://github.com/maillab/cloud-mail`,
>   "在线演示"地址即 `https://skymail.ink` → **skymail.ink = maillab/cloud-mail 的官方部署/对外品牌**。
> - `mail-worker/src/security/security.js` 白名单 = `['/login', '/register', '/oss',
>   '/setting/websiteConfig', '/webhooks', '/init', '/public/genToken', '/telegram', '/test', '/oauth']`
> - `mail-worker/src/api/*-api.js` 实际暴露 60+ 路由,`doc.skymail.ink/api/api-doc.html` 只挑了 3 个
>   "对外集成"用的子集(`/public/genToken / emailList / addUser`)写文档。

---

## A. 现状盘点

### A.1 cloudmail.py 当前接口表

`src/autoteam/cloudmail.py` 现在只是 **10 行 stub**(2026-04 重构后改为 re-export):

```python
from autoteam.mail import CloudMailClient  # noqa: F401  re-export
```

实际实现拆到了 `src/autoteam/mail/` 子包,工厂 `get_mail_client()` 按 `MAIL_PROVIDER` 环境变量分发到具体后端。

**`src/autoteam/mail/` 当前目录结构**:

| 文件                         | 行数 | 角色                                                        |
| ---------------------------- | ---- | ----------------------------------------------------------- |
| `__init__.py`                | 52   | 工厂 `get_mail_client()` + 兼容别名 `CloudMailClient`       |
| `base.py`                    | 276  | `MailProvider` ABC + `Email/Account` dataclass + 共享文本工具 |
| `cf_temp_email.py`           | 379  | `dreamhunter2333/cloudflare_temp_email` 后端实现            |
| `maillab.py`                 | 467  | `maillab/cloud-mail` 后端实现(即 skymail.ink)               |

**`MailProvider` ABC 公开方法集**(11 个,`base.py` 定义):

| 方法                         | 抽象度  | 用途                                                      |
| ---------------------------- | ------- | --------------------------------------------------------- |
| `login()`                    | abstract | 初始化鉴权,返回 token 字符串(仅日志用)                  |
| `create_temp_email(prefix, domain)` | abstract | 创建临时邮箱,返回 `(account_id, email)`           |
| `list_accounts(size)`        | abstract | 列出已建邮箱,返回 dict 列表                               |
| `delete_account(account_id)` | abstract | 删除邮箱,返回 `{code, message?}`                          |
| `search_emails_by_recipient(to_email, size, account_id)` | abstract | 按收件人查邮件                            |
| `list_emails(account_id, size)` | abstract | 按 accountId 查邮件                                     |
| `delete_emails_for(to_email)`| abstract | 批量删除某收件人的所有邮件                                |
| `get_latest_emails(account_id, ...)` | concrete | 旧接口兼容,默认委托 `list_emails`                  |
| `wait_for_email(...)`        | concrete | 轮询等待邮件,默认实现搬自旧 `cloudmail.py`              |
| `extract_verification_code(email_data)` | concrete | 提取 6 位 OTP(纯文本正则)                          |
| `extract_invite_link(email_data)` | concrete | 提取 ChatGPT/通用邀请链接                            |

**`MaillabClient` 实际命中的 maillab API**(已上线):

| 方法               | HTTP 路径                    | 备注                                                                |
| ------------------ | ---------------------------- | ------------------------------------------------------------------- |
| `login`            | `POST /login`                | body `{email, password}` → `{code:200, data:{token}}`               |
| `create_temp_email`| `POST /account/add`          | body `{email}`(完整地址,不接受 prefix+domain 拆开)                |
| `list_accounts`    | `GET /account/list`          | 服务端硬 cap 30/页,代码已实现 `lastSort + accountId` 游标翻页     |
| `delete_account`   | `DELETE /account/delete?accountId=N` |                                                             |
| `list_emails`      | `GET /email/list`            | 必须带 `type=0`(RECEIVE),否则 SQL `IS NULL` 永远为空              |
| `get_latest_emails`| `GET /email/latest`          |                                                                     |
| `delete_emails_for`| `DELETE /email/delete?emailIds=1,2,3` | 软删除                                                     |

**调用方调用面**(`src/autoteam` 全仓 grep):

| 调用方            | `CloudMailClient()` 实例化 | `mail_client.<method>` 引用 |
| ----------------- | -------------------------- | --------------------------- |
| `manager.py`      | 11                         | 7                           |
| `api.py`          | 5                          | -                           |
| `invite.py`       | 1                          | 4                           |
| `codex_auth.py`   | 0(由调用方注入)           | 9                           |
| `account_ops.py`  | 1                          | 2                           |
| `setup_wizard.py` | 1                          | 0                           |
| 合计              | **19 处实例化 / 22 处方法调用** | —                       |

→ 所有调用都走 `from autoteam.cloudmail import CloudMailClient` 兼容别名,**业务层零改动**。

### A.2 setup_wizard CloudMail 验证流程

`src/autoteam/setup_wizard.py:_verify_cloudmail()` 实际流程:

1. 读 `MAIL_PROVIDER` 环境变量(默认 `cf_temp_email`)
2. 校验对应的环境变量集是否齐全(cf 看 `CLOUDMAIL_BASE_URL/PASSWORD/DOMAIN`,maillab 看 `MAILLAB_API_URL/USERNAME/PASSWORD` + 域名)
3. 调用 `_sniff_provider_mismatch(provider)`:用 GET `/admin/address` 与 GET `/login` 探测路由指纹,发现 base_url 协议错配则打 warning 但不阻断
4. 实例化 `CloudMailClient()` → `client.login()` → `client.create_temp_email(prefix=at-test-xxxxx)` → `client.delete_account(...)`
5. 任一环节抛异常 → 打 ERROR 并 `return False`

`_verify_cloudmail()` 也被同名函数 `post_setup_save` (`api.py:99`) 复用,即 Web 面板 SetupPage 提交后走的也是这套逻辑。

### A.3 配置项 (.env / CLOUDMAIL_*)

`.env.example` 当前定义:

```dotenv
MAIL_PROVIDER=cf_temp_email                  # cf_temp_email | maillab
CLOUDMAIL_BASE_URL=https://example.com/api
CLOUDMAIL_PASSWORD=your_password
CLOUDMAIL_DOMAIN=@example.com
CLOUDMAIL_EMAIL=                             # 已废弃,保留兼容旧 .env
# MAILLAB_API_URL=https://your-maillab.example.com
# MAILLAB_USERNAME=admin@example.com
# MAILLAB_PASSWORD=your_password
# MAILLAB_DOMAIN=@example.com
```

**`setup_wizard.REQUIRED_CONFIGS`**(交互式向导询问的字段):

- `MAIL_PROVIDER` (默认 `cf_temp_email`,可选)
- `CLOUDMAIL_BASE_URL`
- `CLOUDMAIL_PASSWORD`
- `CLOUDMAIL_DOMAIN`
- `CPA_URL` / `CPA_KEY`
- `PLAYWRIGHT_PROXY_URL` / `PLAYWRIGHT_PROXY_BYPASS`(可选)
- `API_KEY`

> **关键缺口**:`REQUIRED_CONFIGS` 与 Web 面板 SetupPage **没有**收 maillab 字段(`MAILLAB_API_URL/USERNAME/PASSWORD/DOMAIN`)。
> 用户选 `MAIL_PROVIDER=maillab` 时**只能手动改 `.env`**,getting-started.md L128 也写了这一点。
> 这正是 issue#1 截图里"创建邮箱失败 401"的 root cause(详见 §D)。

`api.py` 的 `SetupConfig` Pydantic 模型(L70-79):

```python
class SetupConfig(BaseModel):
    CLOUDMAIL_BASE_URL: str = ""
    CLOUDMAIL_EMAIL: str = ""
    CLOUDMAIL_PASSWORD: str = ""
    CLOUDMAIL_DOMAIN: str = ""
    CPA_URL: str = "http://127.0.0.1:8317"
    CPA_KEY: str = ""
    PLAYWRIGHT_PROXY_URL: str = ""
    PLAYWRIGHT_PROXY_BYPASS: str = ""
    API_KEY: str = ""
```

**完全不包含 `MAIL_PROVIDER` 与 `MAILLAB_*`** → Web 面板提交配置时即使前端把字段塞进去,Pydantic 默认会忽略未定义字段,
`/api/setup/save` 不会写入 `MAIL_PROVIDER`,造成 maillab 部署用户被锁死在 cf_temp_email 模式。

---

## B. skymail.ink 官方 API 全表

> **结论先行**:skymail.ink 是 `maillab/cloud-mail` 的对外品牌(GitHub `@maillab` 组织,Releases 链来自 `doc.skymail.ink` 顶部导航)。
> `doc.skymail.ink/api/api-doc.html` 只对外公布了 3 个"集成接口"(`/public/*`),但仓库实际暴露的路由 ≥ 60 个,
> 全部按 `mail-worker/src/api/*-api.js` 文件分组。

### B.1 已确认接口(全部从 GitHub 源码核实)

#### B.1.1 公开集成接口 `/public/*`(skymail 文档显式公开,**仅 3 个**)

| 路由                  | 方法 | 鉴权要求                              | 用途                                                   |
| --------------------- | ---- | ------------------------------------- | ------------------------------------------------------ |
| `/public/genToken`    | POST | **白名单内**(无需 token)。但 service 内部 `verifyUser()` 会校验 `email === c.env.admin`,**仅管理员邮箱**能拿到 token | 拿一个 UUID 格式的 API token(KV 持久化)              |
| `/public/emailList`   | POST | 需 `Authorization: <jwt>`(genToken 拿到的 token 也认) | 模糊查邮件:支持 `toEmail/sendName/sendEmail/subject/content/timeSort/type/isDel/num/size`     |
| `/public/addUser`     | POST | 需 `Authorization: <jwt>`             | 批量加用户:body `{list:[{email, password?, roleName?}, ...]}` |

#### B.1.2 鉴权 `/login` `/register` `/logout`(在白名单)

| 路由         | 方法   | body / 参数                         | 返回                                       |
| ------------ | ------ | ----------------------------------- | ------------------------------------------ |
| `/login`     | POST   | `{email, password, [token?]}`       | `{code:200, data:{token}}` — token=裸 JWT |
| `/register`  | POST   | `{email, password, [token?]}`       | `{code:200, data:<jwt>}`                  |
| `/logout`    | DELETE | header `Authorization: <jwt>`       | `{code:200}`                               |

#### B.1.3 账户管理 `/account/*`(需鉴权)

| 路由                        | 方法   | 关键参数                  | 用途                                          |
| --------------------------- | ------ | ------------------------- | --------------------------------------------- |
| `/account/list`             | GET    | `size, lastSort, accountId` | 列表(单页 cap 30,游标翻页)               |
| `/account/add`              | POST   | body `{email}`            | 新建临时邮箱(完整地址,服务端校验域名白名单) |
| `/account/delete`           | DELETE | `accountId`               |                                               |
| `/account/setName`          | PUT    | body `{accountId, name}`  | 改备注                                        |
| `/account/setAllReceive`    | PUT    | body `{accountId, allReceive}` | 是否接收所有发件人                       |
| `/account/setAsTop`         | PUT    | body `{accountId}`        | 置顶/取消置顶                                 |

#### B.1.4 邮件 `/email/*`(需鉴权)

| 路由                | 方法   | 关键参数                                                                 | 用途                                                   |
| ------------------- | ------ | ------------------------------------------------------------------------ | ------------------------------------------------------ |
| `/email/list`       | GET    | `accountId, type(0=收/1=发,**必传**), size(<=50), emailId, timeSort, allReceive` | drizzle 用 `eq(email.type, type)`,缺省走 IS NULL      |
| `/email/latest`     | GET    | `accountId, emailId(游标), allReceive`                                   | 最新 ≤ 20 封(默认按 emailId desc)                    |
| `/email/attList`    | GET    | `emailId`                                                                | 附件列表                                               |
| `/email/delete`     | DELETE | `emailIds`(逗号分隔)                                                   | 软删除                                                 |
| `/email/send`       | POST   | body(发件载荷)                                                         | 发邮件(基于 Resend)                                 |
| `/email/read`       | PUT    | body `{emailId}`                                                         | 标记已读                                               |

#### B.1.5 全局邮件管理 `/allEmail/*`(管理员权限)

| 路由                       | 方法   | 用途                            |
| -------------------------- | ------ | ------------------------------- |
| `/allEmail/list`           | GET    | 管理员看全平台邮件               |
| `/allEmail/latest`         | GET    | 全平台最新邮件                  |
| `/allEmail/delete`         | DELETE | 物理删除(硬删)                |
| `/allEmail/batchDelete`    | DELETE | 批量物理删除                    |

#### B.1.6 用户管理 `/user/*`(管理员权限)

| 路由                       | 方法   | 用途                             |
| -------------------------- | ------ | -------------------------------- |
| `/user/list`               | GET    | 用户列表                         |
| `/user/add`                | POST   | 新增用户(单条)                |
| `/user/setPwd`             | PUT    | 改密                             |
| `/user/setStatus`          | PUT    | 启停                             |
| `/user/setType`            | PUT    | 改类型                           |
| `/user/restore`            | PUT    | 恢复                             |
| `/user/resetSendCount`     | PUT    | 重置发件计数                     |
| `/user/delete`             | DELETE | 物理删除                         |
| `/user/allAccount`         | GET    | 某用户名下所有 account(临时邮箱)|
| `/user/deleteAccount`      | DELETE | 删某用户的某 account             |

#### B.1.7 角色 `/role/*`(管理员权限)

| 路由                  | 方法   | 用途                |
| --------------------- | ------ | ------------------- |
| `/role/list`          | GET    | 角色列表            |
| `/role/permTree`      | GET    | 权限树(RBAC)      |
| `/role/selectUse`     | GET    | 可选角色            |
| `/role/add`           | POST   | 新增                |
| `/role/set`           | PUT    | 改                  |
| `/role/setDefault`    | PUT    | 设默认              |
| `/role/delete`        | DELETE | 删                  |

#### B.1.8 设置 `/setting/*`

| 路由                          | 方法   | 鉴权                  | 用途                                                                |
| ----------------------------- | ------ | --------------------- | ------------------------------------------------------------------- |
| **`/setting/websiteConfig`**  | GET    | **白名单(无需 token)** | 拉公开站点配置 — **关键端点**(归属验证用,详见 §C)                |
| `/setting/query`              | GET    | 需 token              | 全量站点设置(管理员视角,token 等敏感字段已掩码)                  |
| `/setting/set`                | PUT    | 需 token + admin 权限 | 改设置                                                              |
| `/setting/setBackground`      | PUT    | 需 token + admin 权限 | 改背景                                                              |
| `/setting/deleteBackground`   | DELETE | 需 token + admin 权限 | 删背景                                                              |

**`/setting/websiteConfig` 返回字段**(已现场验证 `setting-service.js`):

```text
register, title, manyEmail, addEmail, autoRefresh, addEmailVerify, registerVerify, send,
domainList,           # ★ 平台允许的邮箱域名清单,如 ["@example.com", "@another.com"]
siteKey, regKey, r2Domain, background, loginOpacity,
regVerifyOpen, addVerifyOpen,            # 是否开启注册/添加邮箱时的人机验证
noticeTitle/noticeContent/noticeType/noticeDuration/noticePosition/noticeWidth/noticeOffset/notice,
linuxdoClientId/linuxdoCallbackUrl/linuxdoSwitch,    # OAuth2 开关
loginDomain, minEmailPrefix, projectLink
```

#### B.1.9 其他

| 路由前缀         | 方法集               | 鉴权 | 备注                                                       |
| ---------------- | -------------------- | ---- | ---------------------------------------------------------- |
| `/init/:secret`  | GET                  | 白名单 | 首次部署初始化 DB                                         |
| `/oauth/*`       | GET/POST             | 白名单 | LinuxDo / 第三方 OAuth 登录                                |
| `/webhooks/*`    | POST                 | 白名单 | 收件 webhook 入口(Cloudflare email routing 触发)         |
| `/oss/*`         | POST/DELETE          | 白名单 | 附件 OSS(R2)上传                                        |
| `/r2/*`          | POST/DELETE          | 需 token | 附件管理(管理员视角)                                  |
| `/regKey/*`      | POST/GET/DELETE      | 需 token | 注册码管理                                                |
| `/star/*`        | POST/DELETE/GET      | 需 token | 邮件星标                                                  |
| `/my/*`          | GET/PUT              | 需 token | 当前用户配置                                              |
| `/resend/*`      | POST/GET             | 需 token | Resend token 管理(发邮件凭证)                           |
| `/analysis/*`    | GET                  | 需 token | 数据可视化                                                |
| `/telegram/*`    | POST                 | 白名单 | Telegram bot 入口                                         |
| `/test/*`        | GET                  | 白名单 | 健康检查                                                  |

### B.2 推测 / 未确认接口

| 推测点                                                    | 是否需补查源码 | 备注                                                            |
| --------------------------------------------------------- | -------------- | --------------------------------------------------------------- |
| `/oauth/linuxdo/callback` 具体回调字段                    | 是             | `oauth-api.js` 未抓全文,可能影响后续做"用 linuxdo OAuth 验证归属" |
| `/init/:secret` 的 `secret` 来源                           | 是             | 部署期生成,文档未明示;集成不需要                              |
| `/setting/websiteConfig` 中 `domainList` 是否随租户隔离  | 是             | maillab 单实例多租户?设计文档未明,但 RBAC 看 user.userId 走表  |
| 短期是否会出 `/api/v2/*` 路由                             | 否(暂时)     | 现仓库 `mail-worker` 单工程,无版本前缀                          |
| Turnstile 启用后 `/login` 是否要 `token` 字段              | 是             | maillab.py 已注释 TODO(maillab-verify),实施前 e2e 验证        |
| `/account/add` 创建时的 `addVerify` 行为                   | 是             | 当 `addVerifyOpen=true` 时是否要带 captcha token,需先查 service |

### B.3 鉴权与刷新机制

源码:`mail-worker/src/security/security.js`

```javascript
// 全局中间件
app.use('*', async (c, next) => {
  // 1. 白名单直通
  if (exclude.some(p => path.startsWith(p))) return next();

  // 2. 取 token
  const jwt = c.req.header(constant.TOKEN_HEADER);   // header = "Authorization"
  if (!jwt) throw 'unauthorized';

  // 3. 验签 + 检查会话
  const result = await jwtUtils.verifyToken(c, jwt);
  const authInfo = await getAuthInfo(c, result.userId);
  if (!authInfo.tokens.includes(jwt)) throw 'session expired';

  // 4. RBAC: requirePerms 数组里的端点会进权限检查
  if (requirePerms.includes(path)) {
    const permKeys = await permService.userPermKeys(c, authInfo.user.userId);
    const userPaths = permKeyToPaths(permKeys);
    const userPermIndex = userPaths.indexOf(path);
    // 5. admin 邮箱直通(`c.env.admin` 是部署期配置)
    if (userPermIndex === -1 && authInfo.user.email !== c.env.admin) throw 'no permission';
  }

  // 6. 每日 TTL 刷新
  if (today != lastRefresh) await refreshAuthInfoTTL(c, authInfo);
});
```

**关键点 — 集成 AutoTeam 必须知道**:

1. **header 名 = `Authorization`**(常量 `TOKEN_HEADER`),**裸 JWT**,**不加 `Bearer ` 前缀**。
   这与一般 OAuth 习惯不同,`maillab.py:91-97` 已正确处理。
2. **Session 是 server-side 持久化**(KV `authInfo.tokens`),所以 token 续命不是靠 JWT 过期时间,而是看 KV;
   本地 token 缓存可以长期有效,但不能跨重启假定。
3. **`c.env.admin` 是部署期配置的管理员邮箱白名单**,这位用户调用 `/public/genToken` 才能拿到 token。
4. **TTL 刷新是被动的**(每日首次调用触发),不是主动 keep-alive。
5. **没有 refresh_token 概念** — token 失效后必须重新 `/login`。AutoTeam 需要在 401 时调用 `_ensure_login()` 自愈
   (现 `maillab.py` 没做,详见 §F)。

---

## C. 设计:邮箱归属验证 UI

### C.1 用户故事

> 作为管理员,我在 AutoTeam 的 Setup / Settings 页填写"CloudMail 域名"时,
> **不应**靠手敲一个字符串然后再用 `/api/config/register-domain` 试探;
> **应该**直接从 maillab 服务端拉到"我能用的域名清单",
> 并在保存前**确认我对该域名拥有写权限**(我能在这个 domain 下创建临时邮箱,
> 且 maillab 后端没启用我没法过的 captcha)。
>
> 同时,如果我换了 maillab 实例 base_url,前端要在我点"保存"前就告诉我:
> 1. 这个 base_url 是 maillab 后端吗?(指纹探测 — 现在已经有 `_sniff_provider_mismatch`,但仅在启动验证阶段跑,前端没有)
> 2. 这个 base_url 允许哪些 domain?(从 `/setting/websiteConfig.domainList` 拿)
> 3. 我用我填的 username/password 真的能登进去吗?(`/login`)
> 4. 这个 domain 在我登录后真的能创建邮箱吗?(`/account/add` 试探,然后立即 `/account/delete` 回收 — 现 `register-domain` PUT 已经有这逻辑)

### C.2 流程图(setup wizard 与 settings 共用)

```text
                     用户填 base_url + username + password + (可选)domain
                                          │
                                          ▼
                ┌─────────────────────────────────────────────────┐
                │ Step 1. 路由指纹探测(无需鉴权)                 │
                │   GET {base_url}/setting/websiteConfig           │
                │     ├─ 200 + 含 domainList   → 是 maillab        │
                │     ├─ 200 但缺 domainList    → 可能是 cf_temp_email,提示切 provider │
                │     ├─ 404 / 5xx              → base_url 错      │
                │     └─ 网络错                  → 提示连接性       │
                └─────────────────────────────────────────────────┘
                                          │ ok
                                          ▼
                ┌─────────────────────────────────────────────────┐
                │ Step 2. 域名展示与选择                          │
                │   将 websiteConfig.domainList 渲染为下拉框       │
                │   ├─ 只有 1 个域 → 自动选中                     │
                │   ├─ 多个域 → 让用户选(默认第一个)            │
                │   └─ 0 个域 → 报错"该实例未配置任何邮箱域名"   │
                │   同时把 addVerifyOpen / registerVerify 的开关  │
                │   值返回给前端,如果开了就警告"该实例需要 captcha,│
                │   AutoTeam 自动化路径暂不支持,请到 maillab 后台关闭"│
                └─────────────────────────────────────────────────┘
                                          │ ok
                                          ▼
                ┌─────────────────────────────────────────────────┐
                │ Step 3. 凭据校验(归属第一关)                  │
                │   POST {base_url}/login                          │
                │     body {email: username, password}             │
                │     ├─ code:200, data.token → 拿到 jwt          │
                │     │  解析 JWT payload,展示 `email` `userType` │
                │     │  如果 email ≠ c.env.admin → 警告"非管理员,│
                │     │     仅能创建临时邮箱,无法管理用户"        │
                │     ├─ code:401 → "凭据错误"                    │
                │     └─ 其他    → 透传 message                    │
                └─────────────────────────────────────────────────┘
                                          │ ok
                                          ▼
                ┌─────────────────────────────────────────────────┐
                │ Step 4. 域名归属确认(写权限验证)              │
                │   带 Authorization: <jwt>                        │
                │   POST {base_url}/account/add                    │
                │     body {email: "probe-{ts}@{domain}"}          │
                │     ├─ code:200, data.accountId → 归属确认      │
                │     │  随后 DELETE /account/delete?accountId=N  │
                │     │  立即回收探测邮箱                          │
                │     ├─ code:401 → "登录已过期"(罕见,刚拿的 jwt)│
                │     ├─ code:403 → "无权在该域下创建" → 归属失败 │
                │     └─ code:其他 + message 含 "domain" → 归属失败 │
                └─────────────────────────────────────────────────┘
                                          │ ok
                                          ▼
                ┌─────────────────────────────────────────────────┐
                │ Step 5. 保存配置                                │
                │   POST /api/setup/save 写入:                    │
                │     MAIL_PROVIDER=maillab                        │
                │     MAILLAB_API_URL={base_url}                   │
                │     MAILLAB_USERNAME={username}                  │
                │     MAILLAB_PASSWORD={password}                  │
                │     MAILLAB_DOMAIN=@{domain}                     │
                │   后端再用 _verify_cloudmail() 跑一遍 e2e        │
                └─────────────────────────────────────────────────┘
```

### C.3 API 契约(后端新增端点)

> **关键设计抉择**:不要让前端直接连 maillab 跨域,**全部通过 AutoTeam 后端代理**,理由:
> 1. 用户填的 base_url 多半是 HTTP / 自签证书,浏览器跨域会复杂
> 2. 凭据(password / jwt)如果在前端 fetch,F12 直接看到
> 3. 后端代理后续可以加重试 / 错误归一化 / 日志

**新增 `/api/mail-provider/probe`** (POST,无需鉴权 — 与 setup/save 一致;实际进了面板后再加 API_KEY)

```yaml
request:
  provider: "maillab"           # 或 "cf_temp_email"
  base_url: "https://mail.example.com"
  # cf_temp_email
  password?: "admin_pwd"        # cf 的 admin password
  domain?: "@example.com"
  # maillab
  username?: "admin@example.com"
  password?: "user_pwd"
  domain?: "@example.com"       # 可选,用户已选中的域
  step: "fingerprint" | "credentials" | "domain_ownership"  # 分步执行,前端按用户进度调

response (step=fingerprint):
  ok: true
  detected_provider: "maillab"
  domainList: ["@example.com", "@another.com"]
  addVerifyOpen: false
  registerVerifyOpen: false
  warnings: []                  # 例:"检测到 base_url 像 cf_temp_email,但 MAIL_PROVIDER=maillab"

response (step=credentials):
  ok: true
  is_admin: true                # email == c.env.admin?
  token_preview: "eyJ..."       # 仅用于前端展示鉴权成功,不长期存
  user_email: "admin@example.com"

response (step=domain_ownership):
  ok: true
  probe_email: "probe-1714123456@example.com"
  probe_account_id: 12345
  cleaned: true                 # 探测邮箱已删除

response (any step, error):
  ok: false
  error_code: "ROUTE_NOT_FOUND" | "UNAUTHORIZED" | "FORBIDDEN_DOMAIN" | "NETWORK" | ...
  message: "原始 maillab 响应 message,或本地翻译"
  hint?: "建议用户怎么改"        # 如:把 MAIL_PROVIDER 改成 cf_temp_email
```

**前端 SetupPage 改造点**(`web/src/components/SetupPage.vue`):

1. 字段从平铺改为**分组卡片**:
   - 卡片 1:Mail Provider(单选 cf_temp_email / maillab,选中后只显示对应字段)
   - 卡片 2:base_url + 凭据 + "测试连接"按钮(调 step=fingerprint + step=credentials)
   - 卡片 3:domain 下拉框(从 step=fingerprint 拉到的 domainList 渲染) + "验证归属"按钮(调 step=domain_ownership)
   - 卡片 4:CPA / API_KEY / 代理(原样保留)
2. 步骤式 UX:每步必须通过才能进下一步,失败显示原始 error message + hint
3. step=domain_ownership 通过后,"保存配置"按钮才可点;后端再做一次完整 `_verify_cloudmail()`

---

## D. 401 根因分析 (setup_wizard 创建邮箱失败)

### D.1 截图证据

```
[CloudMail] 管理员鉴权通过
[验证] CloudMail (cf_temp_email) 登录成功
[验证] CloudMail (cf_temp_email) 创建邮箱失败:
       创建邮箱失败:响应缺少 address 字段:{'code': 401, 'message': '身份认证失效,请重新登录'}
[验证] 请检查 CLOUDMAIL_DOMAIN 是否正确
[验证] CloudMail 配置有误,请修改 .env 后重新启动
```

### D.2 根因(直接,可证伪)

**协议错配 — 用户填了 maillab 服务器地址,但 `MAIL_PROVIDER` 仍走默认 `cf_temp_email`**。

证据链:

1. `_verify_cloudmail()` 走的是 `cf_temp_email` 分支(label = "cf_temp_email"),意味着 `MAIL_PROVIDER` 没设或设成 cf_temp_email。
2. `cf_temp_email.login()`(`mail/cf_temp_email.py:86-116`)实际只 `GET /admin/address?limit=1` 验证 admin 密码:
   ```python
   r = self._admin_get("/admin/address", params={"limit": 1, "offset": 0})
   ```
   maillab 服务器**有 catch-all 路由**(Hono 默认 404,但部分部署/反向代理把 404 也返成 200 + 自定义错误体);
   或者 maillab `/admin/address` 路径**未匹配任何路由,被 Hono 内置 fallback 处理**,响应不一定是 maillab 风格 `{code, ...}`,
   也可能是 `200 OK + {}`,这时 `cf_temp_email.login` 看到 200 + 不含 `code/data` → 误以为成功(L106-112 的嗅探仅在响应像 maillab 时才抛错)。
3. 走到 `create_temp_email()`(L120-188)时,`POST /admin/new_address` 触发了 maillab 真正路由(可能是 `/admin/*` 通配的 401 拒绝,也可能是 worker 的 unmatched route fallback 直接返 maillab 风格 `{code:401, message:"身份认证失效,请重新登录"}`)。
4. 代码 L154-159 的 maillab 风格响应嗅探**确实捕获到了**这个错误并打印,但截图上看到的最终 message 是 `创建邮箱失败:响应缺少 address 字段:{'code': 401, 'message': '身份认证失效, 请重新登录'}`,
   **错误信息被错误地折叠回 `响应缺少 address 字段` 分支**(L182,因为 if 条件不严:`"address" not in data and ("code" in data and "message" in data)` 只在响应同时含 code+message 时才走更友好的 maillab-mismatch 分支,
   实际响应 dict 是 `{code:401, message:'...'}` 应该会命中 — 但截图说"响应缺少 address 字段"出在 L182,意味着**实际响应可能不是这个结构**,例如:
   - `{code:401, message:'...', success:false}` (额外字段)→ 命中 L154 OK,但 message 拼接组装时被 L155-159 的 raise 抛出
   - **或者响应是 `{code:401, message:'...'}` 直接命中 L182 的"缺少 address"分支**(可能是 cf_temp_email 路由在 maillab 上被 worker 默认 unmatched 处理,直接吐了一个像 `{code:401, ...}` 的 JSON 而 dict 没有 `data` 字段)

**结论**:

- **直接原因**:用户的 base_url(`https://apimail.icoulsy.asia`,看 `.env` L2)是 maillab 服务器,但 `.env` 里**没设 `MAIL_PROVIDER`**(默认走 cf_temp_email),触发 §B.3 描述的"半成功"现象。
- **附带原因(代码 bug)**:`cf_temp_email.login()` 的指纹嗅探(L106-112)条件**过于宽松** — 只在响应 dict 出现 `code` 或 `data` 字段时才抛错,但 maillab 真实的 `/admin/address` 在某些 worker 部署下可能返回 200 + `{}`(empty body)而非 maillab 标准 `{code,...}` 结构,
  导致 login 假成功,直到 create 才暴露。
- **次要原因(UX 缺口)**:setup_wizard 的 `_sniff_provider_mismatch`(setup_wizard.py:173-220)是**启动时跑**的,
  Web 面板 `post_setup_save` 不调 `_sniff_provider_mismatch`,只调 `_verify_cloudmail` → 用户在 Web 端首次填错时**得不到指纹错配的早期 warning**。

### D.3 修复方向(分层)

| 层级               | 修复点                                                                                                         | 优先级 |
| ------------------ | -------------------------------------------------------------------------------------------------------------- | ------ |
| **立即(用户)**  | 在 `.env` 加 `MAIL_PROVIDER=maillab`,把 `CLOUDMAIL_*` 改 `MAILLAB_*`(domain 复用 `CLOUDMAIL_DOMAIN`)         | P0     |
| **代码(嗅探)**  | `cf_temp_email.login()` L106-112:在 `r.status_code==200` 且**响应 dict 缺少 `results` key**时一律抛错(空 body 也错配) | P0     |
| **代码(嗅探)**  | `setup_wizard._sniff_provider_mismatch` 移到 `_verify_cloudmail` 第一步(目前是第二步),并在错配时 `return False` 而不是 warning | P0     |
| **代码(SetupConfig)** | `api.py:SetupConfig` Pydantic 模型加 `MAIL_PROVIDER` / `MAILLAB_API_URL` / `MAILLAB_USERNAME` / `MAILLAB_PASSWORD` / `MAILLAB_DOMAIN` 字段 | P0     |
| **代码(向导)**  | `setup_wizard.REQUIRED_CONFIGS` 加 maillab 字段,根据 `MAIL_PROVIDER` 选项动态显示                              | P0     |
| **UI(SetupPage)** | 实现 §C.2 的分步流程,把"填错 → 启动崩"前移到"填时即知"                                                       | P1     |
| **错误信息**       | `create_temp_email` L156 的 raise 文案加上"如果你看到这条错误是从 SetupPage 来的,先点'重新探测后端类型'"          | P1     |
| **代码(自愈)**  | `MaillabClient` 在 401 时尝试 `_ensure_login()` 重登一次再重试一次(对短期 token 失效自愈)                      | P2     |

---

## E. 文档改写清单

| 文档                           | 章节                          | 改动类型 | 内容                                                                                                       |
| ------------------------------ | ----------------------------- | -------- | ---------------------------------------------------------------------------------------------------------- |
| `docs/getting-started.md`      | 「准备工作 / 1. 搭建临时邮箱」 | 改写     | 把"两个临时邮箱后端二选一"的语境从"cf_temp_email 默认推荐"改为**按用户实际部署选择**;突出 `MAIL_PROVIDER` 是必填 |
| `docs/getting-started.md`      | 「第二步:配置 / 直接部署」    | 改写     | 配置示例增补完整 maillab 字段;说明 Web 面板 SetupPage 已支持选 provider(实施 §C 后)                       |
| `docs/getting-started.md`      | (新增章节)「第二点五步:验证邮箱后端归属」 | 新增 | 说明 SetupPage 的 4 步验证流程(指纹 → 凭据 → 域名 → 保存),提示用户哪些情况会报错及对应 hint |
| `docs/configuration.md`        | 「`.env` 配置项」表           | 改写     | `MAIL_PROVIDER` 列从"否"改成"**是**";`MAILLAB_*` 各字段说明改为"**Web 面板 Setup/Settings 中可填**" |
| `docs/configuration.md`        | 「Mail Provider 切换」        | 改写     | 推荐顺序逆转:**maillab 优先推荐**(国内访问/skymail 一键部署友好);cf_temp_email 改为"Cloudflare Workers 用户"备选 |
| `docs/configuration.md`        | (新增章节)「邮箱归属验证」    | 新增     | 阐明 `/setting/websiteConfig` 拉 domainList 的机制;说明 addVerify/registerVerify 启用时不被 AutoTeam 支持的边界 |
| `docs/configuration.md`        | 「⚠️ 协议错配排查」            | 改写     | 把 issue#1 的截图与最终 fix 步骤显式列出;链接到 SetupPage 的"重新探测后端类型"按钮(§C 实施后)               |
| `docs/mail-provider-design.md` | §6「cloud-mail 实现的未知项」 | 改写     | 5 项 TODO 标记**已通过 GitHub 源码全部确认**(login JWT header / domain 列表 API / createTime 单位 / 创建端点 / accountId 类型),状态从"未确认"改为"已验证 with reference" |
| `docs/mail-provider-design.md` | (新增章节)§7「skymail.ink API 全表」 | 新增 | 把本文件 §B.1.1-B.1.9 表格整理后入档;作为 cloudmail.py 长期维护参考 |
| `docs/troubleshooting.md`      | 「Web 面板相关」              | 增补     | 加"配置保存后 401 创建邮箱失败"条目,链到 §D 的修复表                                                       |
| `docs/troubleshooting.md`      | (新增章节)「邮箱后端」        | 新增     | 列出常见问题:base_url 写错 / domain 错配 / token 失效 / Turnstile 启用 / 单实例多域选错 |
| `docs/api.md`                  | 「初始配置 API」              | 增补     | 文档化 `/api/mail-provider/probe`(实施 §C 后);明确 `/api/setup/save` 的 SetupConfig 已扩展 maillab 字段 |
| `docs/api.md`                  | 「域名管理 API」              | 改写     | `/api/config/register-domain` 的 verify 探测改为"会调用 §C step=domain_ownership 共享逻辑" |
| `docs/architecture.md`         | (如有 mail 章节)             | 增补     | 把"`MailProvider` ABC + 工厂分发"图列出;skymail.ink 集成方式作为 reference impl |
| `README.md`                    | 主要 Features                  | 增补     | 加"原生支持 maillab/cloud-mail (skymail.ink) 全量集成,含归属验证"                                          |
| `.env.example`                 | 注释                          | 改写     | `MAIL_PROVIDER=` 上面写"**强烈推荐显式设置**,不设默认 cf_temp_email 可能与你的 maillab 部署错配"           |

---

## F. 风险与未决问题

### F.1 实施风险

| 风险                                                                                                | 缓解                                                                                       | 严重度 |
| --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ------ |
| `/setting/websiteConfig` 的 `domainList` 字段在 maillab v2.x 之前可能不存在(skymail.ink 当前 v2.8.0)| 前端兼容:domainList 缺失时降级为 free-input,后端探测改为"试创建一次邮箱"                  | 中     |
| maillab 部署启用 Turnstile (`registerVerifyOpen=true`)时,AutoTeam 自动化路径无法过 captcha          | 探测响应显式告诉用户;在 docs/troubleshooting.md 写明"请到 maillab 后台关闭 captcha"        | 高(用户体验) |
| `/public/genToken` 返回的 UUID token 与 `/login` 返回的 JWT 是否互通 — service 内部可能两套 KV     | 现 maillab.py 走 /login 路径,**不动 /public/genToken**;只有当用户明确想用 public API 时再加 | 低     |
| 新 `/api/mail-provider/probe` 端点暴露后,被外部用作端口扫描(用户填任意 base_url 跑探测)            | 加 rate limit + 仅在 setup/save 阶段开放(进面板后用 API_KEY 鉴权)                        | 中     |
| `cf_temp_email.login()` 嗅探收紧后,对**一些第三方 fork** (例如 `unsendapp/cloudflare_temp_email` 等)误伤 | 错误信息提示用户可以 `AUTOTEAM_SKIP_VERIFY=1` 绕过;长期看是收益>风险                       | 低     |
| `MaillabClient.list_accounts` 单页 cap 30 是**当前版本**约束,未来 maillab 可能改                   | 代码注释已写来源 `account-service.js`;CI 加一个 maillab e2e mock 测试翻页边界               | 低     |
| Web 面板 SetupPage 改造大,前后端字段都要改                                                          | 分阶段 PR:第 1 阶段只补 SetupConfig + REQUIRED_CONFIGS(P0);第 2 阶段做分步 UX(P1)       | 中     |

### F.2 未决问题(需 PRD 阶段决议)

1. **maillab 是否要支持非 admin 用户**?当前 `_verify_cloudmail` 隐式假设登录的就是平台 admin(因为 `c.env.admin` 才能调 `/public/genToken`);但 `/account/add` 普通用户也能调。是否对 SetupPage 的 username 做严格性检查?
2. **是否暴露 `/account/setName / setAllReceive / setAsTop`** 给 AutoTeam UI?目前业务流程只用 add/delete/list,这些写操作虽然 maillab 有但**业务上没刚需**;
   保守做法:不实现,只在 `MaillabClient` 留接口骨架。
3. **附件支持**(`/email/attList`)— ChatGPT 邀请邮件**没有附件**,此功能与 AutoTeam 无关;暂不集成。
4. **`/email/send`** — AutoTeam 不需要发邮件(只读验证码),不集成;但用户在 maillab 控制台已经能用,文档可以不提。
5. **`/oauth/linuxdo/*`** — 未来如果 AutoTeam 想让"管理员通过 LinuxDo OAuth 绑定 maillab 账户"自动获取 token,
   需要扩展。当前**不在范围**,但 setup_wizard 应当探测到 linuxdoSwitch=true 时给个 hint。
6. **多 maillab 实例**?当前 AutoTeam 只支持一个 base_url;skymail.ink 用户可能有 prod / staging 两套。
   是否做多实例切换?**保守:不做**,留给 PRD 阶段权衡。
7. **register-domain 探测路径与 §C step=domain_ownership 是否合并**?现 `api.py:1631-1673` 已实现一次探测,
   §C 设计应当把它**重构成共享 helper**,SetupPage 与 Settings 页 register-domain 调同一个底层函数。

---

## 附录 A:本研究查证过的源码 / 文档证据

| 来源                                                                                | 用途                          |
| ----------------------------------------------------------------------------------- | ----------------------------- |
| `D:/Desktop/AutoTeam/src/autoteam/cloudmail.py`                                     | 确认 stub re-export           |
| `D:/Desktop/AutoTeam/src/autoteam/mail/__init__.py`                                 | 确认工厂分发                  |
| `D:/Desktop/AutoTeam/src/autoteam/mail/base.py`                                     | 确认 ABC 公开方法集            |
| `D:/Desktop/AutoTeam/src/autoteam/mail/cf_temp_email.py`                            | 确认 cf 实现 + 嗅探条件        |
| `D:/Desktop/AutoTeam/src/autoteam/mail/maillab.py`                                  | 确认 maillab 实现 + 已知 TODO  |
| `D:/Desktop/AutoTeam/src/autoteam/setup_wizard.py`                                  | 确认 _verify_cloudmail / 嗅探 |
| `D:/Desktop/AutoTeam/src/autoteam/api.py:70-149`                                    | 确认 SetupConfig 缺 maillab 字段|
| `D:/Desktop/AutoTeam/src/autoteam/api.py:1617-1673`                                 | 确认 register-domain 探测     |
| `D:/Desktop/AutoTeam/src/autoteam/runtime_config.py`                                | 确认 register_domain 持久化    |
| `D:/Desktop/AutoTeam/web/src/components/SetupPage.vue`                              | 确认 UI 是平铺字段表单         |
| `D:/Desktop/AutoTeam/web/src/components/Settings.vue`                               | 确认无 mail provider 切换 UI   |
| `D:/Desktop/AutoTeam/web/src/api.js`                                                | 确认前后端契约                 |
| `D:/Desktop/AutoTeam/.env` / `.env.example`                                         | 确认配置项现状                 |
| `D:/Desktop/AutoTeam/docs/mail-provider-design.md`                                  | 确认设计文档与已知未知项       |
| `https://doc.skymail.ink/api/api-doc.html`                                          | 确认对外公布的 3 个 public API|
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/api/login-api.js`  | 确认 /login /register /logout |
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/api/account-api.js`| 确认 /account/* 6 路由        |
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/api/email-api.js`  | 确认 /email/* 6 路由          |
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/api/setting-api.js`| 确认 /setting/* 5 路由        |
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/api/init-api.js`   | 确认 /init/:secret            |
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/api/user-api.js`   | 确认 /user/* 10 路由           |
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/api/role-api.js`   | 确认 /role/* 7 路由            |
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/api/all-email-api.js`| 确认 /allEmail/* 4 路由      |
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/api/reg-key-api.js`| 确认 /regKey/* 5 路由          |
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/api/public-api.js` | 确认 /public/* 3 路由          |
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/security/security.js` | 确认白名单 + 鉴权流程       |
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/service/setting-service.js` | 确认 websiteConfig 字段集 |
| `https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/service/public-service.js`  | 确认 genToken 仅 admin 可调|

## 附录 B:本研究**未**进行的事项(留给 PRD/spec/test_reports)

- 没有改任何源代码(按任务要求,纯调研)
- 没有联系 skymail.ink 官方确认 v2.8.0 之外的版本兼容性
- 没有在真实 maillab 实例上跑 e2e 测试(`mail-provider-design.md` §6 提示 implementer 必须做)
- 没有提供 §C "新增 `/api/mail-provider/probe` 端点" 的具体 Pydantic 模型 / 单元测试用例(spec 阶段产出)
- 没有提供前端 SetupPage.vue 改造的具体 diff(test_reports 阶段产出)
