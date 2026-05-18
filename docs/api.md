# HTTP API 文档

启动后访问 `http://localhost:8787/docs` 查看 Swagger 交互式文档。

所有 `/api/*` 端点需要：

```text
Authorization: Bearer <API_KEY>
```

但以下接口例外：
- `/api/auth/check`
- `/api/setup/status`
- `/api/setup/save`
- `/api/version`
- `/api/mail-provider/probe`(setup 阶段免鉴权;`API_KEY` 已配置时仍要求 Bearer,见下文)

## 即时返回接口

这些接口直接返回结果，不创建后台任务。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/auth/check` | 验证 API Key |
| GET | `/api/setup/status` | 检查配置是否完整(按 `MAIL_PROVIDER` 动态切换 `optional`) |
| POST | `/api/setup/save` | 保存初始配置(provider 互斥写盘) |
| POST | `/api/mail-provider/probe` | 邮箱后端 3 步探测(fingerprint / credentials / domain_ownership) |
| GET | `/api/version` | 镜像版本指纹（`git_sha` + `build_time`，免鉴权，用于排查 docker 镜像是否过期） |
| GET | `/api/status` | 账号状态 + 实时额度 |
| GET | `/api/accounts` | 所有账号列表 |
| GET | `/api/accounts/active` | 活跃账号 |
| GET | `/api/accounts/standby` | 待命账号 |
| GET | `/api/team/members` | Team 全部成员（含外部成员与邀请） |
| POST | `/api/team/members/remove` | 移出成员 / 取消邀请 |
| GET | `/api/logs` | 最近日志（支持 `?limit=100&since=0`） |
| GET | `/api/cpa/files` | CPA 认证文件列表 |
| GET | `/api/config/auto-check` | 巡检配置 |
| PUT | `/api/config/auto-check` | 修改巡检配置（运行时生效） |
| POST | `/api/sync` | 同步 active 认证文件到 CPA |
| POST | `/api/sync/from-cpa` | 从 CPA 反向同步认证文件到本地（含去重） |
| POST | `/api/sync/accounts` | 从 Team / auths 对账到本地账号池 |
| POST | `/api/accounts/{email}/kick` | 将 active 账号移出 Team |
| DELETE | `/api/accounts/{email}` | 删除本地管理账号及其资源 |

### Team 成员移除

`POST /api/team/members/remove`

请求体：

```json
{
  "email": "user@example.com",
  "user_id": "123",
  "type": "member"
}
```

- `type = member`：从 Team 中移出
- `type = invite`：取消邀请

## 后台任务接口

这些接口返回 `202 Accepted + task_id`。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/tasks/rotate` | 智能轮转 `{"target": 3}` |
| POST | `/api/tasks/check` | 检查额度，`{"include_standby": false}` 追加探测 standby 池（限速 1.5s/号 + 24h 去重） |
| POST | `/api/tasks/add` | 自动注册并添加新账号 |
| POST | `/api/tasks/fill` | 补满成员 `{"target": 3}` |
| POST | `/api/tasks/cleanup` | 清理成员 `{"max_seats": null}` |
| GET | `/api/tasks` | 任务列表 |
| GET | `/api/tasks/{task_id}` | 任务详情 |

> 同一时间只允许一个 Playwright 操作；如果有任务执行中，新请求可能返回 `409 Conflict`。

## 管理员运维

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/admin/reconcile?dry_run=0` | 对账修复：扫描 workspace 实际成员 vs 本地 `accounts.json`，识别**残废 / 错位 / 耗尽未抛弃 / ghost / over-cap**五类异常并按 `RECONCILE_KICK_ORPHAN` / `RECONCILE_KICK_GHOST` 决定 KICK 或打标记。`dry_run=1` 仅预测不动账户（包含第二轮 over-cap 预测），返回结构化诊断 dict（`kicked` / `orphan_kicked` / `orphan_marked` / `misaligned_fixed` / `exhausted_marked` / `ghost_kicked` / `ghost_seen` / `over_cap_kicked` / `flipped_to_active`） |

## 管理员登录

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/admin/status` | 管理员状态 |
| POST | `/api/admin/login/start` | 开始登录 `{"email": "admin@example.com"}` |
| POST | `/api/admin/login/session` | 手动导入 session_token `{"email": "admin@example.com", "session_token": "..."}` |
| POST | `/api/admin/login/password` | 提交密码 `{"password": "..."}` |
| POST | `/api/admin/login/code` | 提交验证码 `{"code": "123456"}` |
| POST | `/api/admin/login/workspace` | 选择组织 `{"option_id": "0"}` |
| POST | `/api/admin/login/cancel` | 取消登录 |
| POST | `/api/admin/logout` | 清除登录态 |

## 主号 Codex 同步

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/main-codex/status` | 同步状态 |
| POST | `/api/main-codex/start` | 开始同步 |
| POST | `/api/main-codex/password` | 提交密码 |
| POST | `/api/main-codex/code` | 提交验证码 |
| POST | `/api/main-codex/cancel` | 取消同步 |

## 手动 OAuth 导入

后端先生成 Codex OAuth 链接，并尝试在 `localhost:1455` 自动接收回调；如果自动回调不可用，也可以手动提交回调 URL。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/manual-account/status` | 当前手动 OAuth 状态 |
| POST | `/api/manual-account/start` | 开始流程，返回 `auth_url` 与状态信息 |
| POST | `/api/manual-account/callback` | 提交回调 URL |
| POST | `/api/manual-account/cancel` | 取消流程 |

### `/api/manual-account/status` 关键字段

| 字段 | 说明 |
|------|------|
| `status` | `idle / pending_callback / completed / error` |
| `auth_url` | 当前 OAuth 链接 |
| `callback_received` | 是否已收到回调 |
| `callback_source` | `auto` 或 `manual` |
| `auto_callback_available` | 本地自动回调服务是否启动成功 |
| `account` | 完成后导入的账号信息 |

## 初始配置 API

### `POST /api/mail-provider/probe`

邮箱后端 3 步探测,SetupPage / Settings 用作切换前置校验。**setup 阶段免 Bearer**(在 `_AUTH_SKIP_PATHS` 白名单);一旦 `API_KEY` 已配置,仍要求 Bearer,且按 IP 限速 60 req/min(超限返 `error_code=RATE_LIMITED` + HTTP 429)。

请求体(共用 schema):

```json
{
  "provider": "cf_temp_email | maillab",
  "step": "fingerprint | credentials | domain_ownership",
  "base_url": "https://example.com/api",
  "username": "admin@example.com",     // 仅 maillab credentials/domain_ownership
  "password": "...",                   // 仅 maillab credentials/domain_ownership
  "admin_password": "...",             // 仅 cf_temp_email credentials/domain_ownership
  "domain": "example.com"              // 仅 domain_ownership
}
```

响应通用字段:

```json
{
  "ok": true,
  "step": "fingerprint",
  "provider": "maillab",
  "detected_provider": "maillab",
  "domain_list": ["@a.com"],
  "warnings": [],
  "error_code": null,
  "message": null,
  "hint": null,
  "leaked_probe": null,
  "cleaned": null
}
```

`error_code` 取值见下表(失败时 `ok=false`):

| `error_code` | HTTP | 说明 |
|---|---|---|
| `PROVIDER_MISMATCH` | 200 | base_url 指纹与 `provider` 不一致(典型 issue#1) |
| `ROUTE_NOT_FOUND` | 200 | base_url 不是任何已知后端 |
| `EMPTY_DOMAIN_LIST` | 200 | maillab `domainList` 空 |
| `UNAUTHORIZED` | 200 | 凭据校验失败 |
| `CAPTCHA_REQUIRED` | 200 | maillab 启用了登录验证码 |
| `DOMAIN_REJECTED` | 200 | 创建探测邮箱被后端拒绝(`addVerify=1` 等) |
| `NETWORK_ERROR` / `TIMEOUT` | 200 | 网络异常 |
| `RATE_LIMITED` | 429 | 60 req/min 限速触发 |

#### 示例 1:`step=fingerprint`(探测后端归属)

```bash
curl -X POST http://localhost:8787/api/mail-provider/probe \
  -H "Content-Type: application/json" \
  -d '{"provider":"maillab","step":"fingerprint","base_url":"https://m.example.com"}'
```

成功响应:

```json
{
  "ok": true,
  "detected_provider": "maillab",
  "domain_list": ["@example.com", "@x.example.com"],
  "warnings": []
}
```

#### 示例 2:`step=credentials`(凭据校验)

cf_temp_email:

```bash
curl -X POST http://localhost:8787/api/mail-provider/probe \
  -H "Content-Type: application/json" \
  -d '{"provider":"cf_temp_email","step":"credentials","base_url":"https://mail.example.com/api","admin_password":"..."}'
```

> `cf_temp_email` 的 `base_url` 对应 `CLOUDMAIL_BASE_URL`，必须包含 `/api` 前缀；不要只填域名根路径。

maillab:

```bash
curl -X POST http://localhost:8787/api/mail-provider/probe \
  -H "Content-Type: application/json" \
  -d '{"provider":"maillab","step":"credentials","base_url":"...","username":"admin@x.com","password":"..."}'
```

#### 示例 3:`step=domain_ownership`(域名归属验证)

```bash
curl -X POST http://localhost:8787/api/mail-provider/probe \
  -H "Content-Type: application/json" \
  -d '{"provider":"maillab","step":"domain_ownership","base_url":"...","username":"...","password":"...","domain":"example.com"}'
```

成功响应:

```json
{
  "ok": true,
  "cleaned": true,
  "leaked_probe": null
}
```

如果探测邮箱删除失败(`cleaned=false`),`leaked_probe` 含 `{"email":"probe-...","account_id":"..."}`,需到管理后台手动删除。

> 内部使用 `autoteam.mail.probe.probe_domain_ownership` helper,与 `/api/config/register-domain`(注册域名验证)共用同一份逻辑,语义对齐。

## 调用示例

```bash
# 查看账号状态
curl -H "Authorization: Bearer YOUR_KEY" \
  http://localhost:8787/api/status

# 触发轮转
curl -X POST -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target": 3}' \
  http://localhost:8787/api/tasks/rotate

# 从 CPA 拉取认证文件到本地
curl -X POST -H "Authorization: Bearer YOUR_KEY" \
  http://localhost:8787/api/sync/from-cpa

# 生成手动 OAuth 链接
curl -X POST -H "Authorization: Bearer YOUR_KEY" \
  http://localhost:8787/api/manual-account/start
```
