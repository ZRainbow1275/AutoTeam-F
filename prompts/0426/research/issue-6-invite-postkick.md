# Issue#6 — 邀请→进 workspace→quota=0→踢出 401→重 OAuth add phone 研究报告

## A. 用户报告解构

用户原话(摘录):
> "手动试了下在 team 里邀请,然后邮件点击链接完成注册,收个邮箱验证码倒是能进 team 了。CPA OAuth 登录再收个验证码登录子号没让选个人还是工作空间,直接就是工作空间了。(还是不显示额度的那种,实际也没额度,调用什么都是 429 没额度)team 踢掉之后,子号就 401 了,重新 OAuth 还是需要手机"

拆为 4 子症状:

| 编号 | 症状 | 严重程度 |
|---|---|---|
| **A** | CPA OAuth 没让选 personal/workspace,直接进 workspace | 中(可能是设计意图) |
| **B** | workspace 模式下 quota=0,调用 429 | 高(账号入池但完全不可用) |
| **C** | team 踢出后子号 401 | 高(状态机没正确识别"被人踢") |
| **D** | 重新 OAuth 时强制 add phone | 高(踢出+重 OAuth 风控) |

## B. 代码现状

### B.1 关键符号清单

| 文件:行号 | 签名 | 作用 |
|---|---|---|
| `src/autoteam/codex_auth.py:250-932` | `login_codex_via_browser(email, password, mail_client=None, *, use_personal=False)` | OAuth 主流程,包含 step-0 注入 `_account` cookie + step-4 workspace 选择循环 |
| `src/autoteam/codex_auth.py:262-263` | `chatgpt_account_id = "" if use_personal else get_chatgpt_account_id()` | use_personal 决定要不要注入 Team workspace cookie |
| `src/autoteam/codex_auth.py:295-318` | step-0:Team 模式注入 `_account` cookie 引导 OAuth 进 Team workspace | personal 模式跳过 |
| `src/autoteam/codex_auth.py:618-733` | step-4 循环:检测"选择一个工作空间"页面,自动点击 Team / Personal | use_personal 时点 Personal,否则点 Team workspace_name |
| `src/autoteam/codex_auth.py:916-930` | personal 模式 plan_type 强校验 | plan_type != "free" 时拒收 bundle(防止 Team 被默认选中颁发 Team token) |
| `src/autoteam/codex_auth.py:1618-1700` | `check_codex_quota(access_token, account_id=None)` | 调 wham/usage,返回 ok / exhausted / auth_error / network_error |
| `src/autoteam/codex_auth.py:1572-1615` | `get_quota_exhausted_info(quota_info, *, limit_reached=False)` | 判定耗尽:`primary_pct >= 100 or weekly_pct >= 100 or limit_reached` |
| `src/autoteam/codex_auth.py:1665-1671` | `if resp.status_code in (401, 403): return "auth_error"` / `429` 归 network_error | 401 token 失效与 429 限流分流 |
| `src/autoteam/manager.py:1386-1486` | `_run_post_register_oauth(email, password, mail_client, leave_workspace=False, ...)` | 注册收尾,走 Team 或 personal 分支,但**不调 check_codex_quota** |
| `src/autoteam/manager.py:1463-1486` | `bundle = login_codex_via_browser(...)` 后直接 `update_account(status=STATUS_ACTIVE)` | **没有 quota probe** |
| `src/autoteam/manager.py:476-600` | `sync_account_states(chatgpt_api=None)` | Team 成员对照本地状态;`active 但不在 Team` 直接刷 standby |
| `src/autoteam/manager.py:526-541` | `elif not in_team and acc["status"] == STATUS_ACTIVE: ... acc["status"] = STATUS_STANDBY` | **不区分"被踢出"和"正常待机"** |
| `src/autoteam/manager.py:161-471` | `_reconcile_team_members` | 检测 ghost / orphan / 错位,KICK 后会标 STATUS_AUTH_INVALID(line 312、339) |
| `src/autoteam/manager.py:2446-2630` | `reinvite_account(chatgpt_api, mail_client, acc)` | standby 复用主流程:重 OAuth → 验 plan_type=team → check_codex_quota → 假恢复检测 |
| `src/autoteam/manager.py:2466` | `bundle = login_codex_via_browser(email, password, mail_client=mail_client)` | 默认 use_personal=False |
| `src/autoteam/manager.py:2489-2494` | `if plan_type != "team": _cleanup_team_leftover; return False` | self_serve_business_* 等 plan 直接被拒 |
| `src/autoteam/invite.py:106-145` | `detect_phone_verification(page)` + `assert_not_blocked(page, step)` | **只在 invite.register_with_invite 流程里调用,login_codex_via_browser 完全没引用** |
| `src/autoteam/api.py:1450-1525` | `POST /api/accounts/{email}/login`(补登录) | 调 login_codex_via_browser,无 add-phone 探针保护 |

### B.2 调用链/数据流

#### 子症状 A:OAuth workspace 选择链

```
用户在 Web 点"补登录" / "fill-team"
    └─ login_codex_via_browser(email, pwd, use_personal=False)
        ├─ step-0: 注入 _account cookie = chatgpt_account_id      ← 强制 Team workspace
        ├─ 浏览器访问 auth_url (auth.openai.com/authorize)
        ├─ 邮箱 + OTP 验证
        └─ step-4 loop (10 轮):
            ├─ 检测"选择一个工作空间"页 → JS click workspace_name
            ├─ 点继续按钮
            └─ 拿 auth_code → exchange → bundle (含 plan_type=team)
```

**整个流程没有"让用户选"的交互点**:cookie 注入决定 default workspace,JS 自动点击决定具体选项。这是设计意图(自动化场景),但与 user 期望"先问我"冲突。

#### 子症状 B:quota=0 进 active 链

```
_run_post_register_oauth(...)                            (manager.py:1463)
    ├─ login_codex_via_browser → bundle.plan_type
    ├─ save_auth_file(bundle) → auth_file
    └─ update_account(status=STATUS_ACTIVE, seat_type, auth_file, ...)
       ↑ 此处缺一道 check_codex_quota:
         如果 wham/usage 返回 quota=0 / limit_reached / 401,
         应该立刻分流到 STATUS_EXHAUSTED 或 STATUS_AUTH_INVALID。
```

对比 `manual_account._finalize_account`(`manual_account.py:227-286`)— 手动添加路径**有** check_codex_quota(line 263-272),会按结果标 STATUS_EXHAUSTED。但**邀请→注册路径**走的是 `_run_post_register_oauth`,这条**没有 quota probe**。这是直接 bug。

更致命的是 `get_quota_exhausted_info`(codex_auth.py:1582)只看 `primary_pct >= 100`。如果 wham/usage 返回 `used_percent=0` 但**总额本身是 0**(self_serve_business_usage_based workspace 没分配 codex 配额的情况),代码当作 ok 状态 + 显示"100% 剩余",但下游 CPA 调用 codex API 立刻 429 — 这就是用户报告的"显示额度但实际没额度"。

#### 子症状 C:team 踢出后 401 链

```
管理员在 ChatGPT 后台手动踢子号(不走 AutoTeam)
    ↓
sync_account_states (manager.py:476)
    ├─ GET /backend-api/accounts/{id}/users → 不见该 email
    └─ acc["status"] = STATUS_STANDBY                  ← 没有标 AUTH_INVALID
    ↓
下次 rotation 选中该 standby
    └─ reinvite_account (manager.py:2446)
        ├─ login_codex_via_browser → bundle (此时会拿到 free / personal plan)
        │  ↑ 因为 cookie 注入失败/workspace 默认变了,
        │    bundle.plan_type 不是 "team"
        ├─ if plan_type != "team": _cleanup_team_leftover  ← line 2490
        └─ return False(死循环)
```

子号在 ChatGPT 后端被 user_id revoke,但 OAuth refresh_token 走的是 chatgpt_account_id 维度 — 一旦该 user_id 在 workspace 不存在,任何 backend-api 调用都 401。**关键缺陷**:`sync_account_states:540` 把"被踢"当成"待机"处理。

#### 子症状 D:重 OAuth add phone 链

```
踢出立即(< 5 分钟)重新跑 reinvite_account
    └─ login_codex_via_browser
        └─ Playwright 浏览器:
            ├─ 邮箱 + OTP
            ├─ ChatGPT 后端检测"刚刚被踢且立即重登" → 触发风控
            └─ 跳转 add-phone 页面
                ↑ 这里 codex_auth.py 完全没 detect_phone_verification!
                  step-4 循环见不到 add-phone,只看到 OTP/workspace/consent 选项,
                  全部 timeout 后 auth_code 为 None,bundle 失败。
```

**对比** `invite.register_with_invite`(invite.py:190-477):每个步骤之间都调 `assert_not_blocked(page, step)`,命中 add-phone 立即 raise RegisterBlocked + 上层放弃账号。这套 detect 在 `login_codex_via_browser` 完全没接入。

## C. 根因分析

### C.1 子症状 B:quota=0 进 active 池

**优先级 P0**(用户痛点最直接 — 进池号不可用)

候选根因:
1. `_run_post_register_oauth` 缺 quota probe(主因) — manual_account 有,这里没,接口不对称。
2. `get_quota_exhausted_info` 不能识别"总额=0"(次因) — 即便加 probe,也只判 used_percent=100,无配额 workspace 仍会被认 ok。
3. `chatgpt_plan_type` 白名单缺失 — `self_serve_business_usage_based` 进来被打 codex 席位,但 codex 在该 workspace 根本不可用。

### C.2 子症状 C:踢出后状态错位

**优先级 P0**

候选根因:
1. `sync_account_states:540` 不区分"被踢"vs"自然待机"(主因) — 应该用 auth_file 实测一次 wham/usage,401 → STATUS_AUTH_INVALID,而不是无脑 standby。
2. `reinvite_account:2490` 对 plan_type != team 的处理简单粗暴(次因) — 此时 token 已经 revoke,正确处置是清掉 auth_file + 标 AUTH_INVALID,而不是回 standby 等下一轮(下一轮还是 401 死循环)。
3. 缺少 standby 准入探测 — manager.py:1166 段有但有 24h 去重,新被踢的号要等 24h 才会被 wham 探测。

### C.3 子症状 D:add-phone 真空区

**优先级 P0**

候选根因:
1. `login_codex_via_browser` 没接入 `detect_phone_verification` 探针(主因) — invite.py 的探针没被复用。
2. 踢出 → 立即重 OAuth 时间太近(次因) — `_run_post_register_oauth:1428` 有 8s 等待,但 reinvite_account 没有类似等待。
3. `_used_email_ids` 状态污染 — `codex_auth.py:260` 每轮新建,不会跨轮污染。但浏览器 context 是新建的,不该带 cookie 残留。

### C.4 子症状 A:workspace 不让选

**优先级 P2**(更像 UX 缺失而非 bug)

候选根因:
1. cookie 注入 + JS 自动点击的设计就是为了无人值守自动化 — 与"让用户选"的交互需求矛盾。
2. 缺少前端 OAuth 模式选择菜单 — 当前 web UI 的"补登录"按钮没有 use_personal 开关,API `POST /accounts/{email}/login` 也是按 acc.status 自动决定 use_personal(api.py:1475)。

## D. 修复方向建议(只是方向,不写代码)

### D.1 必须改的点

1. **`_run_post_register_oauth` 加 quota probe**(P0,issue#6 主因 B)
   - 位置:`manager.py:1463-1486`
   - 拿到 bundle 后立即调 `check_codex_quota(bundle.access_token, bundle.account_id)`
   - 结果分流:
     - `ok` + `quota_info` 写入 last_quota,标 STATUS_ACTIVE(现有行为)
     - `exhausted` 标 STATUS_EXHAUSTED + quota_exhausted_at(参考 manual_account.py:266-272)
     - `auth_error` / `network_error` 写日志,但**不**标 active(避免假入池)— 标 STATUS_AUTH_INVALID,等 reconcile 处理

2. **`check_codex_quota` 识别"总额=0"**(P0,issue#2 / #6 共因)
   - 位置:`codex_auth.py:1685-1700`
   - 解析 rate_limit.primary_window 时除了 `used_percent` 也读 `limit` / `total` / `remaining`
   - 总额=0 / remaining=0 / reset_at=0 → 新增分类 `"no_quota"` 或归到 `"exhausted"` + window=`no_quota`

3. **`sync_account_states` 区分"被踢"vs"自然待机"**(P0,issue#6 主因 C)
   - 位置:`manager.py:526-541`
   - 当 `not in_team and status == STATUS_ACTIVE` 时,如果 acc.auth_file 存在,实测一次 wham/usage:
     - 401/403 → 标 STATUS_AUTH_INVALID(token 已 revoke,等 reconcile 清 auth_file)
     - 200 + ok → 真的是被踢但 token 还在(罕见,可能 OpenAI 缓存延迟),标 STATUS_STANDBY 让 reinvite_account 重新拉
     - network_error → 暂保持 STATUS_ACTIVE 等下轮(避免网络抖动误标)

4. **`login_codex_via_browser` 接入 add-phone 探针**(P0,issue#6 主因 D)
   - 位置:`codex_auth.py:612` 起的 step-4 循环里,每轮 try 块开头 import `from autoteam.invite import detect_phone_verification`
   - 命中 add-phone 直接 break + 设置 `auth_code = None` + log error
   - 函数返回 None,上游 `_run_post_register_oauth` / `reinvite_account` 走"OAuth 失败"分支
   - **额外加** `record_failure(email, "add_phone_at_oauth", "OAuth 流程命中 add-phone 风控")`,UI 失败明细能看到

5. **`reinvite_account` 在踢出后等待 OpenAI 同步**(P1,issue#6 D 次因)
   - 位置:`manager.py:2466` 之前
   - 如果 acc 的状态从 standby 进入 reinvite,且 `last_active_at` < 5 分钟前(刚被踢),先 sleep 8-15s 让 ChatGPT 后端 default workspace 切换生效
   - 与 `_run_post_register_oauth:1427-1429` 的 8s 等待保持对称

6. **`reinvite_account` plan_type != team 时的二次处理**(P1,issue#6 C 次因)
   - 位置:`manager.py:2489-2494`
   - 不仅 `_cleanup_team_leftover` + return False,还要 `update_account(status=STATUS_AUTH_INVALID, auth_file=None)`
   - 让该号下一轮被 reconcile 直接清掉,而不是反复在 standby 池里轮转

### D.2 可选优化

1. **OAuth 模式选择 UI**(issue#6 A)
   - Web Dashboard 的"补登录"按钮加下拉:Team / Personal / Auto(根据 status)
   - API `POST /accounts/{email}/login` 接受 `mode` 参数

2. **standby 探测去重的 cool-down 缩短**(issue#6 C)
   - `manager.py:1166` 24h 去重对新被踢号太长,改成"被踢后 30 分钟即可再探测"
   - 用 acc 的 `last_kicked_at` 字段(新增)而不是 `last_quota_check_at` 判断

3. **add-phone 命中后清理 auth_file**
   - 一旦 OAuth 命中 add-phone,该账号短期内重 OAuth 还是会被风控,清掉 auth_file 标 STATUS_AUTH_INVALID 让用户介入
   - 比反复重试更友好

4. **batch 失败明细**:`failures` 表新增 add_phone_at_oauth 类别,UI 上区分"邀请阶段 add-phone"和"OAuth 阶段 add-phone"

### D.3 测试要点

- [ ] 注册一个新号,通过 invite 流程进 Team,确认 `_run_post_register_oauth` 拿到 bundle 后立刻打 wham/usage
- [ ] 模拟一个 quota=0 的 workspace(可以临时改 wham/usage 返回值)— 确认账号被标 EXHAUSTED 而不是 ACTIVE
- [ ] 在 ChatGPT 后台**手动**踢一个 active 子号 → 等下次 sync_account_states 跑 → 确认状态变 AUTH_INVALID(不是 STANDBY)
- [ ] 主动 kick 一个号后立刻 reinvite,确认 add-phone 风控被探测到 + 失败明细写入 register_failures.json
- [ ] reinvite_account 第一次失败标 AUTH_INVALID 后,确认下一轮 reconcile 清掉 auth_file,不再被 standby 池选中
- [ ] OAuth 模式选择 UI:在补登录时手动选"Team",确认 `_account` cookie 注入 + workspace 选 Team;选"Personal"确认 use_personal=True 路径

## E. 影响面 / 爆炸半径

- **`_run_post_register_oauth` 加 probe**:扩散到 `manager.py:1386-1486` 单点,但调用方多(_complete_registration、create_account_direct、_check_pending_invites)。最大风险是 probe 异常时不能堵死注册流程 — 必须保持"probe 失败不影响 bundle 已成功"的弱化逻辑。
- **`check_codex_quota` 加 no_quota 分类**:扩散到所有 6+ 调用点(manager 多处、api 多处、cpa_sync),需要每个调用点都 handle 新分类,否则会被当成 ok 默认值掉进死循环。
- **`sync_account_states` 加 wham 探测**:这是个高频调用函数(rotation 每轮都会触发),如果探测延时叠加,可能拖慢整个轮转。需要给每个 active 子号探测加并发 + 超时上限。
- **`login_codex_via_browser` 加 add-phone 探针**:仅 codex_auth.py 内部,但行为变化是"OAuth 提前失败"— 调用方都已有 bundle=None 兜底逻辑,影响小。
- **`reinvite_account` 调整 plan_type != team 的清理**:扩散到 manager.py 多个 reinvite 调用点(line 2719 / 3041 / 3391),需要确认它们都依赖"return False 时不动 acc.auth_file"的旧约定。

## F. 风险与未决问题

1. **wham/usage 返回 schema 是否真有 limit / total**:目前代码只读 `used_percent` / `reset_at` / `limit_reached`,如果 OpenAI 没在 self_serve_business 上返回 limit 字段,"总额=0"的识别只能依赖间接信号(reset_at=0 + used_percent=0 + 调用立刻 429)。需要从用户 bundle 实测 wham/usage 响应。
2. **手动踢 vs API 踢的区分**:OpenAI 后端 /users 的 GET 返回不会告诉我们"成员是被踢还是自然离开",只能靠"上次见过 + 这次没见到"推断。AutoTeam 自己 kick 的会有 _reconcile_team_members 主动标 AUTH_INVALID(line 312、339),但**人工 kick** 走 sync_account_states,目前缺识别。
3. **add-phone 探针误报**:invite.py 的 detect_phone_verification 主要靠 URL hint + body 文本,在 OAuth 流程中(auth.openai.com 域)可能误命中 OAuth 同意页里的 phone 帮助链接。需要为 OAuth 流程定制更严格的探针(必须有可见 input[type=tel] 且不在 footer 区)。
4. **reinvite 后 8-15s 等待是否够**:OpenAI 后端的 default workspace 切换延迟可能是 8s 也可能是 60s+,固定等待可能不够。建议 polling auth.openai.com 的 /me 接口确认 workspace 已切换才继续。
5. **加 quota probe 后 _run_post_register_oauth 失败率会上升**:因为之前 quota=0 的号会被认为成功,加 probe 后会被打回失败。这可能让 fill-team 的"成功率"指标看起来变差,但实际是更准确。需要在变更说明中提醒用户这是正向变化。
6. **issue#2 与 issue#6 的修复有重叠**:`check_codex_quota` 增强、`plan_type` 白名单、quota probe 都是两个 issue 共因,实施时应统一规划而不是分两次改。
