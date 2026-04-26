# Shared SPEC: 账号状态机

## 0. 元数据 + 引用方

| 字段 | 内容 |
|---|---|
| 名称 | 账号 7 状态完整状态机与转移规则 |
| 版本 | v1.1 (2026-04-26 Round 7 P2 follow-up — Round 6 落地的 cheap_codex_smoke / uninitialized_seat / AUTH_INVALID 短路语义同步) |
| 主题归属 | `accounts.py` STATUS_* 常量 + 各转移点的触发函数 + 不变量 + uninitialized_seat 中间态(Round 6 引入) |
| 引用方 | PRD-2 / PRD-5 / PRD-6 / spec-2-account-lifecycle.md / FR-D1~D4、FR-E1~E4、FR-H1~H3、FR-P0、FR-P1.2 / FR-P1.4 / FR-D6 / FR-D8 |
| 共因 | synthesis §1 共因 D、E + Issue#6 |
| 不在范围 | seat_type 字段(见 PRD-2 FR-F1~F6)、cpa_sync 同步细节(参考 `cpa_sync.py`) |

---

## 1. 概念定义

| 术语 | 定义 |
|---|---|
| `account state` | 落盘到 `accounts.json` 的 `status` 字段,7 个枚举之一 |
| `transition` | 由代码事件(同步/注册/踢出/探测)触发的状态变化 |
| `terminal state` | 没有自动转出 transition 的状态,需要人工介入或 reconcile 接管 |
| 被踢(kicked) | 在 ChatGPT Team 后端不可见但本地仍 active 的账号;wham/usage 401/403 时确认 |
| 自然待机(natural standby) | 账号 quota 耗尽后被本系统主动 kick,等待 5h reset 自然恢复 |

---

## 2. 完整数据契约

### 2.1 状态枚举(已存在,在 `accounts.py:13-20`)

```python
# src/autoteam/accounts.py 已有:
STATUS_ACTIVE = "active"            # 在 team 中,额度可用
STATUS_EXHAUSTED = "exhausted"      # 在 team 中,额度用完
STATUS_STANDBY = "standby"          # 已移出 team,等待额度恢复
STATUS_PENDING = "pending"          # 已邀请,等待注册完成
STATUS_PERSONAL = "personal"        # 已主动退出 team,走个人号 Codex OAuth
STATUS_AUTH_INVALID = "auth_invalid" # auth_file token 已不可用,待 reconcile 清理或重登
STATUS_ORPHAN = "orphan"            # 在 workspace 占席位但本地无 auth_file
```

### 2.2 Pydantic AccountRecord(完整契约,本 spec 引入)

```python
from typing import Literal, Optional
from pydantic import BaseModel, Field

AccountStatus = Literal[
    "active", "exhausted", "standby", "pending", "personal", "auth_invalid", "orphan",
]
SeatType = Literal["chatgpt", "codex", "unknown"]


class QuotaSnapshot(BaseModel):
    """详见 ./quota-classification.md §2.1"""
    primary_pct: int = 0
    primary_resets_at: int = 0
    primary_total: Optional[int] = None
    primary_remaining: Optional[int] = None
    weekly_pct: int = 0
    weekly_resets_at: int = 0


class AccountRecord(BaseModel):
    email: str
    password: str
    cloudmail_account_id: Optional[str] = None
    status: AccountStatus
    seat_type: SeatType = "unknown"
    workspace_account_id: Optional[str] = None
    auth_file: Optional[str] = None
    quota_exhausted_at: Optional[float] = None
    quota_resets_at: Optional[float] = None
    last_quota_check_at: Optional[float] = None  # FR-E3 探测去重
    last_quota: Optional[QuotaSnapshot] = None
    last_active_at: Optional[float] = None
    created_at: float
    plan_supported: Optional[bool] = None        # 新增,见 ./plan-type-whitelist.md
    plan_type_raw: Optional[str] = None          # 新增,记录原始 OAuth 字面量
    last_kicked_at: Optional[float] = None       # 新增,被踢识别时间戳;reconcile 用
```

### 2.3 状态-字段不变量

| 状态 | 必备字段 | 禁用字段 |
|---|---|---|
| `pending` | `email`, `password`, `created_at` | `auth_file` 必须为 None(注册未完成) |
| `active` | `email`, `auth_file`(非 None) | — |
| `exhausted` | `email`, `auth_file`, `quota_exhausted_at`, `quota_resets_at` | — |
| `standby` | `email` | — |
| `personal` | `email`, `auth_file`(非 None,personal 专属 plan_type=free) | — |
| `auth_invalid` | `email` | — |
| `orphan` | `email`(在 workspace 占席位) | `auth_file` 必须为 None |

---

## 3. 状态机图(ASCII)

### 3.1 完整状态机

```
                            ┌──────────────┐
                            │   PENDING    │   注册中,等收邮件
                            └──────┬───────┘
                                   │
                  注册成功+OAuth+quota ok
                                   │
                                   ▼
       ┌──────────────────────────┐
       │         ACTIVE           │ ◄────┐  reinvite 验证 ok
       │   在 Team,可调 Codex      │      │
       └──┬───────────────────────┘      │
          │                              │
   quota  │ 100%      ┌──────────────────┤
          ▼           │                  │
   ┌──────────┐  本系统kick    ┌────────────┐
   │EXHAUSTED │ ────────────► │  STANDBY   │ ◄── reinvite plan!=team(旧路径)
   │ (lock 5h)│               │ 等待 reset  │     (新路径推 AUTH_INVALID)
   └────┬─────┘               └────┬───────┘
        │                          │
   reset│ 5h 后 reinvite           │ reinvite 验证
        └──────────────────────────┴────────►  返 ACTIVE
                                              或 fall through
                                              (见下方 fail_reason 分支)
                                              
                            ┌─────────────┐
   人工 leave_workspace ──► │  PERSONAL   │ (终态,不参与 Team 轮转)
                            └─────────────┘
                            
   sync 探测 wham 401/403  ┌────────────────┐    reconcile.KICK
   ──────────────────────► │ AUTH_INVALID   │ ──────────────────► 删本地或保留人工介入
   注册收尾 wham no_quota  │ token 已失效    │
   reinvite plan_drift    │                │
   add-phone 命中(Team)   └────────────────┘
   
   _reconcile 发现 ghost  ┌──────────┐    人工 KICK
   ──────────────────────► │  ORPHAN  │ ──────────────────► 删
                          └──────────┘
```

### 3.2 状态分类

| 类别 | 状态 |
|---|---|
| 工作池(轮转参与) | `active`、`exhausted`、`standby`、`pending` |
| 终态(不参与轮转) | `personal`、`auth_invalid`、`orphan` |

### 3.3 uninitialized_seat 中间态(Round 6 引入,Round 7 文档同步)

**语义**:`uninitialized_seat` **不是** 7 个 status 枚举之一,而是 STATUS_ACTIVE / STATUS_PENDING 的"待验证"子态。当 wham/usage 返回半空载形态(`primary_total=null + reset_at>0`)时,`check_codex_quota` 标记 `quota_info.window=="uninitialized_seat"` + `needs_codex_smoke=True`,在内部完成 cheap_codex_smoke 二次验证之前,**该账号在状态机层面仍记为原 status**(PENDING 或 ACTIVE)。

**生命周期**:

```
PENDING (or ACTIVE)
  │
  │ wham/usage 200 + primary_total=null + reset>0 命中 I5
  │ → check_codex_quota 内部判定 uninitialized_seat,准备 smoke
  │
  ├─ 24h cache 命中 (Round 7 FR-D6)
  │   ├─ cached="alive"        → ("ok", quota_info[smoke_cache_hit=True]) → STATUS_ACTIVE
  │   ├─ cached="auth_invalid" → ("auth_error", None) → STATUS_AUTH_INVALID
  │   └─ cached="uncertain"    → ("network_error", None) → 保留原 status
  │
  └─ cache miss → cheap_codex_smoke 网络调用
      ├─ "alive"        → ("ok", quota_info[smoke_verified=True]) → STATUS_ACTIVE,落 last_codex_smoke_at + last_smoke_result
      ├─ "auth_invalid" → ("auth_error", None) → STATUS_AUTH_INVALID,同上落盘
      └─ "uncertain"    → ("network_error", None) → 保留原 status,同上落盘
```

**不变量保证**:
- 账号永远不会停留在 "uninitialized_seat" 中间态超过一次 check_codex_quota 调用周期;调用结束时必有 5 分类 status 之一返回
- `last_codex_smoke_at` / `last_smoke_result` 字段反映最近一次实际 smoke 网络调用,用于 24h 去重
- `quota_info.smoke_verified` / `quota_info.last_smoke_result` 反映本次 quota check 的结果(可能是 cache 命中复用)
- 详见 `./quota-classification.md §4.4 / I8 / I9`

---

## 4. 状态/分类规则

### 4.1 触发函数 → 转移矩阵

| 触发函数 | 文件:行号 | 触发条件 | from → to |
|---|---|---|---|
| `add_account` | `accounts.py:58` | 新增账号(invite 成功) | (none) → PENDING |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle ok + quota ok(经 cheap_codex_smoke 24h cache 验证或 alive,Round 6/7) | PENDING → ACTIVE |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle ok + quota exhausted(新) | PENDING → EXHAUSTED |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle ok + quota no_quota(新) | PENDING → AUTH_INVALID |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle ok + quota auth_error(新)| PENDING → AUTH_INVALID |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle ok + uninitialized_seat 中间态 + cheap_codex_smoke=auth_invalid(Round 6 FR-P0)| PENDING → AUTH_INVALID(经 check_codex_quota 内部消化为 auth_error 路径)|
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle ok + uninitialized_seat 中间态 + cheap_codex_smoke=uncertain(Round 6 FR-P0)| PENDING → ACTIVE(保留原状态,由下轮 sync 校准;经 check_codex_quota 内部转 network_error)|
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle 但 plan_supported=False(新) | PENDING → AUTH_INVALID |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle 失败但已 invite | PENDING → ACTIVE(team_auth_missing,旧行为保留) |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | RegisterBlocked(is_phone=True)(新) | PENDING → AUTH_INVALID |
| `_run_post_register_oauth` personal 分支 | `manager.py:1431` | bundle ok + plan=free | PENDING → PERSONAL |
| `_run_post_register_oauth` personal 分支 | `manager.py:1431` | bundle 失败 / plan != free | PENDING → deleted(record_failure) |
| `_run_post_register_oauth` personal 分支 | `manager.py:1431` | RegisterBlocked(is_phone=True)(新) | PENDING → deleted + record_failure |
| `sync_account_states` | `manager.py:520` | active 在 Team 中(同步成功) | ACTIVE → ACTIVE(无变化) |
| `sync_account_states` | `manager.py:520` | standby/pending 在 Team 中 | STANDBY/PENDING → ACTIVE |
| `sync_account_states` | `manager.py:540` | active 不在 Team + workspace_account_id 不一致 | ACTIVE → ACTIVE(母号切换守卫,旧行为保留) |
| `sync_account_states` | `manager.py:540`(新) | active 不在 Team + wham 401/403 | ACTIVE → AUTH_INVALID |
| `sync_account_states` | `manager.py:540`(Round 6 FR-P0)| active 在 Team + wham uninitialized_seat + cheap_codex_smoke=auth_invalid | ACTIVE → AUTH_INVALID(短路 check_codex_quota 内部消化路径) |
| `sync_account_states` | `manager.py:540`(新) | active 不在 Team + wham ok / network_error | ACTIVE → STANDBY(自然待机,保留旧行为) |
| `cmd_check` quota 探测 | `manager.py:715/748/760` | wham 状态变化 | active → exhausted/auth_invalid/standby |
| `reinvite_account` | `manager.py:2466` | OAuth 成功 + plan=team + quota verified | STANDBY → ACTIVE |
| `reinvite_account` | `manager.py:2466`(新) | OAuth 成功 + plan != team / plan_supported=False | STANDBY → AUTH_INVALID(plan_drift) |
| `reinvite_account` | `manager.py:2466`(新) | OAuth 失败 RegisterBlocked(is_phone=True) | STANDBY → AUTH_INVALID(oauth_phone_blocked) |
| `reinvite_account` | `manager.py:2466`(旧) | OAuth 成功但 quota_low / exhausted | STANDBY → STANDBY(锁 5h) |
| `reinvite_account` | `manager.py:2466`(旧) | OAuth 成功但 quota auth_error / network_error / exception | STANDBY → STANDBY(不锁 5h) |
| `reinvite_account` | `manager.py:2466`(旧) | OAuth 失败 bundle=None | STANDBY → STANDBY(_cleanup_team_leftover) |
| `_reconcile_team_members` | `manager.py:312/339` | 发现 ghost / orphan 错位 | ACTIVE → AUTH_INVALID |
| `_reconcile_team_members` | `manager.py:312/339` | workspace 占席位但本地无 auth_file | (任意) → ORPHAN |
| `_replace_single` kick | `manager.py:2626` | 主动定点替换 | active → STANDBY |
| `delete_managed_account` | `account_ops.py:40` | 用户单点 / 批量删除 | (任意) → deleted |
| `manual_account._finalize_account` | `manual_account.py:227` | 用户粘贴 OAuth callback | (none) → STANDBY(team) / ACTIVE(team+plus 之外) / AUTH_INVALID(plan_unsupported,新) |

### 4.2 "被踢" vs "自然待机" 识别规则(FR-E1)

```
sync_account_states 看到 acc.status == ACTIVE 且 email 不在当前 workspace_team_emails:
  ├─ acc.workspace_account_id ≠ 当前 account_id → 母号切换遗留,保留 ACTIVE(旧行为)
  ├─ acc.auth_file 存在 → 用 access_token 调一次 wham/usage(并发限制 5,超时 5s)
  │   ├─ ("auth_error", _) → STATUS_AUTH_INVALID + last_kicked_at=now  ★被踢
  │   ├─ ("ok", info) → STATUS_STANDBY + last_quota=info  ★自然待机(罕见,可能 OpenAI 缓存延迟)
  │   ├─ ("exhausted", info) → STATUS_STANDBY + quota_exhausted_at  ★自然待机
  │   ├─ ("no_quota", info) → STATUS_AUTH_INVALID(无配额且不在 Team,不会自动恢复)
  │   └─ ("network_error", _) → 保持 ACTIVE 等下轮(避免抖动误标)
  └─ acc.auth_file 缺失 → STATUS_STANDBY(降级,无法验证)
```

### 4.3a 删除链短路(Round 6 FR-P1.2 / FR-P1.4 落地,Round 7 文档同步)

**语义**:`STATUS_AUTH_INVALID` 与 `STATUS_PERSONAL` 在删除链中**等价处置** — 都跳过 ChatGPTTeamAPI 远端同步,直接走本地清理。

**触发条件**:

| 触发函数 | 文件:行号 | 条件 | 行为 |
|---|---|---|---|
| `account_ops.delete_managed_account` | `account_ops.py:79` | `acc.status in (STATUS_PERSONAL, STATUS_AUTH_INVALID)` | short_circuit=True,跳过 fetch_team_state / 不实例化 ChatGPTTeamAPI |
| `api.delete_accounts_batch` | `api.py:1582` | `bool(targets) and all(a.status in (PERSONAL, AUTH_INVALID) for a in targets)` | all_local_only=True,整批不启动 ChatGPTTeamAPI |
| `api.delete_account` 单点 | 复用 `delete_managed_account` | 同 §4.3a 第 1 行 | 同上 |

**理由**:
- AUTH_INVALID 账号的 token 已 401,继续走 fetch_team_state 也很可能 401 拖累整个删除流程
- 主号 session 失效场景下,启动 ChatGPTTeamAPI 会卡死 30s
- 删除 AUTH_INVALID 不需要远端 KICK(reconcile 已经 KICK 过或正在排队),只需清本地 records / auth_file
- PERSONAL 账号已 leave_workspace,远端席位早已不存在

**单测覆盖**:`tests/unit/test_round6_patches.py`:
- `test_auth_invalid_short_circuit_skips_fetch_team_state`(FR-P1.2)
- `test_auth_invalid_short_circuit_does_not_start_chatgpt_api`(FR-P1.2)
- `test_all_personal_short_circuit_skips_chatgpt_api_start`(FR-P1.4)

### 4.3 reinvite_account fail_reason 分支(扩 FR-H1)

```
reinvite_account 拿到 bundle 后:
  ├─ bundle == None
  │   ├─ 由 RegisterBlocked(is_phone=True) 引发 → STATUS_AUTH_INVALID(新,FR-C3)
  │   └─ 其他 → _cleanup_team_leftover("no_bundle") + STATUS_STANDBY(旧)
  ├─ plan_supported == False → STATUS_AUTH_INVALID + record_failure("plan_unsupported")(新)
  ├─ plan_type != "team" → STATUS_AUTH_INVALID + record_failure("plan_drift")(新,替代旧 STATUS_STANDBY)
  └─ plan_type == "team":
      ├─ quota verified ok → STATUS_ACTIVE(旧)
      ├─ quota fail_reason in (exhausted, quota_low) → STATUS_STANDBY + 锁 5h(旧)
      └─ quota fail_reason in (auth_error, network_error, exception) → STATUS_STANDBY 不锁 5h(旧)
```

### 4.4 反向不变量

| 不变量 | 说明 |
|---|---|
| `auth_file 存在 ⇒ status ∈ {active, exhausted, standby_with_token, personal, auth_invalid}` | orphan / pending 必无 auth_file |
| `status == personal ⇒ plan_type_raw == "free"` | personal 路径强校验(`codex_auth.py:920-930`) |
| `status == active ⇒ workspace_account_id 与当前一致 OR workspace_account_id is None` | sync 守卫(`manager.py:531-538`) |
| `last_kicked_at != None ⇒ status in {auth_invalid, deleted}` | 被踢标记的语义边界 |

---

## 5. 调用方处置规范(状态消费方)

### 5.1 工作池筛选

```python
# accounts.py 已存在
def get_active_accounts():
    """status == active 且非主号"""
    return [a for a in load_accounts() if a["status"] == STATUS_ACTIVE]


def get_personal_accounts():
    """status == personal 且非主号"""


def get_standby_accounts():
    """status == standby,按 quota_recovered 排序"""


# 新增推荐(便于 UI 与 reconcile)
def get_terminal_accounts():
    """auth_invalid + orphan,需要人工介入或 reconcile 清理"""
    return [a for a in load_accounts()
            if a["status"] in (STATUS_AUTH_INVALID, STATUS_ORPHAN)]
```

### 5.2 reconcile 处置(`manager.py:_reconcile_team_members` 旧 + 新)

| 输入状态 | reconcile 行为 |
|---|---|
| `auth_invalid` + 在 workspace 占席位 | KICK + 保留本地记录 + 等用户/批量删除 |
| `auth_invalid` + 不在 workspace | 保留本地记录(已自然清理),等用户决定是否删 |
| `orphan` + 在 workspace 占席位 | KICK |
| 其他状态 | 沿用现有处理 |

### 5.3 UI 显示规范

| 状态 | UI 文案 | 操作按钮 |
|---|---|---|
| `active` | 工作中 | 强制下线 / 删除 |
| `exhausted` | 已耗尽,X 小时后恢复 | 删除(灰显:auto check 已锁) |
| `standby` | 待机中(quota 已恢复 / 未恢复) | 立即重用 / 删除 |
| `pending` | 注册中... | (无) |
| `personal` | 个人 free 号 | 删除(短路 fetch_team_state,见 PRD-2 FR-G1) |
| `auth_invalid` | **token 失效,已退出 Team** | 删除(短路 fetch_team_state) |
| `orphan` | **席位异常,等待清理** | KICK + 删除 |

---

## 6. 单元测试 fixture 与样本数据

### 6.1 状态机 transition 表(yaml)

```yaml
# tests/fixtures/state_transitions.yaml
- name: register_team_success
  from: pending
  trigger: _run_post_register_oauth(leave_workspace=False, bundle.plan="team", quota="ok")
  to: active
  expected_fields:
    auth_file: not_null
    seat_type: chatgpt | codex
    last_active_at: not_null

- name: register_team_quota_no_quota
  from: pending
  trigger: _run_post_register_oauth(leave_workspace=False, bundle.plan="team", quota="no_quota")
  to: auth_invalid
  expected_register_failure:
    category: no_quota_assigned

- name: register_team_plan_unsupported
  from: pending
  trigger: _run_post_register_oauth(bundle.plan_supported=False)
  to: auth_invalid
  expected_register_failure:
    category: plan_unsupported

- name: register_team_phone_blocked
  from: pending
  trigger: _run_post_register_oauth raises RegisterBlocked(is_phone=True)
  to: auth_invalid
  expected_register_failure:
    category: oauth_phone_blocked
    stage: run_post_register_oauth_team

- name: sync_active_kicked_by_admin
  from: active
  trigger: sync_account_states(not in_team) + wham 401
  to: auth_invalid
  expected_fields:
    last_kicked_at: not_null

- name: sync_active_natural_standby
  from: active
  trigger: sync_account_states(not in_team) + wham exhausted
  to: standby

- name: sync_active_workspace_drift
  from: active
  trigger: sync_account_states(not in_team) + workspace_account_id 不一致
  to: active  # 保留(母号切换遗留)

- name: reinvite_plan_drift
  from: standby
  trigger: reinvite_account(bundle.plan="free")
  to: auth_invalid
  expected_register_failure:
    category: plan_drift

- name: reinvite_plan_unsupported
  from: standby
  trigger: reinvite_account(bundle.plan_supported=False)
  to: auth_invalid
  expected_register_failure:
    category: plan_unsupported

- name: reinvite_phone_blocked
  from: standby
  trigger: reinvite_account raises RegisterBlocked(is_phone=True)
  to: auth_invalid
  expected_register_failure:
    category: oauth_phone_blocked
    stage: reinvite_account

- name: reinvite_team_quota_low
  from: standby
  trigger: reinvite_account(bundle.plan="team", quota="ok" but pct<threshold)
  to: standby  # 锁 5h(旧行为)
  expected_fields:
    quota_exhausted_at: not_null

- name: reinvite_team_success
  from: standby
  trigger: reinvite_account(bundle.plan="team", quota verified)
  to: active
```

### 6.2 单测代码

```python
# tests/unit/test_state_machine.py
import pytest
import yaml
from pathlib import Path
from autoteam.accounts import (
    STATUS_ACTIVE, STATUS_STANDBY, STATUS_PENDING, STATUS_AUTH_INVALID,
    STATUS_PERSONAL, STATUS_ORPHAN, STATUS_EXHAUSTED, AccountRecord,
)

TRANSITIONS = yaml.safe_load(Path("tests/fixtures/state_transitions.yaml").read_text())


@pytest.mark.parametrize("case", TRANSITIONS)
def test_state_transition(case, mock_factory):
    """每个 transition 跑一遍,验证 from / to / 失败记录字段"""
    acc = mock_factory.account(status=case["from"])
    mock_factory.fire_trigger(case["trigger"], acc)
    final = mock_factory.reload(acc.email)
    assert final.status == case["to"]
    if "expected_register_failure" in case:
        rec = mock_factory.last_failure(acc.email)
        for k, v in case["expected_register_failure"].items():
            assert rec[k] == v


def test_state_field_invariants():
    """每个 status 的 必备/禁用 字段不变量"""
    cases = [
        # (status, must_have_keys, must_be_none_keys)
        (STATUS_PENDING, ["email", "password", "created_at"], ["auth_file"]),
        (STATUS_ACTIVE, ["email", "auth_file"], []),
        (STATUS_EXHAUSTED, ["email", "auth_file", "quota_exhausted_at", "quota_resets_at"], []),
        (STATUS_PERSONAL, ["email", "auth_file"], []),
        (STATUS_ORPHAN, ["email"], ["auth_file"]),
    ]
    for status, must_have, must_none in cases:
        acc = AccountRecord(...)  # 用工厂构造
        for k in must_have:
            assert getattr(acc, k) is not None
        for k in must_none:
            assert getattr(acc, k) is None


def test_pydantic_account_record_round_trip():
    """JSON 序列化 / 反序列化后字段不丢失"""
    acc = AccountRecord(
        email="t@example.com", password="x", status="active",
        seat_type="codex", auth_file="/auths/codex-t.json",
        plan_supported=True, plan_type_raw="team",
        created_at=1714000000.0,
    )
    js = acc.model_dump_json()
    acc2 = AccountRecord.model_validate_json(js)
    assert acc2.status == "active"
    assert acc2.plan_supported is True
```

### 6.3 完整 accounts.json 样本

```json
[
  {
    "email": "alice-team@example.com",
    "password": "abc",
    "cloudmail_account_id": "cm-1",
    "status": "active",
    "seat_type": "chatgpt",
    "workspace_account_id": "ws-100",
    "auth_file": "/abs/auths/codex-alice-team-team-deadbeef.json",
    "quota_exhausted_at": null,
    "quota_resets_at": null,
    "last_quota_check_at": 1714050000.0,
    "last_quota": {
      "primary_pct": 35,
      "primary_resets_at": 1714060000,
      "primary_total": 100,
      "primary_remaining": 65,
      "weekly_pct": 10,
      "weekly_resets_at": 1714600000
    },
    "last_active_at": 1714050000.0,
    "created_at": 1714000000.0,
    "plan_supported": true,
    "plan_type_raw": "team",
    "last_kicked_at": null
  },
  {
    "email": "bob-self-serve@example.com",
    "password": "xyz",
    "cloudmail_account_id": "cm-2",
    "status": "auth_invalid",
    "seat_type": "codex",
    "workspace_account_id": "ws-100",
    "auth_file": null,
    "quota_exhausted_at": null,
    "quota_resets_at": null,
    "last_quota_check_at": null,
    "last_quota": null,
    "last_active_at": null,
    "created_at": 1714000000.0,
    "plan_supported": false,
    "plan_type_raw": "self_serve_business_usage_based",
    "last_kicked_at": null
  },
  {
    "email": "charlie-kicked@example.com",
    "password": "def",
    "cloudmail_account_id": "cm-3",
    "status": "auth_invalid",
    "seat_type": "chatgpt",
    "workspace_account_id": "ws-100",
    "auth_file": "/abs/auths/codex-charlie-team-team-cafe1234.json",
    "quota_exhausted_at": null,
    "quota_resets_at": null,
    "last_quota_check_at": 1714050000.0,
    "last_quota": null,
    "last_active_at": 1714045000.0,
    "created_at": 1714000000.0,
    "plan_supported": true,
    "plan_type_raw": "team",
    "last_kicked_at": 1714050000.0
  }
]
```

---

## 7. 不变量(Invariants)

- **I1**:任何状态变更必须经过 `update_account` 入口,**禁止**直接 dict 改写后 `save_accounts`(避免漏触发持久化)
- **I2**:`STATUS_ACTIVE` 必须有 `auth_file`(注册收尾或 reinvite 验证后写入);任何把 active 设回但不写 auth_file 的代码都是 bug
- **I3**:`STATUS_AUTH_INVALID` 不能有 `quota_exhausted_at`(因为不会自然恢复);写时必须清空
- **I4**:`STATUS_PERSONAL` 是终态,不能回到 active(转换路径只有 manual_account 重新注册或新 OAuth bundle)
- **I5**:`STATUS_ORPHAN` 必须 `auth_file == None`(定义:占席位但本地无凭证);`auth_file` 存在的应判 active / standby / auth_invalid
- **I6**:`last_kicked_at` 字段一旦写入,后续状态转移不能清掉(用于 reconcile 历史回放;只在 delete_account 时随记录一起删)
- **I7**:reconcile_anomalies(`manager.py:161-471`)对 `auth_invalid` 的 KICK 行为必须保持幂等(重复 KICK 不抛异常,`kick_status="already_absent"` 视为成功)
- **I8**:状态白名单变更(新增枚举)需要全局检查 4 处:`Dashboard.vue` statusClass / `cpa_sync.py` 同步规则 / `sync_account_states` 处置 / 本 spec §4.2
- **I9**(Round 6 落地,Round 7 文档同步):**add-phone 探针必须接入 7 处**(invite 4 + OAuth 3,Round 6 P1.1 后 OAuth 扩为 4 处,合计 8 处)。具体清单:
  - **invite 阶段 4 处**(`invite.py:247/282/364/446`):`invite_filling`、`invite_confirm`、`invite_pre_submit`、`invite_post_submit`
  - **OAuth 阶段 4 处**(`codex_auth.py:586/638/910/939`,Round 6 加 C-P4):`oauth_about_you`(C-P1)、`oauth_consent_{step}`(C-P2)、`oauth_callback_wait`(C-P3)、`oauth_personal_check`(C-P4,Round 6 PRD-5 FR-P1.1 引入)
  - 任一探针缺失即视为 bug;新增 personal/team 邀请路径必须复用 `assert_not_blocked` + `RegisterBlocked` 复用语义
  - 详见 `./add-phone-detection.md §4.1` 探针接入清单

---

## 附录 A:状态机变更历史

| 版本 | 时间 | 变更 |
|---|---|---|
| v0.1 | round-1 | 初始 4 状态(active/exhausted/standby/pending) |
| v0.2 | round-2 | 加 personal |
| v0.3 | round-3 | 加 auth_invalid + orphan(commit cf2f7d3) |
| v1.0 | 2026-04-26 PRD-2 | 加 last_kicked_at / plan_supported / plan_type_raw 字段;补全转移规则;不新增 STATUS_PHONE_REQUIRED(复用 auth_invalid + register_failures) |
| v1.1 | 2026-04-26 Round 7 P2 follow-up | (1) §3.3 加 uninitialized_seat 中间态(Round 6 引入,STATUS_ACTIVE/PENDING 的"待验证"子态,经 cheap_codex_smoke 二次验证后转 5 分类);(2) §4.1 转移矩阵加 cheap_codex_smoke 触发条件(uninitialized_seat + smoke=auth_invalid → AUTH_INVALID;smoke=uncertain → 保留原状态);(3) §4.3a 加删除链短路语义(STATUS_AUTH_INVALID 与 STATUS_PERSONAL 等价处置;Round 6 FR-P1.2 / FR-P1.4 落地);(4) §6 加 I9 不变量 — add-phone 探针 7 处接入(invite 4 + OAuth C-P1~C-P4 共 4 处,Round 6 加 C-P4);(5) 引用方加 PRD-5/PRD-6 + FR-P0/P1.2/P1.4/D6/D8;关联 `prompts/0426/prd/prd-6-p2-followup.md` §5.8 |

---

**文档结束。** 工程师据此可直接编写 7 状态 + 转移点的代码改造、Pydantic 模型、单测,不需额外决策。
