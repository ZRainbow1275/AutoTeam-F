# Issue#2 — 席位策略与套餐 / 免费号删除 研究报告

## A. 用户报告解构

用户提出 4 条诉求:

1. **默认走 codex 席位**:目前生成免费号 / Team 邀请号默认拿"完整 ChatGPT 席位",用户希望默认只占 codex 席位(让 ChatGPT 主席位留给真人/管理员)。
2. **免费号无法移除**:被标记为 personal(已退出 Team 走个人 free OAuth)的子号,从 UI 上点删除/批量删除拿不到结果。
3. **`self_serve_business_usage_based` 套餐识别**:OpenAI 在某些 workspace 下把 `chatgpt_plan_type` 标成这种新值,目前代码只匹配 `team` / `free`,其它一律走 fallback 分支,实际效果是"号能注册成功但 Codex 不可用"。
4. **席位策略可配置**:相比写死 default 邀请,用户希望管理员可以选择策略。

## B. 代码现状

### B.1 关键符号清单

| 文件:行号 | 签名/常量 | 作用 |
|---|---|---|
| `src/autoteam/accounts.py:23-25` | `SEAT_CHATGPT="chatgpt"` / `SEAT_CODEX="codex"` / `SEAT_UNKNOWN="unknown"` | 落盘到 `accounts.json` 的 `seat_type` 字段 |
| `src/autoteam/accounts.py:58` | `add_account(email, password, cloudmail_account_id=None, seat_type=SEAT_UNKNOWN, workspace_account_id=None)` | 默认 SEAT_UNKNOWN,邀请/注册流程会显式覆盖 |
| `src/autoteam/invite.py:39-44` | `_seat_label_from_raw(raw_seat)` | 把 ChatGPT API 返回的 `_seat_type` 字面量(`chatgpt` / `usage_based` / `unknown`)翻译成 `SEAT_*` 常量 |
| `src/autoteam/invite.py:496` | `chatgpt.invite_member(email, seat_type="default")` | **默认邀请入口写死 default(完整 ChatGPT 席位)** |
| `src/autoteam/chatgpt_api.py:1365-1411` | `invite_member(email, seat_type="usage_based")` + `_invite_member_with_fallback` | 自带 default → usage_based 兜底,只翻一次 |
| `src/autoteam/chatgpt_api.py:1414-1511` | `_invite_member_once(email, seat_type)` | POST /invites + POST 200 后 PATCH 升级到 default |
| `src/autoteam/chatgpt_api.py:1487-1510` | `data["_seat_type"] = "usage_based" / "chatgpt"` 写入 | usage_based 入口 + PATCH 全失败 → 标 SEAT_CODEX;default 入口 + 200 → 直接标 SEAT_CHATGPT |
| `src/autoteam/manual_account.py:233-237` | `seat_label = "chatgpt" if plan_type == "team" else "codex"` | 手动添加(用户粘贴 OAuth 回调)按 plan_type 反推 seat_type |
| `src/autoteam/manager.py:1463-1486` | `_run_post_register_oauth` 收尾段 | `seat_label = "chatgpt" if bundle_plan == "team" else "codex"` |
| `src/autoteam/codex_auth.py:111` | `bundle["plan_type"] = auth_claims.get("chatgpt_plan_type", "unknown")` | id_token JWT 直接提取,任何新值都会原样落盘 |
| `src/autoteam/cpa_sync.py:132-140` | plan_type 推断回退链 | 当 bundle 缺失时根据文件名 `-team-` / `-plus-` / `-free-` 反推 |
| `src/autoteam/api.py:1268-1303` | `DELETE /api/accounts/{email}` | 单点删除 |
| `src/autoteam/api.py:1306-1404` | `POST /api/accounts/delete-batch` | 批量删除 |
| `src/autoteam/account_ops.py:40-162` | `delete_managed_account(email, ...)` | 实际执行器:Team 远端删 + 本地 auth_file + cpa + cloudmail |
| `web/src/components/Dashboard.vue:133-142` | 删除按钮 `v-if="!acc.is_main_account"` | 只屏蔽主号,personal/standby/auth_invalid 都渲染 |

### B.2 调用链/数据流

#### 邀请席位决策链(用户 → `accounts.json`)

```
invite.run() / cmd_fill_team
    └─ ChatGPTTeamAPI.invite_member(email, seat_type="default")    ← 入口写死
        └─ _invite_member_with_fallback(seat_type="default", allow_fallback=True)
            ├─ _invite_member_once("default") → POST /invites
            │   └─ 200 + 无 errored → data["_seat_type"]="chatgpt"      ← 完整 ChatGPT 席位
            │   └─ 200 + errored / 非 200 → 触发兜底
            └─ _invite_member_with_fallback(seat_type="usage_based", allow_fallback=False)
                └─ _invite_member_once("usage_based")
                    ├─ POST /invites 拿 invite_id 列表
                    └─ 对每个 invite_id PATCH seat_type="default"
                        ├─ 全失败 → data["_seat_type"]="usage_based"    ← SEAT_CODEX
                        └─ 任意成功 → data["_seat_type"]="chatgpt"
    ↓
invite.run() 用 _seat_label_from_raw() 转成 SEAT_* 常量
    └─ add_account(..., seat_type=seat_label, ...)   ← 落盘
```

**关键观察**:目前的 fallback 是**单向的"先升级,失败保留 codex"**,根本不存在"从一开始就只要 codex 席位、不尝试 PATCH"的入口。`chatgpt_api.py:1487-1499` 在 seat_type=="usage_based" 的分支里**永远**会调 `_update_invite_seat_type(invite_id, "default")` 试图升级。

#### 注册后回写席位的链(plan_type → seat_type)

```
_run_post_register_oauth (manager.py:1463)
    └─ login_codex_via_browser → 拿 bundle (含 plan_type)
        └─ bundle.plan_type == "team"   → seat_label="chatgpt"
        └─ bundle.plan_type ∈ {free,plus,unknown,self_serve_*,...} → seat_label="codex"
            ↑ 这里决定的 seat_type 与 invite 阶段决定的 seat_type 可能不一致,
              update_account 会覆写 invite 阶段的值。
```

#### 删除 personal 子号链

```
DELETE /api/accounts/{email}                                   (api.py:1268)
    └─ acquire _playwright_lock (BLOCKING_FALSE)
        └─ _pw_executor.run(delete_managed_account, email)
            └─ delete_managed_account(email, remove_remote=True) (account_ops.py:40)
                └─ if remote_state is None:
                    └─ ChatGPTTeamAPI().start()                ← 主号 session 失效 → 抛
                    └─ fetch_team_state() 获取 members + invites
                ├─ member_matches = []  (personal 不在 Team)
                ├─ invite_matches = []
                ├─ 删本地 auth_file (codex-{email}-*.json)
                ├─ 删 CPA 文件
                ├─ 删本地 accounts.json 记录
                └─ 删 CloudMail 邮箱
```

## C. 根因分析

### C.1 "默认走 codex 席位"无法直接配置

**优先级 P0** — 缺乏配置开关。`invite.py:496` 的 `seat_type="default"` 是一行硬编码,且 `_invite_member_once` 在 usage_based 路径里**永远**会试图 PATCH 升级到 default(line 1498),即便管理员手动改 invite.py 入口为 `usage_based`,实际效果也是"先 usage_based 占席,再 PATCH 抢 ChatGPT 席位",而不是"只占 codex 席位"。

候选根因(按可能性排序):
1. **设计选择问题**:历史上 default 升级是为了让 ChatGPT 主席位用尽时仍能拿到 codex,作者把 fallback 写死成单向。
2. **缺少 runtime_config 字段**:对照 `runtime_config.py` / `config.py` 是否有 `PREFERRED_SEAT_TYPE` 的 hint,目前没有。
3. **PATCH 升级是无条件的**:`_invite_member_once` line 1489-1506 完全没有 opt-out。

### C.2 "免费号无法移除"

**优先级 P1** — 多个候选互相叠加:

候选根因(按可能性排序):
1. **`actionDisabled` 锁死按钮(P1 主犯)**:`Dashboard.vue:330` 的 `actionDisabled = !!props.runningTask || !adminReady.value`。如果用户**没登录主号**或**有任何任务在跑**,删除按钮会变灰失能。用户测试过程中很可能正在跑 fill 任务 / 主号 session 已过期但 UI 没及时刷新 `adminReady`。
2. **批量删除强依赖主号 session**:`api.py:1349-1354` 即便要删的全是 personal 子号(都不在 Team),也强制 `ChatGPTTeamAPI().start()` + `fetch_team_state()`。主号 session 失效时整批 500,没有"全为 personal 时跳过 Team 拉取"的优化。
3. **后端 _playwright_lock 409**:`api.py:1271-1283` 一旦 `_playwright_lock.acquire(blocking=False)` 失败就 409,前端表现为"点击没反应",但有 message 弹窗。
4. **CPA 文件名错位导致清理不彻底**:`account_ops.py:120` 用 `AUTH_DIR.glob(f"codex-{email}-*.json")`,但 personal 号是 `codex-{email}-free-*.json`,glob 能命中。然后 `cpa_sync.list_cpa_files / delete_from_cpa` 是按 name + email 双 key 删除,personal 号文件名是 free-,理论上能匹配。这一支不是主因,但需要在测试时确认。
5. **不是真删失败,而是 UI 不刷新**:删除完成后没有触发账号列表 reload(检查 Dashboard.vue 的 `removeAccount` 方法,line 568-584 region)。

### C.3 `self_serve_business_usage_based` 套餐处理

**优先级 P0** — 完全无识别。

候选根因:
1. **plan_type 检查只匹配 "team"**:`manual_account.py:234`、`manager.py:1468`、`manager.py:2490` 都是 `if plan_type == "team": ... else: codex / standby / 拒绝复用`。`self_serve_business_usage_based` 全部落入 else 分支,被标 SEAT_CODEX,但**实际上**这种 plan 的 workspace 根本没有 codex 配额。
2. **wham/usage 不能区分 quota=0 与 quota=正常**:`codex_auth.py:1582` 用 `primary_pct >= 100` 判定耗尽,`self_serve_business_usage_based` 这种新计费 workspace 实际返回的可能是 `used_percent=0` + `total=0` —— 后者无法表达,代码当作 ok + 显示 100% 剩余。这就是用户报告"不显示额度的那种,实际也没额度,调用什么都是 429 没额度"的根因。
3. **`reinvite_account` 直接判 plan_type != "team" 失败回收(manager.py:2490)**:任何 self_serve_* plan 一进来就被 `_cleanup_team_leftover` 踢回 standby — 死循环。

## D. 修复方向建议(只是方向,不写代码)

### D.1 必须改的点

1. **加 PREFERRED_SEAT_TYPE 配置开关**(P0)
   - 位置:`runtime_config.py` + Web Settings 页
   - 取值:`chatgpt`(默认升级,保留现行) / `codex`(默认 usage_based 不升级)
   - 改动点:
     - `invite.py:496` 改成读 config 的 `PREFERRED_SEAT_TYPE`,映射到 `seat_type=` 入参
     - `chatgpt_api.py:1487-1506` 在 usage_based 分支里,只有 `PREFERRED_SEAT_TYPE=="chatgpt"` 时才执行 PATCH 升级,反之直接保留 usage_based
     - `_invite_member_with_fallback` 的兜底也要读 config:codex 偏好下,default → usage_based 兜底改成"default 直接失败不再尝试 usage_based"或反向

2. **识别异常 plan_type**(P0)
   - 位置:`codex_auth.py` 在 `_exchange_auth_code` 拿 bundle 后,新增一个常量集 `SUPPORTED_PLAN_TYPES = {"team", "free", "plus", "pro"}`
   - 任何不在白名单的 plan(如 `self_serve_business_usage_based`)直接日志告警 + bundle["plan_type_unsupported"]=True
   - 下游 `_run_post_register_oauth` / `manual_account._finalize_account` 看到 unsupported 标记时,选项:a) 拒绝接收 bundle(类似 codex_auth.py:920-930 的 personal 模式校验),b) 接收但标 STATUS_AUTH_INVALID + 在 UI 显示"不支持的套餐"

3. **wham/usage 增强:识别 quota 总额=0**(P0)
   - 位置:`codex_auth.py:1685-1700`
   - 解析 `rate_limit.primary_window` 时,除了 `used_percent` 也读 `limit` / `total`(如果接口返回了)
   - 总额=0 或 reset_at=0 → 直接归为 "exhausted" 或新增 "no_quota"
   - 上游标 STATUS_EXHAUSTED + 写一个明确的 quota_info `{"reason": "no_quota_assigned"}`,UI 显示"无配额"而不是"100% 剩余"

4. **personal 删除链解耦主号 session**(P1)
   - 位置:`account_ops.py:73-84`
   - 检查 acc.status:如果是 STATUS_PERSONAL / STATUS_AUTH_INVALID(确认不在 Team),跳过 `fetch_team_state()`,直接删本地资源
   - 单点和批量入口都加这个 short-circuit
   - `api.py:1306-1404` 批量删除前先扫一遍状态,如果全是 personal,完全不起 ChatGPTTeamAPI

5. **UI 删除失败需要明确 toast**(P1)
   - 位置:`Dashboard.vue:566-585`(removeAccount)
   - 失败原因(actionDisabled / 409 / 500)需要 message.value 不同提示,避免"按钮没反应"误解
   - 删除成功后立即 `emit('refresh')` 触发账号列表 reload

### D.2 可选优化

1. **invite_member 加 dry_run 预览**:管理员可以在 web 上看 default vs usage_based 的预期结果。
2. **seat_type 漂移检测**:`_reconcile_team_members` 第二轮里,检查每个成员的实际 seat_type 是否与本地 `accounts.json` 一致(GET /users 返回的成员对象有 seat_type 字段),漂移就告警。
3. **plan_type=self_serve_business 转 personal 模式**:既然这种 plan workspace 没 codex 配额,直接用 `_run_post_register_oauth(leave_workspace=True)` 走 personal 流程能不能拿到 free plan?需要实际测试。
4. **删除批量:全 personal 时不需要 fetch_team_state**:跳过整个 ChatGPTTeamAPI 启动,加速删除。

### D.3 测试要点

- [ ] PREFERRED_SEAT_TYPE=codex 时,invite 流程返回的 seat_type 应该稳定=usage_based,且 PATCH 不被调用(看日志中 `修改邀请 seat_type` 是否出现)
- [ ] PREFERRED_SEAT_TYPE=chatgpt 保持现有行为不变(回归测试)
- [ ] 注册一个 self_serve_business_usage_based workspace 子号(可以临时用一个商用 workspace),确认 codex_auth.py 拒绝接收 bundle / 标 unsupported
- [ ] 创建一个 personal 子号(`fill-personal`),用 web UI 删除按钮,确认按钮可点 + 删除成功 + 列表刷新
- [ ] 批量删除 5 个 personal 子号,主号 session 故意先注销,确认不报 ChatGPTTeamAPI 启动失败
- [ ] wham/usage 返回 used_percent=0 但实际无配额的场景:本地是否能正确显示"无配额"

## E. 影响面 / 爆炸半径

- **PREFERRED_SEAT_TYPE 配置改动**:扩散到 `invite.py`、`chatgpt_api.py`、`runtime_config.py`、`web/Settings.vue`,4 个文件;最大风险是 fallback 链向后兼容(老的 default 入口 + PATCH 行为是否仍可用作"失败兜底")。需要对 `_invite_member_with_fallback` 做 unit test。
- **plan_type 白名单**:扩散到 `codex_auth.py` + `manual_account.py` + `manager.py`(_run_post_register_oauth、reinvite_account)+ `api.py`(login),5 个文件。注意 `cpa_sync.py:132-140` 的回退链不能漏改。
- **wham/usage 解析增强**:只动 `codex_auth.py:check_codex_quota / get_quota_exhausted_info`,影响所有调用方(manager / api 多处)的 status_str 分类,需要确认 "no_quota" 不会被误归 "auth_error"。
- **personal 删除短路**:只动 `account_ops.py` + `api.py:delete_accounts_batch`,不会影响 active 子号删除链。
- **UI toast 改动**:仅 Dashboard.vue,无后端影响。

## F. 风险与未决问题

1. **`self_serve_business_usage_based` 是否真的存在**:用户报告中提及,但 OpenAI 公开 API 文档没有这个值。需要从用户实际 bundle 取证(`auths/codex-*.json` 中 plan_type 字段)确认字面量,可能是 `chatgpt_business_usage_based` 或 `self_serve_team` 之类变体。
2. **PATCH 升级失败的统计**:目前 chatgpt_api.py:1503 只 log error,没有 metric。改默认策略前应该先看真实失败率 — 如果失败率 <5%,默认 ChatGPT 升级是合理的;如果 >30%,默认 codex 才合理。
3. **删除按钮 actionDisabled 是否有副作用**:adminReady=false 时禁删除是安全设计(避免 fetch_team_state 起 chatgpt_api 失败),不能简单去掉。修复方向应是"personal 子号不依赖 adminReady"。
4. **plan_type 白名单是否需要兼容大写**:`codex_auth.py:111` 直接读 JWT claim,OpenAI 后端可能某些版本返回大写 `Team`。所有判定建议 `.lower()` 后再比。
5. **如果用户希望"全部用 codex 席位"**,主号 ChatGPT 席位会闲置吗?需要确认主号的 codex 配额来自哪里(似乎来自主号本身的 ChatGPT 主席位,不需要 PATCH)。
