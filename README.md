<div align="center">

# AutoTeam

**ChatGPT Team 账号自动轮转管理工具**

自动创建账号、注册、获取 Codex 认证、检查额度、智能轮换，并同步认证文件到 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?style=for-the-badge&logo=playwright&logoColor=white)](https://playwright.dev)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?style=for-the-badge)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/FastAPI-API_&_Web-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Vue](https://img.shields.io/badge/Vue_3-Frontend-4FC08D?style=for-the-badge&logo=vue.js&logoColor=white)](https://vuejs.org)

</div>

---

## 特性

| | 功能 | 描述 |
|---|---|---|
| 📧 | **自动注册** | 创建临时邮箱 → 注册 ChatGPT → 自动填写验证码/个人信息 |
| 🔐 | **Codex OAuth** | 自动完成 Codex 登录，无密码时走一次性验证码，保存 CPA 兼容认证文件 |
| 📊 | **额度检查** | 检测 Codex 5h/周额度，低于阈值自动标记，token 过期按历史额度判断 |
| 🔄 | **智能轮转** | 额度低于阈值自动移出，复用前验证额度，超员自动清理，万不得已才创建新号 |
| ☁️ | **CPA 同步** | 认证文件自动上传覆盖，只同步 active 账号，登录后自动同步 |
| 👥 | **Team 管理** | 自动补满/清理成员，查看全部 Team 成员（含外部成员） |
| 🌐 | **HTTP API** | FastAPI 接口 + API Key 鉴权，方便对接外部系统 |
| 🖥️ | **Web 面板** | 侧边栏分页管理：仪表盘、Team 成员、操作任务、实时日志、巡检设置 |
| 🔍 | **自动巡检** | 后台定时检查额度，低于阈值自动触发轮转，阈值可在面板配置 |
| 🛡️ | **启动验证** | 每次启动自动验证 CloudMail 和 CPA 连通性，配置有误立即提示 |

## 快速开始

### 安装

```bash
# 一键安装
bash setup.sh

# 或手动
uv sync
uv run playwright install chromium
```

### 配置

首次运行任何命令时会自动进入配置向导，交互式填写必填项并验证连通性：

```bash
uv run autoteam api    # 启动时自动检测并提示填写缺失配置
```

也可以手动编辑 `.env`：

```bash
cp .env.example .env   # 复制配置模板，填入实际值
```

**`.env` 配置项：**

| 配置项 | 说明 | 必填 |
|--------|------|------|
| `CLOUDMAIL_BASE_URL` | CloudMail API 地址 | 是 |
| `CLOUDMAIL_EMAIL` | CloudMail 登录邮箱 | 是 |
| `CLOUDMAIL_PASSWORD` | CloudMail 登录密码 | 是 |
| `CLOUDMAIL_DOMAIN` | 临时邮箱域名（如 `@example.com`） | 是 |
| `CPA_URL` | CLIProxyAPI 地址 | 否（默认 `http://127.0.0.1:8317`） |
| `CPA_KEY` | CPA 管理密钥 | 是 |
| `API_KEY` | Web 面板 / API 鉴权密钥 | 否（首次启动自动生成） |
| `AUTO_CHECK_THRESHOLD` | 额度低于此百分比触发轮转 | 否（默认 `10`，可在面板修改） |
| `AUTO_CHECK_INTERVAL` | 巡检间隔（秒） | 否（默认 `300`） |
| `AUTO_CHECK_MIN_LOW` | 至少几个账号低于阈值才触发 | 否（默认 `2`） |

**管理员登录：**

首次启动后在 Web 面板或命令行完成主号登录，系统自动保存到 `state.json`（邮箱、session token、workspace ID 等）。

### 使用

```bash
uv run autoteam <command> [args]
```

| 命令 | 说明 |
|------|------|
| `api` | 启动 HTTP API + Web 管理面板（默认端口 8787） |
| `status` | 查看所有账号状态（自动同步 Team 成员 + auths 目录） |
| `check` | 检查 active 账号额度，低于阈值标记 exhausted |
| `rotate [N]` | 智能轮转：检查 → 移出 → 复用/创建 → 补满到 N 个（默认 5） |
| `add` | 手动添加一个新账号 |
| `fill [N]` | 补满 Team 成员到 N 个 |
| `cleanup [N]` | 清理多余成员到 N 个（只移除本地管理的） |
| `sync` | 手动同步认证文件到 CPA（覆盖上传） |
| `admin-login` | 交互式完成管理员主号登录 |
| `main-codex-sync` | 交互式同步主号 Codex 到 CPA |

**日常只需一条命令：**

```bash
uv run autoteam rotate
```

### Web 管理面板

```bash
uv run autoteam api                # 默认 0.0.0.0:8787
uv run autoteam api --port 9000   # 自定义端口
```

启动后访问 `http://localhost:8787`，首次需要输入 API Key（启动时自动生成并显示）。

**面板页面：**

| 页面 | 功能 |
|------|------|
| 📊 仪表盘 | 账号统计卡片 + 账号表格（实时额度、状态、登录/移出/删除操作） |
| 👥 Team 成员 | 全部 Team 成员列表（含手动添加的外部成员），10 分钟缓存 |
| ⚡ 操作 & 任务 | 一键执行轮转/检查/补满/添加/清理/同步，任务历史和状态跟踪 |
| 📋 日志 | 实时日志查看器，3 秒刷新，自动滚动 |
| ⚙️ 设置 | 管理员登录、主号 Codex 同步、巡检参数配置 |

适配桌面端（侧边栏）和手机端（底部 tab 栏）。

### HTTP API

启动后访问 `http://localhost:8787/docs` 查看 Swagger 文档。所有 `/api/*` 端点需要 `Authorization: Bearer <API_KEY>` 认证。

<details>
<summary>端点一览</summary>

**即时返回**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/auth/check` | 验证 API Key |
| GET | `/api/status` | 账号状态 + 实时额度 |
| GET | `/api/accounts` | 所有账号列表 |
| GET | `/api/team/members` | Team 全部成员（含外部） |
| GET | `/api/logs` | 最近日志 |
| GET | `/api/config/auto-check` | 巡检配置 |
| PUT | `/api/config/auto-check` | 修改巡检配置 |
| POST | `/api/sync` | 同步认证文件到 CPA |
| POST | `/api/accounts/login` | 触发单账号 Codex 登录 |
| POST | `/api/accounts/{email}/kick` | 移出 Team |
| DELETE | `/api/accounts/{email}` | 删除账号 |

**后台任务（返回 task_id）**

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/tasks/rotate` | 智能轮转 |
| POST | `/api/tasks/check` | 检查额度 |
| POST | `/api/tasks/add` | 添加新账号 |
| POST | `/api/tasks/fill` | 补满成员 |
| POST | `/api/tasks/cleanup` | 清理成员 |
| GET | `/api/tasks` | 任务列表 |
| GET | `/api/tasks/{task_id}` | 任务详情 |

**管理员登录 / 主号 Codex**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/admin/status` | 管理员状态 |
| POST | `/api/admin/login/start` | 开始登录 |
| POST | `/api/admin/login/password` | 提交密码 |
| POST | `/api/admin/login/code` | 提交验证码 |
| POST | `/api/admin/login/workspace` | 选择组织 |
| POST | `/api/admin/login/cancel` | 取消登录 |
| POST | `/api/admin/logout` | 清除登录态 |
| POST | `/api/main-codex/start` | 开始主号 Codex 同步 |
| POST | `/api/main-codex/password` | 提交密码 |
| POST | `/api/main-codex/code` | 提交验证码 |
| POST | `/api/main-codex/cancel` | 取消同步 |

</details>

## 工作原理

### 轮转流程

```
                    ┌─────────────┐
                    │  同步 Team   │
                    │  实际状态    │
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │  检查所有    │
                    │ active 额度  │
                    └──────┬──────┘
                           ▼
              ┌────────────┴────────────┐
              ▼                         ▼
        额度 ≥ 阈值 ✅          额度 < 阈值 ❌
        保持不动                  移出 Team
                                       │
                           ┌───────────┴───────────┐
                           ▼                       ▼
                    旧号额度 ≥ 阈值？         全部不可用？
                     验证后复用 ♻️           创建新号 🆕
                           │                       │
                           └───────────┬───────────┘
                                       ▼
                                ┌─────────────┐
                                │  同步到 CPA  │
                                └─────────────┘
```

### 账号状态机

```
  ┌──────────┐  额度<阈值  ┌───────────┐   移出Team   ┌──────────┐
  │  active  │ ──────────→ │ exhausted  │ ──────────→ │ standby  │
  └──────────┘             └───────────┘              └────┬─────┘
       ▲                                                   │
       └──────── 额度恢复（验证通过），重新加入 ───────────┘
```

| 状态 | 含义 |
|------|------|
| `active` | 在 Team 中，额度高于阈值 |
| `exhausted` | 在 Team 中，额度低于阈值，等待移出 |
| `standby` | 已移出 Team，等待额度恢复后复用（复用前验证） |
| `pending` | 已创建，等待注册完成 |

## 项目结构

```
autoteam/
├── pyproject.toml              # 项目配置 + 依赖
├── setup.sh                    # 一键安装脚本
├── .env.example                # 配置模板
├── .pre-commit-config.yaml     # pre-commit (ruff lint + format)
├── ruff.toml                   # ruff 配置
├── .github/workflows/          # GitHub Actions CI
├── src/autoteam/
│   ├── manager.py              # CLI 入口，所有命令
│   ├── api.py                  # HTTP API + 鉴权 + 自动巡检
│   ├── setup_wizard.py         # 首次配置向导 + 连通性验证
│   ├── admin_state.py          # 管理员状态管理 (state.json)
│   ├── config.py               # 配置加载（从 .env）
│   ├── display.py              # 虚拟显示器自动设置
│   ├── accounts.py             # 账号池持久化管理
│   ├── account_ops.py          # 账号删除/清理操作
│   ├── chatgpt_api.py          # ChatGPT 内部 API + 管理员登录
│   ├── cloudmail.py            # CloudMail API 客户端
│   ├── codex_auth.py           # Codex OAuth + token + 主号同步
│   ├── cpa_sync.py             # CPA 认证文件同步
│   ├── invite.py               # 注册流程自动化
│   └── web/dist/               # 前端构建产物（已内置）
└── web/                        # 前端源码（Vue 3 + Vite + Tailwind）
    ├── src/
    │   ├── App.vue             # 主组件 + 鉴权 + 路由
    │   ├── api.js              # API 调用封装
    │   └── components/         # Sidebar / Dashboard / TeamMembers / TasksPage / LogViewer / Settings
    ├── package.json
    └── vite.config.js
```

## 认证文件格式

兼容 CLIProxyAPI，文件名格式：`codex-{email}-{plan_type}-{hash}.json`

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

## 依赖

| 依赖 | 用途 |
|------|------|
| [Python 3.10+](https://python.org) | 运行环境 |
| [uv](https://docs.astral.sh/uv/) | 包管理 |
| [Playwright](https://playwright.dev) | 浏览器自动化 (Chromium) |
| [FastAPI](https://fastapi.tiangolo.com) | HTTP API 框架 |
| [Rich](https://rich.readthedocs.io) | 终端美化输出 |
| [Vue 3](https://vuejs.org) | Web 管理面板前端框架 |
| [Tailwind CSS](https://tailwindcss.com) | 前端样式 |
| xvfb | Linux 无头服务器虚拟显示 |
| [CloudMail](https://github.com/maillab/cloud-mail) | Cloudflare Workers 临时邮箱服务 |
| [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | Codex 代理，认证文件同步目标 |

**前端开发（可选）：**

```bash
cd web
npm install
npm run dev       # Vite dev server :5173，自动代理 /api → :8787
npm run build     # 构建产物输出到 src/autoteam/web/dist/
```

## 友情链接

感谢 **LinuxDo** 社区的支持！

[![LinuxDo](https://img.shields.io/badge/社区-LinuxDo-blue?style=for-the-badge)](https://linux.do/)

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=cnitlrt/AutoTeam&type=Date)](https://star-history.com/#cnitlrt/AutoTeam&Date)
