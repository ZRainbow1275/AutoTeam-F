<div align="center">

# AutoTeam-F

**面向 ChatGPT Team 的账号轮转、免费号生产与认证同步工具 · Fix + Free + Hardening 版**

基于 [cnitlrt/AutoTeam](https://github.com/cnitlrt/AutoTeam) 的 fork，修掉若干阻塞性 bug，保留并强化 **批量生产免费号（Personal）** 主路径，同时补齐并行轮转、IPv6、同步目标分发和更好的前端体验。

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

> **当前状态（2026-05-17）**：免费号自动注册路径仍然保留，并且是当前实现的主路径之一。Web 面板里的「生成免费号」和 API `POST /api/tasks/fill { target: N, leave_workspace: true }` 会走 `注册 → 主号踢出 → personal OAuth → free plan 校验 → 认证文件落盘 → 同步目标` 这一链路。与上游相比，本 fork 不再把 Personal/free 当成临时旁路，而是把它和 Team 轮转、同步和前端操作统一进同一套状态机。

## 求赞

如果本项目真的对你有用！请不要吝啬地给作者一个star（和fork那就更好啦！）吧！作者烧了很多钱进去探索出来的这个路径QAQ

## 特性

| | 功能 | 描述 |
|---|---|---|
| 📧 | **自动注册** | 临时邮箱(`cf_temp_email` / `maillab` / 兼容后端) + Playwright 自动注册，SetupPage 4 步状态机指引切换并做协议指纹嗅探 |
| 🆓 | **生产免费号** 🆕 | 批量注册 → 主号踢出 → Personal OAuth，一条龙（当前仍是主路径） |
| 🔐 | **Codex OAuth** | 自动登录 Codex，Team / Personal 双模式 |
| 🔑 | **手动 OAuth 导入** | localhost 自动回调，失败可手动粘贴 |
| 🔄 | **智能轮转** | 额度不足自动移出，旧号恢复后优先复用，并支持更安全的先踢人再换人策略 |
| ⚙️ | **并行轮转** | 复用 standby、探测与同步并行化，默认保守但可开并发 |
| 🌐 | **IPv6 出口** | 独立 IPv6 pool / proxy，按账号分配出口 |
| ☁️ | **统一同步** | 本地 active 可同步到 CPA / Sub2API，主号也有独立同步路径 |
| 🖥️ | **Web 面板** | 仪表盘、同步中心、OAuth 登录、任务历史、日志、设置、实时进度 |
| 🛑 | **软停止任务** 🆕 | 随时中止跑到一半的批次，协作式退出不留半成品 |
| 📊 | **失败分类** 🆕 | `register_failures.json` 持久化各类失败（手机号/重复/踢人/OAuth 等） |
| 🔧 | **自诊断** 🆕 | `/api/admin/diagnose` + `/api/admin/fix-account-id` 一键定位 401 |
| 🗑️ | **批量删除** 🆕 | Web 面板多选 + 一次性 kick + 删邮箱 + sync CPA |
| 🛡️ | **运行时硬化** 🆕 | Playwright guard、浏览器清理、Docker 自检、资源探针、版本指纹 |
| ✨ | **更好的前端** 🆕 | Dashboard / Pool / Sync / Tasks / Logs / Settings 全面重构，实时性、信息密度和可读性提升 |
| 🔍 | **自动巡检** | 后台定时检查额度并触发轮转 |
| 📤 | **导出认证** | 一键导出 Codex CLI 格式 auth.json |
| 🐳 | **Docker** | 支持容器部署与数据持久化 |

> 🆕 = 相对原仓库新增。其余承袭自 [cnitlrt/AutoTeam](https://github.com/cnitlrt/AutoTeam)。

## 与上游 `cnitlrt/AutoTeam` 的功能差异

> 下表按能力域汇总当前 fork 与上游的实际差异，重点覆盖你关心的免费号主路径、安全轮换、并行、IPv6、更安全的指纹、同步目标分发和前端重构。不是逐文件罗列，但已经把当前 README 应该交代的功能差覆盖到位。

| 能力域 | 上游 `cnitlrt/AutoTeam` | AutoTeam-F 当前实现 | 备注 |
|---|---|---|---|
| 免费号自动注册（Personal） | 只把自动注册描述成 Team 方向的能力，没有把 Personal/free 生产作为当前主路径展开 | 仍保留并可直接走：注册 → Team 邀请 → 主号踢出 / leave_workspace → personal OAuth → free plan 校验 → auth 文件落盘 | 当前主路径 |
| Team 子号自动注册 | 基础 Team 注册与 OAuth | 加入 add-phone 检测、workspace 选择、session_token 注入、plan 白名单、失败留池和重试退避 | 主路径更稳 |
| 安全轮换 | 常规按额度轮转 | 先踢人再换人、对账先清异常再补位，减少席位冲突和残留 | 轮换安全性更高 |
| 并行能力 | 以串行为主 | `ROTATE_CONCURRENCY`、并发 standby 复用、并发被踢探测、Sub2API 并发同步 | 默认保守，可开 |
| 预测式替换 | 无 | `PREDICTIVE_ENABLED` + `QuotaPredictor`，在预计耗尽前抢先换出 | 提前释放席位 |
| 账号状态机 | 状态较少 | 扩展为 `active / exhausted / standby / pending / personal / auth_invalid / orphan / disabled`，并落 `seat_type`、`workspace_account_id` 等字段 | 支持更细粒度运维 |
| 对账与异常清理 | 基础同步 / 清理 | `reconcile` 识别残废、错位、耗尽未抛弃、ghost、over-cap，支持 dry-run | 自动修复更完整 |
| 主号健康门 | 无统一健康闸 | `master_health` / grace / cancelled / unhealthy 门禁，personal/team 入口都先校验 | 避免错路由 |
| 邮箱后端 | `CloudMail` / `Cloudflare Temp Email` 简单切换 | provider 抽象层 + `maillab` / `cf_temp_email` / `addy.io` / `simplelogin` + 路由探测 | 兼容更多后端 |
| 邮箱归属验证 | 基础连通性校验 | 4 步状态机：provider → 连接 → 域名归属 → 保存 | 误配更早暴露 |
| 远端同步 | 仅 CPA 基线同步 | `sync_targets` 统一分发，支持 CPA + Sub2API + 主号同步，新增目标级配置、优先级、并发、白名单、WS 模式、自动暂停 | 不再只有单同步点 |
| 主号 Codex 同步 | 基本导出 | 主号同步、session token fallback、动态目标文案、目标层统一接入 | 对主号更友好 |
| IPv6 | 无专门出口池 | `ipv6_pool` / `ipv6_proxy`，按账号分配独立 IPv6 出口 | 适配更细 |
| 运行时 / Playwright | 基础自动化 | `_playwright_guard`、browser cleanup、资源探针、Docker self-check、浏览器 zombie 监测、版本指纹 | 更稳、更可观测 |
| Docker / 部署 | 基础 compose | entrypoint 自检、镜像 revision / build time、`/api/version`、部署与重建 SOP、资源边界守卫 | 更容易排障 |
| 前端 | 基础面板 | Dashboard / Pool / Sync / Settings / Tasks / Logs / Setup 全面重构，实时状态、SSE 进度、Toast、健康卡片、批量操作 | 体验和响应更好 |
| 运维 API | 基础 API | 新增 / 增强 `/api/admin/reconcile`、`/api/admin/diagnose`、`/api/admin/fix-account-id`、`/api/accounts/{email}/probe`、`/api/version`、`/api/tasks/check include_standby` | 运维能力更全 |
| 失败与日志 | 单次报错为主 | `register_failures.json` 持久化失败分类，实时任务历史 / 日志面板 | 便于回溯 |
| 多 workspace 池 | 无 | `workspace_pool` + 自动 failover + Team 池资源调度 | 适合更大规模 |
| 账号禁用 | 无本地禁用态 | `disabled` 本地禁用字段，禁用账号自动跳过轮转 / 同步 | 便于人工管控 |

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

首次启动会自动引导配置 临时邮箱后端(`cf_temp_email` / `maillab` 双后端,**强烈推荐显式声明 `MAIL_PROVIDER`**)、CPA、API Key,并验证连通性。两种后端的差异见 [配置说明 · Mail Provider 切换](docs/configuration.md#mail-provider-切换)。

> **推荐顺序**:`maillab/cloud-mail`(国内一键部署、社区活跃)→ `dreamhunter2333/cloudflare_temp_email`(经典 Cloudflare Workers 实现)。两者功能等价,根据部署条件选用即可。
>
> ⚠️ 如果你之前用的是上游 [cnitlrt/AutoTeam](https://github.com/cnitlrt/AutoTeam) 的 "cloudmail",那其实是 [`maillab/cloud-mail`](https://github.com/maillab/cloud-mail)。本 fork 把它独立成 `MAIL_PROVIDER=maillab` 后端,需要在 `.env` 里显式设置(不再是默认)。详见 [docs/configuration.md#mail-provider-切换](docs/configuration.md#mail-provider-切换)。
>
> SetupPage / Settings 提供 4 步状态机引导(provider → 服务器连接 → 域名归属 → 保存),通过 `/api/mail-provider/probe` 提前检测错配 / 凭据 / 域名归属;启动时还会做协议指纹嗅探,base_url 与 `MAIL_PROVIDER` 错配会**直接 abort**,避免"登录成功 → 创建邮箱 401"半成功假象([issue #1](https://github.com/ZRainbow1275/AutoTeam-F/issues/1))。

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
| `rotate [N]` | 智能轮转，补满到 N 个总席位（默认 3，最多 3） |
| `status` | 查看账号状态 |
| `check` | 检查额度 |
| `add` | 添加新账号 |
| `manual-add` | 手动 OAuth 添加账号 |
| `fill [N]` | 补满成员（Team 模式） |
| `cleanup [N]` | 清理多余成员 |
| `sync` | 同步认证文件到 CPA / Sub2API |
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
| 🔄 同步中心 | 同步账号、同步 CPA / Sub2API、拉取 CPA |
| 🔐 OAuth 登录 | 生成认证链接；localhost 自动回调 + 手动粘贴兜底 |
| 📜 任务历史 | 后台任务执行状态 + **实时停止** 🆕 |
| 📋 日志 | 实时日志查看器 |
| ⚙️ 设置 | 管理员登录 + 主号 Codex 同步 + 巡检 / 轮转 / 同步目标配置 |

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
- **子号巡检在网络抖动 / 5xx 时被错误标 auth_invalid → 整批号被踢** 🆕 — `check_codex_quota` 新增 `network_error` 分类(DNS / Timeout / SSL / 5xx / 429 / 4xx 非 401/403 / JSON 解析失败 → 临时性故障),`_probe_standby_quota` 看到 `network_error` 不写 `last_quota_check_at`、不改 status,等下一轮立即重试,不再被 24h 去重屏蔽

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

- **IP 风险** — VPS 的 IP 容易被 OpenAI / Cloudflare 标记，建议使用住宅代理
- **并发边界** — 默认仍以保守串行为主；开启并发后，Playwright 浏览器上下文共享仍需要遵守线程安全边界
- **验证码** — OpenAI 验证码有效期短，网络延迟可能导致过期
- **软停止 ≠ 硬停止** — 点"停止任务"后，当前账号注册（~2 分钟）会跑完再退出，不中途打断浏览器
- **平台策略变化** — 免费号路径依赖母号健康、workspace 选择和 OpenAI 后端策略；如果 plan drift / add-phone / workspace 规则变化，可能会 fail-fast 留池
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
