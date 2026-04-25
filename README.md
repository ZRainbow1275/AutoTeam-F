<div align="center">

# AutoTeam-F

**面向 ChatGPT Team 的账号轮转与认证同步工具 · Fix + Free 增强版**

基于 [cnitlrt/AutoTeam](https://github.com/cnitlrt/AutoTeam) 的 fork，修掉若干阻塞性 bug，新增 **批量生产免费号（Personal）** 能力，改善操作体验。

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?style=for-the-badge&logo=playwright&logoColor=white)](https://playwright.dev)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?style=for-the-badge)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/FastAPI-API_&_Web-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Vue](https://img.shields.io/badge/Vue_3-Frontend-4FC08D?style=for-the-badge&logo=vue.js&logoColor=white)](https://vuejs.org)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)

</div>

---

## 致谢

- 💚 感谢 [cnitlrt/AutoTeam](https://github.com/cnitlrt/AutoTeam) 的前置工作 —— 没有原作者搭好的轮转/同步骨架，就没有这个 fork。
- 💙 感谢 [LinuxDo](https://linux.do/) 社区的支持 —— **"学 AI，上 L 站"**。

`AutoTeam-F` 的 **F = Fix + Free**。

---

> **免责声明**：本项目仅供学习和研究用途。使用本工具可能违反 OpenAI 的服务条款。使用者需自行承担账号封禁、IP 限制等后果。

## 特性

| | 功能 | 描述 |
|---|---|---|
| 📧 | **自动注册** | 临时邮箱（`cf_temp_email` 或 `maillab` 双后端可切换）+ Playwright 自动注册 |
| 🆓 | **生产免费号** 🆕 | 批量注册 → 主号踢出 → Personal OAuth，一条龙 |
| 🔐 | **Codex OAuth** | 自动登录 Codex，Team / Personal 双模式 |
| 🔑 | **手动 OAuth 导入** | localhost 自动回调，失败可手动粘贴 |
| 🔄 | **智能轮转** | 额度不足自动移出，旧号恢复后优先复用 |
| ☁️ | **CPA 双向同步** | 本地 active 上传到 CPA，也可反向导入 |
| 🖥️ | **Web 面板** | 仪表盘、同步中心、OAuth 登录、任务历史、日志、设置 |
| 🛑 | **软停止任务** 🆕 | 随时中止跑到一半的批次，协作式退出不留半成品 |
| 📊 | **失败分类** 🆕 | `register_failures.json` 持久化各类失败（手机号/重复/踢人/OAuth 等） |
| 🔧 | **自诊断** 🆕 | `/api/admin/diagnose` + `/api/admin/fix-account-id` 一键定位 401 |
| 🗑️ | **批量删除** 🆕 | Web 面板多选 + 一次性 kick + 删邮箱 + sync CPA |
| 🔍 | **自动巡检** | 后台定时检查额度并触发轮转 |
| 📤 | **导出认证** | 一键导出 Codex CLI 格式 auth.json |
| 🐳 | **Docker** | 支持容器部署与数据持久化 |

> 🆕 = 相对原仓库新增。其余承袭自 [cnitlrt/AutoTeam](https://github.com/cnitlrt/AutoTeam)。

**首次使用建议直接看**：[从零开始部署教程](docs/getting-started.md)

## 快速开始

### 安装

```bash
# Linux
bash setup.sh
# 或手动: uv sync && uv run playwright install chromium

# Windows / macOS
uv sync
uv run playwright install chromium
```

支持 Linux、Windows、macOS。Windows/macOS 不需要 xvfb。

### 启动

```bash
# Web 面板 + API（推荐）
uv run autoteam api

# 或直接轮转
uv run autoteam rotate
```

首次启动会自动引导配置 临时邮箱后端（`cf_temp_email` 默认 / `maillab` 可选）、CPA、API Key，并验证连通性。两种后端的差异见 [配置说明 · Mail Provider 切换](docs/configuration.md#mail-provider-切换)。

### Docker 部署

```bash
git clone https://github.com/ZRainbow1275/AutoTeam-F.git && cd AutoTeam-F
mkdir -p data && cp .env.example data/.env
# 编辑 data/.env 填入配置（或启动后在 Web 页面配置）
docker compose up -d
```

Linux + Docker 访问宿主机服务，详见 [Docker 部署文档](docs/docker.md)。

### CLI 命令

| 命令 | 说明 |
|------|------|
| `api` | 启动 Web 面板 + HTTP API（默认端口 8787） |
| `rotate [N]` | 智能轮转，补满到 N 个（默认 5） |
| `status` | 查看账号状态 |
| `check` | 检查额度 |
| `add` | 添加新账号 |
| `manual-add` | 手动 OAuth 添加账号 |
| `fill [N]` | 补满成员（Team 模式） |
| `cleanup [N]` | 清理多余成员 |
| `sync` | 同步认证文件到 CPA |
| `pull-cpa` | 从 CPA 反向同步认证文件到本地 |
| `admin-login` | 管理员登录 |

> **生产免费号**通过 Web 面板的"生成免费号"按钮触发，对应 API：`POST /api/tasks/fill { target: N, leave_workspace: true }`

## Web 管理面板

启动 `uv run autoteam api` 后访问 `http://localhost:8787`。

| 页面 | 功能 |
|------|------|
| 📊 仪表盘 | 账号统计 + 状态表格 + 登录/移出/删除/**批量删除** 🆕 |
| 👥 Team 成员 | 全部 Team 成员（含外部成员） |
| 🔁 账号池操作 | 轮转 / 检查 / 补满 / 添加 / **生成免费号** 🆕 / 清理 |
| 🔄 同步中心 | 同步账号、同步 CPA、拉取 CPA |
| 🔐 OAuth 登录 | 生成认证链接；localhost 自动回调 + 手动粘贴兜底 |
| 📜 任务历史 | 后台任务执行状态 + **实时停止** 🆕 |
| 📋 日志 | 实时日志查看器 |
| ⚙️ 设置 | 管理员登录 + 主号 Codex 同步 + 巡检配置 |

## 修复了什么

- **session_token 导入会存错 `account_id`** — 改以 `/backend-api/accounts` 为权威来源 + `/settings` 二次验证
- **Codex OAuth "Operation timed out"** — Personal 模式下跳过 step-0 ChatGPT 预登录
- **注册密码长度不足 12** — 密码生成器改为"双词 + 3-4 位数字 + 符号"，稳定 15-17 字符
- **任务取消被静默吞掉** — `_run_task` 里 `reset()` 与 `task_id` 暴露顺序修正
- **批量操作 300s 硬超时** — `_PlaywrightExecutor` 加 `run_with_timeout(timeout, func)`，按批次大小动态算
- **Team fill 后面员数 401 未触发 fail-fast** — 连续 3 次 401/403 直接中止，输出 body 片段而不是干等 180s
- **邀请 seat 兜底失败时账号被静默丢失** 🆕 — `invite_member` POST/PATCH 都加退避重试,PATCH 失败时保留 `usage_based`(codex-only) 席位,把 `seat_type` 落到 `accounts.json` 供下游差异化对待
- **`cmd_check` 只扫 active,standby 永远没额度数据** 🆕 — `autoteam check --include-standby`(或 `POST /api/tasks/check {include_standby:true}`)追加探测 standby 池,限速 1.5s + 24h 去重;401/403 标记为 `auth_invalid`
- **workspace 有席位但本地 auth 缺失的"残废 / 错位 / ghost"账号无人清理** 🆕 — `autoteam reconcile [--dry-run]`(或 `POST /api/admin/reconcile?dry_run=1`)一键识别残废 / 错位 / 耗尽未抛弃 / ghost,可通过 `RECONCILE_KICK_ORPHAN` / `RECONCILE_KICK_GHOST` 控制是 KICK 还是打标记

若你遇到 401 "Must be part of this workspace"，不用 logout 重登：

```bash
KEY="$(grep '^API_KEY' .env | cut -d= -f2)"
curl -s -H "Authorization: Bearer $KEY" http://localhost:8787/api/admin/diagnose | jq        # 看四个接口真实状态
curl -s -X POST -H "Authorization: Bearer $KEY" http://localhost:8787/api/admin/fix-account-id | jq  # 热修复
```

## 文档

原仓库的文档在 `docs/` 目录下，大部分仍然适用。

| 文档 | 内容 |
|------|------|
| [从零开始部署](docs/getting-started.md) | 完整首次部署教程 |
| [配置说明](docs/configuration.md) | .env 配置项、管理员登录、认证文件格式 |
| [Docker 部署](docs/docker.md) | Docker Compose、数据持久化 |
| [API 文档](docs/api.md) | 全部 HTTP 端点、调用示例 |
| [工作原理](docs/architecture.md) | 轮转流程、状态机、项目结构、依赖 |
| [常见问题](docs/troubleshooting.md) | 安装/登录/轮转/Docker/Web 面板问题 |

## 适用场景

- 需要维持固定数量的 Team 可用席位
- 需要**批量生产免费号**并把 Codex 认证推到 CLIProxyAPI
- 需要在 Web 面板里完成日常轮转、对账、OAuth 导入
- 在原仓库踩到本文档「修复了什么」小节中的坑

## 已知限制

- **IP 风险** — VPS 的 IP 容易被 OpenAI/Cloudflare 标记，建议使用住宅代理
- **并发限制** — 同一时间只允许一个 Playwright 操作
- **验证码** — OpenAI 验证码有效期短，网络延迟可能导致过期
- **软停止 ≠ 硬停止** — 点"停止任务"后，当前账号注册（~2 分钟）会跑完再退出，不中途打断浏览器
- **Team 席位上限** — 免费号生产时，baseline + 本批新号 ≤ 4，超过会自动缩批

更多详见 [常见问题](docs/troubleshooting.md)

## 友情链接

- 原仓库 [cnitlrt/AutoTeam](https://github.com/cnitlrt/AutoTeam)
- 认证代理 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)

感谢 **LinuxDo** 社区的支持！

[![LinuxDo](https://img.shields.io/badge/社区-LinuxDo-blue?style=for-the-badge)](https://linux.do/)

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=ZRainbow1275/AutoTeam-F&type=Date)](https://star-history.com/#ZRainbow1275/AutoTeam-F&Date)
