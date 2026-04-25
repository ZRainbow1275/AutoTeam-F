# 工作原理

## 总体目标

AutoTeam 的目标不是单纯“多开号”，而是：

1. 维护 **Team 总人数** 在目标值附近
2. 让 active 账号尽量保持可用额度
3. 将可用认证文件同步到 CPA
4. 在需要时从 CPA 反向恢复认证文件到本地

## 轮转流程

```text
同步 Team 实际状态
        ↓
检查 active 账号额度
        ↓
额度不足 → 标记 exhausted → 移出 Team → standby
        ↓
优先复用 standby 旧号
        ↓
不够再创建新号
        ↓
同步 active 认证文件到 CPA
```

> 轮转目标是 **Team 总人数**。
> Team 中已有的 owner / 外部成员也会计入目标人数。

## 账号状态机

```text
             ┌───────────── 额度恢复 / 登录成功 ──────────────┐
             │                                                │
             ↓                                                │
pending ──> active ──额度不足──> exhausted ──移出 Team──> standby
             │                                                │
             │                                      ┌─ 401/403 探测
             │                                      ↓
             │                               auth_invalid ──对账 KICK──> (删除 / 重登)
             │
             └── 对账发现本地 auth_file 缺失 ──> orphan ──人工补登──> active
                                                   │
                                                   └── RECONCILE_KICK_ORPHAN=true ──> KICK
```

| 状态 | 含义 |
|------|------|
| `active` | 当前在 Team 中，且本地认为可用 |
| `exhausted` | 当前在 Team 中，但额度不足，等待移出 |
| `standby` | 已不在当前轮转席位中，等待后续复用 |
| `pending` | 注册 / 创建流程尚未完成 |
| `personal` | 已主动退出 Team，走个人号 Codex OAuth，不再参与 Team 轮转 |
| `auth_invalid` | `auth_file` token 已不可用(401/403)。`cmd_check --include-standby` 探测或主动使用时落入此态,等对账 KICK 或重登 |
| `orphan` | workspace 仍占席 + 本地 `auth_file` 缺失。默认 `RECONCILE_KICK_ORPHAN=true` 直接 KICK;关掉则打此标记等人工 |

### 对账(reconcile)分支

`_reconcile_team_members` 在 `cmd_check` 入口和独立命令 `autoteam reconcile [--dry-run]` 都会执行,对齐 workspace `/users` 的事实与本地 `accounts.json`:

| workspace ↔ 本地 | 处置 |
|-------------------|------|
| active + auth_file 存在 | 正常 |
| active + auth_file 缺失 | **残废**:先找 `auths/codex-{email}-team-*.json` 兜底,找不到按 `RECONCILE_KICK_ORPHAN` 决定 KICK 或标 `orphan` |
| active + `last_quota` 5h/周均 100% | **耗尽未抛弃**:标 `exhausted` + `quota_exhausted_at=now`,**不立即 kick**(避开 token_revoked 风控) |
| active ↔ pending | 升 `active` |
| active ↔ standby | **错位**:改回 `active` + 补齐 auth_file |
| active ↔ exhausted / personal / auth_invalid | KICK |
| active ↔ orphan | 已标记,跳过 |
| active ↔ 本地无记录 | **ghost**:按 `RECONCILE_KICK_GHOST` 决定 KICK 或留给 `sync_account_states` 补录 |

## 同步模型

项目中有三类“同步”：

| 动作 | 方向 | 用途 |
|------|------|------|
| `同步账号` | Team / `auths/` → `accounts.json` | 修复本地账号池记录 |
| `同步 CPA` | 本地 active → CPA | 只把 active 认证文件同步到 CPA |
| `拉取 CPA` | CPA → 本地 | 从 CPA 反向恢复 / 导入认证文件 |

### 反向同步特点

- 同账号去重（CPA 与本地都只保留一份）
- 按本地命名规范重写文件名
- 比较 `last_refresh` / `expired`，避免用旧 CPA 文件覆盖本地新 token
- 新导入账号默认标记为 `standby`

## OAuth 导入模型

手动 OAuth 导入支持两种回调方式：

### 1. 自动回调

系统尝试在本机启动：

```text
http://localhost:1455/auth/callback
```

如果浏览器和 AutoTeam 在同一台机器上，OpenAI 成功回跳后可自动完成认证。

### 2. 手动回调

如果浏览器不在同一台机器上，或 `localhost:1455` 无法回到 AutoTeam：

- 用户在浏览器完成登录
- 再把最终回调 URL 粘贴给 AutoTeam
- 系统提取 `code/state` 完成 token 交换

## 核心模块

| 模块 | 作用 |
|------|------|
| `manager.py` | CLI 入口与核心轮转逻辑 |
| `api.py` | HTTP API、鉴权、后台任务、自动巡检 |
| `accounts.py` | 本地账号池持久化 |
| `chatgpt_api.py` | 通过浏览器上下文调用 ChatGPT 内部接口 |
| `codex_auth.py` | Codex OAuth、refresh、额度检查 |
| `invite.py` | 自动注册流程 |
| `cloudmail.py` | CloudMail 临时邮箱客户端 |
| `cpa_sync.py` | CPA 双向同步与去重 |
| `manual_account.py` | 手动 OAuth 导入（自动 / 手动回调） |

## 项目结构

```text
autoteam/
├── docs/                       # 文档
├── src/autoteam/
│   ├── manager.py              # CLI 入口
│   ├── api.py                  # HTTP API + 后台任务 + 自动巡检
│   ├── setup_wizard.py         # 首次配置向导
│   ├── admin_state.py          # 管理员登录态 (state.json)
│   ├── config.py               # 配置加载
│   ├── accounts.py             # 账号池持久化
│   ├── account_ops.py          # 删除 / 清理 / 对账
│   ├── chatgpt_api.py          # ChatGPT Team 内部 API 调用
│   ├── cloudmail.py            # CloudMail 客户端
│   ├── codex_auth.py           # Codex OAuth 与 token 管理
│   ├── cpa_sync.py             # CPA 正反向同步
│   ├── manual_account.py       # 手动 OAuth 导入
│   ├── invite.py               # 自动注册流程
│   └── web/dist/               # 前端构建产物
└── web/src/components/         # 仪表盘 / 同步中心 / OAuth 登录 / 任务历史等页面
```

## 前端结构

当前 Web 面板已按职责拆分为：

- 仪表盘
- Team 成员
- 账号池操作
- 同步中心
- OAuth 登录
- 任务历史
- 日志
- 设置

## 开发

```bash
cd web
npm install
npm run dev
npm run build
```
