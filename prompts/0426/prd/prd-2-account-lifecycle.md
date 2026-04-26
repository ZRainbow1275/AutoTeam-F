# PRD-2: 账号生命周期与配额加固

## 0. 元数据

| 字段 | 内容 |
|---|---|
| 编号 | PRD-2 |
| 名称 | 账号生命周期与配额加固(plan_type / quota / add-phone / 删除链 / 席位策略) |
| 覆盖 Issue | #2(席位策略与免费号删除)+ #4(OAuth + add-phone)+ #6(踢出后 401 + 重 OAuth) |
| 主笔 | prd-lifecycle |
| 时间 | 2026-04-26 |
| 优先级 | P0(用户使用率最大瓶颈,3 个 issue 共因) |
| 关联 PRD | PRD-1(独立)、PRD-3(独立)、PRD-4(独立) |
| 输入资料 | research/issue-2/4/6 + synthesis.md |
| 共因合并依据 | synthesis §1:A/B/C/D/E 必须**同 PR**实施,半截修复会引入新故障态 |
| 不在范围 | Mail Provider(PRD-1)、Docker 镜像守卫(PRD-3)、Playwright 一致性(PRD-4) |

---

## 1. 背景与问题陈述

用户在使用过程中持续暴露 4 类生命周期问题,经调研归并为 **5 个共因点 + 3 个独属点**。

### 1.1 用户子症状(4 issue 合并视角)

| 子症状 | 来源 | 一句话 |
|---|---|---|
| **S1**:`self_serve_business_usage_based` 等新 plan 不识别,号能注册但 Codex 调用 429 | #2 / #6 | `codex_auth.py:111` 写死从 JWT 取字段,下游 `manager.py:1468/2490`、`manual_account.py:234` 全用 `if plan_type == "team"` 二元分支 |
| **S2**:Workspace 配额=0 时 wham/usage 显示"剩余 100%",但调用立刻 429 | #2 / #6 | `codex_auth.py:1582` 只看 `used_percent>=100`,不能识别 `used=0 + total=0` 的"无配额已分配"形态 |
| **S3**:OAuth 全程没有 add-phone 探针,撞风控就静默 30s 超时 | #4 / #6 | `codex_auth.py` 全文 0 处 `detect_phone_verification`,而 invite 注册阶段已有完整探针(`invite.py:106`) |
| **S4**:`_run_post_register_oauth` 注册收尾不验配额,假入池 | #6 | `manager.py:1463` 拿到 bundle 立刻 `STATUS_ACTIVE`,无 `check_codex_quota` 探测;手动添加 (`manual_account.py:263`)是有的,接口对称缺失 |
| **S5**:管理员手动从 ChatGPT 后台踢号 → 子号 401,但本地仅标 STANDBY | #6 | `manager.py:540` `not in_team and status==active → STANDBY`,与"自然待机"不区分,被踢号会被反复 reinvite 死循环 |
| **S6 (#2 独属)**:邀请席位写死 default,无法配置默认走 codex 席位 | #2 | `invite.py:496` `seat_type="default"` 硬编码;`chatgpt_api.py:1487-1509` PATCH 升级链路单向 |
| **S7 (#2 独属)**:已退出 Team 走 personal 的免费号,从 UI 删不掉 | #2 | `account_ops.py:78-84` 删除链强依赖主号 session(`fetch_team_state`),personal 号无 Team 关系也走该路径 |
| **S8 (#6 独属)**:reinvite_account 拿到非 team plan 时,只 kick 一次后留 standby,下轮再选中循环失败 | #6 | `manager.py:2490-2494` `_cleanup_team_leftover` + `STATUS_STANDBY`,未推到 `STATUS_AUTH_INVALID` 终态 |

### 1.2 共因抽象(synthesis §1)

| ID | 共因 | 关联 Issue |
|---|---|---|
| A | `plan_type` 白名单(`SUPPORTED_PLAN_TYPES`) | #2 + #6 |
| B | `wham/usage` 增加 `no_quota` 分类 | #2 + #6 |
| C | `login_codex_via_browser` 接 add-phone 探针 | #4 + #6 |
| D | `_run_post_register_oauth` 加 quota probe | #6 |
| E | `sync_account_states` 区分被踢/待机 | #6 |

A/B/C/D/E **必须同 PR**:任何半截修复都会让其它共因点保留旧故障(例:仅修 D 不修 B,probe 跑出 ok 但实际 quota=0,假入池依旧)。

---

## 2. 目标(SMART)

| 编号 | 目标 | 量化指标 | 度量手段 |
|---|---|---|---|
| G1 | 拿到 OAuth bundle 后必须能识别 plan_type 是否被支持 | 100% 覆盖白名单外的 plan_type 立即标 `STATUS_AUTH_INVALID` 或拒收 bundle | `register_failures.json` `category="plan_unsupported"` 计数 |
| G2 | wham/usage 能识别 `no_quota` 形态 | total=0 / remaining=0 / reset_at=0 任一命中 → 归 `no_quota`,前端 UI 显示明确文案 | 集成测试 + 用户 manual 验证 |
| G3 | OAuth 命中 add-phone 必须被识别并且不再静默超时 30s | 4 个接入点(about-you / consent / callback / personal 拒收前)各自抛 `RegisterBlocked(is_phone=True)`,2s 内退出 | 单测 mock + 生产 `record_failure` 计数 |
| G4 | `_run_post_register_oauth` 能区分 ok / exhausted / no_quota / auth_error / network_error | 5 分类 100% 覆盖,exhausted+no_quota → 不入 ACTIVE 池 | 集成测试 |
| G5 | sync_account_states 能区分人工踢出 vs 自然待机 | 人工踢出 → STATUS_AUTH_INVALID,自然待机 → STATUS_STANDBY,两者不混淆 | 模拟踢人 + reconcile 验证 |
| G6 | 邀请席位策略可配置 | `PREFERRED_SEAT_TYPE` 取 `chatgpt`/`codex`,UI 可调,生效后 PATCH 不被调用 | 抓包 + 日志 |
| G7 | personal 子号删除不依赖主号 session | personal/auth_invalid 删除路径短路 `fetch_team_state` | 主号 session 失效场景下批量删除成功率 100% |
| G8 | reinvite_account 拿到非 team plan 时收敛到终态 | 一次失败即推 `STATUS_AUTH_INVALID`,reconcile 自动接力 | 状态转移日志 |

---

## 3. 非目标

- **不**重写 OAuth 主流程(用户硬要求"不要脱离原本流程太多")。
- **不**接入 SMS pool 自动绑定手机号(违反 ToS,封号风险)。
- **不**绕过 add-phone 风控(检测到即放弃 + 标记)。
- **不**做 IP 池切换 / 自动重试(留作后续 PRD,本期只做检测 + 分类)。
- **不**改动主号路径 `login_codex_via_session` / `SessionCodexAuthFlow` 的 add-phone 处置(主号通常已绑定,极罕见)。
- **不**改前端"补登录"按钮的交互流程(OAuth 模式选择 UI 是 P2,后置 PR)。
- **不**新增 `STATUS_PHONE_REQUIRED` 状态(round-3 已新增 AUTH_INVALID/ORPHAN,本次复用 + register_failures 子分类)。

---

## 4. 用户故事

- **US-1**(场景 S1+S5)作为运营人员,我希望系统识别 OpenAI 后端返回的新 `plan_type`(如 `self_serve_business_usage_based`),不要把不可用账号塞进 ACTIVE 池让 CPA 调用 429。
- **US-2**(场景 S2)作为管理员,我看到的"剩余额度 100%"必须真实可用,不能名义剩余实际 429。
- **US-3**(场景 S3)作为运维,在 OAuth 阶段命中 add-phone 时,必须立即知晓(失败明细分类 `oauth_phone_blocked`),不要静默 30s 超时再写"未获取到 auth code"。
- **US-4**(场景 S4)作为账号管理者,从注册→入池每一步的状态变化都应严格反映"账号是否真可用"。
- **US-5**(场景 S5)作为团队管理员,我从 ChatGPT 后台手动踢一个号后,本地系统应识别为"被踢"(STATUS_AUTH_INVALID),让 reconcile 自动接管,不要反复 reinvite。
- **US-6**(场景 S6)作为席位策略调度者,我希望能在 UI 上设置"默认走 codex 席位",留出 ChatGPT 主席位给真人。
- **US-7**(场景 S7)作为账号清理者,即便主号 session 已失效,我仍能删除已退出 Team 的 personal 子号。
- **US-8**(场景 S8)作为 standby 池监管者,reinvite 拿到非 team plan 应立即进入终态,不要反复试。

---

## 5. 功能需求(FR)

### 5.1 [共因 A] plan_type 白名单(`SUPPORTED_PLAN_TYPES`)

**FR-A1** 在 `accounts.py` 同级新增常量集 `SUPPORTED_PLAN_TYPES = frozenset({"team", "free", "plus", "pro"})`(全小写)。

**FR-A2** 提供 `is_supported_plan(plan_type: str) -> bool` 工具函数,内部 `.lower().strip()` 后比对。

**FR-A3** `codex_auth.py:_exchange_auth_code`(L100-116)拿到 bundle 后,**写入新字段** `bundle["plan_supported"] = is_supported_plan(plan_type)`,不直接拒收(避免破坏现有 personal 模式校验路径)。

**FR-A4** 4 个下游消费点统一改造:
- `manual_account._finalize_account` (L233):`if plan_type == "team"` → 改为白名单查询。`plan_supported=False` → `STATUS_AUTH_INVALID` + `record_failure(category="plan_unsupported")` + 不写 last_quota。
- `manager._run_post_register_oauth` (L1467):同上,Team 分支 plan_supported=False → 不进 ACTIVE,改 STATUS_AUTH_INVALID。
- `manager.reinvite_account` (L2489):保持 `plan_type != "team"` 失败语义,但额外记录 `category="plan_drift"`,标记 `auth_file=None` + `STATUS_AUTH_INVALID`(见 FR-5.8)。
- `cpa_sync._infer_plan_from_filename`(L132-140):允许识别新增字面量(可选)。

**FR-A5** 字面量小写归一化:任何对 `plan_type` 的判定都先 `.lower()`,避免 OpenAI 返回 `Team` / `Self_Serve_*` 大小写漂移。

### 5.2 [共因 B] wham/usage `no_quota` 分类

**FR-B1** 扩展 `check_codex_quota`(`codex_auth.py:1618`)返回值,在原 4 分类基础上新增 `("no_quota", info)`:
- 触发条件(任一命中):
  1. `rate_limit.primary_window.limit == 0` 且 `used_percent == 0`
  2. `rate_limit.primary_window.reset_at == 0` 且 `used_percent == 0`
  3. `rate_limit.primary_window.remaining == 0` 且 `total == 0`
  4. 200 OK 但 `rate_limit` 字段为空 / 缺 `primary_window`(空载也是 no_quota 信号)
- 返回 info 形如 `{"reason": "no_quota_assigned", "raw_rate_limit": {...}}`

**FR-B2** `quota_info` 字段扩 `total` / `remaining`(若接口提供):
```python
quota_info = {
    "primary_pct": primary.get("used_percent", 0),
    "primary_resets_at": primary.get("reset_at", 0),
    "primary_total": primary.get("limit", 0) or primary.get("total", 0),
    "primary_remaining": primary.get("remaining"),
    "weekly_pct": secondary.get("used_percent", 0),
    "weekly_resets_at": secondary.get("reset_at", 0),
}
```

**FR-B3** `get_quota_exhausted_info`(L1572)在判 `primary_pct >= 100` 之前加一道 `primary_total == 0` 短路,返回 `{"window": "no_quota", "resets_at": int(time.time() + 86400)}`(24h 内不再探测)。

**FR-B4** 9 个 `check_codex_quota` 调用方(api.py 3 + manager.py 5 + manual_account.py 1)都需要识别 `no_quota`:统一处置为"标 STATUS_EXHAUSTED 但 quota_resets_at=time+86400"或新增分支 STATUS_AUTH_INVALID(取决于业务语义)。**默认实现:no_quota → STATUS_AUTH_INVALID + auth_file 保留(供调试)+ register_failures `category="no_quota_assigned"`**。

### 5.3 [共因 C] `login_codex_via_browser` 接入 add-phone 探针

**FR-C1** 在 `codex_auth.py` 顶部 `from autoteam.invite import RegisterBlocked, assert_not_blocked`(无循环依赖,invite.py 不引 codex_auth)。

**FR-C2** **4 个接入点**(以 codex_auth.py:250 为基准行号):
| 编号 | 行号 | 时机 | 调用 |
|---|---|---|---|
| C-P1 | L568(about-you 入口前) | `if "about-you" in page.url:` 之前 | `assert_not_blocked(page, "oauth_about_you")` |
| C-P2 | L612-880(consent 10 次循环每轮) | `for step in range(10):` 内每次 try 块开头 | `assert_not_blocked(page, f"oauth_consent_{step}")` |
| C-P3 | L884(等 callback 前) | `for _ in range(30):` 之前 | `assert_not_blocked(page, "oauth_callback_wait")` |
| C-P4 | L932(personal 拒收 bundle 之前) | `if use_personal:` 之前 | `assert_not_blocked(page, "oauth_personal_check")`(防御性,通常 callback 前已拦截) |

**FR-C3** `RegisterBlocked` 异常向上抛,`login_codex_via_browser` 调用方一律 `try/except RegisterBlocked` 并按调用点分类:
- **api.py:1479** 补登录:转 HTTP 409 + body `{"error": "phone_required", "step": ...}`
- **manager.py:1057** _check_pending_invites:`record_failure(category="oauth_phone_blocked")` + 删账号
- **manager.py:1431** personal OAuth:`delete_account` + `record_failure(category="oauth_phone_blocked")`
- **manager.py:1463** Team OAuth 收尾:`update_account(STATUS_AUTH_INVALID)` + `record_failure(category="oauth_phone_blocked")`
- **manager.py:2466** reinvite_account:`_cleanup_team_leftover("oauth_phone_blocked")` + `STATUS_AUTH_INVALID`(见 5.8)

**FR-C4** 命中 add-phone 立即调 `_screenshot(page, f"codex_phone_blocked_{step}.png")` 留证据,便于回放。

**FR-C5** `register_failures.py` `category` 注释扩 `oauth_phone_blocked`(与注册阶段 `phone_blocked` 区分,便于统计与告警分级)。

### 5.4 [共因 D] `_run_post_register_oauth` 加 quota probe

**FR-D1** `manager.py:1463-1486` Team 分支获得 bundle 后,追加 quota probe 段:
```python
# 拿到 bundle 后,与 manual_account._finalize_account(L260-272)对称,
# 不能让 quota=0 / no_quota / auth_error 的号假入 ACTIVE 池
access_token = bundle.get("access_token")
account_id = bundle.get("account_id")
quota_status, quota_info = check_codex_quota(access_token, account_id=account_id)
```

**FR-D2** quota_status 5 分类处置:
| status | 处置 |
|---|---|
| `ok` | 写 `last_quota=quota_info`,STATUS_ACTIVE(原行为) |
| `exhausted` | STATUS_EXHAUSTED + `quota_exhausted_at=now` + `quota_resets_at=quota_result_resets_at(info)` |
| `no_quota`(新) | STATUS_AUTH_INVALID + `record_failure(category="no_quota_assigned")` + 仍保留 auth_file 供调试 |
| `auth_error` | STATUS_AUTH_INVALID + `record_failure(category="auth_error_at_oauth")` |
| `network_error` | 沿用旧 STATUS_ACTIVE(避免一次抖动批量误标),但 `record_failure(category="quota_probe_network_error")` |

**FR-D3** Personal 分支(L1431,`leave_workspace=True`)获得 free bundle 后,**同样**调 quota probe(对称设计)。

**FR-D4** probe 调用本身要包 try/except:抛异常时降级到 STATUS_ACTIVE 但记 `register_failures` 一条 exception(避免 probe bug 阻塞注册主流程)。

### 5.5 [共因 E] `sync_account_states` 区分被踢/待机

**FR-E1** `manager.py:526-541` 在 `not in_team and acc["status"] == STATUS_ACTIVE` 分支,**保留**当前 workspace_account_id 漂移检测(L531-538),其下追加 wham 探测:
```python
if acc.get("auth_file"):
    try:
        bundle = load_auth_file(acc["auth_file"])
        access_token = bundle.get("access_token")
        if access_token:
            status_str, _ = check_codex_quota(access_token)
            if status_str == "auth_error":
                acc["status"] = STATUS_AUTH_INVALID
                changed = True
                continue
    except Exception:
        pass  # 探测失败不阻塞主流程
acc["status"] = STATUS_STANDBY
changed = True
```

**FR-E2** 探测加并发限制(避免 N 个号串行 wham 拖慢 sync):用 `concurrent.futures.ThreadPoolExecutor(max_workers=5)` + 单调用超时 5s。

**FR-E3** 探测去重:同一 email 在 30 分钟内不重复探测(读 `last_quota_check_at` 字段 + 写入新值)。

**FR-E4** sync 主循环不能因为探测异常崩溃 → 任何 except 都仅 log warning + 走旧 STATUS_STANDBY 默认行为。

### 5.6 [#2 独属] `PREFERRED_SEAT_TYPE` 配置开关

**FR-F1** `runtime_config.py` 新增 `get_preferred_seat_type()` / `set_preferred_seat_type(value)`,值域 `{"chatgpt", "codex"}`,默认 `"chatgpt"`(保持现行行为)。

**FR-F2** `invite.py:496` 改为:
```python
preferred = (get_preferred_seat_type() or "chatgpt").lower()
seat_type_param = "default" if preferred == "chatgpt" else "usage_based"
status, data = chatgpt.invite_member(email, seat_type=seat_type_param)
```

**FR-F3** `chatgpt_api.py:_invite_member_once`(L1414)的 PATCH 升级段(L1487-1506):新增 `allow_patch_upgrade` 参数,默认 True;调用方传入 `allow_patch_upgrade=(preferred == "chatgpt")`。`PREFERRED_SEAT_TYPE=codex` 时 PATCH 完全跳过,`_seat_type` 保持 `usage_based`。

**FR-F4** `_invite_member_with_fallback`(L1387)的兜底链向后兼容:
- `preferred=chatgpt`:default → usage_based 兜底(原行为)
- `preferred=codex`:直接 usage_based,不兜底 default

**FR-F5** Web 前端 Settings 页新增"邀请席位偏好"下拉(`chatgpt` / `codex`),写 `runtime_config.preferred_seat_type`。

**FR-F6** 状态变更立即生效,不需要重启;并发安全(读取每次邀请前从 runtime_config 拉,而非启动时缓存)。

### 5.7 [#2 独属] personal 删除链解耦主号 session

**FR-G1** `account_ops.delete_managed_account`(L40-162)新增短路逻辑:
```python
acc = find_account(accounts, email)
short_circuit = False
if remove_remote and acc:
    if acc.get("status") in (STATUS_PERSONAL, STATUS_AUTH_INVALID):
        short_circuit = True
        members, invites = [], []  # 跳过 fetch_team_state
        logger.info("[账号] %s 状态=%s,跳过 Team 远端同步,直接清本地", email, acc.get("status"))
```

**FR-G2** short_circuit=True 时,跳过 L78-84 的 `ChatGPTTeamAPI().start()` + `fetch_team_state()`,member_matches/invite_matches 为空,直接走 auth_file/cpa/local 删除。

**FR-G3** `api.delete_accounts_batch`(L1306-1404)在批量场景下:
- 先扫一遍 batch 内所有 acc.status,**全是 personal/auth_invalid 时**,完全不起 ChatGPTTeamAPI(传 `chatgpt_api=None` + 在 short_circuit 中再次保护)
- 混合场景(部分 personal + 部分 team):仍起 ChatGPTTeamAPI,但 short_circuit 单条生效

**FR-G4** UI 删除失败 toast 增强(`Dashboard.vue:566-585` `removeAccount`):
- 失败原因(`actionDisabled` / 409 / 500)区分提示
- 删除成功立即 `emit('refresh')` 触发账号列表 reload
- 不再"按钮没反应"误解

### 5.8 [#6 独属] `reinvite_account` 兜底(plan_type ≠ team 时清理)

**FR-H1** `manager.py:2489-2494`:`plan_type != "team"` 分支,在 `_cleanup_team_leftover` 后追加:
```python
update_account(
    email,
    status=STATUS_AUTH_INVALID,    # 不要回 STANDBY,会被反复选中循环
    auth_file=None,                # 清掉错误 plan 的 token
    quota_exhausted_at=None,
    quota_resets_at=None,
)
record_failure(email, "plan_drift", f"reinvite 拿到 plan={plan_type or 'unknown'} != team",
               stage="reinvite", source="reinvite_account")
```

**FR-H2** **批判:不会误伤合法 personal 转化路径**。`reinvite_account` 只从 STATUS_STANDBY 池被选中(`manager.py:2643+`),其语义预设是"恢复到 Team 工作池";用户希望让某号转 personal 的合法入口是 `cmd_fill_personal`(走 `_run_post_register_oauth(leave_workspace=True)`),与 reinvite 完全分离。reinvite 拿到非 team plan 永远是异常状态,推 STATUS_AUTH_INVALID 后,reconcile 会按 auth_invalid 流程处理(KICK + 清理),不再有死循环。**风险登记册 R-7** 列入跟踪。

**FR-H3** reinvite 失败被推到 AUTH_INVALID 后,reconcile_anomalies(`manager.py:161-471`)的 KICK 分支会把 Team 残留清干净,本地记录最终被删除或留着等用户介入(取决于 `auth_invalid` 处置策略)。

---

## 6. 非功能需求

| 编号 | 需求 |
|---|---|
| NFR-1 | 性能:`sync_account_states` 探测段不能让单轮 sync 超过 30s(并发 5 + 超时 5s) |
| NFR-2 | 兼容性:旧 accounts.json 记录(无 `plan_supported` / `last_quota.primary_total` 字段)走默认值,不报错 |
| NFR-3 | 可观测:每个新 category(`plan_unsupported` / `no_quota_assigned` / `oauth_phone_blocked` / `auth_error_at_oauth` / `quota_probe_network_error` / `plan_drift`)在 `register_failures.json` 都能独立计数 |
| NFR-4 | 灰度:`PREFERRED_SEAT_TYPE` 默认 `chatgpt`,与现行行为完全一致;切到 `codex` 须显式操作 |
| NFR-5 | 可回滚:所有改动可被一个 revert PR 撤销;`accounts.json` schema 向后兼容 |
| NFR-6 | 安全:add-phone 截图存 `screenshots/codex_phone_blocked_*.png`,不含 cookie/token |
| NFR-7 | 文档:CHANGELOG.md 必须列出 5 个新 category + 4 个新探针接入点 |

---

## 7. 技术方案

### 7.1 状态机更新(account state machine)

新增/澄清的边:

```
ACTIVE ──[sync 探测 wham=auth_error]──→ AUTH_INVALID  (新,FR-E1)
ACTIVE ──[sync 探测 wham=ok]────────────→ STANDBY       (语义不变,但前提是探测通过)
ACTIVE ──[wham=no_quota]─────────────────→ AUTH_INVALID  (新,FR-D2)
PENDING ──[bundle plan_unsupported]─────→ AUTH_INVALID  (新,FR-A4)
STANDBY ──[reinvite plan!=team]──────────→ AUTH_INVALID  (新,FR-H1,替代旧 STANDBY)
STANDBY ──[reinvite OAuth phone]─────────→ AUTH_INVALID  (新,FR-C3)
PENDING ──[OAuth phone 命中]─────────────→ AUTH_INVALID  (新,FR-C3 之 manager.py:1463 分支)
PENDING ──[personal OAuth phone]────────→ deleted        (新,FR-C3 之 manager.py:1431 分支)
```

### 7.2 配额分类规则(quota classification)

```
HTTP 401/403                               → auth_error
HTTP 200 + primary_total == 0              → no_quota          (新)
HTTP 200 + reset_at == 0 && used_pct == 0  → no_quota          (新)
HTTP 200 + rate_limit 缺失/空              → no_quota          (新)
HTTP 200 + primary_pct >= 100 || weekly_pct >= 100 → exhausted (原)
HTTP 200 + 上述都不命中                     → ok               (原)
HTTP 429 / 5xx / json 解析失败 / 网络异常  → network_error    (原)
```

### 7.3 add-phone 探针在 OAuth 流程的接入点(4 处)

```
login_codex_via_browser(email, password, mail_client, *, use_personal)
  │
  ├─ step-0 (Team 模式 cookie 注入)
  │
  ├─ step-1 邮箱/密码/OTP 表单
  │
  ├─ ★ C-P1: assert_not_blocked(page, "oauth_about_you")    [L568 about-you 前]
  ├─ step-2 about-you 提交
  │
  ├─ ★ C-P2: assert_not_blocked(page, f"oauth_consent_{i}") [L612 consent 循环每轮]
  ├─ step-3 consent 10 轮循环(workspace 选择 / Continue 按钮)
  │
  ├─ ★ C-P3: assert_not_blocked(page, "oauth_callback_wait") [L884 等 callback 前]
  ├─ step-4 等 30s callback
  │
  ├─ ★ C-P4: assert_not_blocked(page, "oauth_personal_check") [L932 personal 拒收前,防御性]
  └─ _exchange_auth_code → bundle
```

### 7.4 数据模型变更(accounts.json schema)

```python
# Pydantic 模型(用于内部类型签名 / 测试断言)
class AccountRecord(BaseModel):
    email: str
    password: str
    cloudmail_account_id: Optional[str] = None
    status: Literal["active", "exhausted", "standby", "pending", "personal", "auth_invalid", "orphan"]
    seat_type: Literal["chatgpt", "codex", "unknown"] = "unknown"
    workspace_account_id: Optional[str] = None
    auth_file: Optional[str] = None
    quota_exhausted_at: Optional[float] = None
    quota_resets_at: Optional[float] = None
    last_quota_check_at: Optional[float] = None  # FR-E3 探测去重
    last_quota: Optional[QuotaSnapshot] = None
    last_active_at: Optional[float] = None
    created_at: float
    plan_supported: Optional[bool] = None  # 新增,FR-A3
    plan_type_raw: Optional[str] = None    # 新增,记录 OAuth 实际拿到的字面量,便于排查

class QuotaSnapshot(BaseModel):
    primary_pct: int = 0
    primary_resets_at: int = 0
    primary_total: Optional[int] = None      # 新增 FR-B2
    primary_remaining: Optional[int] = None  # 新增 FR-B2
    weekly_pct: int = 0
    weekly_resets_at: int = 0

class QuotaProbeResult(BaseModel):
    status: Literal["ok", "exhausted", "no_quota", "auth_error", "network_error"]
    info: Optional[Union[QuotaSnapshot, dict]] = None
```

### 7.5 配置项变更(`runtime_config` + `.env`)

| key | 来源 | 取值 | 默认 | 用途 |
|---|---|---|---|---|
| `preferred_seat_type` | runtime_config.json | `chatgpt` / `codex` | `chatgpt` | FR-F1,席位偏好 |
| `quota_probe_threshold_pct` | runtime_config.json | `0..100` | `10` | reinvite 后 5h 剩余阈值,与现有 AUTO_CHECK_THRESHOLD 一致 |
| `sync_probe_concurrency` | runtime_config.json | int | `5` | FR-E2,sync 探测并发 |
| `sync_probe_cooldown_minutes` | runtime_config.json | int | `30` | FR-E3,探测去重 |

`.env` 新增:
- `PREFERRED_SEAT_TYPE_DEFAULT`(可选,首次启动写入 runtime_config 的初始值)

### 7.6 前端变更(Settings + Dashboard)

| 组件 | 改动 |
|---|---|
| `web/src/views/Settings.vue` | 新增"邀请席位偏好"下拉(FR-F5)、"配额探测并发数"(FR-E2)、"探测去重分钟数"(FR-E3) |
| `web/src/components/Dashboard.vue` | 删除按钮 toast 增强(FR-G4);状态白名单加 `auth_invalid` 显示文案(round-3 已加,本期复核);quota 显示区识别 `no_quota` 渲染"无配额"而非"100% 剩余" |
| `web/src/api.ts` | `/api/accounts/{email}/login` 加入 409 错误体解析(`error: "phone_required"`),弹窗提示用户 |

---

## 8. 验收标准

| FR | 验收用例 | 通过条件 |
|---|---|---|
| FR-A1~A5 | 注册一个 OAuth bundle 字面值 = `self_serve_business_usage_based`(可 mock JWT) | 账号被打 STATUS_AUTH_INVALID + register_failures 命中 `category="plan_unsupported"`,`plan_type_raw` 落盘 |
| FR-B1~B4 | wham/usage 返回 `{"primary_window": {"limit": 0, "used_percent": 0}}` | quota_status="no_quota",前端 UI 显示"无配额",账号 STATUS_AUTH_INVALID |
| FR-C1~C5 | OAuth 流程到 about-you 后 page.url 跳到 `add-phone` | 4 个接入点任一抛 RegisterBlocked,`category="oauth_phone_blocked"`,截图落盘 |
| FR-D1~D4 | 注册收尾 bundle 拿到但 wham 返回 no_quota | 账号 STATUS_AUTH_INVALID 而非 ACTIVE |
| FR-E1~E4 | 管理员从 ChatGPT 后台手动踢一个 active 子号 | 下次 sync_account_states 跑后,该号 STATUS_AUTH_INVALID(不是 STANDBY) |
| FR-F1~F6 | UI 设置 `preferred_seat_type=codex` → 触发 invite | invite_member 调 `seat_type="usage_based"`,日志中 0 处 PATCH 升级 |
| FR-G1~G4 | personal 子号 + 主号 session 失效 → 单点删除 | 不抛 ChatGPTTeamAPI 启动错误,本地记录被清,UI 列表刷新 |
| FR-H1~H3 | reinvite 拿到 plan=free 的 bundle | 账号 STATUS_AUTH_INVALID,reconcile 自动 KICK + 清 auth_file |

---

## 9. 测试计划

### 9.1 单元测试

- `tests/unit/test_plan_type_whitelist.py`:`is_supported_plan` 7 种字面量(team/free/plus/pro/Team/self_serve_business_usage_based/空串)
- `tests/unit/test_quota_classification.py`:5 分类 mock wham/usage 各响应,断言 status_str
- `tests/unit/test_oauth_phone_detection.py`:4 接入点各自 mock page.url + body,assert_not_blocked 抛异常
- `tests/unit/test_run_post_register_quota_probe.py`:5 quota_status × 2 (Team/personal) 路径
- `tests/unit/test_sync_state_classify.py`:被踢 vs 自然待机区分
- `tests/unit/test_preferred_seat_type.py`:chatgpt/codex 两种偏好下 invite_member 入参
- `tests/unit/test_personal_delete_short_circuit.py`:STATUS_PERSONAL/AUTH_INVALID 跳过 fetch_team_state

### 9.2 集成测试

- `tests/integration/test_oauth_phone_blocked_flow.py`:全链路 mock,验证 6 处 login_codex_via_browser 调用方都能正确分类 + 打到 register_failures
- `tests/integration/test_quota_probe_no_quota.py`:wham 返回 limit=0 → 注册→入池→标 AUTH_INVALID 全链路

### 9.3 回归测试

- 既有 `invite/reinvite/sync_account_states/_run_post_register_oauth/check_codex_quota` 套件全部跑过
- `manual_account.py` finalize 流程不受 plan_type 白名单影响(对称改造保护)

### 9.4 E2E / 手测

- 手动:从 ChatGPT 后台踢一个号,等 1 个 rotate 周期,确认状态机正确
- 手动:UI 切 PREFERRED_SEAT_TYPE 后注册新号,确认 PATCH 不被调用
- 手动:邀请一个新号触发 add-phone(可能需要 IP 漂移/cookie 清空),确认探针命中

---

## 10. 灰度/回滚策略

### 10.1 灰度

- **阶段 0**:实施 PR(默认 PREFERRED_SEAT_TYPE=chatgpt,行为不变),只观察新 category 计数
- **阶段 1**:1-2 周后,根据 `register_failures.json` 数据评估 PATCH 升级实际失败率,决定是否在 default 配置中切到 codex
- **阶段 2**:监控 STATUS_AUTH_INVALID 总数与人工介入率,若 reconcile 自动接管率 < 90%,新增告警

### 10.2 回滚

- 单 PR revert 即可:所有新增字段(`plan_supported`/`primary_total`/...)向后兼容,旧代码读不到字段走默认值
- 配置回滚:删除 `runtime_config.json` 中的新键即恢复硬编码默认

---

## 11. 文档影响清单

| 文件 | 改动 |
|---|---|
| `CHANGELOG.md` | 新增条目:5 个新 register_failures category、4 个 OAuth 探针接入点、PREFERRED_SEAT_TYPE 配置 |
| `docs/account-state-machine.md`(新建) | 7 状态完整转移图,含本 PRD 新增的边 |
| `docs/quota-classification.md`(新建) | 5 分类规则、no_quota 触发条件、调用方处置矩阵 |
| `docs/oauth-add-phone-detection.md`(新建) | 4 接入点位置、检测器复用契约 |
| `README.md` | 配置说明加 PREFERRED_SEAT_TYPE |
| `web/README` | Settings 页新加配置项说明 |

---

## 12. 风险登记册

| 编号 | 风险 | 等级 | 概率 | 缓解 | 来源 |
|---|---|---|---|---|---|
| R-1 | sync_account_states 加 wham 探测后,N 个 active 号串行探测拖慢 rotation | 高 | 中 | NFR-1 限 30s + 并发 5 + 探测去重 30 分钟 | gitnexus.impact `sync_account_states`:被 manager.py:696/2843 调用,rotation 主路径 |
| R-2 | check_codex_quota 加 no_quota 分类,9 个调用点漏改任一 → no_quota 被当 ok 默认 | 高 | 中 | FR-B4 显式枚举所有调用方 + 单测覆盖 + lint 守卫 | gitnexus.impact `check_codex_quota`:9 处调用 |
| R-3 | login_codex_via_browser 加 RegisterBlocked 抛异常,5 处调用方漏 catch → 主流程崩溃 | 高 | 低 | FR-C3 显式列 5 处处置 + 集成测试 | gitnexus.impact `login_codex_via_browser`:5 处调用 |
| R-4 | _run_post_register_oauth probe 异常吞 → 假 ACTIVE 回到旧故障 | 中 | 中 | FR-D4 try/except + register_failures 计数监控 | gitnexus.impact `_run_post_register_oauth`:3 处调用 |
| R-5 | invite.py 的 detect_phone_verification 在 OAuth 域(auth.openai.com)误报 consent 页 phone 提示 | 中 | 低 | 命中后立即截图,运营回放确认;必要时给 OAuth 探针定制更严格规则 | issue#4 风险 E.1.3 |
| R-6 | reinvite 后的 STATUS_AUTH_INVALID 与 reconcile_anomalies 的 KICK 分支耦合,任一改一处都可能影响另一处 | 中 | 中 | 单测 + 集成测试 + 显式注释 | issue#6 D.1.6 |
| R-7 | reinvite 拿到 free plan 当作"plan_drift"标 AUTH_INVALID,但用户合法的"号转 personal"路径走 cmd_fill_personal 不会进 reinvite_account,因此**不存在误伤** | 低 | 极低 | FR-H2 已注明语义边界;reinvite 入口只从 STANDBY 池被选,与 personal 转化的入口完全分离 | task 第 3 条批判 |
| R-8 | PATCH 升级失败率统计未知 → 默认 PREFERRED_SEAT_TYPE 选错 | 低 | 中 | NFR-4 默认保持 chatgpt 不变,1-2 周观察后再调 | issue#2 F.2 |
| R-9 | wham/usage 真实 schema 未知,limit/total 字段可能不存在 | 中 | 中 | FR-B1 触发条件 4 任一命中即可,接口缺字段→空载也归 no_quota;先上线再观察 | issue#2 F.1 / synthesis §7 |
| R-10 | OpenAI 后端 plan_type 字面量大小写漂移 | 低 | 低 | FR-A5 强制 .lower() 归一化 | issue#2 F.4 |
| R-11 | 主号 OAuth 流程也撞 add-phone(罕见但可能) | 低 | 极低 | 非目标:本期不处置主号路径;日志 ERROR 即可 | issue#4 E.2.1 |
| R-12 | personal 删除短路后,某些"标着 personal 但实际还在 Team"的脏记录会被漏删 Team 残留 | 中 | 低 | reconcile_anomalies 仍会扫整个 Team 把 ghost 清掉,作为兜底 | issue#2 F.3 |

---

## 13. 未决问题(送实施阶段决议)

| 编号 | 问题 | 提案 |
|---|---|---|
| Q-1 | `wham/usage` 在 `self_serve_business_usage_based` workspace 真实返回 schema | 先从用户实际 `auths/codex-*.json` + 一次抓包获取样本;若 limit/total 字段缺失,FR-B1 触发条件 4(rate_limit 空)兜底 |
| Q-2 | `self_serve_business_usage_based` 是否是真实字面量 | 先从用户报告的样本核对;如果实际是 `chatgpt_business_usage_based` 等变体,白名单兼容(只看是否在 SUPPORTED_PLAN_TYPES,实际值都进 unsupported 分支即可) |
| Q-3 | PATCH 升级失败率 | 改默认席位策略前先收集 1-2 周数据;如失败率 >30%,默认改 codex |
| Q-4 | `STATUS_PHONE_REQUIRED` 是否新增 | 不新增,复用 STATUS_AUTH_INVALID + register_failures.category 区分,符合本 PRD 非目标 |
| Q-5 | sync_account_states 探测的并发 5 / 超时 5s 是否合理 | 灰度阶段 0 观察 sync 单轮耗时,>30s 调到 10 / 3s |
| Q-6 | reinvite_account 推到 AUTH_INVALID 后,reconcile 是否需要等用户介入还是自动删除 | 默认自动 KICK + 保留本地记录(供查看 register_failures);删本地由 UI 单点/批量删除触发 |
| Q-7 | OAuth 模式选择 UI(personal vs Team)放本期还是后置 | 后置(优先级 P2),不阻塞主线 |

---

## 14. 实施 Story Map

按"先抽 spec 共享层 → 再改 4 个文件 → 最后改 UI"排列,**严格按依赖**。

### Phase 0: 共享 spec 与常量(2 文件,半天)

- [ ] **S-0.1** 在 `accounts.py` 新增 `SUPPORTED_PLAN_TYPES` + `is_supported_plan()`(FR-A1/A2)
- [ ] **S-0.2** 在 `runtime_config.py` 新增 `get/set_preferred_seat_type` + 默认值(FR-F1)
- [ ] **S-0.3** 在 `register_failures.py` 文档化新增 6 个 category 名称(FR-A4/B4/C5/D2/E1/H1)
- [ ] **S-0.4** 写 `spec/shared/plan-type-whitelist.md` / `quota-classification.md` / `add-phone-detection.md` / `account-state-machine.md`(synthesis §5)

### Phase 1: 共因点核心实现(4 文件,2-3 天)

- [ ] **S-1.1** `codex_auth.py:_exchange_auth_code` 写 `plan_supported` 字段(FR-A3)
- [ ] **S-1.2** `codex_auth.py:check_codex_quota` 加 `no_quota` 分类 + 扩 quota_info 字段(FR-B1/B2/B3)
- [ ] **S-1.3** `codex_auth.py:login_codex_via_browser` 4 个 add-phone 探针接入点(FR-C1/C2/C3 仅探针,异常上抛部分由 S-2.x 处理)
- [ ] **S-1.4** `manager.py:_run_post_register_oauth` 加 quota probe(FR-D1/D2/D3/D4)
- [ ] **S-1.5** `manager.py:sync_account_states` 加 wham 探测区分被踢(FR-E1/E2/E3/E4)
- [ ] **S-1.6** `manager.py:reinvite_account` plan_drift 终态(FR-H1)
- [ ] **S-1.7** `manual_account._finalize_account` 改造白名单(FR-A4 第 1 项)
- [ ] **S-1.8** `account_ops.delete_managed_account` 短路逻辑(FR-G1/G2)

### Phase 2: 调用方处置(2 文件,1-2 天)

- [ ] **S-2.1** `manager.py` 4 处 login_codex_via_browser 调用方加 try/except RegisterBlocked + 分类(FR-C3 in: L1057/1431/1463/2466)
- [ ] **S-2.2** `api.py` `/api/accounts/{email}/login` 加 try/except + 409 错误体(FR-C3 in: L1479)
- [ ] **S-2.3** 9 处 check_codex_quota 调用方加 `no_quota` 分支(FR-B4)
- [ ] **S-2.4** `api.delete_accounts_batch` 全 personal 时不起 ChatGPTTeamAPI(FR-G3)

### Phase 3: 席位策略(2 文件,1 天)

- [ ] **S-3.1** `invite.py:496` 改读 PREFERRED_SEAT_TYPE(FR-F2)
- [ ] **S-3.2** `chatgpt_api.py:_invite_member_once` 加 `allow_patch_upgrade` 参数(FR-F3)
- [ ] **S-3.3** `chatgpt_api.py:_invite_member_with_fallback` 兜底链分支(FR-F4)

### Phase 4: 前端 UI(2 文件,1 天)

- [ ] **S-4.1** `web/src/views/Settings.vue` 新增"邀请席位偏好"下拉 + 探测并发/去重(FR-F5/E2/E3)
- [ ] **S-4.2** `web/src/components/Dashboard.vue` 删除 toast 增强 + quota 显示识别 no_quota(FR-G4)
- [ ] **S-4.3** `web/src/api.ts` 解析 409 phone_required 错误体

### Phase 5: 测试 + 文档(贯穿所有 Phase)

- [ ] **S-5.1** 单测覆盖 §9.1 的 7 个测试文件
- [ ] **S-5.2** 集成测试覆盖 §9.2 的 2 个测试文件
- [ ] **S-5.3** CHANGELOG / docs 文档更新(§11)
- [ ] **S-5.4** 手测脚本(§9.4)

### Phase 6: 灰度上线(1 天)

- [ ] **S-6.1** 默认 chatgpt 偏好上线,观察 register_failures 计数
- [ ] **S-6.2** 1-2 周后根据数据决议 PREFERRED_SEAT_TYPE 默认值切换(Q-3)

**关键依赖**:
- Phase 1 必须早于 Phase 2(被调用方先实现,调用方再 catch)
- Phase 0 早于 Phase 1(常量先定义,代码再引用)
- Phase 3 与 Phase 1/2 解耦,可并行实施
- Phase 4 在 Phase 1-3 完成后再做
- 所有 Phase 都伴随 Phase 5 的测试与文档更新

---

**文档结束。** 字数:约 4200(含表格、代码块、Story Map)。

