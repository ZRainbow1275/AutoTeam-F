# Mail Provider 抽象层设计

> 基于 task #1 (contract-auditor) + task #2 (upstream-researcher) 的产出。
> 目标：让 AutoTeam 同时支持 `dreamhunter2333/cloudflare_temp_email`（现状）与
> `maillab/cloud-mail`（社区想用的真正"cloudmail"），且不破坏现有调用方。

---

## 0. 现状问题（一句话）

`cloudmail.py` 把 `dreamhunter2333/cloudflare_temp_email` 的 `/admin/*` 路由 +
`x-admin-auth` header 硬编码成 `CloudMailClient`。命名误导，无法切换后端。

调用面：当前 `CloudMailClient` 在 8 个文件、约 **31 个调用点**被实例化或调用
（`api.py`、`manager.py`、`codex_auth.py`、`invite.py`、`account_ops.py`、
`setup_wizard.py`、`accounts.py` 间接、tests）。任何抽象层改造必须保持向后兼容。

---

## 1. MailProvider 抽象基类

放在新文件 `src/autoteam/mail/base.py`，使用 `abc.ABC`。
方法集**严格覆盖** `cloudmail.py` 现有公开方法，不增不减。

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Email:
    """统一邮件 IR — provider 无关的中间表示。"""
    id: int                       # provider 主键，统一为 int（cf 用 mails.id；maillab 用 emailId）
    recipient: str                # 收件人邮箱（小写）
    sender: str                   # 发件人邮箱
    subject: str
    text: str | None              # 纯文本正文（cf 解析自 MIME；maillab 取 text 字段）
    html: str | None              # HTML 正文（cf 解析自 MIME；maillab 取 content/message）
    received_at: int              # epoch seconds（cf created_at 转换；maillab createTime 转换）
    raw: dict = field(default_factory=dict)  # provider 原始结构，做兜底/调试


@dataclass
class Account:
    """临时邮箱账户。"""
    account_id: int               # provider 主键
    email: str                    # 完整邮箱地址（小写）
    password: str | None = None   # cf 不返；maillab 可能返
    create_time: int | None = None
    extra: dict = field(default_factory=dict)  # 扩展字段（jwt、mail_count 等）


class MailProvider(ABC):
    """所有 mail backend 必须实现的接口。命名/语义保持与现有 CloudMailClient 调用方一致。"""

    # ---- 鉴权 ----
    @abstractmethod
    def login(self) -> str:
        """初始化鉴权，返回一个不透明 token 字符串（仅用于日志展示）。失败抛 Exception。"""

    # ---- 账户管理 ----
    @abstractmethod
    def create_temp_email(self, prefix: str | None = None, domain: str | None = None) -> tuple[int, str]:
        """创建临时邮箱，返回 (account_id, email)。"""

    @abstractmethod
    def list_accounts(self, size: int = 200) -> list[dict]:
        """列出已创建的临时邮箱。返回与现版兼容的 dict 列表（保留 accountId/email/...）。"""

    @abstractmethod
    def delete_account(self, account_id: int | str) -> dict:
        """删除账户。account_id 允许是数字 id 或完整 email（自动解析）。返回 {code, message?}。"""

    # ---- 邮件读取 ----
    @abstractmethod
    def search_emails_by_recipient(
        self, to_email: str, size: int = 10, account_id: int | None = None
    ) -> list[dict]:
        """按收件人查邮件（最新优先）。返回 dict 列表（兼容字段，详见 §2.1）。"""

    @abstractmethod
    def list_emails(self, account_id: int | str, size: int = 10) -> list[dict]:
        """按 account_id 查邮件。账户名解析交给 provider 自己实现。"""

    def get_latest_emails(self, account_id: int | str, email_id: int = 0, all_receive: int = 0) -> list[dict]:
        """旧名兼容；默认实现委托 list_emails。子类可覆写。"""
        return self.list_emails(account_id, size=5)

    # ---- 邮件删除 ----
    @abstractmethod
    def delete_emails_for(self, to_email: str) -> int:
        """删除指定收件人的全部邮件，返回删除数量（或 1 表示批量成功）。"""

    # ---- 等待 / OTP / 链接（共用实现，不抽象） ----
    def wait_for_email(self, to_email: str, timeout: int | None = None, sender_keyword: str | None = None) -> dict:
        """轮询等待。base.py 提供默认实现：循环调用 search_emails_by_recipient + sender_keyword 过滤。"""
        ...  # 默认实现见 base.py（搬现 cloudmail.py 的逻辑）

    @staticmethod
    def extract_verification_code(email_data: dict) -> str | None:
        """从邮件 text/html 提取 6 位 OTP。纯文本处理，与 provider 无关 → 放 base 静态方法。"""
        ...

    @staticmethod
    def extract_invite_link(email_data: dict) -> str | None:
        """从邮件正文提取邀请链接。同上，纯文本处理。"""
        ...
```

### 1.1 设计原则

- **方法集与现 `CloudMailClient` 的公开 API 一一对应**——避免改 31 个调用点。
- **`Email` / `Account` dataclass 仅作内部 IR**；现阶段 `search_emails_by_recipient` 等方法对外
  仍返回 dict，等后续把调用方逐步迁到 dataclass 后再切。这样首版改造可以零回归。
- **`wait_for_email` / `extract_verification_code` / `extract_invite_link` 不抽象**：
  - 等待是 `search_emails_by_recipient` 之上的轮询循环；
  - OTP / 邀请链接抽取是纯文本正则，跟后端无关。
  - 全部放 base 类当 mixin，子类零改动。

---

## 2. 字段映射表

### 2.1 邮件 dict 字段（现调用方依赖的契约）

调用方实际只读这些 key（grep 结果证实）：
`emailId`、`accountEmail`、`receiveEmail`、`toEmail`、`sendEmail`、
`sender`、`subject`、`text`、`content`（=html）、`createTime`、`raw`。

| 统一字段        | cf_temp_email 来源                        | maillab 来源                          |
| --------------- | ----------------------------------------- | ------------------------------------- |
| `emailId`       | `mails.id`                                | `emailId`                             |
| `accountEmail`  | `mails.address`                           | `toEmail`                             |
| `receiveEmail`  | `mails.address`                           | `toEmail`                             |
| `toEmail`       | MIME `To:` header（_parse_mime 解出）     | `toEmail`                             |
| `sendEmail`     | `mails.source` 或 MIME `From:`            | `sendEmail`                           |
| `sender`        | MIME `From:`                              | `name`（发件人显示名）                |
| `subject`       | MIME `Subject:`                           | `subject`                             |
| `text`          | MIME text/plain part 解码                 | `text`（如缺则从 html 剥）            |
| `content`       | MIME text/html part 解码                  | `content` 或 `message`（待验证）      |
| `messageId`     | MIME `Message-ID:`                        | 无；置 None                           |
| `createTime`    | `mails.created_at`（已是 epoch）          | `createTime`（需检查是否要 ÷1000）    |
| `raw`           | `mails.raw`（原始 MIME 字符串）           | 整个 email 对象 dump                  |

### 2.2 Account dict 字段

| 统一字段     | cf_temp_email 来源 | maillab 来源                |
| ------------ | ------------------ | --------------------------- |
| `accountId`  | `address.id`       | `accountId` / `id`（待确认）|
| `email`      | `address.name`     | `email`                     |
| `password`   | `address.password` | 不返（创建时一次性给）      |
| `createTime` | `address.created_at` | `createTime`              |

---

## 3. 配置切换

### 3.1 环境变量

```bash
# ---- 选择后端 ----
MAIL_PROVIDER=cf_temp_email          # 或 maillab；缺省=cf_temp_email（保持现状）

# ---- cf_temp_email（现 CLOUDMAIL_*，保留向后兼容） ----
CLOUDMAIL_BASE_URL=https://your-domain.com/api  # cf_temp_email 必须包含 /api
CLOUDMAIL_PASSWORD=...               # 实际是 admin password
CLOUDMAIL_DOMAIN=@example.com
# CLOUDMAIL_EMAIL 标记为 deprecated；setup_wizard 不再强制要求

# ---- maillab（新增） ----
MAILLAB_API_URL=https://...
MAILLAB_USERNAME=admin@xxx
MAILLAB_PASSWORD=xxx
MAILLAB_DOMAIN=@xxx                  # 创建邮箱时用
```

**别名兼容**：当 `MAIL_PROVIDER=cf_temp_email` 且未设置 `CF_TEMP_EMAIL_*` 时，
工厂回落到读 `CLOUDMAIL_*`（避免逼用户改 .env）。

### 3.2 工厂函数

新文件 `src/autoteam/mail/__init__.py`：

```python
import os
from autoteam.mail.base import MailProvider

def get_mail_client() -> MailProvider:
    provider = (os.environ.get("MAIL_PROVIDER") or "cf_temp_email").strip().lower()
    if provider in ("cf_temp_email", "cloudflare_temp_email"):
        from autoteam.mail.cf_temp_email import CfTempEmailClient
        return CfTempEmailClient()
    if provider == "maillab":
        from autoteam.mail.maillab import MaillabClient
        return MaillabClient()
    raise ValueError(f"未知 MAIL_PROVIDER={provider}（可选: cf_temp_email | maillab）")

# 向后兼容别名 — 现有 31 处 `CloudMailClient()` 调用零改动
CloudMailClient = get_mail_client
```

> `CloudMailClient = get_mail_client` 让 `CloudMailClient()` 调用语法保持原样，
> 实际 dispatch 到具体 provider。

---

## 4. 命名争议解决（采纳方案 A：拆包）

**方案 A（采纳）**：拆 `src/autoteam/mail/` 包

```
src/autoteam/mail/
├── __init__.py        # 工厂 + CloudMailClient 别名
├── base.py            # MailProvider ABC + Email/Account dataclass + wait/extract 默认实现
├── cf_temp_email.py   # 从现 cloudmail.py 拆出，重命名为 CfTempEmailClient
└── maillab.py         # 新写
```

`cloudmail.py` 改为 1 行 stub：`from autoteam.mail import CloudMailClient  # noqa`，
保留是为了任何还没迁移的外部脚本可以继续 `from autoteam.cloudmail import CloudMailClient`。

**理由**（vs 方案 B 单文件多 class）：
1. **关注点分离**：cf_temp_email 现在 ~520 行，再塞一个 maillab provider 单文件会 > 1000 行难维护。
2. **测试可读性**：现 `tests/unit/test_cloudmail.py` 测的全是 cf 的 admin 路由；拆包后可
   并行新增 `test_maillab.py`，测试边界清晰。
3. **延迟 import**：工厂用 `if` 内 import，仅按需加载对应 provider 的依赖。

---

## 5. 改造计划

### 5.1 新建文件（4 个）

| 文件                                  | 行数估算 | 意图                                     |
| ------------------------------------- | -------- | ---------------------------------------- |
| `src/autoteam/mail/__init__.py`       | ~30      | 工厂 + 兼容别名                          |
| `src/autoteam/mail/base.py`           | ~150     | ABC、dataclass、wait/OTP/link 默认实现   |
| `src/autoteam/mail/cf_temp_email.py`  | ~430     | 把现 cloudmail.py 业务搬过来，重命名类   |
| `src/autoteam/mail/maillab.py`        | ~350     | 新写：login/email/list/delete + 字段映射 |
| `tests/unit/test_maillab.py`          | ~200     | 新增：覆盖 maillab provider              |

### 5.2 修改文件（4 个）

| 文件                                  | 行数估算 | 意图                                                              |
| ------------------------------------- | -------- | ----------------------------------------------------------------- |
| `src/autoteam/cloudmail.py`           | -510     | 缩为 1 行 re-export（删除业务代码，保 import 兼容）               |
| `src/autoteam/config.py`              | +10      | 新增 `MAIL_PROVIDER` / `MAILLAB_*` 读取；标记 `CLOUDMAIL_EMAIL` 废弃 |
| `.env.example`                        | +6 / -1  | 加 `MAIL_PROVIDER=cf_temp_email` 注释块 + maillab 段              |
| `src/autoteam/setup_wizard.py`        | ~10      | 不再强制要求 `CLOUDMAIL_EMAIL`；按 provider 走 if 分支             |
| `tests/unit/test_cloudmail.py`        | ~5       | 改 import 路径为 `autoteam.mail.cf_temp_email`，断言不变           |
| `docs/configuration.md`               | +20      | 加 mail provider 章节                                             |
| `README.md`                           | +5       | 修正"cloudmail 不等于 maillab/cloud-mail"的脚注                   |

**业务调用方零改动**（31 个调用点全部保持 `from autoteam.cloudmail import CloudMailClient` +
`CloudMailClient()` 语法不变）。

### 5.3 风险点

| 风险                                                               | 缓解                                                               |
| ------------------------------------------------------------------ | ------------------------------------------------------------------ |
| `CloudMailClient = get_mail_client`（class 实例化变函数调用）有副作用 | 工厂函数返回的对象就是 provider 实例；语法 `CloudMailClient()` 完全兼容 |
| `tests/unit/test_cloudmail.py` 直接 patch `cloudmail.requests`     | cf_temp_email.py 内的 `requests` 路径变了，需要把 patch target 改名 |
| 旧 `cloudmail.py` 留 stub 期间，`from autoteam.cloudmail import _parse_mime` 等私有函数可能有人引用 | grep 全仓 `_parse_mime / _decode_jwt_payload`：仅 cloudmail.py 内部用，**确认安全**（已查） |
| `MAIL_PROVIDER` 拼错（如 `MAILAB`）→ 启动崩 ValueError              | 错误信息列出可选值；setup_wizard 加交互式选择                       |
| maillab 后端字段差异未 100% 摸清（见 §6）                          | 保留 `extra` / `raw` dict 兜底，先实现 80% 路径，发布前小步对齐     |

### 5.4 调用点统计（用 Grep 数实数）

| 调用方                | `CloudMailClient()` 实例化 | `mail_client.<method>` 调用 |
| --------------------- | -------------------------- | --------------------------- |
| `manager.py`          | 11                         | 7                           |
| `api.py`              | 5                          | -                           |
| `invite.py`           | 1                          | 4                           |
| `codex_auth.py`       | 0（接收外部传入）          | 9                           |
| `account_ops.py`      | 1                          | 2                           |
| `setup_wizard.py`     | 1                          | 0                           |
| `tests/unit/`         | 6                          | -                           |
| **合计**              | **25**                     | **22**                      |

→ 总 47 处对 `CloudMailClient` 名称的引用，全部走"别名 → 工厂"零改动路径。

---

## 6. cloud-mail 实现的未知项 — close-out

implementer 开工前的 5 项现场验证已经全部 ✅ 完成,结论引用 `maillab/cloud-mail` 上游源码:

1. **HTML 正文字段名** ✅ — 字段为 `content`(对照 [`mail-vue/src/views/home/index.vue` 的 v-html 绑定](https://github.com/maillab/cloud-mail/blob/main/mail-vue/src/views/home/index.vue))。`text` 为去标签后的纯文本,`message` 仅在子站发送邮件回显时出现。
2. **鉴权 header** ✅ — 业务接口统一走 `Authorization: Bearer <token>`,token 由 `/login` 返 JSON Web Token,payload 含 `userId/email/userType`(`userType==1` 为 admin)。验证文件 [`mail-worker/src/middleware/auth.js`](https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/middleware/auth.js)。
3. **`createTime` 单位** ✅ — epoch **毫秒**(13 位),IR 转秒时需 ÷1000。验证 [`mail-worker/src/service/email-service.js`](https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/service/email-service.js) 中 `Date.now()` 写入逻辑。
4. **创建邮箱端点** ✅ — `POST /account/add` (admin) / `POST /account/register` (普通用户)。验证 [`mail-worker/src/api/account.js`](https://github.com/maillab/cloud-mail/blob/main/mail-worker/src/api/account.js)。
5. **`accountId` 类型** ✅ — 数字主键(int auto-increment),`/account/delete?accountId={int}`。

> 所有未知项已 close,`MaillabClient` 无需再做现场摸索。

---

## 7. skymail.ink API 全表(maillab/cloud-mail)

由 issue-1-cloudmail.md §B 整理,所有路由均要求 `Authorization: Bearer <token>`(除显式标注的免登录端点)。

| 端点 | 方法 | 用途 | 关键字段 |
|---|---|---|---|
| `/login` | POST | 管理员/普通用户登录 | `email` / `password` → `data.token` (JWT) |
| `/setting/websiteConfig` | GET | 站点配置(免登录) | `domainList[]` / `addVerify` / `register` |
| `/account/add` | POST | 创建临时邮箱(admin) | body `{email, name?, domain}` → `data.accountId` |
| `/account/register` | POST | 创建临时邮箱(普通用户) | 同上,但受 `register` 开关控制 |
| `/account/delete` | DELETE | 删除邮箱 | query `?accountId={int}` |
| `/account/list` | GET | 列出邮箱 | query `?size={n}&cursor={int}` 翻页 |
| `/email/list` | GET | 列邮件 | query `?accountId={int}&size={n}&cursor={int}` |
| `/email/info` | GET | 单封邮件详情 | query `?emailId={int}` |
| `/email/delete` | DELETE | 删邮件(单封 / 批量) | query `?emailId={int}` 或 `?accountId={int}&all=1` |
| `/auto-reply/setting` | GET/POST | 自动回复(本项目暂不用) | — |
| `/audit/list` | GET | 审计(admin) | — |

JWT payload 结构(`autoteam.mail.base.decode_jwt_payload` 解析):

```json
{ "userId": 1, "email": "admin@x.com", "userType": 1, "exp": 1234567890 }
```

`userType==1` 为 admin,probe `step=credentials` 检查该字段以决定是否标记 `is_admin=true`。

---

## 8. 401 自愈策略(SPEC-1 §3.3)

`MaillabClient` 的 `_get/_post/_delete/_put` 全部被 `_with_login_retry` 装饰器包裹:

- 任何业务方法收 `code:401` → 清 token,调 `login()` 重新登录,重试一次原方法。
- **双 guard** 防递归:
  - `_LOGIN_GUARD.in_login`:`login()` 内部 `/login` 自身回 401 时,装饰器立即透传,不再触发自愈(避免 token 错的死循环)。
  - `_LOGIN_GUARD.retried`:已重试一次后再 401 → 抛 `MaillabAuthFailed("重 login 后仍 401")`,日志含明确关键字,便于排查"凭据过期但仍能登录"等灰色场景。
- 线程局部:guard 通过 `threading.local()` 实现,API 端 + 后台任务并发不会互相影响。
- 日志关键字:
  - `[maillab] {path} 收到 code:401,自愈中...` — 触发自愈
  - `[maillab] {path} token 已自愈` — 自愈成功
  - 自愈失败抛 `MaillabAuthFailed`,堆栈定位调用点

测试覆盖见 `tests/unit/test_mail_maillab_self_heal.py`(3 个场景)。

---

## 附录 A：现 `cloudmail.py` 公开方法清单（确保抽象层 100% 覆盖）

```
login()
create_temp_email(prefix=None, domain=None) -> (id, email)
list_accounts(size=200) -> list[dict]
delete_account(account_id) -> dict
search_emails_by_recipient(to_email, size=10, account_id=None) -> list[dict]
list_emails(account_id, size=10) -> list[dict]
get_latest_emails(account_id, email_id=0, all_receive=0) -> list[dict]
delete_emails_for(to_email) -> int
wait_for_email(to_email, timeout=None, sender_keyword=None) -> dict
extract_verification_code(email_data) -> str | None
extract_invite_link(email_data) -> str | None
```

11 个公开方法 — §1 抽象类一一对应，无遗漏。

---

## 附录 B：实施顺序建议

1. 拉新分支 `feat/mail-provider-abstraction`。
2. 建 `src/autoteam/mail/` 目录骨架（base.py + 工厂 + stub cf/maillab）。
3. 把 cloudmail.py 业务搬到 `mail/cf_temp_email.py`，跑 `tests/unit/test_cloudmail.py` 全绿。
4. 改 `cloudmail.py` 为 1 行 re-export，再跑测试。
5. 实现 `mail/maillab.py`，按 §6 清单先做现场验证。
6. 写 `tests/unit/test_maillab.py`。
7. 更新 `setup_wizard.py` 走 provider 分支，更新 `.env.example` / `docs/configuration.md`。
8. 全量 e2e：在两个 provider 各跑一遍 manager 的注册流程。
