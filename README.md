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
| 🔐 | **Codex OAuth** | 自动完成 Codex 登录，保存 CPA 兼容的认证文件 |
| 📊 | **额度检查** | 检测 Codex 5h 额度，token 过期自动刷新/重新登录 |
| 🔄 | **智能轮转** | 额度低于阈值自动移出，复用前验证额度，万不得已才创建新号 |
| ☁️ | **CPA 同步** | 认证文件自动上传/删除，只同步 active 账号 |
| 👥 | **Team 管理** | 自动补满/清理成员，同步 Team 实际状态 |
| 🌐 | **HTTP API** | FastAPI 接口，方便对接外部系统、定时任务 |
| 🖥️ | **Web 面板** | Vue 3 管理面板，账号状态、操作、任务历史、巡检设置 |
| 🔍 | **自动巡检** | 后台定时检查额度，低于阈值自动触发轮转，阈值可在面板配置 |

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

```bash
cp .env.example .env   # 复制配置模板，填入实际值
```

**`.env` 配置项：**

| 配置项 | 说明 |
|--------|------|
| **CloudMail** | 临时邮箱服务地址和凭据 |
| **ChatGPT** | Team Account ID（从 ChatGPT admin 页面获取），Workspace 名称可自动检测 |
| **CPA** | CLIProxyAPI 地址和管理密钥 |
| **AUTO_CHECK_THRESHOLD** | 额度低于此百分比触发轮转，默认 `10`（可在 Web 面板修改） |
| **AUTO_CHECK_INTERVAL** | 巡检间隔（秒），默认 `300`（5 分钟） |
| **AUTO_CHECK_MIN_LOW** | 至少几个账号低于阈值才触发轮转，默认 `2` |

**文件配置：**

| 文件 | 说明 |
|------|------|
| `session` | ChatGPT 管理员的 `__Secure-next-auth.session-token`（拼接 `.0` 和 `.1`） |

### 使用

```bash
uv run autoteam <command> [args]
```

| 命令 | 说明 |
|------|------|
| `status` | 查看所有账号状态（自动同步 Team 实际成员） |
| `check` | 检查 active 账号额度，低于阈值标记 exhausted，token 失效按历史额度判断 |
| `rotate [N]` | 智能轮转：检查额度 → 移出低于阈值的 → 验证旧号额度后复用 → 补满到 N 个（默认 5） |
| `add` | 手动添加一个新账号 |
| `fill [N]` | 补满 Team 成员到 N 个（默认 5） |
| `cleanup [N]` | 清理多余成员到 N 个（只移除本地管理的） |
| `sync` | 手动同步认证文件到 CPA |
| `api` | 启动 HTTP API 服务器（默认端口 8787） |

**日常只需一条命令：**

```bash
uv run autoteam rotate
```

### HTTP API

启动 API 服务器后，所有管理功能均可通过 HTTP 调用，方便对接定时任务平台、Web 面板等外部系统。

```bash
uv run autoteam api                # 默认 0.0.0.0:8787
uv run autoteam api --port 9000   # 自定义端口
```

启动后访问 `http://localhost:8787/docs` 查看交互式 API 文档（Swagger UI）。

#### 端点一览

**同步端点（即时返回）**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/status` | 账号状态 + active 实时额度 |
| GET | `/api/accounts` | 所有账号列表 |
| GET | `/api/accounts/active` | 活跃账号 |
| GET | `/api/accounts/standby` | 待命账号 |
| POST | `/api/sync` | 同步认证文件到 CPA |
| GET | `/api/cpa/files` | CPA 认证文件列表 |
| GET | `/api/config/auto-check` | 获取巡检配置 |
| PUT | `/api/config/auto-check` | 修改巡检配置（运行时生效） |

**后台任务端点（返回 task_id，轮询获取结果）**

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/tasks/rotate` | 智能轮转 `{"target": 5}` |
| POST | `/api/tasks/check` | 检查所有 active 额度 |
| POST | `/api/tasks/add` | 添加新账号 |
| POST | `/api/tasks/fill` | 补满成员 `{"target": 5}` |
| POST | `/api/tasks/cleanup` | 清理多余成员 `{"max_seats": null}` |
| GET | `/api/tasks` | 查看所有任务 |
| GET | `/api/tasks/{task_id}` | 查看任务状态和结果 |

#### 调用示例

```bash
# 查看账号状态
curl http://localhost:8787/api/status

# 触发轮转（后台执行）
curl -X POST http://localhost:8787/api/tasks/rotate \
  -H 'Content-Type: application/json' \
  -d '{"target": 5}'
# 返回: {"task_id": "a1b2c3d4e5f6", "status": "pending", ...}

# 查看任务进度
curl http://localhost:8787/api/tasks/a1b2c3d4e5f6
# 返回: {"task_id": "...", "status": "completed", "result": ..., ...}
```

> 后台任务使用线程锁防止并发冲突，同一时间只允许一个 Playwright 操作。若有任务正在执行，新请求返回 `409 Conflict`。

### Web 管理面板

启动 API 后直接访问 `http://localhost:8787` 即可打开 Web 面板，无需额外安装 Node.js（构建产物已内置）。

**面板功能：**

- **Dashboard** — 账号统计卡片（活跃/待命/用完/总计）+ 账号表格（实时额度、颜色标签、重置时间）
- **操作面板** — 一键执行轮转、检查额度、补满、添加、清理、同步 CPA，任务执行中自动禁用按钮
- **任务历史** — 所有任务记录，实时状态跟踪（等待中/执行中/已完成/失败），耗时统计
- **巡检设置** — 可视化配置巡检间隔、额度阈值、触发账号数，修改后运行时立即生效

面板每 10 分钟自动刷新数据，有任务执行时切换为 30 秒轮询，任务完成后自动恢复。

**前端开发（可选）：**

```bash
cd web
npm install
npm run dev       # Vite dev server :5173，自动代理 /api → :8787
npm run build     # 构建产物输出到 src/autoteam/web/dist/
```

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
├── src/autoteam/
│   ├── manager.py              # CLI 入口，所有命令
│   ├── api.py                  # HTTP API（FastAPI）
│   ├── config.py               # 配置加载（从 .env）
│   ├── display.py              # 虚拟显示器自动设置
│   ├── accounts.py             # 账号池持久化管理
│   ├── chatgpt_api.py          # ChatGPT 内部 API
│   ├── cloudmail.py            # CloudMail API 客户端
│   ├── codex_auth.py           # Codex OAuth + token 管理
│   ├── cpa_sync.py             # CPA 认证文件同步
│   ├── invite.py               # 注册流程自动化
│   └── web/dist/               # 前端构建产物（已内置）
└── web/                        # 前端源码（Vue 3 + Vite + Tailwind）
    ├── src/
    │   ├── App.vue             # 主组件
    │   ├── api.js              # API 调用封装
    │   └── components/         # Dashboard / TaskPanel / TaskHistory
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

## 友情链接

感谢 **LinuxDo** 社区的支持！

[![LinuxDo](https://img.shields.io/badge/社区-LinuxDo-blue?style=for-the-badge)](https://linux.do/)

---

<div align="center">

如果觉得有用，欢迎 Star ⭐

[![GitHub stars](https://img.shields.io/github/stars/cnitlrt/AutoTeam?style=social)](https://github.com/cnitlrt/AutoTeam)

</div>
