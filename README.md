<div align="center">

# AutoTeam

**ChatGPT Team 账号自动轮转管理工具**

自动创建账号、注册、获取 Codex 认证、检查额度、智能轮换，并同步认证文件到 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?style=for-the-badge&logo=playwright&logoColor=white)](https://playwright.dev)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?style=for-the-badge)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/FastAPI-API_&_Web-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Vue](https://img.shields.io/badge/Vue_3-Frontend-4FC08D?style=for-the-badge&logo=vue.js&logoColor=white)](https://vuejs.org)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)

</div>

---

> **免责声明**：本项目仅供学习和研究用途。使用本工具可能违反 OpenAI 的服务条款，包括但不限于自动化操作、多账号管理等。使用者需自行承担所有风险，包括账号封禁、IP 限制等后果。作者不对任何因使用本工具造成的损失承担责任。

## 特性

| | 功能 | 描述 |
|---|---|---|
| 📧 | **自动注册** | 创建临时邮箱 → 注册 ChatGPT → 自动填写验证码/个人信息 |
| 🔐 | **Codex OAuth** | 自动完成 Codex 登录，无密码时走一次性验证码 |
| 🔄 | **智能轮转** | 额度低于阈值自动移出，复用前验证，超员自动清理 |
| ☁️ | **CPA 同步** | 认证文件自动上传覆盖，只同步 active 账号 |
| 🖥️ | **Web 面板** | 仪表盘、Team 成员、操作任务、实时日志、巡检设置 |
| 🔍 | **自动巡检** | 后台定时检查额度，低于阈值自动触发轮转 |
| 🐳 | **Docker** | 一键部署，Web 页面配置，数据持久化 |

**首次使用？** 查看 [从零开始部署教程](docs/getting-started.md)，手把手完成安装、配置、首次轮转。

## 快速开始

### 安装

```bash
bash setup.sh
# 或手动: uv sync && uv run playwright install chromium
```

### 启动

```bash
# Web 面板 + API（推荐）
uv run autoteam api

# 或直接轮转
uv run autoteam rotate
```

首次启动会自动引导配置 CloudMail、CPA、API Key，并验证连通性。

### Docker 部署

```bash
git clone https://github.com/cnitlrt/AutoTeam.git && cd AutoTeam
mkdir -p data && cp .env.example data/.env
# 编辑 data/.env 填入配置（或启动后在 Web 页面配置）
docker compose up -d
```

详见 [Docker 部署文档](docs/docker.md)

### CLI 命令

| 命令 | 说明 |
|------|------|
| `api` | 启动 Web 面板 + HTTP API（默认端口 8787） |
| `rotate [N]` | 智能轮转，补满到 N 个（默认 5） |
| `status` | 查看账号状态 |
| `check` | 检查额度 |
| `add` | 添加新账号 |
| `fill [N]` | 补满成员 |
| `cleanup [N]` | 清理多余成员 |
| `sync` | 同步认证文件到 CPA |
| `admin-login` | 管理员登录 |

## Web 管理面板

启动 `uv run autoteam api` 后访问 `http://localhost:8787`。

| 页面 | 功能 |
|------|------|
| 📊 仪表盘 | 账号统计 + 状态表格 + 登录/移出/删除/同步操作 |
| 👥 Team 成员 | 全部 Team 成员（含外部成员） |
| ⚡ 操作 & 任务 | 一键轮转/检查/补满/清理/同步 + 任务历史 |
| 📋 日志 | 实时日志查看器 |
| ⚙️ 设置 | 管理员登录 + 主号 Codex 同步 + 巡检配置 |

适配桌面端和手机端。

## 文档

| 文档 | 内容 |
|------|------|
| [从零开始部署](docs/getting-started.md) | 完整的首次部署教程，从安装到首次轮转 |
| [配置说明](docs/configuration.md) | .env 配置项、管理员登录、认证文件格式 |
| [Docker 部署](docs/docker.md) | Docker Compose、数据持久化、Web 配置 |
| [API 文档](docs/api.md) | 全部 HTTP 端点、调用示例 |
| [工作原理](docs/architecture.md) | 轮转流程、状态机、项目结构、依赖 |
| [常见问题](docs/troubleshooting.md) | 安装/登录/轮转/Docker/Web 面板问题 |

## 已知限制

- **IP 风险** — VPS 的 IP 容易被 OpenAI/Cloudflare 标记，建议使用住宅代理
- **并发限制** — 同一时间只允许一个 Playwright 操作
- **验证码** — OpenAI 验证码有效期短，网络延迟可能导致过期

更多详见 [常见问题](docs/troubleshooting.md)

## 友情链接

感谢 **LinuxDo** 社区的支持！

[![LinuxDo](https://img.shields.io/badge/社区-LinuxDo-blue?style=for-the-badge)](https://linux.do/)

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=cnitlrt/AutoTeam&type=Date)](https://star-history.com/#cnitlrt/AutoTeam&Date)
