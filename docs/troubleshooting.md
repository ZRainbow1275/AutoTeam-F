# 常见问题

## 安装相关

### Playwright 安装失败

```bash
uv run playwright install chromium
uv run playwright install-deps chromium
```

### macOS 上 Playwright Sync API 报错

```text
playwright._impl._errors.Error: It looks like you are using Playwright Sync API inside the asyncio loop.
```

设置环境变量：

```bash
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
uv run autoteam rotate
```

### Windows 启动时出现编码报错

如果历史 `.env` 文件含有 GBK / ANSI 编码或旧版内联注释格式，建议：

1. 将 `.env` 保存为 UTF-8
2. 确认配置值格式为：

```env
AUTO_CHECK_INTERVAL=300  # 5 分钟
```

新版本已兼容 UTF-8 与尾部注释。

## 登录相关

### Codex OAuth 登录失败：未获取到 authorization code

常见原因：
1. **IP 被标记** — VPS 的 IP 被 OpenAI/Cloudflare 拦截，建议换住宅代理
2. **Cloudflare 验证** — 浏览器环境被检测，需更新 Chromium 或切换网络
3. **workspace 选择失败** — 页面结构变化，查看 `screenshots/codex_04_*.png`
4. **自动回调不可达** — 如果浏览器和 AutoTeam 不在同一台机器，`localhost:1455` 回调可能不会到达 AutoTeam，此时请改用手动粘贴回调 URL
5. **本地回调被代理拦截** — 如果启用了 `PLAYWRIGHT_PROXY_URL`，建议同时设置 `PLAYWRIGHT_PROXY_BYPASS=localhost,127.0.0.1`

### 登录后 plan 显示 free 而不是 team

通常是 `state.json` 中的 `workspace_name` 或 `account_id` 不正确。

检查：

```bash
cat state.json | python -m json.tool
```

确认：
- `account_id` 是有效 UUID
- `workspace_name` 是 Team 名称

### 验证码一直获取失败

- 检查 CloudMail 是否正常
- 检查邮箱域名 `CLOUDMAIL_DOMAIN`
- 系统会按 **邮件 ID** 跳过已经尝试过的验证码邮件，而不是按 6 位数字去重
- 如果浏览器长时间停在 `email-verification`，通常说明新的验证码邮件没有到达，或拿到的是旧邮件

## 轮转相关

### rotate 没有补号

先看 `get_team_member_count` 是否失败。若返回 `-1`，说明 Team API 调用异常：

- 确认管理员已登录（`state.json` 有 session token）
- 确认 `account_id` 是有效 UUID

### rotate 的目标人数为什么算不准

`rotate 3` / `fill 3` 中的 `3` 指的是 **Team 总人数目标**。

也就是说：
- owner
- 外部成员
- 本地管理成员

都会一起计入这 3 个席位。当前自动轮转契约最多保留 `1 owner + 2 managed children`。

### 旧号一直被复用但额度不够

旧号复用前会先验证额度。

如果验证返回 `auth_error`（token 失效），系统会参考：
- `last_quota`
- `quota_resets_at`

判断是否值得继续复用。5h 重置时间过后，旧数据会视为过期。

### Team 超员但没有清理

`rotate` 会自动清理超员成员。如果没生效，可手动执行：

```bash
uv run autoteam cleanup 3
```

## CPA 同步相关

### 反向同步后本地 token 似乎“变旧了”

新版本会比较本地与 CPA 两侧文件的：

- `last_refresh`
- `expired`

只有 CPA 文件更“新”时，才会覆盖本地文件。

如果你怀疑历史版本已经把旧 token 写回本地，可以先重新登录目标账号，再执行：

```bash
uv run autoteam pull-cpa
```

查看日志里的：
- `local_kept_newer`
- `cpa_duplicates_deleted`
- `local_duplicates_deleted`

### 同账号在 CPA / 本地出现多个文件名不同的认证文件

新版本会在同步时按同账号去重：

- CPA 侧只保留一份
- 本地也只保留一份
- 并统一重写为本地命名规范

如果你怀疑之前版本遗留了重复文件，执行一次：

```bash
uv run autoteam pull-cpa
```

## Docker 相关

### 容器一直重启

```bash
docker compose logs
```

通常是配置缺失或连通性验证失败。

### `data` 目录没有写权限

入口脚本会自动 `chmod -R 777 /app/data`。若仍有问题：

```bash
sudo chmod -R 777 data/
```

### 重建容器后配置丢失

确认 `docker-compose.yml` 中有：

```yaml
volumes:
  - ./data:/app/data
```

### 容器里访问不到宿主机 SOCKS5 代理

如果代理在宿主机上，比如 `host.docker.internal:1080`，请先确认容器内可以解析并访问宿主机代理地址；不同 Docker / Podman 环境的宿主机别名配置方式可能不同。

然后在 `data/.env` 中配置：

```dotenv
PLAYWRIGHT_PROXY_URL=socks5://host.docker.internal:1080
PLAYWRIGHT_PROXY_BYPASS=localhost,127.0.0.1
```

如果代理需要认证，可以直接写成：

```dotenv
PLAYWRIGHT_PROXY_URL=socks5://username:password@host.docker.internal:1080
```

## Web 面板相关

### 页面显示 `JSON parse error`

说明后端返回了非 JSON 响应（通常是 500 错误）。查看后端日志定位具体异常。

### 操作按钮全部禁用

轮转 / 补满 / 清理等账号池操作需要先在「设置」页完成管理员登录。

### Team 成员页的 owner 为什么没有“移出”按钮

`account-owner` 角色不会显示“移出”按钮，因为这类账号通常无法通过普通成员删除接口移出。

### 刷新后数据没更新

点击侧边栏底部的「刷新数据」按钮手动刷新。

### 配置保存后 401 创建邮箱失败

issue#1 错配的典型表现:配置时填的是 maillab 的 `*_BASE_URL`,但 `MAIL_PROVIDER` 还是默认的 `cf_temp_email` → 启动校验在 `/admin/address` catch-all 路由上误回 200,但创建邮箱阶段被 maillab 拒回 `code:401`。

修复路径见 [配置说明 → ⚠️ 协议错配排查](configuration.md#-协议错配排查issue-1) 与 [SetupPage 4 步状态机](getting-started.md#25-邮箱后端归属验证)。

## 邮箱后端

### 5 个常见错配场景与对应 `error_code`

| 场景 | 触发条件 | 报错 `error_code` | 修复 |
|---|---|---|---|
| issue#1 错配 | 选 `cf_temp_email` 但 base_url 是 maillab(或反向) | `PROVIDER_MISMATCH` | SetupPage 切换 provider 后重试 |
| 域名后台为空 | maillab `/setting/websiteConfig.domainList` 为空 | `EMPTY_DOMAIN_LIST` | 在 maillab 后台先添加可用域名 |
| 凭据错误 | 管理员密码或 username/password 错 | `UNAUTHORIZED` | 重置密码或确认主账号 |
| 启用了登录验证码 | maillab 后台 captcha = ON | `CAPTCHA_REQUIRED`(warning) | 关闭 captcha 或改用 admin 直登 |
| 域名未授权 | maillab 设置了 `addVerify=1` 拒绝创建非白名单域名 | `DOMAIN_REJECTED` | 在管理后台添加域名白名单 |

### 紧急逃生口

如果嗅探阻断了你已确认无误的配置,可以临时:

```bash
export AUTOTEAM_SKIP_PROVIDER_SNIFF=1
uv run autoteam api
```

仅在确诊误报时使用,生产环境不建议长期开启。

### maillab 401 自愈

`MaillabClient` 业务方法被装饰器包裹,任何一次响应 `code:401` 会自动触发 re-login 并重试。如果重试仍 401,会抛 `MaillabAuthFailed`(日志关键字 `[maillab] token 已自愈` 表示自愈成功)。详细策略见 [mail-provider-design.md §8](mail-provider-design.md#8-401-自愈策略)。
