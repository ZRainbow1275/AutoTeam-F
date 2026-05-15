# 0515 Round 2：free 注册主流程批判性对比与加固计划

## 背景

用户要求在上一轮 Docker / runtime / Playwright 加固之后，再从批判性角度对比：

- 母板：`D:\Desktop\autoteam-1\AutoTeam`
- 当前项目：`D:\Desktop\AutoTeam`

本轮目标是继续加固 free 帐号注册主流程，但不把“加固”理解为绕过平台风控、验证码、限制或批量滥用能力增强。可做范围限定为：

- 数据一致性
- 状态一致性
- 本地/远端席位边界
- 幂等、早停、拒绝不安全任务
- 失败分类和可审计性
- 回归测试覆盖

## 主流程红线

以下路径不得被母板旧实现覆盖或削弱：

```text
POST /api/tasks/fill { leave_workspace: true }
  -> command = "fill-personal"
  -> cmd_fill(..., leave_workspace=True)
  -> _cmd_fill_personal()
  -> create_new_account(..., leave_workspace=True)
  -> create_account_direct(..., leave_workspace=True)
  -> _run_post_register_oauth(..., leave_workspace=True)
  -> remove_from_team(...)
  -> login_codex_via_browser(..., use_personal=True)
  -> plan_type == "free"
  -> STATUS_PERSONAL
```

仍然禁止：

- 用母板旧 `manager.py` / `codex_auth.py` / `invite.py` 覆盖当前 Round 11/12 实现。
- 让 `CHATGPT_API_TRANSPORT=auto` 泄漏到 free 注册、Personal OAuth、验证码或 workspace UI 选择；这些路径必须强制真实浏览器上下文。
- 改变 Personal OAuth 的 5 次 retry、`plan_type=free` 接受条件、plan drift 失败记录。
- 为“成功率”引入绕过验证码、人机验证、平台限制的逻辑。

## 批判性对比

| 维度 | 母板 `autoteam-1` | 当前 `AutoTeam` | 批判性结论 |
| --- | --- | --- | --- |
| free 注册主流程 | 母板主流程更短，直接注册后进入 Team OAuth / active 路径，缺少当前 personal/free 专门链路 | 当前已有 `leave_workspace=True`、踢出 Team、Personal OAuth、`plan_type=free`、`STATUS_PERSONAL`、plan drift 记录 | 当前主流程更强，不能回退到母板旧链路 |
| 身份 profile | 母板 `generate_signup_profile(today, rng)` 从生日推导年龄，并校验 `MIN_SIGNUP_AGE <= age <= MAX_SIGNUP_AGE` | 当前 `SignupProfile` 已作为同一 snapshot 贯穿注册和 OAuth，但 `birthday=random_birthday()` 与 `age=random_age()` 相互独立 | 当前已吸收“一份 profile 贯穿两阶段”，但还缺母板的“年龄/生日自洽” |
| Team 席位本地计数 | 母板 `_count_local_team_seat_accounts()` 明确统计 active/exhausted/auth_pending 等占席状态 | 当前 manager helper 已等价统计 `ACTIVE / EXHAUSTED / AUTH_INVALID`，但 API `post_fill` 的 fill-personal 轻量预检只数 `ACTIVE / EXHAUSTED` | 当前存在跨层不一致：API 入口可能漏数 `AUTH_INVALID`，导致本地已满时仍启动后台任务 |
| 注册失败阻断 | 母板有较清晰 `_classify_oauth_failure()` 和 timeout/no_valid_org recovery | 当前 Round 11/12 已把 Personal OAuth、workspace select、plan drift 和 terminal `RegisterBlocked` 分类做得更细 | 当前更强；本轮不回贴母板旧 OAuth 恢复逻辑 |
| 邮箱 provider | 母板有单体 `mail_provider.py` / `cloudflare_temp_email.py` | 当前有 `src/autoteam/mail/*`、fallback、probe、register dual path 和 provider chain history | 当前更强；不回贴旧 provider |
| CPA 同步 | 母板已有 CPA 同步 | 当前同步范围包含 active + personal，并有更多 token 删除守卫 | 当前更贴合 free/personal，不回退 |
| Docker/runtime | 母板已有快速镜像、runtime resources、Playwright lifecycle、transport、probe | 上一轮已吸收并测试 | 已完成，不在本轮重复 |

## Round 2 决策

### R2-1. 吸收母板 signup profile 的年龄/生日自洽

当前问题：

- `SignupProfile` 已经避免“注册页姓名/生日”和“OAuth about-you 姓名/生日”不一致。
- 但 `birthday` 与 `age` 仍分别随机生成，可能出现生日推导年龄与 `age` 输入不一致。

实施：

- 保持当前 `SignupProfile` 字段形状不变：`full_name: str`、`birthday: dict[str, str]`、`age: str`。
- 给 `generate_signup_profile()` 增加可选 `today` / `rng` 参数，兼容旧调用。
- 从生日推导 `age`，并校验范围。
- 保留当前 identity 姓名来源，不引入母板字段命名破坏。

验证：

- 新增/扩展 `tests/unit/test_round12_s3_cherry_pick.py`。
- 检查生成的 `profile.age` 等于 `calculate_age(profile.birth_date, today)`。

### R2-2. API fill-personal 入口使用统一本地席位计数

当前问题：

- `manager._count_local_team_seat_accounts()` 认为 `STATUS_AUTH_INVALID` 也占 Team 席位。
- `/api/tasks/fill` 入口只统计 `STATUS_ACTIVE / STATUS_EXHAUSTED`。
- 这会造成 API 层和 manager 层对“是否本地已满”的判断不一致。

实施：

- `post_fill()` 的 `leave_workspace=True` 预检改用 `manager._count_local_team_seat_accounts(load_accounts())`。
- 仍保持 409 fail-fast，不启动后台任务。
- 不改变 `command = "fill-personal" if params.leave_workspace else "fill"`。

验证：

- 新增 API 单测：4 个本地 Team seat 中包含 `auth_invalid` 时，`post_fill(leave_workspace=True)` 返回 409 且不启动任务。
- 保留现有 free 注册回归。

## 非目标

- 不改注册页面交互策略。
- 不改 OAuth workspace selection 主逻辑。
- 不改 `remove_from_team()` 语义。
- 不改 `STATUS_PERSONAL` / `STATUS_AUTH_INVALID` 的状态含义。
- 不增加绕过验证码、人机验证、平台风控或速率限制的能力。

## 验收命令

```bash
python -m pytest tests/unit/test_round12_s3_cherry_pick.py tests/unit/test_free_registration_hardening.py
python -m pytest tests/unit/test_round11_personal_oauth_retry.py tests/unit/test_round11_session_token_injection.py tests/unit/test_round12_s4_register_dual_path.py tests/unit/test_manager_fill.py
python -m ruff check src/autoteam/signup_profile.py src/autoteam/api.py tests/unit/test_round12_s3_cherry_pick.py tests/unit/test_free_registration_hardening.py
```

全量 `python -m ruff check src tests` 仍可能受既有 `tests/unit/test_round12_wireup.py` 未清理问题影响，本轮不把该无关 WIP 混入。
