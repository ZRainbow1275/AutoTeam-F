# Round 8 Quality-Reviewer 终审报告

> 时间:2026-04-27(Asia/Shanghai)
> Reviewer:quality-reviewer(Stage 3)
> Scope:Round 8 — Master Team Subscription Expiry + OAuth Sticky-Rejoin 深度修复
> 输入:Stage 1 spec(2 份新 shared + spec-2 v1.5)+ Stage 2 实现(2 个新模块 + 4 个修改 + 3 个新单测文件)+ `.trellis/tasks/04-27-master-team-degrade-oauth-rejoin/prd.md` 7 项 AC

---

## 0. 终审结论

| 维度 | 结果 |
|---|---|
| pytest 全绿 | ✅ 258 / 258 通过(基线 215 + Round 8 新增 43,>"≥18" 要求) |
| ruff lint | ✅ Round 8 新文件 + 修改文件 0 lint(9 条 I001 全在 Round 6/7 baseline,与本轮无关) |
| pnpm build | ✅ 1.05s 成功(`web/dist` 产物已更新到 `index-uLYWyiUr.js / index-CVOmCVGW.css`) |
| 4 项关键代码到位 | ✅ L1 fail-fast / L2 workspace_select / L3 五次重试 / sleep(8) 删除 全部对齐 spec |
| Round 1~7 既有套件回归 | ✅ test_round6_patches + test_round7_patches + test_spec2_lifecycle + test_plan_type_whitelist + test_reconcile_anomalies 共 104 用例全绿 |
| 文档校核 | ✅ shared/ 同目录 link 全部存在,0 dead link;PRD-7 引用以 `.trellis/tasks/04-27-…/prd.md` 形式存在(prompts/0426/prd/prd-7-…md 待补,P1 偏差) |
| 抓包决策记录 | ✅ research/capture-oauth-flow.md 完整(母号已 cancel 不能 live test → 双轨实施 + 上线观察,符合 task 允许跳过 live OAuth 实测的指引) |

**最终判定:NEEDS_FIX(P1 必修 1 项 / P2 建议 3 项)**

> P0 缺陷 0 项,功能层全部到位。P1 偏差 1 项(spec §3.7 表 M-T2 Team 路径 master probe 未实施)是与 spec 一致性问题,**不阻断 personal 主链路**(personal 路径完整,可上线生产)。如果用户接受"Team 路径 master probe 留 backlog,后续 patch 期补",可改判 PASS。

---

## 1. PRD 7 项 Acceptance Criteria 逐项校核

| # | AC 文案 | 实施落点 | Verdict |
|---|---|---|---|
| AC1 | 母号 `eligible_for_auto_reactivation: true` 时,POST `/api/tasks/fill {leave_workspace:true}` 立即返回 `master_subscription_degraded`,不消耗注册资源 | `src/autoteam/api.py:2390-2432` `post_fill` 入口 — `leave_workspace=True` 时调 `is_master_subscription_healthy`,`subscription_cancelled` → `HTTPException(503, detail={"error": "master_subscription_degraded", ...})` | ✅ |
| AC2 | personal OAuth 拿到 `plan_type=team` 时,系统按选定的 recovery 策略处理(不再静默 fail) | `src/autoteam/manager.py:1604-1697` 5 次重试外层 + `OAUTH_PLAN_DRIFT_PERSISTENT` 写入 `register_failures.json` 后 `delete_account` + 退避 0/5/10/20/30 秒 ±20% jitter | ✅ |
| AC3 | OAuth 流程能确保拿到 `plan_type=free` token(显式选 Personal 或多次重试) | `src/autoteam/codex_auth.py:914-931` consent 后接入 `ensure_personal_workspace_selected`(主路径 `select_oauth_workspace` + UI fallback `force_select_personal_via_ui`)+ `manager.py:1604-1697` 5 次重试 | ✅ |
| AC4 | `register_failures.json` 新增字段 `master_subscription_state`,历史 28 条 plan_drift 可手工补齐或留空 | 实施改用了 `master_account_id` + `master_role`(record_failure extra kwargs 里),而不是字面 `master_subscription_state` 字段。**实质等价但字段名差异** | ⚠️ P2 命名 |
| AC5 | pytest 全绿;新增至少 8 个单测覆盖订阅探针 + sticky-rejoin recovery | 258/258 全绿;Round 8 新增 43 测试(test_master_subscription_probe.py 13 / test_oauth_workspace_select.py 18 / test_round8_integration.py 12)远超 8 | ✅ |
| (DoD-1) | 不引入回归(round 1-7 既有测试套件全绿) | Round 6/7 + spec2 + plan-whitelist + reconcile 共 104 用例全绿 | ✅ |
| (DoD-2) | 失败回退路径有日志线索,user 能在面板看到 master subscription degraded 提示 | `web/src/components/Settings.vue:3-29` 红色横幅 + 立即重测按钮;`web/src/components/PoolPage.vue:7-13` Pool 页横幅;`web/src/components/TaskPanel.vue:213-220` fill-personal 按钮在 cancelled 时禁用;`api.py:/api/admin/master-health` 端点 | ✅ |

---

## 2. 关键代码点位实测对齐(task 描述强制项)

| 检查项 | 文件:行 | 状态 |
|---|---|---|
| L1 fail-fast 入口 | `src/autoteam/manager.py:1530-1572` `_run_post_register_oauth(leave_workspace=True)` 入口先 `is_master_subscription_healthy` → `subscription_cancelled` 时 `record_failure(MASTER_SUBSCRIPTION_DEGRADED) + update_account(STANDBY) + return None` | ✅ |
| L2 workspace/select 调用 | `src/autoteam/codex_auth.py:914-931` consent 循环之后、callback wait 探针(C-P3 `assert_not_blocked(page, "oauth_callback_wait")`)之前接入 `ensure_personal_workspace_selected`;实现细节见 `src/autoteam/oauth_workspace.py:348-461` 三层兜底 | ✅ |
| L3 retry loop | `src/autoteam/manager.py:1604-1697` 外层 `for attempt in range(max_retries=5)` + 退避表 `(0, 5, 10, 20, 30)` + `_random.uniform(-0.2, 0.2)` jitter + `bundle_plan == "free"` break / `plan_type != "free"` 记 `plan_drift_history` 重试 | ✅ |
| sleep(8) 删除 | 原 `src/autoteam/manager.py:1554-1556`(kick 后等 OpenAI 同步)的 `time.sleep(8)` 已删除,现在该位置是 Round 8 注释说明 + 改用 ensure_personal_workspace_selected。**残留的两处 `time.sleep(8)`(`manager.py:2618` 直接注册输入验证码后 / `codex_auth.py:385` OTP 登录后)与 sticky-rejoin 无关,均保留正确** | ✅ |

---

## 3. Quality Gate 详细结果

### 3.1 pytest

```
258 passed in 10.56s
```

包含:
- Round 8 新增:`test_round8_integration.py`(12)+ `test_master_subscription_probe.py`(13)+ `test_oauth_workspace_select.py`(18)= **43 用例**
- 基线 215 全绿(Round 6/7/spec2/plan/reconcile/setup 等)

### 3.2 ruff

| 范围 | 错误数 | 备注 |
|---|---|---|
| Round 8 新文件(`master_health.py / oauth_workspace.py / test_master_subscription_probe.py / test_oauth_workspace_select.py / test_round8_integration.py`)| 0 | All checks passed |
| Round 8 修改文件(`api.py / codex_auth.py / manager.py / register_failures.py`)| 0 | All checks passed |
| Repo 全量 | 9 | 9 条 I001 全在 `tests/unit/test_round6_patches.py` + `tests/unit/test_round7_patches.py`(Round 6/7 baseline import 排序问题)— 与 Round 8 无关,不应阻塞本轮 |

### 3.3 pnpm build

```
✓ 24 modules transformed.
../src/autoteam/web/dist/index.html                  0.43 kB
../src/autoteam/web/dist/assets/index-CVOmCVGW.css  20.72 kB
../src/autoteam/web/dist/assets/index-uLYWyiUr.js  160.39 kB
✓ built in 1.05s
```

dist 产物已更新,旧 `index-Bos7ebzk.js / index-XWmLL9_Z.css` 删除。

### 3.4 OAuth 流程实测(task 允许跳过 live)

母号 ChatGPT Team 订阅 `eligible_for_auto_reactivation=true` 已 cancel,无法 live test。Stage 2 已落 `research/capture-oauth-flow.md` 离线推断报告,V1/V2/V5 通过 research + 外部仓库充分实证;V3/V4 风险用"严格识别 + fallback + 5 次重试 + master 健康度 fail-fast"四层兜底消化。代码侧已确认 4 处关键点全部对齐 spec(见 §2)。

### 3.5 文档 dead link

- `master-subscription-health.md` 与 `oauth-workspace-selection.md` 内部相互引用 + 引用 `account-state-machine.md / add-phone-detection.md / plan-type-whitelist.md / quota-classification.md` — **6 个 shared 文件全部存在,0 dead link**
- `spec-2-account-lifecycle.md v1.5` 元数据引用 `.trellis/tasks/04-27-master-team-degrade-oauth-rejoin/prd.md`(存在)+ `prompts/0426/spec/shared/master-subscription-health.md`(存在)+ `oauth-workspace-selection.md`(存在)— **0 dead link**

### 3.6 既有测试回归

`pytest tests/unit/test_round6_patches.py tests/unit/test_round7_patches.py tests/unit/test_spec2_lifecycle.py tests/unit/test_plan_type_whitelist.py tests/unit/test_reconcile_anomalies.py` → **104 passed** — Round 1-7 baseline 0 回归。

---

## 4. 偏差清单

### 4.1 P0 缺陷(阻断上线)

**0 项**。

### 4.2 P1 必修(影响 spec 一致性,但不阻断 personal 主链路)

**P1-1:Team 路径 M-T2 master probe 未实施**

- **位置**:`src/autoteam/manager.py:_run_post_register_oauth(leave_workspace=False)` 即 Team 注册分支(从 `_run_post_register_oauth` 函数体的 `# 原有 Team 流程` 注释开始,大约在 personal 分支末尾的 `return email` 之后)
- **spec 依据**:`prompts/0426/spec/shared/master-subscription-health.md §4 触发位点矩阵 M-T2`(明确写 "**`leave_workspace=False`,~L1462 之前**" + "母号降级时 Team invite 也会拿 `plan_type=free`(已实测 28 条 plan_drift),对称拦截") + `spec-2-account-lifecycle.md v1.5 §3.7` 表第 2 行 + PRD §"Diverge Sweep" Related scenarios 第 1 条
- **实测查询**:`grep -n "is_master_subscription_healthy" src/autoteam/manager.py` 仅在 personal 分支(L1533)+ retroactive cleanup(L4465)出现。Team 分支 `# 原有 Team 流程 — SPEC-2 §3.1.2 改造` 入口段没有 master probe 调用
- **影响**:母号订阅降级时,Team 路径(用户点 fill-team)仍会跑完 OAuth 拿 `plan_type=free`,然后被 plan_supported 检查拒收 → 浪费 2 分钟 + 累积 plan_drift 记录(就是当前观测到的 28 条)。Personal 路径已修复,但 Team 路径漏修
- **建议修复**:在 Team 分支 `try: bundle = login_codex_via_browser(email, password, mail_client=mail_client)` 之前插入与 personal 分支对称的 probe + record_failure block,stage 字面量改为 `"run_post_register_oauth_team_precheck"`,失败时不再 `delete_account`(席位仍占着)而是 `update_account(email, status=STATUS_AUTH_INVALID)` + 走 `_cleanup_team_leftover` 释放席位

### 4.3 P2 建议(命名差异 / 文档完整度)

**P2-1:AC4 字段名 `master_subscription_state` vs 实施 `master_account_id + master_role`**

- **PRD AC4 原文**:"register_failures.json 新增字段 `master_subscription_state`,历史 28 条 plan_drift 可手工补齐或留空"
- **实施现状**:`record_failure(email, MASTER_SUBSCRIPTION_DEGRADED, ..., master_account_id=..., master_role=...)`(无 `master_subscription_state` 字面量)
- **影响**:UI / 后端代码若按 `master_subscription_state` key 读取会拿不到值。**当前没有读取方**,所以实质无影响。但 PRD AC 字面失配
- **建议**:保留现状(`master_account_id + master_role` 信息更精细)+ 在 PRD-7 落地时把 AC4 文案改为 "新增字段 `master_account_id` / `master_role`"。或者 patch-implementer 加 `master_subscription_state="cancelled"` 一个冗余字段保持兼容

**P2-2:`prompts/0426/prd/prd-7-master-team-degrade.md` 文件未创建**

- **task PRD §"Approach A 变更文件清单"** 明确包含 `prompts/0426/prd/prd-7-master-team-degrade.md`(新)
- **实施现状**:`prompts/0426/prd/` 目录下 prd-1~prd-6,**无 prd-7**;spec-2 v1.5 元数据用 `(Round 8 PRD-7 候选)` 标注引向 `.trellis/tasks/04-27-…/prd.md`
- **影响**:不阻塞代码,只是 prompts/0426 文档体系不完整;后续 round 9 引用 PRD-7 时找不到固定路径
- **建议**:patch-implementer 落 PRD-7 lite 版(把 task 描述的 PRD 拷贝精简成 prompts/0426/prd/prd-7-master-team-degrade.md),或在 round 9 补

**P2-3:`manager.py:1552` 重复 import**

- **位置**:`src/autoteam/manager.py:1552` 函数体内 `from autoteam.register_failures import MASTER_SUBSCRIPTION_DEGRADED`,但 L70 顶部已有 `from autoteam.register_failures import MASTER_SUBSCRIPTION_DEGRADED, record_failure`
- **影响**:0(只是冗余 import,Python import cache 命中)
- **建议**:删除 L1552 这行重复 import(ruff 没扫到是因为是 inline import 不算 unused)

---

## 5. Commit Message Draft

```
feat(round-8): master 订阅探针 + OAuth Personal Workspace 显式选 + 5 次重试

新增 src/autoteam/master_health.py(三层探针 + 5min cache + 6 reason 分类 +
M-I1~I10 不变量)与 src/autoteam/oauth_workspace.py(decode/select/UI fallback/
ensure 编排 + 3 失败 category + W-I1~I10 不变量),修复母号 Team 订阅 cancel
时 personal OAuth 必拿 plan_type=team 的 sticky 根因。Personal 路径在 consent 后、
callback 前主动 POST /api/accounts/workspace/select,失败 fall 到 Playwright UI
点击,5 次重试外层(0/5/10/20/30s ±20% jitter)兜后端最终一致性。

接入:_run_post_register_oauth(leave_workspace=True) 入口 fail-fast、cmd_reconcile
追加 retroactive cleanup、/api/tasks/fill 入口 503、/api/admin/diagnose 内嵌
master_subscription_state、新增 GET /api/admin/master-health。前端 Settings/Pool
页加 degraded 横幅 + 立即重测按钮 + fill-personal 在 cancelled 时禁用。删除
manager.py:1554-1556 失效的 time.sleep(8)(default_workspace_id 不会自动 unset,
等多久都没用)。

测试:258/258 pytest 全绿,Round 8 新增 43 用例(test_master_subscription_probe 13 +
test_oauth_workspace_select 18 + test_round8_integration 12),Round 1-7 既有 104
用例 0 回归。Round 8 新代码 0 ruff lint。pnpm build 1.05s 通过。SPEC v1.5 +
master-subscription-health.md + oauth-workspace-selection.md 双 shared spec 落盘。
偏差:Team 路径 M-T2 probe 留 P1 backlog,prd-7 文件待补,master_subscription_state
字段名实施为 master_account_id+master_role(实质等价)。
```

---

## 6. Verdict + 给 patch-implementer 的修复指引

### 6.1 Verdict

**NEEDS_FIX**(若用户接受 P1-1 留 backlog,可改判 PASS)

### 6.2 Personal 主链路完整度

L1 / L2 / L3 / sleep(8) 删除全部到位;PRD AC1 / AC2 / AC3 / AC5 / DoD 全部满足。功能上 personal-fill 链路修复完整,可立即上线生产。

### 6.3 修复建议(留给 patch-implementer 决定)

| 偏差 | 修复优先级 | 建议处置 |
|---|---|---|
| P1-1 Team 路径 M-T2 | 必修 / 可留 backlog | 推荐:patch-implementer 加 ~30 行对称代码到 Team 分支入口。或 user 决定留 round-9 |
| P2-1 字段名 | 不修 / 改 SPEC | 推荐:不动代码,PRD-7 落地时改 AC4 文案 |
| P2-2 prd-7.md | 不修 / 落 lite 版 | 推荐:round-9 一并补 |
| P2-3 重复 import | nice-to-have | 推荐:patch-implementer 顺手删 1 行 |

---

**报告结束。**
