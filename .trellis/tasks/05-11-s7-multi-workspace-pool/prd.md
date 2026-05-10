# S7 — 多 team workspace 池化（冷备 + 秒级故障切换）

> **Round 12 子任务**，父任务 `05-11-upstream-align-register-multimail-frontend-refresh` 中
> 「Q2 创新方向 5 — 多 team workspace 池化」的实施单元。

## 1. 背景

当前 AutoTeam 仅维持一个 active team workspace（admin_state.json 单 admin email + account_id）。
单 workspace 故障（被封 / 订阅 cancel / Cloudflare 全域风控）整套自动化即停摆。

S7 引入 workspace 池：注册 K 个 workspace（active=1 + warm/cold N-1 互为冷备），主 workspace
连续探测失败超阈值时秒切到下一个 warm，cold 升 warm，整体不停摆。

## 2. 范围

### MVP（本任务）

1. 新建 `src/autoteam/workspace_pool.py`（持久化 `workspaces.json` + `WorkspacePool`）
2. `admin_state.get_admin_email/get_chatgpt_account_id` 路由到 `WorkspacePool.get_active()`
3. `master_health` 失败路径调 `pool.mark_unhealthy()` 触发自动切换（阈值 env `MASTER_HEALTH_FAIL_THRESHOLD=3`）
4. `cpa_sync` 同步目标改为 active workspace
5. 测试 ≥ 15 case（mock 故障注入；禁止真实 OpenAI 调用）

### 非范围

- 不实现真实 OpenAI workspace 创建 API（user 自建 workspace 后手动 register）
- 不动 mail provider / account_state / manager.py 主流程
- 不做 active workspace 内 accounts 数据隔离（accounts.json 仍是全局视图）
- 不动 web 前端（后续基于 SSE 流展示）

## 3. 数据 schema

### `workspaces.json`

```json
{
  "schema_version": 1,
  "active": "ws-aaa",
  "workspaces": [
    {
      "id": "ws-aaa",
      "admin_email": "ad1@example.com",
      "account_id": "00000000-0000-0000-0000-000000000001",
      "tier": "active",
      "status": "healthy",
      "fail_count": 0,
      "last_check_ts": 1715300000.0,
      "registered_at": 1715200000.0,
      "transition_log": [
        {"ts": 1715200000.0, "from": null, "to": "warm", "reason": "register"},
        {"ts": 1715200100.0, "from": "warm", "to": "active", "reason": "promoted_no_active"}
      ]
    }
  ]
}
```

### tier / status 取值

- **tier**: `active` | `warm` | `cold` （任一时刻 `active` 至多 1 个）
- **status**: `healthy` | `unhealthy` | `unknown`

## 4. 不变量（强制执行）

- **I1**: 任意时刻 workspaces[*].tier == "active" 至多 1 个
- **I2**: `mark_unhealthy` 命中 active 且 fail_count ≥ threshold → 自动 promote 一个 warm/cold 至 active；原 active 降为 cold + status=unhealthy
- **I3**: 写盘原子（snapshot `.bak` → write `.tmp` → `os.replace`），失败回滚
- **I4**: 单 workspace 模式向后兼容：`workspaces.json` 不存在时，从 `state.json` 读 admin_email/account_id 自动 seed 为单一 active workspace；外部 API（`get_admin_email` 等）签名不变
- **I5**: `register / set_active / mark_unhealthy` 全部追加 `transition_log` 条目
- **I6**: 与 S1 default_machine 风格一致（lock 内修改、lock 外发布事件）
- **I7**: 永不抛未捕获异常（与 master_health.py M-I1 看齐）

## 5. 关键 API

```python
class WorkspacePool:
    def register(self, workspace_id: str, admin_email: str, account_id: str, tier: str = "warm") -> dict
    def get_active(self) -> dict | None         # 当前 active 单条；可能 None
    def set_active(self, workspace_id: str) -> dict
    def mark_unhealthy(self, workspace_id: str, reason: str) -> dict | None
    def list_all(self) -> list[dict]
    def get(self, workspace_id: str) -> dict | None
```

模块级单例 `default_pool: WorkspacePool` 用于生产路径。

## 6. 故障切换流程

```
master_health probe FAIL
     │
     ▼
default_pool.mark_unhealthy(active_ws_id, reason)
     │ fail_count += 1
     ▼
fail_count >= MASTER_HEALTH_FAIL_THRESHOLD ?
     ├── No  → 仅累计，保持 active
     └── Yes → 1) 当前 active.tier := cold, status := unhealthy
              2) 选 warm（最早注册）→ tier := active, status := unknown
              3) 若无 warm → 选 cold healthy → tier := active
              4) 若无可升者 → active = None，记 transition_log {reason: no_failover_candidate}
              5) 写盘 + 转移日志
```

切换写 `state_log.jsonl`（与 default_machine.subscribe 复用渠道，前端 SSE 流即可感知）。

## 7. 验收（DoD）

- ruff check 全绿
- pytest 不退化 + 新增 ≥ 15 case 全绿
- commit `feat(round-12 S7): multi team workspace pool + auto failover`
- 改动文件：
  - 新增 `src/autoteam/workspace_pool.py`
  - 新增 `tests/unit/test_round12_s7_workspace_pool.py`
  - 改 `src/autoteam/admin_state.py`（路由）
  - 改 `src/autoteam/master_health.py`（失败路径调 pool）
  - 改 `src/autoteam/cpa_sync.py`（active workspace 切换感知）

## 8. 风险

- **R1**: 现有调用方 `get_admin_email/get_chatgpt_account_id` 极多，若 workspace_pool 退路不严密会破坏所有路径 → 强制兜底（pool 空时回退 state.json）
- **R2**: master_health 是热路径，增加 pool I/O 不能阻塞 → 写盘异步友好（已在 lock 内但写盘耗时 ms 级，可接受）
- **R3**: 用户当前无 team 母号子号，无法 e2e；MVP 只做单测 + mock 故障注入
