# autoteam1 IPv6 proxy isolation migration

## Goal

把 `D:\Desktop\autoteam-1\AutoTeam` 里已经验证过的 IPv6 pool / per-account proxy isolation 能力迁移到当前 `D:\Desktop\AutoTeam`，让注册、登录、探活、同步等会触发浏览器或外部网络访问的路径能够按账号分配独立 IPv6/proxy 资源，同时保持当前免费注册语义、现有 Round 11/12 hardening、以及 Docker 运行态不被破坏。

## What I already know

* 当前仓 `src/autoteam/` 里没有 `ipv6_pool.py` / `ipv6_proxy.py`，也没有 `AUTOTEAM_IPV6_POOL_ENABLED` 相关的当前实现或测试。
* 目标仓 `D:\Desktop\autoteam-1\AutoTeam` 已有完整 IPv6 相关实现：
  * `src/autoteam/ipv6_pool.py`
  * `src/autoteam/ipv6_proxy.py`
  * `src/autoteam/config.py` 中的 `AUTOTEAM_IPV6_POOL_ENABLED` / `AUTOTEAM_IPV6_POOL_REQUIRED` / `IPV6_PREFIX` / `IPV6_IFACE` / `IPV6_PROXY_POOL_FILE`
  * `src/autoteam/manager.py` 中的 `_ensure_account_ipv6_proxy()` / `_release_account_ipv6_proxy()` 及注册路径挂接
  * `src/autoteam/chatgpt_api.py`、`src/autoteam/cpa_sync.py` 里的 admin / sync 入口挂接
  * `tests/unit/test_ipv6_pool.py`
* 现有审计文件已经把 IPv6 gap 标成 HIGH，且确认它不是当前前端 UI/UX 任务的一部分，而是一个独立迁移 slice。
* 当前仓已经有更近的 free-registration hardening、Playwright lifecycle cleanup、runtime resource probes、以及其它 Round 11/12 修复；IPv6 迁移不能把这些语义倒退。

## Assumptions (temporary)

* 迁移目标是“功能对齐 + 向后兼容”，不是重写注册流。
* 默认仍保持 IPv6 pool 关闭，只有显式配置时才启用。
* 目标仓的实现模式是当前最可信参考，除非 current repo 的现有约定明显冲突。
* Sub2API sync / proxy 保持为后续独立任务，不并入本 slice。

## Open Questions

* None so far. 当前缺口已经足够明确，先按目标仓对齐实现，再通过测试和浏览器/运行态验证发现剩余问题。

## Requirements (evolving)

* 新增 IPv6 proxy pool 模块，支持按账号分配、复用、释放、持久化和恢复。
* 将配置项补入 `src/autoteam/config.py` 与 `.env.example`，并保持默认关闭。
* 在 `manager.py` 的账号注册 / 登录 / 同步路径中接入 `_ensure_account_ipv6_proxy()` 和 `_release_account_ipv6_proxy()` 语义。
* 在 `chatgpt_api.py` 与必要的同步入口中接入 admin 侧 IPv6 proxy。
* `/api/status` 暴露 IPv6 pool 状态，`/api` 的启动 / 停止路径能管理 pool 生命周期。
* 当 `AUTOTEAM_IPV6_POOL_REQUIRED=true` 时，分配失败应是显式错误，不得静默回退到直连。
* 当前免费注册语义、现有回归测试、Docker 自检与 runtime resource 行为不得被破坏。

## Acceptance Criteria (evolving)

* [x] 当前仓新增 `src/autoteam/ipv6_pool.py` 和 `src/autoteam/ipv6_proxy.py`，并通过静态导入检查。
* [x] `src/autoteam/config.py` 与 `.env.example` 包含 IPv6 pool/proxy 配置项。
* [x] `manager.py`、`chatgpt_api.py`、`cpa_sync.py` / 相关入口接入 IPv6 proxy 分配与释放逻辑。
* [x] `/api/status` 返回 IPv6 pool 状态信息，`/api` 启动与关闭路径可驱动 pool 生命周期。
* [x] `tests/unit/test_ipv6_pool.py` 或等价回归测试在当前仓补齐并通过。
* [x] 相关现有 free registration / runtime / status 回归测试继续通过。
* [x] 目标功能的代码层状态合约不要求手动刷新才能看到 pool 状态变化；本轮通过 `/api/status.ipv6_pool` 单测、前端既有 status 刷新链路、以及 `8790` 本地浏览器 DOM / 截图验证承接，未扰动现有 `8787` 容器。

## Completion Evidence

* 代码迁移范围：`.env.example`、`src/autoteam/config.py`、`src/autoteam/ipv6_pool.py`、`src/autoteam/ipv6_proxy.py`、`src/autoteam/chatgpt_transport.py`、`src/autoteam/chatgpt_api.py`、`src/autoteam/api.py`、`src/autoteam/manager.py`、`src/autoteam/codex_auth.py`、`src/autoteam/cpa_sync.py`、`web/src/components/PoolPage.vue`、`src/autoteam/web/dist/**`。
* 回归覆盖：`tests/unit/test_ipv6_pool.py`、`tests/unit/test_api_status.py`、`tests/unit/test_cpa_sync.py`，以及 Round 11/12 free-registration / Playwright cleanup / session-token 相关单测。
* 前端浏览器证据：`web/src/components/PoolPage.vue` 新增 IPv6 pool 面板；Playwright 在 `http://127.0.0.1:8790/` 的 Pool 页面验证了 `IPV6 PROXY POOL`、`默认关闭`、`分配数/异常/过期/端口使用` 文本存在，桌面与 mobile 视口均无横向溢出；截图已保存到 `screenshots/autoteam-ipv6-pool-page-1440-2026-05-17T06-49-02-795Z.png` 和 `screenshots/autoteam-ipv6-pool-page-mobile-2026-05-17T06-49-36-098Z.png`。
* 当前运行容器边界：未重启或改动现有 `autoteam` 容器；本轮以本地代码、静态检查和单测验证为准。
* 明确不包含：Sub2API sync / proxy，仍作为后续独立 gap。

## Definition of Done

* 相关测试新增 / 更新，并在当前仓通过。
* lint / build / 关键单测通过。
* 容器与本地运行态不会被破坏。
* 如果实现里形成了新约定，补入 `.trellis/spec/` 或任务研究记录。

## Out of Scope (explicit)

* 不把 Sub2API sync / proxy 混进这个任务。
* 不重写当前注册业务逻辑本身。
* 不改动 Docker 运行容器的现有进程。

## Technical Notes

* Current repo gap audit: `.trellis/tasks/05-15-frontend-uiux-deep-repair/research/autoteam1-code-review-gap-audit.md`
* Target repo references:
  * `D:\Desktop\autoteam-1\AutoTeam\src\autoteam\ipv6_pool.py`
  * `D:\Desktop\autoteam-1\AutoTeam\src\autoteam\ipv6_proxy.py`
  * `D:\Desktop\autoteam-1\AutoTeam\src\autoteam\manager.py`
  * `D:\Desktop\autoteam-1\AutoTeam\src\autoteam\chatgpt_api.py`
  * `D:\Desktop\autoteam-1\AutoTeam\src\autoteam\cpa_sync.py`
  * `D:\Desktop\autoteam-1\AutoTeam\tests\unit\test_ipv6_pool.py`
  * `D:\Desktop\autoteam-1\AutoTeam\.env.example`
* Current repo references:
  * `src/autoteam/config.py`
  * `src/autoteam/api.py`
  * `src/autoteam/manager.py`
  * `src/autoteam/chatgpt_api.py`
  * `src/autoteam/cpa_sync.py`
  * `tests/unit/test_api_status.py`
  * `tests/unit/test_free_registration_hardening.py`
