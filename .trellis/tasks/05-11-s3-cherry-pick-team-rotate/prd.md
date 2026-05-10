# S3 — Cherry-pick team 替换链路 9 函数回贴

## 父 PRD 引用

继承 `05-11-upstream-align-register-multimail-frontend-refresh/prd.md` 中
Q1 = Approach 1（Cherry-pick 补丁模式）+ S3 子任务范围。本子任务负责按
S0 报告 (`.trellis/tasks/05-11-s0-upstream-team-rotate-diff/research/upstream-diff.md`)
中标 ✓ 应回贴 / ⚠ 需 S1 适配 的项执行。

## Goal

1. 按 S0 报告精确清单回贴上游 9 函数中标注「上游独有 / 本地退化」的
   关键逻辑 + 整套 `auth_repair` 状态机适配（基于 round-12 S1 落地的
   `account_state.py` StateMachine + `update_account` 已统一路由）。
2. 所有 status 写入仍走 `update_account()` —— 不绕过状态机。
3. 保留 round-9~11 本地 fork 增强（GRACE / cancel_signal / quota 二次
   实测 / RegisterBlocked 三态分类 / retroactive helper 等）。

## Scope（按 S0 分类对照执行）

### A. 应回贴（独立先行，不依赖 S1）

1. **`invite_to_team(chatgpt_api, email, seat_type="default")`** —
   原样回贴上游 14 行（`manager.py`）。本地 `chatgpt_api.invite_member`
   已自带 default→usage_based fallback；该 helper 仅做 bool 语义封装，
   降低 `reinvite_account` token-revoke 路径走完整 OAuth 的成本。

2. **`signup_profile` 透传**（`_complete_registration` + `register_with_invite`）：
   - 新建 `src/autoteam/signup_profile.py`（dataclass `SignupProfile` +
     `generate_signup_profile()`）。
   - `invite.register_with_invite(..., signup_profile=None)` 加可选
     形参，向后兼容。
   - `_complete_registration` 生成 profile 并透传给 `register_with_invite`。
   - 让"注册-OAuth 资料一致"成为可观察约束，避免 about-you 阶段
     OpenAI 因前后姓名/生日不一致触发风控。

### B. 部分回贴 cmd_rotate 4 helper（不依赖 S1）

3. **`_pool_active_target(team_target) -> int`** —— `max(0, team_target - 1)`。

4. **`_count_pool_active_accounts(accounts=None, *, require_auth=False) -> int`** ——
   非主号 + status=ACTIVE（可选 `_has_auth_file`）。

5. **`_count_local_team_seat_accounts(accounts=None) -> int`** ——
   非主号 + status ∈ {ACTIVE, EXHAUSTED, AUTH_INVALID}。

6. **`_estimate_local_team_member_count(team_target, accounts=None) -> int`** ——
   `_count_local_team_seat_accounts() + (1 if team_target > 0 else 0)`。

7. **双指标终止条件 + vacancy 兜底**：
   - vacancy 计算用 `_estimate_local_team_member_count` 替代
     `local_active = sum(... STATUS_ACTIVE)` 兜底（更精确）。
   - 主循环 5/5 终止改为
     `current_count >= TARGET AND pool_active >= ACTIVE_TARGET`。

8. **`ensure_account_mail(acc)`**（cmd_rotate 内部 helper）：
   按 acc 的 `mail_provider` / `mail_account_id` / `cloudmail_account_id`
   选 mail client，缓存到 `reuse_mail_clients` dict。S2 接 Addy.io 的
   前置基础设施。

### C. 结合 S1 状态机的 auth_repair 重设计（依赖 S1）

9. **常量**：
   - `AUTH_REPAIR_HARD_FAILURE_TYPES = frozenset({"human_verification"})`
   - `config.AUTO_CHECK_RETRY_ADD_PHONE` (默认 True)
   - `config.AUTO_CHECK_ADD_PHONE_MAX_RETRIES` (默认 3)

10. **辅助**：
    - `_auth_repair_reset_fields() -> dict` —— 6 字段清零模板。
    - `_auth_repair_retry_delays() -> tuple[int, int, int]` ——
      `(2x, 4x, 6x) * AUTO_CHECK_INTERVAL`。
    - `_auth_repair_retry_add_phone_enabled() -> bool`
    - `_auth_repair_add_phone_max_retries() -> int`
    - `_auth_repair_add_phone_retry_delays(max_retries) -> tuple[int, ...]`
      —— `(2^idx) * AUTO_CHECK_INTERVAL`。
    - `_auth_repair_error_label(error_type)` —— 失败类型中文标签映射。
    - `_auth_repair_state_suffix(state)` / `_auth_repair_result_suffix(result)`
      —— 给日志和 UI 提供"约 X 分钟后重试 / 已暂停 / 已释放席位"语义。
    - `_auth_repair_skip_reason(acc, *, force=False, now=None)` ——
      返回 None 表示可走修复，否则返回中文跳过原因（冷却 / 暂停）。
    - `_auth_repair_reset(email)` —— `update_account` 写清零字段。

11. **`_release_auth_repair_team_seat(email, *, chatgpt_api=None) -> str`** ——
    上游回贴；返回 `"removed" | "already_absent" | "failed"`。
    依赖本地 `remove_from_team(...,return_status=True)`（已存在）。

12. **`_record_auth_repair_failure(email, error_type, error_detail, *, chatgpt_api=None) -> dict`**:
    - 三分支：`add_phone(可重试)` / `add_phone(超限) | hard_failure` /
      普通衰退式 retry_after。
    - 写 `auth_retry_count / auth_last_error / auth_last_error_detail /
      auth_last_failed_at / auth_retry_after / auth_retry_paused` 字段
      + `status=STATUS_AUTH_INVALID|STATUS_STANDBY` 终态。
    - **本地适配**：上游 `STATUS_AUTH_PENDING` → 本地 `STATUS_AUTH_INVALID`
      （literal 同为 `"auth_invalid"`，state machine 中 `AccountState.AUTH_PENDING`
      映射到 `"auth_invalid"`，保持一致）。
    - **状态机路由**：通过 `update_account(email, status=...)` 自动走
      `default_machine.transition`，无需显式调用 transition。

13. **保留本地 fork** 不动：`reinvite_account` 内的
    `_cleanup_team_leftover` / `is_supported_plan` 白名单 / quota 二次
    实测 / RegisterBlocked / GRACE / cancel_signal / retroactive helper。

## 必读引用

- `.trellis/tasks/05-11-s0-upstream-team-rotate-diff/research/upstream-diff.md`
  —— S0 diff 报告（精确分类清单）
- `.trellis/tasks/05-11-upstream-align-register-multimail-frontend-refresh/prd.md`
  —— 父 PRD
- `.upstream/manager.py` —— 上游 baseline（行号见 S0 报告）
- `src/autoteam/account_state.py` —— S1 状态机（已 commit ef1637c）
- `src/autoteam/accounts.py` —— `update_account` 已自动路由 transition
- `src/autoteam/mail/__init__.py` —— S2 多 provider fallback 工厂

## Acceptance Criteria

- [ ] `invite_to_team` 模块级 helper 落地 + 单测
- [ ] `signup_profile.py` 模块 + `register_with_invite(signup_profile=)` 可选参 + 单测
- [ ] `_pool_active_target` / `_count_pool_active_accounts` /
      `_count_local_team_seat_accounts` / `_estimate_local_team_member_count`
      四 helper 落地 + 单测
- [ ] cmd_rotate vacancy 兜底改用 `_estimate_local_team_member_count`
- [ ] `ensure_account_mail` per-account provider 路由（cmd_rotate 内）+ 单测
- [ ] `AUTH_REPAIR_HARD_FAILURE_TYPES` 常量 + config 两个开关落地
- [ ] `_release_auth_repair_team_seat` + `_record_auth_repair_failure`
      + 全部 `_auth_repair_*` helper 落地，所有 status 写入走 `update_account`
- [ ] 上游 `STATUS_AUTH_PENDING` → 本地 `STATUS_AUTH_INVALID` 适配（语义等价）
- [ ] 单测覆盖 `_record_auth_repair_failure` 的 add_phone 软重试 / hard_failure
      暂停 / 普通衰退 retry_after 三分支
- [ ] `ruff check src/autoteam/` 全绿
- [ ] `pytest tests/` >= 576 passed（不退化）

## Definition of Done

- 所有验收点 ✓
- 新增/修改的代码全部走 update_account 路由（不绕状态机）
- commit 信息：`feat(round-12 S3): cherry-pick upstream team rotate (per S0 diff report)`
- git add 仅自己改的文件（严禁 -A）

## Out of Scope

- 不动 S1 模块（account_state.py / accounts.py）—— 仅消费
- 不动 mail provider 模块 —— 仅消费
- 不实现 S4 注册收尾双路径修复
- 不实现 S5/S6 预测/并发
- 不动 web/
- 不重命名上游 `STATUS_AUTH_PENDING`（保持本地 `STATUS_AUTH_INVALID`，
  literal 已同为 `auth_invalid`）

## Risk Notes

- ⚠ 上游 `_record_auth_repair_failure` 期望 STATUS_AUTH_PENDING ≠ STATUS_STANDBY
  双状态语义；本地 STATUS_AUTH_INVALID 等价于 AUTH_PENDING（state_machine 映射），
  迁移时确保不产生新 illegal transition。
- ⚠ `register_with_invite` 改签名为可选参，向后兼容（默认 None → 保持
  原 random 行为）。
- ⚠ `_auth_repair_skip_reason` 暂未在 cmd_rotate 中接入（需 S1 完整字段
  落本地 acc 后才有意义）—— 本任务仅落地 helper，不在主流程调用，
  避免影响现有路径行为。
