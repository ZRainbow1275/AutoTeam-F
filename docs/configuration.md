# 配置说明

## `.env` 配置项

首次运行任何命令时会自动进入配置向导，交互式填写必填项并验证连通性。也可以手动编辑：

```bash
cp .env.example .env
```

| 配置项 | 说明 | 必填 |
|--------|------|------|
| `MAIL_PROVIDER` | 临时邮箱后端,`cf_temp_email`(默认) 或 `maillab` | 否 |
| `CLOUDMAIL_BASE_URL` | cf_temp_email 后端的 API 地址 | `MAIL_PROVIDER=cf_temp_email` 时是 |
| `CLOUDMAIL_PASSWORD` | cf_temp_email 后端的管理员密码 | `MAIL_PROVIDER=cf_temp_email` 时是 |
| `CLOUDMAIL_DOMAIN` | 临时邮箱域名(如 `@example.com`) | 是 |
| `CLOUDMAIL_EMAIL` | 已废弃,保留只为兼容旧 `.env`;不再被使用 | 否 |
| `MAILLAB_API_URL` | maillab/cloud-mail 后端的 API 地址 | `MAIL_PROVIDER=maillab` 时是 |
| `MAILLAB_USERNAME` | maillab 主账号邮箱(用于登录) | `MAIL_PROVIDER=maillab` 时是 |
| `MAILLAB_PASSWORD` | maillab 主账号密码 | `MAIL_PROVIDER=maillab` 时是 |
| `MAILLAB_DOMAIN` | maillab 创建邮箱时的域名;缺省回落 `CLOUDMAIL_DOMAIN` | 否 |
| `CPA_URL` | CLIProxyAPI 地址 | 是（留空使用默认 `http://127.0.0.1:8317`） |
| `CPA_KEY` | CPA 管理密钥 | 是 |
| `API_KEY` | Web 面板 / API 鉴权密钥 | 是（首次启动可自动生成） |
| `PLAYWRIGHT_PROXY_URL` | Playwright 浏览器代理 URL，如 `socks5://user:pass@host:port` | 否 |
| `PLAYWRIGHT_PROXY_BYPASS` | Playwright 代理绕过列表，如 `localhost,127.0.0.1` | 否 |
| `AUTO_CHECK_THRESHOLD` | 额度低于此百分比触发轮转 | 否（默认 `10`） |
| `AUTO_CHECK_INTERVAL` | 巡检间隔（秒） | 否（默认 `300`） |
| `AUTO_CHECK_MIN_LOW` | 至少几个账号低于阈值才触发 | 否（默认 `2`） |
| `RECONCILE_KICK_ORPHAN` | 对账发现"残废"成员(workspace 有 active + 本地 `auth_file` 缺失)时是否自动 KICK。关掉则标记 `STATUS_ORPHAN` 等人工处理 | 否（默认 `true`） |
| `RECONCILE_KICK_GHOST` | 对账发现"ghost"成员(workspace 有但本地完全无记录)时是否自动 KICK。关掉则留给 `sync_account_states` 反向补录 | 否（默认 `true`） |

## 账号状态与席位字段

`accounts.json` 中每条记录的 `status` 枚举(常量见 `src/autoteam/accounts.py`):

| 状态 | 含义 |
|------|------|
| `active` | 在 Team 中且本地认为可用 |
| `exhausted` | 在 Team 中但额度耗尽,等待移出 |
| `standby` | 已移出 Team,等待后续复用 |
| `pending` | 注册 / 创建流程尚未完成 |
| `personal` | 已主动退出 Team,走个人号 Codex OAuth,不再参与 Team 轮转 |
| `auth_invalid` | `auth_file` token 已失效(401/403),等对账清理或重登。`cmd_check --include-standby` 探到 401/403 时会落到这个状态 |
| `orphan` | workspace 仍占席但本地 `auth_file` 缺失。`RECONCILE_KICK_ORPHAN=false` 时对账会把残废成员打上此标记而不 KICK,等人工补登 |

`seat_type` 字段标记该账号在 ChatGPT Team 里被授予的席位种类:

| seat_type | 含义 |
|-----------|------|
| `chatgpt` | 完整 ChatGPT 席位(PATCH `seat_type=default` 成功) |
| `codex` | 仅 Codex 席位(`usage_based`,PATCH 改 default 失败时的兜底) |
| `unknown` | 未知 / 老记录默认值,手动导入时若未指定也落在这里 |

`last_quota_check_at`(epoch 秒)记录最近一次 wham/usage 探测时间,供 `cmd_check --include-standby` 的 24h 去重使用。

## Mail Provider 切换

AutoTeam 支持两个临时邮箱后端,通过 `MAIL_PROVIDER` 环境变量切换:

| Provider          | 上游仓库                                 | 适配字段                                                  |
| ----------------- | ---------------------------------------- | --------------------------------------------------------- |
| `cf_temp_email`   | `dreamhunter2333/cloudflare_temp_email`  | `CLOUDMAIL_BASE_URL` / `CLOUDMAIL_PASSWORD` / `CLOUDMAIL_DOMAIN` |
| `maillab`         | `maillab/cloud-mail`                     | `MAILLAB_API_URL` / `MAILLAB_USERNAME` / `MAILLAB_PASSWORD` / `MAILLAB_DOMAIN` |

> 命名说明:旧版的 `CLOUDMAIL_*` 配置实际指向的是 `cloudflare_temp_email`,
> 与 `maillab/cloud-mail`(社区里另一个同名项目)是两个不同的后端,因此在
> v2026-04 起拆分了两套配置。`MAIL_PROVIDER` 缺省为 `cf_temp_email`,与历史行为完全一致。

切换方法:在 `.env` 中显式设置:

```dotenv
# 用社区 maillab/cloud-mail
MAIL_PROVIDER=maillab
MAILLAB_API_URL=https://your-maillab.example.com
MAILLAB_USERNAME=admin@example.com
MAILLAB_PASSWORD=xxx
MAILLAB_DOMAIN=@example.com
```

业务调用方零改动:`from autoteam.cloudmail import CloudMailClient` 仍然有效,
工厂会按 `MAIL_PROVIDER` 自动 dispatch 到对应 provider 实例。

## Playwright 代理

AutoTeam 的浏览器流量（ChatGPT 登录、邀请接受、Codex OAuth 等）现在支持单独配置代理。

推荐优先使用一个环境变量：

```dotenv
PLAYWRIGHT_PROXY_URL=socks5://host.docker.internal:1080
PLAYWRIGHT_PROXY_BYPASS=localhost,127.0.0.1
```

如果代理需要认证，也可以直接写进 URL：

```dotenv
PLAYWRIGHT_PROXY_URL=socks5://username:password@host.docker.internal:1080
```

说明：

- `PLAYWRIGHT_PROXY_URL` 会被解析为 Playwright 所需的 `server` / `username` / `password` 字段
- `PLAYWRIGHT_PROXY_BYPASS` 建议至少包含 `localhost,127.0.0.1`，避免本地回调或容器内本地服务误走代理

### 内联注释

`.env` 支持尾部内联注释，例如：

```env
AUTO_CHECK_INTERVAL=300  # 5 分钟
```

Windows / macOS 下也会按 UTF-8 正常读取。

## 管理员登录态

首次启动后，在 Web 面板「设置」页或命令行完成主号登录：

```bash
uv run autoteam admin-login
uv run autoteam admin-login --email you@example.com
```

系统会自动保存到 `state.json`，包括：
- 邮箱
- session token
- workspace ID
- workspace 名称
- 密码（如果你走的是密码登录）

## 主号 Codex 同步

`main-codex-sync` 用于把管理员主号的 Codex 登录态单独同步到 CPA。

- **前置条件**：先完成 `admin-login`
- **结果文件**：`auths/codex-main-*.json`
- **作用范围**：主号专用，不进入轮转池

```bash
uv run autoteam main-codex-sync
```

## 认证文件格式

兼容 CLIProxyAPI，文件名格式：

```text
codex-{email}-{plan_type}-{hash}.json
```

文件内容示例：

```json
{
  "type": "codex",
  "id_token": "eyJ...",
  "access_token": "eyJ...",
  "refresh_token": "rt_...",
  "account_id": "...",
  "email": "...",
  "expired": "2026-04-20T10:00:00Z",
  "last_refresh": "2026-04-10T10:00:00Z"
}
```

反向同步 (`pull-cpa`) 时，CPA 中下载回来的文件也会被重新整理成这个命名规范。

## 本地数据文件

| 文件 / 目录 | 作用 |
|-------------|------|
| `.env` | 运行配置 |
| `accounts.json` | 本地账号池状态 |
| `state.json` | 管理员登录态 |
| `auths/` | 轮转账号与主号的 Codex 认证文件 |
| `screenshots/` | 浏览器自动化调试截图 |

其中：
- `auths/codex-main-*.json` 是主号专用
- `auths/codex-{email}-{plan}-{hash}.json` 是轮转账号
- 从 CPA 反向同步时会自动清理同账号重复文件

## 启动验证

每次启动会自动验证 CloudMail 和 CPA 的连通性：

- CloudMail：登录 → 创建测试邮箱 → 删除
- CPA：获取认证文件列表

验证失败会提示具体哪个环节有问题，配置有误时会拒绝启动。
