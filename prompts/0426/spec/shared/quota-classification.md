# Shared SPEC: wham/usage 配额分类

## 0. 元数据 + 引用方

| 字段 | 内容 |
|---|---|
| 名称 | Codex 配额查询(wham/usage)五分类规则 |
| 版本 | v1.0 (2026-04-26) |
| 主题归属 | `check_codex_quota` 返回值契约 + 9 个调用方处置 |
| 引用方 | PRD-2 / spec-2-account-lifecycle.md / FR-B1~B4、FR-D1~D4、FR-E1 |
| 共因 | synthesis §1 共因 B、D;Issue#2 + Issue#6 |
| 不在范围 | refresh_token 刷新(`refresh_access_token`)、auth_file 写入(`save_auth_file`)、plan_type 白名单(见 [`./plan-type-whitelist.md`](./plan-type-whitelist.md)) |

---

## 1. 概念定义

| 术语 | 定义 |
|---|---|
| `wham/usage` | OpenAI 后端接口 `https://chatgpt.com/backend-api/wham/usage`,返回当前 access_token 对应账号的 Codex 配额状态 |
| `quota_status` | 本系统对 wham/usage 调用结果的 5 类归一化:`ok / exhausted / no_quota / auth_error / network_error` |
| `quota_info` / `QuotaSnapshot` | 落盘到 `accounts.json` 的配额快照,字段:`primary_pct`、`primary_resets_at`、`primary_total`、`primary_remaining`、`weekly_pct`、`weekly_resets_at` |
| `no_quota`(新) | "workspace 已分配但 codex 配额=0"形态 — used_percent=0 但 limit/total=0,UI 不应显示"剩余 100%" |
| `limit_reached` | OpenAI 返回 `rate_limit.limit_reached: true`,与 `primary_pct >= 100` 等价 |

---

## 2. 完整数据契约

### 2.1 类型定义(置于 `src/autoteam/codex_auth.py` 顶部 typing 区)

```python
from typing import Literal, Optional, TypedDict, Union

QuotaStatus = Literal["ok", "exhausted", "no_quota", "auth_error", "network_error"]


class QuotaSnapshot(TypedDict, total=False):
    """落盘到 accounts.json 的 last_quota 字段结构。

    所有字段都允许缺失(向后兼容旧记录),消费方需用 .get(...) 取值。
    """
    primary_pct: int           # 0..100,主配额已用百分比
    primary_resets_at: int     # epoch seconds,主配额重置时间;0 表示未知
    primary_total: int         # 主配额总额(令牌或请求数);0 表示 no_quota
    primary_remaining: int     # 主配额剩余值(若 OpenAI 返回)
    weekly_pct: int            # 周配额已用百分比
    weekly_resets_at: int      # 周配额重置时间


class QuotaExhaustedInfo(TypedDict):
    """get_quota_exhausted_info 的返回结构。"""
    window: Literal["primary", "weekly", "combined", "limit", "no_quota"]
    resets_at: int             # epoch seconds,何时可重试
    quota_info: QuotaSnapshot
    limit_reached: bool


# check_codex_quota 返回类型
QuotaProbeResult = tuple[QuotaStatus, Optional[Union[QuotaSnapshot, QuotaExhaustedInfo]]]
```

### 2.2 函数签名(实施期目标)

```python
def check_codex_quota(
    access_token: str,
    account_id: Optional[str] = None,
) -> QuotaProbeResult:
    """通过 wham/usage 探测 Codex 配额状态。

    返回 (status, info):
      ("ok", QuotaSnapshot)             — 200 + 配额可用
      ("exhausted", QuotaExhaustedInfo) — 200 + 主/周配额耗尽
      ("no_quota", QuotaExhaustedInfo)  — 200 + 配额总额=0(workspace 未分配)  ★新增
      ("auth_error", None)              — 401/403,token/seat 失效
      ("network_error", None)           — DNS/timeout/SSL/5xx/429/JSON 解析异常
    """


def get_quota_exhausted_info(
    quota_info: QuotaSnapshot,
    *,
    limit_reached: bool = False,
) -> Optional[QuotaExhaustedInfo]:
    """根据 QuotaSnapshot 判断是否耗尽,返回耗尽详情或 None。

    新增:primary_total == 0 → 返回 window="no_quota" 形态。
    """
```

### 2.3 落盘字段扩展

```python
# accounts.json 中 acc.last_quota 的字段集(QuotaSnapshot)
{
  "primary_pct": int,
  "primary_resets_at": int,
  "primary_total": int,        # 新增,FR-B2
  "primary_remaining": int,    # 新增,FR-B2(可缺失)
  "weekly_pct": int,
  "weekly_resets_at": int
}
```

---

## 3. 行为契约

### 3.1 前置条件

- `access_token` 非空字符串(空串短路返回 `("auth_error", None)`)
- `account_id` 可选;若为 None,内部从 `get_chatgpt_account_id()` 拉取
- 调用方在并发场景下需要自己控制并发数(本函数无 rate limit)

### 3.2 后置条件

- 返回元组永远 2 元素,第 1 元素永远是 5 个字面量之一
- `("ok", info)` 时,info 必为 QuotaSnapshot
- `("exhausted", info)` / `("no_quota", info)` 时,info 必为 QuotaExhaustedInfo
- `("auth_error", None)` / `("network_error", None)` 时,info 必为 None
- 函数永不抛异常(所有异常归为 `network_error`)

### 3.3 异常类型

| 实际异常 | 归类 |
|---|---|
| `requests.exceptions.ConnectionError` | network_error |
| `requests.exceptions.Timeout` | network_error |
| `requests.exceptions.SSLError` | network_error |
| `requests.exceptions.RequestException`(其它) | network_error |
| HTTP 401 / 403 | auth_error |
| HTTP 429 / 5xx | network_error |
| HTTP 4xx(非 401/403/429) | network_error |
| JSON 解析失败 | network_error |
| 任意 `Exception` 兜底 | network_error |

**不变量**:`auth_error` 与 `network_error` 严格区分,前者会触发"标记 AUTH_INVALID/重登"等破坏性流程,网络抖动绝不能落入该分支。

---

## 4. 分类规则(决策矩阵)

### 4.1 HTTP 状态 → 分类

```
┌──────────────────┬──────────────────┐
│ HTTP / 异常       │ 分类              │
├──────────────────┼──────────────────┤
│ 200 OK + 解析成功 │ → 看下表 4.2     │
│ 401 / 403         │ auth_error        │
│ 429               │ network_error     │
│ 5xx               │ network_error     │
│ 其他 4xx          │ network_error     │
│ Connection/Timeout│ network_error     │
│ JSON 解析异常     │ network_error     │
│ 未知 Exception    │ network_error(兜底)│
└──────────────────┴──────────────────┘
```

### 4.2 200 OK 内部分类(关键)

按**优先级从高到低**判定(命中即返回):

```
┌──────────────────────────────────────────────┬──────────────┐
│ 触发条件(任一命中)                            │ 分类         │
├──────────────────────────────────────────────┼──────────────┤
│ rate_limit 字段缺失或为空                     │ no_quota     │
│ rate_limit.primary_window 字段缺失或为空      │ no_quota     │
│ primary.limit == 0(显式 0)                  │ no_quota     │
│ primary.total == 0                            │ no_quota     │
│ primary.reset_at == 0 AND used_percent == 0   │ no_quota     │
│ primary.remaining == 0 AND total == 0         │ no_quota     │
│ rate_limit.limit_reached == true              │ exhausted    │
│ primary_pct >= 100                            │ exhausted    │
│ weekly_pct >= 100                             │ exhausted    │
│ 上述都不命中                                  │ ok           │
└──────────────────────────────────────────────┴──────────────┘
```

### 4.3 `no_quota` 与 `exhausted` 的语义区分

| 维度 | exhausted | no_quota |
|---|---|---|
| 触发前提 | 配额本来有但已用完 | workspace 从未被分配配额 |
| primary_pct | 100 | 0(或缺失) |
| primary_total | > 0 | 0(或缺失) |
| 用户操作 | 等待 reset_at 后自然恢复 | 需要管理员介入(配额从来没有) |
| UI 文案 | "已耗尽,X 小时后恢复" | "无配额,联系管理员" |
| 是否 retry | 5h 后 retry | **不**自动 retry,直接终态 |

---

## 5. 调用方处置规范(9 个调用点)

### 5.1 调用点清单

| # | 文件:行号 | 函数/上下文 |
|---|---|---|
| 1 | `manual_account.py:263` | `_finalize_account` 手动添加完成 |
| 2 | `manager.py:715` | `cmd_check` 巡检 |
| 3 | `manager.py:748` | `cmd_check` 失败重试 |
| 4 | `manager.py:760` | `cmd_check` refresh_token 后重测 |
| 5 | `manager.py:2521` | `reinvite_account` 假恢复检测 |
| 6 | `manager.py:2683` | `_replace_single` 后续验证 |
| 7 | `manager.py:2964` | rotation 周期检查 |
| 8 | `api.py:1499` | `/api/accounts/{email}/login` 补登录 |
| 9 | `api.py:1558`、`api.py:2136` | API 端点直接验配额 |
| 10 | **新增** `manager.py:_run_post_register_oauth` | FR-D1 注册收尾 probe |
| 11 | **新增** `manager.py:sync_account_states` | FR-E1 被踢识别 |

### 5.2 9+2 调用方统一处置矩阵

| status | 处置 | last_quota 是否更新 | 状态变更 |
|---|---|---|---|
| `ok` | 沿用现有逻辑(STATUS_ACTIVE) | 写入 quota_info | 无变更或→ ACTIVE |
| `exhausted` | 锁 5h 等恢复 | 写入(从 info["quota_info"]) | → STATUS_EXHAUSTED + quota_exhausted_at + quota_resets_at |
| `no_quota`(新) | **不**锁 5h(不会自然恢复) | 写入 + 标 primary_total=0 | → STATUS_AUTH_INVALID + record_failure("no_quota_assigned") |
| `auth_error` | 触发重登 / KICK 路径 | 不更新 | → STATUS_AUTH_INVALID(reconcile 接管) |
| `network_error` | 不动 status,等下轮 | 不更新 | 保持原状态 |

### 5.3 各调用点差异

| 调用点 | 特殊处置 |
|---|---|
| `manual_account.py:263` | no_quota → `update_account(status=STATUS_AUTH_INVALID, last_quota=info["quota_info"])` + 不删 auth_file(保留供调试) |
| `manager.py:_run_post_register_oauth` (新) | no_quota → 与 manual_account 对称,但额外触发 kick(账号已在 Team) |
| `manager.py:sync_account_states` (新) | auth_error → `STATUS_AUTH_INVALID`(替换原 STATUS_STANDBY 行为) |
| `manager.py:reinvite_account:2521` | no_quota → 走 `_cleanup_team_leftover("no_quota_assigned")` + STATUS_AUTH_INVALID,不进 STANDBY 池(避免反复试) |
| `api.py:1499` | no_quota → 返回 HTTP 422 + body `{"error": "no_quota_assigned", "plan_type": ...}` |

### 5.4 auto-check 阈值与 quota_verified 联动

`reinvite_account:2528-2530` 用 `100 - primary_pct >= threshold`(默认 10%)判 quota_verified:
- `no_quota` 分支:不进入 quota_verified 判断,直接 fail_reason="no_quota_assigned",`_cleanup_team_leftover`
- `ok` 分支保持 threshold 判定不变

---

## 6. 单元测试 fixture 与样本数据

### 6.1 wham/usage 响应样本(json)

```json
// tests/fixtures/wham_usage_responses.json
{
  "ok_normal": {
    "status_code": 200,
    "body": {
      "rate_limit": {
        "primary_window": {"used_percent": 35, "reset_at": 1714200000, "limit": 100, "remaining": 65},
        "secondary_window": {"used_percent": 10, "reset_at": 1714780800}
      }
    }
  },
  "exhausted_primary": {
    "status_code": 200,
    "body": {
      "rate_limit": {
        "limit_reached": true,
        "primary_window": {"used_percent": 100, "reset_at": 1714200000, "limit": 100, "remaining": 0},
        "secondary_window": {"used_percent": 50, "reset_at": 1714780800}
      }
    }
  },
  "exhausted_weekly": {
    "status_code": 200,
    "body": {
      "rate_limit": {
        "primary_window": {"used_percent": 30, "reset_at": 1714200000, "limit": 100, "remaining": 70},
        "secondary_window": {"used_percent": 100, "reset_at": 1714780800}
      }
    }
  },
  "no_quota_limit_zero": {
    "status_code": 200,
    "body": {
      "rate_limit": {
        "primary_window": {"used_percent": 0, "reset_at": 0, "limit": 0, "remaining": 0},
        "secondary_window": {"used_percent": 0, "reset_at": 0}
      }
    }
  },
  "no_quota_empty_rate_limit": {
    "status_code": 200,
    "body": {}
  },
  "no_quota_missing_primary": {
    "status_code": 200,
    "body": {
      "rate_limit": {
        "secondary_window": {"used_percent": 0, "reset_at": 0}
      }
    }
  },
  "auth_error_401": {
    "status_code": 401,
    "body": {"error": {"code": "invalid_token"}}
  },
  "auth_error_403": {
    "status_code": 403,
    "body": {"error": {"code": "forbidden"}}
  },
  "rate_limited_429": {
    "status_code": 429,
    "body": {"error": "Too Many Requests"}
  },
  "server_error_500": {
    "status_code": 500,
    "body": {"error": "Internal Server Error"}
  },
  "json_invalid": {
    "status_code": 200,
    "body": "<html>Service Unavailable</html>"
  }
}
```

### 6.2 单测代码

```python
# tests/unit/test_quota_classification.py
import json
import pytest
from unittest.mock import patch, MagicMock
from autoteam.codex_auth import check_codex_quota, get_quota_exhausted_info

FIXTURE = json.loads(Path("tests/fixtures/wham_usage_responses.json").read_text())


@pytest.mark.parametrize("name,expected_status", [
    ("ok_normal", "ok"),
    ("exhausted_primary", "exhausted"),
    ("exhausted_weekly", "exhausted"),
    ("no_quota_limit_zero", "no_quota"),
    ("no_quota_empty_rate_limit", "no_quota"),
    ("no_quota_missing_primary", "no_quota"),
    ("auth_error_401", "auth_error"),
    ("auth_error_403", "auth_error"),
    ("rate_limited_429", "network_error"),
    ("server_error_500", "network_error"),
    ("json_invalid", "network_error"),
])
def test_check_codex_quota_classification(name, expected_status):
    sample = FIXTURE[name]
    mock_resp = MagicMock()
    mock_resp.status_code = sample["status_code"]
    if isinstance(sample["body"], str):
        mock_resp.json.side_effect = ValueError("not JSON")
        mock_resp.text = sample["body"]
    else:
        mock_resp.json.return_value = sample["body"]
        mock_resp.text = json.dumps(sample["body"])

    with patch("autoteam.codex_auth.requests.get", return_value=mock_resp):
        status, _ = check_codex_quota("test-token", account_id="acc-1")
        assert status == expected_status


def test_no_quota_includes_quota_info():
    """no_quota 分类必须返回 QuotaExhaustedInfo,info["window"] == "no_quota" """
    sample = FIXTURE["no_quota_limit_zero"]
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = sample["body"]
    with patch("autoteam.codex_auth.requests.get", return_value=mock_resp):
        status, info = check_codex_quota("test-token")
        assert status == "no_quota"
        assert isinstance(info, dict)
        assert info["window"] == "no_quota"
        assert info["quota_info"]["primary_total"] == 0


def test_exhausted_uses_existing_window_label():
    """exhausted 分支不能误标为 no_quota"""
    sample = FIXTURE["exhausted_primary"]
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = sample["body"]
    with patch("autoteam.codex_auth.requests.get", return_value=mock_resp):
        status, info = check_codex_quota("test-token")
        assert status == "exhausted"
        assert info["window"] in ("primary", "combined")
```

---

## 7. 不变量(Invariants)

- **I1**:`check_codex_quota` 永不抛异常(任何异常归为 network_error 返回)
- **I2**:`auth_error` 与 `network_error` 严格区分,401/403 是 auth_error 唯一来源,**429 不能落入 auth_error**
- **I3**:`no_quota` 必须返回 `QuotaExhaustedInfo` 结构,`window` 字段值为 `"no_quota"`(便于上游分支判定)
- **I4**:`get_quota_exhausted_info` 中 `primary_total == 0` 优先级**高于** `primary_pct >= 100`(no_quota 优先于 exhausted)
- **I5**:`last_quota` 字段更新仅在 ok / exhausted / no_quota 情况发生;auth_error / network_error 不更新(避免脏数据)
- **I6**:`exhausted` 与 `no_quota` 的 reset_at 语义不同:exhausted 给真实重置时间,no_quota 给 `time.time() + 86400`(占位,不应被用作重试依据)
- **I7**:9+2 个调用点必须**显式**处理 `no_quota` 分支,不能让 `no_quota` 走默认 `else` 路径被当作 ok / exhausted

---

## 附录 A:wham/usage 真实 schema 待核实

PRD-2 Q-1 列出未决:`limit` / `total` / `remaining` 字段是否真实返回需要从用户实际抓包确认。**实施期约定**:
1. 先按本 spec 实现 4 个 no_quota 触发条件
2. 上线后通过 `register_failures.json` 的 `category="no_quota_assigned"` 计数,每条记录附 `raw_rate_limit` 字段以便回溯
3. 1-2 周后根据数据决议是否需要补充新的 no_quota 触发条件

---

**文档结束。** 工程师据此可直接编写 `check_codex_quota` 改造 + `get_quota_exhausted_info` 扩展 + 9+2 调用点的处置代码,无需额外决策。
