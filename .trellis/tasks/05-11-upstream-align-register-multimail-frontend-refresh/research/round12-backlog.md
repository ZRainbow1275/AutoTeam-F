# Round-12 Backlog — deferred to round-13

生成日期：2026-05-11
来源：round-12 wire-up audit fixes (commit hash: TBD after commit).

## 上下文

本次 round-12 wire-up commit 修复了 audit 报告里的所有 Critical (C1-C3) + 6 个 Major (M1-M4 + Deep M2) + 3 个 Minor (quota_predictor fsync / alias_reader token return / SSE bounded queue). 以下 Minor 与 Tech-Debt 项目延后到 round-13 处理.

## 延后清单

### Minor / Tech-Debt

| 编号 | 来源报告 | 描述 | 建议位置 |
|------|----------|------|----------|
| N1 (integration) | review-integration.md | `add_account` 对 `IllegalTransitionError` 「吞掉但仍落盘」不对称 | `src/autoteam/accounts.py:158-166` |
| N2 (integration) | review-integration.md | S7 transition_log 与 S1 state_log.jsonl 彼此独立,前端 SSE 看不到 workspace failover | `src/autoteam/api.py:_build_sse_event_stream` + `workspace_pool.subscribe` |
| N3 (integration) | review-integration.md | quota_predictor history 文件大小无 cap (max-file-bytes 触发轮转) | `src/autoteam/quota_predictor.py:record` |
| N4 (integration) | review-integration.md | `CloudMailClient = get_mail_client` alias 可能导致 47+ 处旧调用点行为漂移 | `src/autoteam/mail/__init__.py:123` + 47 个调用点全扫描 |
| m3 (deep) | review-deep.md | `workspace_pool._seed_from_admin_state` 返回 doc 但不持久化 (反复 import + seed) | `src/autoteam/workspace_pool.py:161-198` |
| m4 (deep) | review-deep.md | `mail_provider_state.json` chmod 0o666 + last_error 可能含 API 响应片段 | `src/autoteam/mail/fallback.py:96-104` |
| m5 (deep) | review-deep.md | `classify_register_failure` 子串匹配可能误判 (无 word boundary) | `src/autoteam/mail/register_dual_path.py:99-113` |
| m8 (deep) | review-deep.md | `api.py` skip-auth 列表含 `/api/setup/save` (写端点),应做"已配置时强制 API_KEY" | `src/autoteam/api.py:114-121` |
| m9 (deep) | review-deep.md | `simplelogin._parse_json` / `addy_io._parse_json` 错误信息包含 `r.text[:200]` 可能泄露 PII | `src/autoteam/mail/{simplelogin,addy_io}.py` |
| M3 (deep) | review-deep.md | mypy --strict 73 errors on r12 modules (`workspace_pool.py` + `mail/*.py`) — generic dict 缺类型参数, `__init__` 无 return annotation 等 | 批量加 `dict[str, Any]` + `Action[T]` 重构 |
| M4 (deep) | review-deep.md | `state_log.jsonl` 每次 transition 全文件重写 (O(N²) 风险) | `src/autoteam/account_state.py:_write_state_log` 改 native append |

### F1 后续

| 编号 | 描述 | 备注 |
|------|------|------|
| F1-followup | 21 个组件仍用 `text-white` / `bg-gray-800` 等暗色 utility, 当前靠 `style.css §Compat` 翻转 — round-13 应规划完整 token 迁移到 `text-on-accent` / `bg-surface-hover` | `web/src/components/**/*.vue` |

### C3 真正解耦 (round-13 必做)

| 编号 | 描述 | 备注 |
|------|------|------|
| C3-real | 实现 per-worker `ChatGPTTeamAPI` lifecycle: 每 worker 自己 `.start()` 一个独立的 BrowserContext, 不再共享主线程构造的 chatgpt 实例 — 真正解锁 ROTATE_CONCURRENCY > 1 | `src/autoteam/manager.py:cmd_rotate` + `chatgpt_api.py` |

### 优先级

- **High**: M4 (deep) state_log O(N²) — 数千 transition 后可观察到延迟堆积
- **Medium**: N4 (CloudMailClient alias 漂移), m5 (classify 误判), M3 mypy strict
- **Low**: 其他 minor / N1 / N2 / m3 / F1 token 迁移

## round-13 入口

建议下一轮以 "tech-debt + 真并发" 为主题, 三个 P0:
1. C3-real per-worker browser
2. M4 (deep) state_log native append
3. mypy strict + dict 类型参数完整化
