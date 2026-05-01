# Round 11 — Master Grace 真 healthy 修复 + 实时探活 + 注册 1team+1free + 模型分级实测

## Goal

修复 Round 8 + Round 9 留下的 **master_health 守恒 bug + 入口 fail-fast disconnect**,让母号在 grace 期内被识别为 **healthy** 状态(允许新 invite),并实测验证整套生产链路:

1. 母号 grace 期内 → master_health 返回 `healthy=True, reason="subscription_grace"`(**新状态**)
2. fail-fast 入口对 grace 状态放行(api.py:2491 / manager.py:1590,1819)
3. UI banner 由红色 critical 改为黄色 warning + grace 倒计时(MasterHealthBanner 已支持设计,只缺数据)
4. 加子号 + 母号"立即探活"实时刷新按钮(用户 Q3 痛点)
5. 注册 1 team + 1 free 子号 → 用 access_token 实测 cheap_codex_smoke 拿到真实对话内容(用户 Q4 硬性要求)

## What I already know(2026-04-28 实测)

### Round 8 + Round 9 留下的 disconnect bug(根因)

**SPEC §1 第 24 行原文**:
> `eligible_for_auto_reactivation` ≡ Stripe `cancel_at_period_end=true` **且 period 已过**

**用户 Q1 实证**:ChatGPT 网页端 team 权限**仍然有效** → 上面这个 spec 假设是错的:**`eligible_for_auto_reactivation=true` 在 grace 期内就是 true,不是"period 已过"才 true**。

**结果**:
- master_health.py `_classify_l1` 看到 `eligible_for_auto_reactivation is True` → 直接判 `subscription_cancelled` (M-I7 守恒)
- 入口 (api.py:2491 / manager.py:1590, 1819) 对 cancelled 一律 503 fail-fast
- UI banner 渲染红色 critical "母号订阅已 cancel · fill-personal 入口已 503 拒绝"

**Round 9 已落 GRACE 子号状态机**(子号在 grace 期内可继续用),**但 master_health 函数本身没区分 grace 期** → 子号被标 GRACE 但**母号入口仍 fail-fast 拒绝新 invite**。

### 已确认的代码现状

| 项 | 状态 | 文件位置 |
|---|---|---|
| runtime_config.json `register_domain: "zrainbow1257.com"` | ✓ 已配置(Q5 满足) | `runtime_config.json` |
| cf_temp_email.py 注册时读 runtime_config | ✓ | `mail/cf_temp_email.py:132` |
| Team 注册 plan=team → seat=chatgpt | ✓(Q5 满足) | `manager.py:1920` |
| Personal(free)注册 → seat=codex 强制 | ✓ 现行行为 | `manager.py:1766` |
| master_health Banner UI 已支持 warning yellow + grace 倒计时 | ✓ 设计完整,缺数据 | `MasterHealthBanner.vue:95-99` |
| `/backend-api/models` 在代码中 0 引用 | ✗ 没查过可用模型列表 | — |
| cheap_codex_smoke `model="gpt-5"` 写死 | ✗ 不能区分 free/team-only 模型 | `codex_auth.py:1723` |
| cheap_codex_smoke 只读 `response.created` 帧 | ✗ 不能拿真实对话内容验证 | `codex_auth.py:1784-1785` |
| 子号"立即探活"按钮 | ✗ UI 无此按钮 | — |
| 母号"立即重测"按钮 | ✓ 已有(截图) | `MasterHealthBanner.vue:65-73` |
| accounts/ 目录无 codex-main-*.json | ✗ Round 10 没跑过 | 需要手动触发 admin 重登 |

### 用户 5 个回答的关键解读

| Q | 答案 | 实施影响 |
|---|---|---|
| Q1 | ChatGPT 网页 team 权限仍有 | **核心修复** — master_health 加 `subscription_grace` healthy=True |
| Q2 | UI banner 截图 critical 红色 | banner 数据流 disconnect — Q1 修好后自动变 warning yellow |
| Q3 | 子号 + 母号状态不能及时探活 | 加子号"立即探活"按钮(母号已有) |
| Q4 | gpt-5.5=team-only / gpt-5.4=通用 + 必须拿真实对话内容 | cheap_codex_smoke 加 model 参数 + 读 SSE 完整对话内容 + 注册后查 `/backend-api/models` 找真实 slug |
| Q5 | cloudmail + chatgpt 席位 + zrainbow1257.com | 已全部就绪,无需改 |

## Decision (ADR-lite)

**Context**:Round 8 spec 把 `eligible_for_auto_reactivation=true` 等价于 "period 已过 + 必拿 free",这是错的。用户实证 grace 期内 ChatGPT 网页 team 权限仍可用,新 invite 应该仍能拿 plan_type=team。Round 9 加了 GRACE 子号状态机但漏改 master_health.healthy 判定。

**Decision**:**Approach A — 在 `_classify_l1` 中加 grace_until JWT 解析**,当 `eligible_for_auto_reactivation=true` 但 `grace_until > now`时返回新状态 `(healthy=True, reason="subscription_grace")`,fail-fast 入口对 healthy=True 自动放行,UI banner 自动渲染 warning。**最小改动 + 守恒不变**。

**Why not Approach B**(改 fail-fast 入口加白名单 grace):
- 入口 3 处都要改(api.py + 2 处 manager.py),容易漏
- master_health.healthy 含义被弱化(healthy=True 但允许 cancelled)
- A 维护性更好

**Consequences**:
- master_health.py 加 ~30 行(JWT 解析 + grace 判定)
- spec `master-subscription-health.md` v1.1 → v1.2 加 subscription_grace 状态
- account-state-machine.md v2.0 → v2.1 加注释:子号 GRACE + 母号 subscription_grace 双侧 healthy
- 不动 fail-fast 入口代码(代码减少 = bug 减少)
- UI banner 自动正确渲染(MasterHealthBanner.vue 设计已就绪)

## Approach A 实施大纲

### 🔴 P0-1: master_health.py 加 subscription_grace healthy 状态

**文件**: `src/autoteam/master_health.py`

```python
# _classify_l1 增强(line 142+)
def _classify_l1(items, account_id, *, id_token=None):
    target = ...
    if not target: return False, "workspace_missing", ...
    role = ...
    if role and role not in _OWNER_ROLES: return False, "role_not_owner", ...

    # 关键修改:eligible_for_auto_reactivation=True 时,先看 grace_until
    if target.get("eligible_for_auto_reactivation") is True:
        grace_until = extract_grace_until_from_jwt(id_token) if id_token else None
        if grace_until and grace_until > time.time():
            return True, "subscription_grace", {  # ← healthy=True!
                "current_user_role": role,
                "raw_item": target,
                "grace_until": grace_until,
                "grace_remain_seconds": grace_until - time.time(),
            }
        return False, "subscription_cancelled", {
            "current_user_role": role,
            "raw_item": target,
            "grace_until": grace_until,  # 可能 None 或已过期
        }

    return True, "active", ...
```

`is_master_subscription_healthy` 入口在调 `_classify_l1` 前,从 admin_state 或 codex-main-*.json 读 id_token,传入。

**新增 helper**: `_load_admin_id_token()` — 从 `accounts/codex-main-*.json` 读 id_token,fallback 从 state.json admin_state 读。

### 🔴 P0-2: fail-fast 入口语义自动正确

**文件**: `src/autoteam/api.py:2491`, `src/autoteam/manager.py:1590, 1819`

**改动**:0 行!现有 `if not healthy and reason == "subscription_cancelled":` 在 grace 期内 healthy=True → 自动跳过 fail-fast。

**单测保证**:加测试用例,grace_until > now → fill 不被 503。

### 🟡 P1-1: UI Banner 自动渲染 warning(0 改动)

P0-1 改后 master_health API 返回 `healthy=True, reason="subscription_grace", grace_until=...`,Banner 已设计支持(`MasterHealthBanner.vue:95-99`),自动渲染 warning yellow + 倒计时。

仅需改 `web/src/composables/useStatus.js` reason → severity 映射加 subscription_grace → warning。

### 🟡 P1-2: 子号"立即探活"按钮 + API endpoint

**新增 API**: `POST /api/accounts/{email}/probe` — force=True 调 cheap_codex_smoke + check_codex_quota,落 last_quota_check_at。

**前端**: `web/src/components/PoolPage.vue` 每行加按钮 → 调 API → 刷新该行状态。

### 🟡 P1-3: cheap_codex_smoke 接受 model 参数 + 完整对话内容

**改动**: `codex_auth.py:1662-1801`
- `cheap_codex_smoke(access_token, account_id, *, model="gpt-5", max_output_tokens=64)` 加 model 参数
- 不止读 `response.created` 帧,继续读到 `response.completed` 拿 `output_text` 内容
- 返回 `("alive", {"model": "gpt-5", "response_text": "...", "tokens": ...})` 或 `("auth_invalid", ...)` 等

**新增 API**: `GET /api/accounts/{email}/models` — 用 access_token 调 `/backend-api/models` 拿可用模型列表(让用户/系统选 team-only vs 通用)

### 🟢 P2-1: 注册实测 + 模型分级验证

**实测前提**:
1. 用户先在 UI 重新触发 admin 登录 / `POST /api/admin/login/session` → 生成 `accounts/codex-main-*.json`(包含 id_token + access_token + refresh_token)
2. force_refresh master_health → 验证返回 `subscription_grace` + grace_until

**实测步骤**:
- POST `/api/tasks/fill {target:1, leave_workspace:false}` → 1 team 号(seat=chatgpt, plan=team)
- POST `/api/tasks/fill {target:1, leave_workspace:true}` → 1 free 号(seat=codex, plan=free)
- 用 team 号 access_token 调 `/backend-api/models` → 拿可用模型列表 → 找出 team-only 模型 slug(用户口语 gpt-5.5)
- 用 free 号 access_token 调 `/backend-api/models` → 找通用模型 slug(用户口语 gpt-5.4)
- cheap_codex_smoke(team token, model=team-only-slug) → 拿真实对话
- cheap_codex_smoke(free token, model=通用-slug) → 拿真实对话
- 截图 + JSON 摘录写到 review-report

### 🟢 P2-2: spec 升级

- `prompts/0426/spec/shared/master-subscription-health.md` v1.1 → v1.2 — 加 §14 subscription_grace 状态 + 决策矩阵
- `prompts/0426/spec/shared/account-state-machine.md` v2.0 → v2.1 — 加 §x:GRACE 子号 + 母号 subscription_grace 双侧 healthy 联动
- `prompts/0426/spec/shared/realtime-probe.md` v1.0 — 新增,子号 + 母号实时探活(force_refresh)
- `prompts/0426/spec/spec-2-account-lifecycle.md` v1.6 → v1.7

## Acceptance Criteria

- [ ] AC1. master_health probe 在 admin grace 期内返回 `(True, "subscription_grace", evidence)`,evidence 含 `grace_until` + `grace_remain_seconds`
- [ ] AC2. POST `/api/tasks/fill {leave_workspace:false}` 在 grace 期内**不被 503 拒绝**,后台任务正常启动
- [ ] AC3. POST `/api/tasks/fill {leave_workspace:true}` 在 grace 期内**不被 503 拒绝**,后台任务正常启动
- [ ] AC4. UI banner 在 grace 期内显示**黄色 warning + 倒计时 + 立即重测按钮**(不是红色 critical)
- [ ] AC5. 子号每行有"立即探活"按钮,点击后 N 秒内 status / last_quota_check_at 刷新
- [ ] AC6. cheap_codex_smoke(access_token, model="gpt-5") 返回 `("alive", {"response_text": "...", ...})` 含真实对话内容
- [ ] AC7. **实测自验**(用户 Q4 硬性要求):
  - 注册 1 team 号(seat=chatgpt, plan=team)成功落 accounts.json + auth_file
  - 注册 1 free 号(seat=codex, plan=free)成功落 accounts.json + auth_file
  - team 号 access_token 调 `/backend-api/models` 拿到模型列表(含 team-only 模型 slug)
  - free 号 access_token 调 `/backend-api/models` 拿到模型列表(基础模型可用)
  - team 号 cheap_codex_smoke(model=team-only-slug)拿到真实对话内容
  - free 号 cheap_codex_smoke(model=通用-slug)拿到真实对话内容
  - 全部截图/JSON 摘录写到 `prompts/0426/verify/round11-review-report.md`
- [ ] AC8. pytest 全绿(基线 272 + Round 11 新增 ≥6 = 278) + ruff 0
- [ ] AC9. 不破坏 Round 1-10 既有路径(master OAuth via session_token / GRACE 子号状态机 / 5 触发点 retroactive)

## Definition of Done

- master_health.py + 单测(grace 期内 / grace 已过期 / id_token 缺失三种路径)
- realtime-probe API endpoint + 单测
- cheap_codex_smoke 增强 + 单测(mock SSE)
- 4 个 spec 文档升级
- review-report PASS 含 AC7 全部 6 项实测证据(JSON + 截图)
- 不动 .env(用户已用 runtime_config.json 覆盖)

## Out of Scope

- 自动续订 / 购买 Team 订阅
- 多母号管理(round 9+ backlog)
- 为不同账号配置不同 model slug 偏好(后续 backlog)
- chatgpt 席位 vs codex 席位的强制切换(plan_type 自动决定)

## Technical Notes

- `src/autoteam/master_health.py:142-173` `_classify_l1` — 主修改点
- `src/autoteam/master_health.py:397-433` `extract_grace_until_from_jwt` — Round 9 已有,直接复用
- `src/autoteam/master_health.py:436-456` `_read_access_token_from_auth_file` — Round 9 已有,直接复用
- `src/autoteam/codex_auth.py:1662-1801` `cheap_codex_smoke` + `_cheap_codex_smoke_network` — 加 model 参数 + 读完整 SSE
- `src/autoteam/api.py:2446` POST `/api/tasks/fill` — fail-fast 入口(代码不动,subscription_grace 自动放行)
- `web/src/components/MasterHealthBanner.vue:95-99` reason → severity 映射 — 加 subscription_grace
- `web/src/components/PoolPage.vue` — 加每行"立即探活"按钮
- `accounts/codex-main-{account_id}.json` — admin id_token / access_token / refresh_token 来源(Round 10 产物)

## 实施前置条件(BLOCKING)

⚠️ **AC7 实测自验需要先有 admin id_token**:

当前 `accounts/` 目录**没有 codex-main-*.json**,master_health 没法拿到 grace_until JWT 字段 → P0-1 实施需要先生成。

**用户需在实施开始前**:
1. 启动 server `python -m autoteam.api` 或 docker
2. UI 中触发 admin 重登录(/api/admin/login/session 或 UI 的 "重新认证主号"按钮)
3. 验证 `accounts/codex-main-{bac969ea}.json` 存在 + 含 id_token

完成后我才能跑 force_refresh + 注册 + smoke。

## 用户原话(2026-04-28)

> 1. 母号订阅仍有，但是报错
> 2. 状态无法实时更新
>
> 在完善母号订阅的情况下，注册一个team号和一个free号看看是否成功
> 成功的标准是能正常唤醒并使用gpt-5.5和gpt-5.4的模型

## 用户 5 答原文

- Q1: 目前的帐号在 chatgpt 网页版还有 team 的权限
- Q2: 具体在 Web 端 [Image #3] 详情见日志
- Q3: 无法实时更新指帐号以及母号的状态不能及时探活,无法及时从失效-有效状态反映
- Q4: gpt-5.5 和 gpt-5.4 是指能使用的模型。gpt-5.5 只有 team 号可以使用,gpt-5.4 free 号和 team 号也可以使用。能够正常通过 api 拿到和模型的对话结果才能作为注册成功
- Q5: 用 cloudmail 生成,席位默认必须是 chatgpt 的,用 zrainbow1257.com 这个域名注册
