# Shared SPEC: Master 母号订阅健康度探针

## 0. 元数据 + 引用方

| 字段 | 内容 |
|---|---|
| 名称 | Master ChatGPT Team 母号订阅降级探针(三层判定 + 5min 缓存 + 触发位点矩阵) |
| 版本 | v1.0 (2026-04-27 Round 8 — Master Team Subscription Expiry 修复) |
| 主题归属 | `is_master_subscription_healthy()` 函数契约 + 三层探针 + 缓存策略 + 5 个触发位点 + 5 个误判缓解 |
| 引用方 | PRD-7(Round 8 master-team-degrade-oauth-rejoin) / spec-2-account-lifecycle.md v1.5 §3.6 / FR-M1~M4(待 PRD-7 落地) |
| 共因 | Round 8 PRD §1 外部根因 — `eligible_for_auto_reactivation: true` 等价 Stripe `cancel_at_period_end=true` 后周期已过 |
| 不在范围 | OAuth 显式选 personal workspace(见 [`./oauth-workspace-selection.md`](./oauth-workspace-selection.md)) / wham/usage 配额分类(见 [`./quota-classification.md`](./quota-classification.md)) / 自动续费 / 多母号支持(超 PRD-7 Out of Scope) |

---

## 1. 概念定义

| 术语 | 定义 |
|---|---|
| `master account` | AutoTeam 主号(workspace owner / admin),即被 admin_state cookies 登录、持有 ChatGPT Team 订阅的"母号" |
| `master subscription` | master account 在 OpenAI 后端持有的 ChatGPT Team 订阅(由 Stripe 计费),其状态决定子号 invite 后能否拿到 `plan_type=team` |
| `subscription healthy` | 订阅处于 `active` 状态,可继续生产子号 |
| `subscription degraded` | 订阅 cancel 但 workspace 实体仍存在,`/backend-api/accounts` items[*].`eligible_for_auto_reactivation == True`;子号 invite 后必拿 `plan_type=free` |
| `eligible_for_auto_reactivation` | OpenAI 内部字段,语义对应 Stripe `subscription.cancel_at_period_end=true` 且 period 已过(订阅 inactive 但仍可一键续订) — research/master-subscription-probe.md §1.2 |
| `三层探针` | (L1) `eligible_for_auto_reactivation` authoritative 主探针 / (L2) 新邀请子号 plan_type corroborating 反推 / (L3) `/billing` 401/404 + workspace `/settings.plan != "team"` safety net |

---

## 2. 完整数据契约

### 2.1 类型定义(置于 `src/autoteam/chatgpt_api.py` 末尾或新模块 `master_health.py` 顶部)

```python
from typing import Literal, Optional, TypedDict

MasterHealthReason = Literal[
    "active",                  # 健康 — 可继续生产子号
    "subscription_cancelled",  # eligible_for_auto_reactivation=True
    "workspace_missing",       # /accounts 找不到目标 workspace(实体已删 / account_id 漂移)
    "role_not_owner",          # current_user_role != account-owner / admin
    "auth_invalid",             # /accounts 401/403,master session 已失效
    "network_error",           # DNS / timeout / SSL / 5xx,需上层决定保守路径
]


class MasterHealthEvidence(TypedDict, total=False):
    """is_master_subscription_healthy 返回的 evidence dict 字段集。"""
    raw_account_item: dict        # /backend-api/accounts items[i] 完整原始记录(便于事后排查)
    http_status: int              # /backend-api/accounts 响应状态码
    probed_at: float              # epoch seconds,本次 probe 开始时间(非 cache 命中时间)
    cache_hit: bool               # 本次返回是否来自 5min cache
    cache_age_seconds: Optional[float]  # cache 命中时距上次实测时间;cache miss 为 None
    detail: Optional[str]         # 失败原因纯文本(network_error / workspace_missing 时填)
    items_count: Optional[int]    # /accounts 返回的 items[] 总数(workspace_missing 调试用)
    account_id: Optional[str]     # 实际比对使用的 master account_id
    current_user_role: Optional[str]  # role_not_owner 时记录原始字面量
    # —— 副探针字段(L2/L3 命中时填,主探针命中时缺省) ——
    plan_field: Optional[str]     # /accounts/{wid}/settings.plan(若 fetch 成功,L3)
    billing_status: Optional[int] # /backend-api/billing/* 探测的 HTTP 状态(L3 兜底)


# 函数返回类型
MasterHealthResult = tuple[bool, MasterHealthReason, MasterHealthEvidence]
```

### 2.2 函数签名(实施期目标)

```python
def is_master_subscription_healthy(
    chatgpt_api: "ChatGPTTeamAPI",
    *,
    account_id: Optional[str] = None,
    timeout: float = 10.0,
    cache_ttl: float = 300.0,
    force_refresh: bool = False,
) -> MasterHealthResult:
    """判定 master 母号 ChatGPT Team 订阅是否健康。

    三层探针(优先级从高到低):
      L1 (authoritative) — /backend-api/accounts items[].eligible_for_auto_reactivation
      L2 (corroborating) — 新邀请子号 OAuth bundle.plan_type 反推(由 _run_post_register_oauth 的
                            既有 plan_drift 路径承担,本函数 *不* 主动触发 invite,只读 cache 旁证)
      L3 (safety net)    — workspace /settings.plan + /billing 401/404 兜底(当 L1 字段缺失时)

    参数:
      chatgpt_api: 已 start() 的 ChatGPTTeamAPI 实例(主号 session)
      account_id: 目标 master workspace account_id;None → 内部从 get_chatgpt_account_id() 取
      timeout: 单次 HTTP 探测超时(秒)
      cache_ttl: 5 分钟内不重复探测;0 表示禁用 cache
      force_refresh: True 表示忽略 cache 强制实测(用于 /api/admin/master-health 手动刷新)

    返回:
      (healthy, reason, evidence)
      healthy True  仅当 reason == "active"
      healthy False 当 reason ∈ {subscription_cancelled, workspace_missing, role_not_owner, auth_invalid, network_error}

    不变量:
      - 函数永不抛异常(所有 Exception 转 network_error,与 check_codex_quota 对齐)
      - cache 命中不产生 HTTP 调用(cache_hit=True + raw_account_item 来自上次实测,
        cache_age_seconds 反映距上次实测的秒数)
      - cache miss 触发 1 次 GET /backend-api/accounts;不触发 invite / settings / billing(L3 仅
        在 L1 字段缺失时执行,且至多 1 次额外 HTTP)
    """
```

### 2.3 缓存落盘契约(`accounts/.master_health_cache.json`)

```json
{
  "schema_version": 1,
  "cache": {
    "<master_account_id_uuid>": {
      "healthy": false,
      "reason": "subscription_cancelled",
      "probed_at": 1777699200.0,
      "evidence": {
        "raw_account_item": { "...": "..." },
        "http_status": 200,
        "current_user_role": "account-owner"
      }
    }
  }
}
```

**字段约束**:

| 字段 | 类型 | 约束 |
|---|---|---|
| `schema_version` | int | 当前 1;未来 schema 不兼容时 +1,旧缓存读到不同版本时整体丢弃(treat as miss) |
| `cache` | dict[str, entry] | key 是 master account_id(UUID);允许多 master 共存(将来 E3 多母号扩展) |
| `entry.healthy` | bool | True/False,与函数返回 healthy 一致 |
| `entry.reason` | str | 6 个 MasterHealthReason 字面量之一 |
| `entry.probed_at` | float | 实测 epoch seconds;cache_age = `time.time() - probed_at` |
| `entry.evidence` | dict | 写盘前裁剪敏感字段(token / cookie 不入盘);默认仅写 `raw_account_item` 子集 + http_status |

**裁剪规则**:`raw_account_item` 落盘时只保留 `id / structure / current_user_role / eligible_for_auto_reactivation / name / workspace_name`,丢弃 OpenAI 后端可能附带的 token/email/seat 列表等(避免把 token 写入磁盘)。

---

## 3. 行为契约(三层探针执行规则)

### 3.1 前置条件

- `chatgpt_api` 必须已 `start()` 成功(主号 session cookie 可用);否则 L1 直接 `auth_invalid`
- `account_id` 优先级:函数参数 > `get_chatgpt_account_id()` > admin_state cookies / `.env` `CHATGPT_ACCOUNT_ID`
- 调用方在并发场景下不需要自己加锁 — 本函数内部读写 `.master_health_cache.json` 走 `accounts.json` 同款 file-lock

### 3.2 后置条件

- 返回元组永远 3 元素;第 2 元素必为 6 个 `MasterHealthReason` 字面量之一
- `healthy == True ⇔ reason == "active"`(双向蕴含,严格)
- cache 命中 → `evidence["cache_hit"] == True` + `cache_age_seconds` 不为 None;
  cache miss → `cache_hit` 缺省或 `False`,`cache_age_seconds` 为 None,且 `probed_at` 是本次实测时间
- 函数永不抛异常;任何 Exception 归为 `("network_error", evidence)` 返回

### 3.3 三层探针执行顺序

```
┌──────────────────────────────────────────────────────────────────┐
│ Step 0: cache lookup                                              │
│   if (not force_refresh) and cache_age < cache_ttl:               │
│     → 直接返回 cache 中的 (healthy, reason, evidence|cache_hit)    │
├──────────────────────────────────────────────────────────────────┤
│ Step 1: L1 主探针 — GET /backend-api/accounts                       │
│   ├─ 401/403            → ("auth_invalid", ..)                    │
│   ├─ 5xx / network      → ("network_error", ..)                   │
│   ├─ 200 + items[] 中找不到目标 account_id  → ("workspace_missing")  │
│   ├─ 200 + 找到目标 + role 不在 {account-owner, admin, org-admin,   │
│   │       workspace-owner}  → ("role_not_owner")                  │
│   ├─ 200 + 找到目标 + eligible_for_auto_reactivation == True       │
│   │       → ("subscription_cancelled")  ← 主判定命中                │
│   └─ 200 + 上述都不命中  → 进入 Step 2 L3 副判定                    │
├──────────────────────────────────────────────────────────────────┤
│ Step 2: L3 副判定 — workspace /settings.plan(可选,仅当 L1 字段缺失) │
│   背景:OpenAI 后端可能去除 eligible_for_auto_reactivation 字段(误判 │
│   缓解 §5.1 场景 A)。L3 在 L1 没拒判但又拿不到该字段时再确认一次:    │
│   ├─ GET /backend-api/accounts/{account_id}/settings              │
│   ├─ settings.plan ∉ {"team", "business", "enterprise"}           │
│   │       → ("subscription_cancelled", evidence with plan_field)  │
│   ├─ 字段缺失 / 200 但 plan 字段不存在  → 视作 active(不能反向认定)  │
│   └─ 401/403  → ("auth_invalid")                                   │
│   注:L3 不主动调 /backend-api/billing(老 API 已 410/redirect),由   │
│         研究 §2 表 1 确认;仅在未来字段失效时新增 1 处 HTTP。        │
├──────────────────────────────────────────────────────────────────┤
│ Step 3: 落盘 cache + 返回                                          │
│   write_cache({account_id, healthy, reason, probed_at, evidence})  │
│   return (healthy, reason, evidence)                                │
└──────────────────────────────────────────────────────────────────┘
```

### 3.4 异常映射

| 实际异常 | 归类 |
|---|---|
| `requests.exceptions.ConnectionError` | network_error |
| `requests.exceptions.Timeout` | network_error |
| `requests.exceptions.SSLError` | network_error |
| `requests.exceptions.RequestException`(其他) | network_error |
| HTTP 401 / 403 | auth_invalid |
| HTTP 5xx / 429 / 其他 4xx | network_error |
| JSON 解析失败 | network_error |
| 任何未识别 `Exception` 兜底 | network_error |

**对齐**:与 `check_codex_quota`(`shared/quota-classification.md §3.3`)异常映射对齐 — `auth_*` 与 `network_*` 必须严格区分,网络抖动绝不能落入 `auth_invalid`(否则上层会触发"主号重登"等破坏性流程)。

---

## 4. 触发位点矩阵(5 处)

| # | 文件:函数 | 同步/异步 | 失败行为 | 备注 |
|---|---|---|---|---|
| M-T1 | `manager.py:_run_post_register_oauth` 入口(`leave_workspace=True` personal 分支,~L1528 之前) | 同步 | `subscription_cancelled` → `record_failure(category="master_subscription_degraded", stage="run_post_register_oauth_personal_precheck")` + `update_account(email, status=STATUS_STANDBY)` + 不进 OAuth 流程 + `_record_outcome("master_degraded")` | **PRD-7 R1 入口** — fail-fast,避免浪费 2 分钟跑出 plan_drift |
| M-T2 | `manager.py:_run_post_register_oauth` Team 分支入口(`leave_workspace=False`,~L1462 之前) | 同步 | 同 M-T1,但 stage="run_post_register_oauth_team_precheck",且子号已 invite → 走 `_cleanup_team_leftover` 不直接 STANDBY | 母号降级时 Team invite 也会拿 `plan_type=free`(已实测 28 条 plan_drift),对称拦截 |
| M-T3 | `api.py:fill_team_task` / `fill_personal_task` 任务起点(`/api/tasks/fill` handler 入口) | 同步 | `subscription_cancelled` / `auth_invalid` → 直接 HTTP 503,body `{"error": "master_subscription_degraded", "reason": "<reason>", "evidence": <裁剪后 evidence>}` | 让前端直接显示告警横幅,而非等 2 分钟拿到失败 |
| M-T4 | `api.py:get_admin_diagnose`(`/api/admin/diagnose` 现有 4-probe 实现旁挂) | 同步 | 任何 reason 都返回(放在 response body 新增 `master_subscription_state` 字段) | 给 UI Settings 页面横幅数据;支持 `?force_refresh=1` query param |
| M-T5 | `manager.py:cmd_reconcile` 启动前(reconcile entry,~L161-471 入口) | 同步 | `subscription_cancelled` → reconcile 仅做"扫描不动作",日志告警 + 不执行 KICK / state 改写;其他不健康 reason → 跳过 reconcile 这一轮 | 防止母号降级时 reconcile 错误 KICK 健康账号(因为 wham 401 假阳性) |

**T6(可选,Round 8 不实施)**:`api.py` background task 定时器,每 5 分钟主动 probe 1 次推 UI banner 推送 — 留 E1 演进项,Round 8 暂不引入新后台任务。

### 4.1 触发顺序与依赖

```
用户点 fill (前端)
      │
      ▼
M-T3 api.fill_*_task 入口  ──── 母号降级 → 503 ──→ 前端横幅 (不进后端)
      │ healthy
      ▼
manager 启动
      │
      ├── Team 分支:M-T2 → 入 invite → OAuth → bundle 检查
      │
      └── personal 分支:M-T1 → 入 leave_workspace → OAuth (workspace/select)
                                                    └── 见 oauth-workspace-selection.md
```

---

## 5. 误判分析(5 个场景 + 缓解)

### 5.1 False Negative — 应判 degraded 却判 healthy

| # | 场景 | 风险 | 缓解 |
|---|---|---|---|
| FN-A | OpenAI 改字段名 / 移除 `eligible_for_auto_reactivation`(后端字段稳定性无 SLA,research §1.3 已列为开放问题) | 高 — 整套 fail-fast 失效,回到 28 条 plan_drift 状态 | (1) L3 副判定兜底:`/settings.plan != "team"` 也能命中;(2) `register_failures.json` `plan_drift` 持续观测,1-2 周内 0 命中视为字段失效预警 |
| FN-B | `/backend-api/accounts` 被 Cloudflare challenge / 5xx | 中 — `network_error` 时缺省**保守失败**(不视作 healthy) | 调用方按 §5.3 处置:`network_error` 不 fail-fast,记 warning + 走原 OAuth 流程,失败由现有 plan_drift 拦截 |
| FN-C | 字段返回 `null` 而非 `True`(OpenAI 字段为可空) | 低 — 严格按 `is True` 比对,`null/false/missing` 都视作 active | 仅当字段值严格 `True`(boolean)时判 degraded;实施代码必须用 `target.get("eligible_for_auto_reactivation") is True`,不能用 `target.get(...) or False` |
| FN-D | Master 续费 webhook 未到 OpenAI(用户刚续费 30 秒内) | 低 — 短暂窗口仍判 degraded | (1) cache_ttl 5min 让用户主动 force_refresh / `/api/admin/master-health?force_refresh=1`;(2) 失败横幅文案明确"如已续费请 1 分钟后刷新" |
| FN-E | 缓存窗口内 master 已降级 | 中 — cache 5min 内仍判 healthy | 保持 5min TTL 不延长(业务可接受 5min 延迟,延长 cache 反而扩大此窗口);加 force_refresh 入口 |

### 5.2 False Positive — 应判 healthy 却判 degraded

| # | 场景 | 风险 | 缓解 |
|---|---|---|---|
| FP-A | `eligible_for_auto_reactivation` 同时表示 trial / past_due(字段语义不止 cancel) | 中 — research §1.3 暂定为 cancel 强信号,但 OpenAI 未发布字段语义文档 | 实施期 1-2 周内 `register_failures` 抽样:degraded 命中且子号实际拿 `plan_type=team` 的反例 → 字段语义比预期宽,需补充判定条件(例如 `subscription_status` 字段) |
| FP-B | Master 暂时被踢出 owner role(权限漂移) | 低 — `role_not_owner` 单独分类,UI 提示重新接管 | reason 区分,UI 文案不同(degraded 是订阅问题,role_not_owner 是权限问题),用户能定位具体动作 |
| FP-C | 多 workspace 误取错 account(account_id 漂移) | 中 — `.env CHATGPT_ACCOUNT_ID` 与 admin_state 实际登录不一致(PRD §1 已观测) | strict 比对 `target.id == account_id` 字符串相等;不命中 → `workspace_missing`(明确分类,不混入 cancelled) |
| FP-D | cache 过期前 user 已手动 reactivate | 低 — 5min TTL 可接受 | 提供 `/api/admin/master-health?force_refresh=1` 入口,UI 横幅旁加"立即重测"按钮 |
| FP-E | 非 owner 角色不暴露该字段(research §7 Q4 未决) | 中 — 字段对 user/member 可能为空 → `null`,与 cancelled 字段缺失同形 | (1) 先判 role_not_owner;(2) FP-C 强相关 — 如果实测发现 owner 也读不到该字段,降级 L3 副判定 |

### 5.3 调用方推荐处置(对应 §4 触发位点)

```python
# 通用模板(M-T1 / M-T2)
healthy, reason, evidence = is_master_subscription_healthy(chatgpt_api)
if not healthy:
    if reason == "subscription_cancelled":
        # P0:fail-fast,不进 OAuth
        record_failure(
            email,
            category="master_subscription_degraded",
            reason=f"master {get_admin_email()} 订阅已取消(eligible_for_auto_reactivation=true)",
            stage=<stage 名,见 §4 矩阵>,
            master_account_id=evidence.get("raw_account_item", {}).get("id"),
            master_role=evidence.get("current_user_role"),
        )
        update_account(email, status=STATUS_STANDBY)
        _record_outcome("master_degraded", reason="master subscription cancelled")
        return None

    if reason in ("network_error", "auth_invalid"):
        # 保守路径:走原 OAuth,失败由现有 plan_drift / oauth_failed 兜底
        logger.warning("[注册] master health probe 不确定 (%s),按既有逻辑放行", reason)

    elif reason in ("workspace_missing", "role_not_owner"):
        # 中度异常:记录但放行(reconcile 会处理 workspace 漂移 / 权限掉线)
        logger.warning("[注册] master 异常 (%s, role=%s),仍尝试 OAuth",
                       reason, evidence.get("current_user_role"))
```

---

## 6. 缓存策略

### 6.1 TTL 选择理由

- **5 分钟 TTL**:research §4.3 + PRD-7 默认建议
  - master 订阅状态变更通常需要用户手动 cancel / reactivate,**不会高频抖动**
  - 5min 内若用户产生 4-6 个并发任务,可全部复用同一 cache,减少 API 调用噪声
  - 5min 也是 OpenAI 后端字段最终一致性的合理上限(research §1.3 推测)
- **不延长 TTL 的理由**:延长会扩大 FN-E 窗口(降级后仍误判 healthy)
- **不缩短 TTL 的理由**:`/backend-api/accounts` 本身 200ms,但 ChatGPTTeamAPI 上下文切换 + Cloudflare 等 ~3-8s,过短会拖慢 fill 链路

### 6.2 缓存失效时机(invalidation triggers)

| 时机 | 行为 |
|---|---|
| `cache_age >= cache_ttl` | 自然过期,下次 probe 实测 + 重写 cache |
| `force_refresh=True` 调用 | 忽略 cache,实测后重写 cache |
| `/api/admin/master-health?force_refresh=1` | 同上;UI Settings 页"立即重测"按钮触发 |
| schema_version 不一致 | 整体丢弃 cache 文件,作 miss 处理 |
| **不**触发失效 | 子号 OAuth 拿到 plan_drift / kick 单个子号 / reconcile 单轮 — 这些不是 master 订阅状态变化的可靠信号 |

### 6.3 并发安全

- 读写 `.master_health_cache.json` 走 `load_accounts / save_accounts` 同款 file-lock(避免并发 fill 任务读到半写状态)
- 不在内存维护 cache singleton(进程重启后从盘读起,与 admin_state 持久化机制对齐)

---

## 7. 不变量(Invariants)

- **M-I1**:`is_master_subscription_healthy` 永不抛异常(任何 Exception 归为 `network_error` 返回)
- **M-I2**:`auth_invalid` 与 `network_error` 严格区分;**401/403 是 auth_invalid 唯一来源**;5xx / Timeout / Connection 必落 network_error
- **M-I3**:`healthy == True ⇔ reason == "active"`(严格双向蕴含;任何代码路径不能让 `healthy=True` 配 `reason != "active"`,反之亦然)
- **M-I4**:cache 命中时**不发起任何 HTTP 调用**(L1 / L3 / billing 都不调);命中后 evidence 中 `cache_hit=True` + `cache_age_seconds is not None`
- **M-I5**:cache miss 时**最多发 2 次 HTTP**(L1 + 可选 L3);L2(invite 反推)由 OAuth 既有路径承担,本函数不主动 invite
- **M-I6**:落盘 evidence **不含** access_token / refresh_token / cookie / `__Secure-next-auth.session-token` 等敏感字段;只允许 `raw_account_item` 的白名单子集(§2.3 裁剪规则)
- **M-I7**:`eligible_for_auto_reactivation` 严格 `is True` 比对,**不**用 truthy 判断(防止 `null / "true" / 1` 等假信号触发误判)
- **M-I8**:M-T1 / M-T2 触发位点的 `record_failure` 必须使用 category=`master_subscription_degraded`(spec-2 v1.5 register_failures schema 新增枚举);`stage` 必须明确 OAuth 分支(team/personal),便于日后按分支统计命中率
- **M-I9**:reason=`subscription_cancelled` 是**唯一**触发 fail-fast 的分支;其他 5 个 reason 都走"保守放行 + 记录"路径(避免一次抖动让所有 fill 全瘫)
- **M-I10**:M-T5 reconcile 入口若 master 不健康,**禁止**执行 KICK / state flip 改写动作;只允许 read-only 扫描和日志输出 — 防止误踢真活号

---

## 8. 单元测试 fixture 与样本数据

### 8.1 `/backend-api/accounts` 响应样本

```json
// tests/fixtures/master_accounts_responses.json
{
  "active_team": {
    "status_code": 200,
    "body": {
      "items": [
        {
          "id": "b328bd37-aaaa-bbbb-cccc-16d08e98a0b5",
          "structure": "workspace",
          "current_user_role": "account-owner",
          "name": "Master Team",
          "workspace_name": "Master Team",
          "eligible_for_auto_reactivation": false
        },
        {
          "id": "personal-uuid-1111-2222-3333-444455556666",
          "structure": "personal",
          "current_user_role": "account-owner"
        }
      ]
    }
  },
  "subscription_cancelled": {
    "status_code": 200,
    "body": {
      "items": [
        {
          "id": "b328bd37-aaaa-bbbb-cccc-16d08e98a0b5",
          "structure": "workspace",
          "current_user_role": "account-owner",
          "name": "Master Team",
          "eligible_for_auto_reactivation": true
        }
      ]
    }
  },
  "field_missing_treat_as_active": {
    "status_code": 200,
    "body": {
      "items": [
        {
          "id": "b328bd37-aaaa-bbbb-cccc-16d08e98a0b5",
          "structure": "workspace",
          "current_user_role": "account-owner"
        }
      ]
    }
  },
  "field_null_treat_as_active": {
    "status_code": 200,
    "body": {
      "items": [
        {
          "id": "b328bd37-aaaa-bbbb-cccc-16d08e98a0b5",
          "structure": "workspace",
          "current_user_role": "account-owner",
          "eligible_for_auto_reactivation": null
        }
      ]
    }
  },
  "workspace_missing": {
    "status_code": 200,
    "body": {
      "items": [
        {
          "id": "different-uuid-not-master",
          "structure": "workspace",
          "current_user_role": "user"
        }
      ]
    }
  },
  "role_not_owner": {
    "status_code": 200,
    "body": {
      "items": [
        {
          "id": "b328bd37-aaaa-bbbb-cccc-16d08e98a0b5",
          "structure": "workspace",
          "current_user_role": "user"
        }
      ]
    }
  },
  "auth_invalid_401": {
    "status_code": 401,
    "body": {"error": {"code": "invalid_token"}}
  },
  "network_error_500": {
    "status_code": 500,
    "body": {"error": "Internal Server Error"}
  }
}
```

### 8.2 推荐单测代码

```python
# tests/unit/test_master_subscription_probe.py
import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from autoteam.chatgpt_api import is_master_subscription_healthy  # 或 master_health 模块

FIXTURE_PATH = Path("tests/fixtures/master_accounts_responses.json")
FIXTURE = json.loads(FIXTURE_PATH.read_text())
TARGET_WID = "b328bd37-aaaa-bbbb-cccc-16d08e98a0b5"


def _mock_chatgpt_api(sample_name: str):
    sample = FIXTURE[sample_name]
    api = MagicMock()
    api._api_fetch.return_value = {
        "status": sample["status_code"],
        "body": json.dumps(sample["body"]),
    }
    return api


@pytest.mark.parametrize("name,expected_healthy,expected_reason", [
    ("active_team",                   True,  "active"),
    ("subscription_cancelled",        False, "subscription_cancelled"),
    ("field_missing_treat_as_active", True,  "active"),  # FN-C 缓解
    ("field_null_treat_as_active",    True,  "active"),
    ("workspace_missing",             False, "workspace_missing"),
    ("role_not_owner",                False, "role_not_owner"),
    ("auth_invalid_401",              False, "auth_invalid"),
    ("network_error_500",             False, "network_error"),
])
def test_master_health_classification(name, expected_healthy, expected_reason, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # 隔离 cache 文件
    api = _mock_chatgpt_api(name)
    healthy, reason, evidence = is_master_subscription_healthy(
        api, account_id=TARGET_WID, cache_ttl=0,
    )
    assert healthy is expected_healthy
    assert reason == expected_reason
    if reason == "active":
        assert healthy is True
    else:
        assert healthy is False  # M-I3 双向蕴含


def test_cache_hit_no_http(tmp_path, monkeypatch):
    """M-I4:cache 命中不发起 HTTP."""
    monkeypatch.chdir(tmp_path)
    api = _mock_chatgpt_api("subscription_cancelled")
    healthy1, reason1, evidence1 = is_master_subscription_healthy(api, account_id=TARGET_WID)
    assert evidence1.get("cache_hit") is False
    api._api_fetch.reset_mock()
    healthy2, reason2, evidence2 = is_master_subscription_healthy(api, account_id=TARGET_WID)
    assert evidence2.get("cache_hit") is True
    assert evidence2.get("cache_age_seconds") is not None
    assert api._api_fetch.call_count == 0  # 关键不变量
    assert (healthy2, reason2) == (healthy1, reason1)


def test_force_refresh_bypasses_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = _mock_chatgpt_api("active_team")
    is_master_subscription_healthy(api, account_id=TARGET_WID)
    api._api_fetch.reset_mock()
    is_master_subscription_healthy(api, account_id=TARGET_WID, force_refresh=True)
    assert api._api_fetch.call_count == 1


def test_field_strict_is_true_only(tmp_path, monkeypatch):
    """M-I7:严格 `is True`,不接受 truthy."""
    monkeypatch.chdir(tmp_path)
    api = MagicMock()
    api._api_fetch.return_value = {
        "status": 200,
        "body": json.dumps({"items": [{
            "id": TARGET_WID, "structure": "workspace",
            "current_user_role": "account-owner",
            "eligible_for_auto_reactivation": "true",  # 字符串 truthy
        }]}),
    }
    healthy, reason, _ = is_master_subscription_healthy(api, account_id=TARGET_WID, cache_ttl=0)
    assert healthy is True and reason == "active"


def test_evidence_no_token_leak(tmp_path, monkeypatch):
    """M-I6:落盘 evidence 不含敏感字段."""
    monkeypatch.chdir(tmp_path)
    api = MagicMock()
    api._api_fetch.return_value = {
        "status": 200,
        "body": json.dumps({"items": [{
            "id": TARGET_WID, "structure": "workspace",
            "current_user_role": "account-owner",
            "eligible_for_auto_reactivation": True,
            "session_token": "SHOULD_NOT_PERSIST",
            "access_token": "SHOULD_NOT_PERSIST",
        }]}),
    }
    is_master_subscription_healthy(api, account_id=TARGET_WID, cache_ttl=300)
    cache_path = Path(".") / "accounts" / ".master_health_cache.json"
    if cache_path.exists():
        text = cache_path.read_text()
        assert "SHOULD_NOT_PERSIST" not in text
        assert "session_token" not in text
        assert "access_token" not in text
```

---

## 9. 与既有 spec / FR 的关系

| 关系对象 | 说明 |
|---|---|
| `spec-2 v1.5 §3.6` | 引用本 spec — 定义 master health 在 `_run_post_register_oauth` / `cmd_reconcile` / `api.fill_*` 的接入位置 |
| [`./oauth-workspace-selection.md`](./oauth-workspace-selection.md) | 互补 — master health=`active` 时,personal OAuth 走 workspace/select 主动选 personal;不健康时不进 OAuth |
| [`./quota-classification.md`](./quota-classification.md) | 异常映射对齐(`auth_*` / `network_*` 严格区分);本 spec 不复用其 5 分类(因为语义不同) |
| [`./plan-type-whitelist.md`](./plan-type-whitelist.md) | L2 反推路径相关 — bundle.plan_type=`free` 是母号降级的旁证,但反推由 plan_drift 路径承担,本函数不主动 invite |
| [`./account-state-machine.md`](./account-state-machine.md) | M-T1 / M-T2 处置使用 `STATUS_STANDBY`(子号回 standby 等用户处理),不引入新状态 |
| `register_failures.json schema` | 新增 category=`master_subscription_degraded`(spec-2 v1.5 RegisterFailureRecord enum 扩) |

---

## 10. 参考资料

### 10.1 内部研究

- `.trellis/tasks/04-27-master-team-degrade-oauth-rejoin/research/master-subscription-probe.md`
  - §1.1-1.2 字段所属 / 语义分析(Stripe 对照)
  - §2 可用 API 端点矩阵(★★★★★ 主探针选择依据)
  - §3 反推法(L2 副判定语义)
  - §4 推荐探针方案(本 spec 函数签名直接源自此处)
  - §5 误判分析(本 spec §5 的来源)
  - §7 后续未决(本 spec §5.1 FN-A / FP-A 的开放问题对应)

### 10.2 内部代码引用(实施期目标位置)

- `src/autoteam/api.py:927-995` — `/api/admin/diagnose` 现有 4-probe 实现,本 spec M-T4 在此扩展
- `src/autoteam/chatgpt_api.py:920-952` — `_list_real_workspaces` 已读 `items[*].structure / current_user_role`,本 spec 函数复用相同 `_api_fetch`
- `src/autoteam/chatgpt_api.py:1097-1124` — admin role 选择逻辑(role 白名单参考)
- `src/autoteam/chatgpt_api.py:1277-1302` — `_api_fetch` 通用封装
- `src/autoteam/manager.py:1513-1656` — `_run_post_register_oauth` 全流程(M-T1 / M-T2 接入点)
- `src/autoteam/manager.py:161-471` — reconcile 入口(M-T5)

### 10.3 OpenAI 官方源(交叉验证)

- `openai/codex codex-rs/protocol/src/auth.rs` — `KnownPlan` 枚举(`Team / Free / ...`)
- `openai/codex codex-rs/login/src/token_data.rs` — `IdTokenInfo` JWT claims(`chatgpt_plan_type` 字段)

### 10.4 Stripe 字段对照

- Stripe `Subscription.cancel_at_period_end` / `status="canceled"` — 与 `eligible_for_auto_reactivation: true` 语义最近似映射
- 文档:<https://docs.stripe.com/api/subscriptions/object>

---

**文档结束。** 工程师据此可直接编写 `is_master_subscription_healthy` 函数 + 5 处接入 + 单测,无需额外决策。

---

## 附录 A:修订记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-04-27 Round 8 | 初版 — 三层探针(L1 主 / L2 反推 / L3 副)+ 5 触发位点(M-T1~T5)+ 5min cache + 5 误判缓解(FN-A~E + FP-A~E)+ 10 不变量(M-I1~I10)。源自 `.trellis/tasks/04-27-master-team-degrade-oauth-rejoin/research/master-subscription-probe.md` §1-§7。配套 PRD-7 Approach A R1 母号订阅探针落地。 |
