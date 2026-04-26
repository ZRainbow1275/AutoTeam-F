# Shared SPEC: plan_type 白名单

## 0. 元数据 + 引用方

| 字段 | 内容 |
|---|---|
| 名称 | plan_type 白名单与不支持套餐处置 |
| 版本 | v1.0 (2026-04-26) |
| 主题归属 | OAuth bundle 解析 + 账号入池前置校验 |
| 引用方 | PRD-2 / spec-2-account-lifecycle.md / FR-A1~A5 |
| 共因 | synthesis §1 共因 A;Issue#2 + Issue#6 |
| 不在范围 | seat_type 决策(见 PRD-2 FR-F1~F6)、quota 分类(见 [`./quota-classification.md`](./quota-classification.md)) |

---

## 1. 概念定义

| 术语 | 定义 |
|---|---|
| `plan_type` | OpenAI 在 OAuth id_token JWT claim `https://api.openai.com/auth.chatgpt_plan_type` 中下发的字符串字面量,标识当前 workspace 的套餐类型 |
| `SUPPORTED_PLAN_TYPES` | 本系统能够正确处理(注册→入池→Codex 调用)的 plan_type 集合 |
| `unsupported plan_type` | 出现在 OAuth bundle 中但不在白名单内的字面量,例如 `self_serve_business_usage_based`、`enterprise` |
| `plan_type_raw` | 落盘到 `accounts.json` 的原始字面量(.lower() 后),用于事后排查 |
| `plan_supported` | 落盘到 `accounts.json` 的布尔值,反映当时 OAuth bundle 是否在白名单内 |

---

## 2. 完整数据契约

### 2.1 常量集(置于 `src/autoteam/accounts.py`)

```python
# accounts.py 顶部新增
SUPPORTED_PLAN_TYPES: frozenset[str] = frozenset({
    "team",   # ChatGPT Team workspace,本系统主要工作池
    "free",   # 已退出 Team 的个人 free,personal 子号路径
    "plus",   # 个人付费,允许通过 manual_account 手动添加
    "pro",    # 个人 Pro,同上
})
"""
支持的 plan_type 白名单。

不在此集合内的字面量(如 self_serve_business_usage_based / enterprise / unknown)
均视为 unsupported,触发 STATUS_AUTH_INVALID + register_failures.category="plan_unsupported"。

新增字面量需经过测试验证(quota 是否能正常拉取、Codex 调用是否能用)后再加入白名单。
"""
```

### 2.2 工具函数

```python
# accounts.py 紧随常量定义之后
def is_supported_plan(plan_type: str | None) -> bool:
    """判定 plan_type 是否在白名单内。

    None / 空串 / "unknown" 一律返回 False。
    比对前先 .lower().strip(),避免 OpenAI 后端大小写漂移。
    """
    if not plan_type:
        return False
    return plan_type.strip().lower() in SUPPORTED_PLAN_TYPES


def normalize_plan_type(plan_type: str | None) -> str:
    """归一化用于落盘 / 比对的 plan_type。

    None → "unknown",其余统一 .lower().strip()。
    """
    if not plan_type:
        return "unknown"
    return plan_type.strip().lower()
```

### 2.3 OAuth bundle 字段扩展(`codex_auth.py:_exchange_auth_code` L100-116)

```python
# 现有
bundle = {
    "access_token": ...,
    "refresh_token": ...,
    "id_token": ...,
    "account_id": ...,
    "email": ...,
    "plan_type": auth_claims.get("chatgpt_plan_type", "unknown"),
    "expired": ...,
}

# 新增字段(在已有字段之后追加,不破坏旧字段)
bundle["plan_type_raw"] = bundle["plan_type"]            # 原始字面量,事后排查用
bundle["plan_type"] = normalize_plan_type(bundle["plan_type"])  # 归一化后的小写值
bundle["plan_supported"] = is_supported_plan(bundle["plan_type"])
```

---

## 3. 行为契约

### 3.1 函数签名

```python
def is_supported_plan(plan_type: str | None) -> bool: ...
def normalize_plan_type(plan_type: str | None) -> str: ...
```

### 3.2 前置条件

- 调用 `is_supported_plan` 不要求参数已 lower(函数内会处理)
- `SUPPORTED_PLAN_TYPES` 是 `frozenset`,运行时不可修改
- 任何对 `bundle["plan_type"]` 的判定**必须**先经过 `normalize_plan_type` 或 `is_supported_plan`

### 3.3 后置条件

- `normalize_plan_type(None) == "unknown"`,`normalize_plan_type("Team") == "team"`
- `is_supported_plan` 不抛异常(对任何输入都给出布尔值)
- bundle 含 `plan_supported` 字段后,所有下游消费方应只读该字段而非自己 .lower() 比对

### 3.4 异常类型

- 工具函数本身不抛异常
- 触发 unsupported 的下游处置依赖 `register_failures.record_failure`,该函数不向外抛异常

---

## 4. 决策矩阵

### 4.1 OAuth bundle 检查 → 处置矩阵

| 场景 | bundle["plan_type"] | bundle["plan_supported"] | 处置 |
|---|---|---|---|
| Team 注册成功 | "team" | True | 沿用现行流程 → STATUS_ACTIVE(配额另判,见 quota-classification.md) |
| Personal OAuth 成功 | "free" | True | 沿用现行流程 → STATUS_PERSONAL |
| 手动添加 Plus 号 | "plus" | True | manual_account 走 STATUS_STANDBY(plus 无 Team 调度) |
| 手动添加 Pro 号 | "pro" | True | 同 plus |
| **新计费 workspace** | "self_serve_business_usage_based" | False | **STATUS_AUTH_INVALID + record_failure("plan_unsupported")** |
| **企业版 workspace** | "enterprise" | False | 同上 |
| JWT 缺 claim | "unknown" | False | 同上(标 plan_unsupported,理由是无法判定) |
| **大写漂移** | "Team" → 归一为 "team" | True | 与 team 等价 |

### 4.2 6 个调用点处置矩阵

| 调用点 | 文件:行号 | 旧处置 | 新处置 |
|---|---|---|---|
| 1. OAuth bundle 落盘 | `codex_auth.py:111` | 直接落 `plan_type` | 落 `plan_type_raw` + `plan_type` + `plan_supported` |
| 2. 手动添加完成 | `manual_account.py:233` | `if plan_type == "team"` 二元分支 | `if not bundle["plan_supported"]` → STATUS_AUTH_INVALID + record_failure;否则按 plan_type 走原 seat_type 决策 |
| 3. Team 注册收尾 | `manager.py:1467-1478` | `seat_label = "chatgpt" if bundle_plan == "team" else "codex"` | 先判 `plan_supported`,False → STATUS_AUTH_INVALID + record_failure;True 时再判 plan_type 决定 seat |
| 4. personal 注册收尾 | `manager.py:1431-1460`(use_personal=True 分支) | `if plan_type != "free": delete + oauth_failed` | `if not plan_supported` 优先级最高 → record_failure("plan_unsupported");其次保留 plan != "free" 的拒收 |
| 5. reinvite_account 校验 | `manager.py:2489-2494` | `plan_type != "team": _cleanup + STANDBY` | 增加一道 `plan_supported` 判定:False → STATUS_AUTH_INVALID + record_failure("plan_unsupported");True 但 plan != "team" → 沿用 plan_drift 路径 |
| 6. cpa_sync 文件名推断 | `cpa_sync.py:132-140` | `team / plus / free` 子串 | 不改判定逻辑,但记录到的 plan_type 走 normalize_plan_type |

---

## 5. 调用方处置规范

### 5.1 共通原则

```python
# 任何 OAuth bundle 消费方都必须按这个顺序检查
if not bundle.get("plan_supported"):
    record_failure(
        email,
        category="plan_unsupported",
        reason=f"OAuth bundle plan_type={bundle.get('plan_type_raw')} 不在白名单",
        plan_type=bundle.get("plan_type"),
        plan_type_raw=bundle.get("plan_type_raw"),
        stage=<调用点名称>,
    )
    update_account(email, status=STATUS_AUTH_INVALID, plan_type_raw=bundle.get("plan_type_raw"))
    return None  # 或上游约定的失败值
```

### 5.2 各调用方差异

| 调用方 | 差异处置 |
|---|---|
| `manual_account._finalize_account` | unsupported → 不写 last_quota / auth_file 不删(保留供人工排查) |
| `manager._run_post_register_oauth` (Team) | unsupported + bundle 已成功 → kick 该 email 出 Team(避免占席位)+ STATUS_AUTH_INVALID |
| `manager._run_post_register_oauth` (personal) | unsupported → delete_account + record_failure(因为已 leave_workspace,本地无价值) |
| `manager.reinvite_account` | unsupported → `_cleanup_team_leftover("plan_unsupported")` + STATUS_AUTH_INVALID + auth_file=None |

---

## 6. 单元测试 fixture 与样本数据

### 6.1 输入样本(yaml)

```yaml
# tests/fixtures/plan_type_samples.yaml
supported:
  - input: "team"
    expected_supported: true
    expected_normalized: "team"
  - input: "Team"
    expected_supported: true
    expected_normalized: "team"
  - input: "FREE"
    expected_supported: true
    expected_normalized: "free"
  - input: "  plus  "
    expected_supported: true
    expected_normalized: "plus"
  - input: "pro"
    expected_supported: true
    expected_normalized: "pro"

unsupported:
  - input: "self_serve_business_usage_based"
    expected_supported: false
    expected_normalized: "self_serve_business_usage_based"
  - input: "chatgpt_business_usage_based"
    expected_supported: false
    expected_normalized: "chatgpt_business_usage_based"
  - input: "enterprise"
    expected_supported: false
    expected_normalized: "enterprise"
  - input: "unknown"
    expected_supported: false
    expected_normalized: "unknown"
  - input: ""
    expected_supported: false
    expected_normalized: "unknown"
  - input: null
    expected_supported: false
    expected_normalized: "unknown"
```

### 6.2 完整测试用例

```python
# tests/unit/test_plan_type_whitelist.py
import pytest
import yaml
from pathlib import Path
from autoteam.accounts import is_supported_plan, normalize_plan_type, SUPPORTED_PLAN_TYPES

FIXTURE = yaml.safe_load(Path("tests/fixtures/plan_type_samples.yaml").read_text())


@pytest.mark.parametrize("case", FIXTURE["supported"])
def test_supported_plan_types(case):
    assert is_supported_plan(case["input"]) is True
    assert normalize_plan_type(case["input"]) == case["expected_normalized"]


@pytest.mark.parametrize("case", FIXTURE["unsupported"])
def test_unsupported_plan_types(case):
    assert is_supported_plan(case["input"]) is False
    assert normalize_plan_type(case["input"]) == case["expected_normalized"]


def test_whitelist_contains_only_lowercase():
    """SUPPORTED_PLAN_TYPES 不能含大写,否则 is_supported_plan 比对会失败"""
    for plan in SUPPORTED_PLAN_TYPES:
        assert plan == plan.lower(), f"{plan} 必须全小写"


def test_whitelist_is_frozen():
    """SUPPORTED_PLAN_TYPES 必须是 frozenset,运行时不可变"""
    assert isinstance(SUPPORTED_PLAN_TYPES, frozenset)
```

### 6.3 OAuth bundle 集成 fixture

```json
{
  "bundle_team_supported": {
    "access_token": "sk-***",
    "refresh_token": "rt-***",
    "id_token": "ey***",
    "account_id": "acc-12345",
    "email": "test@example.com",
    "plan_type": "team",
    "plan_type_raw": "team",
    "plan_supported": true,
    "expired": 1714123200.0
  },
  "bundle_self_serve_business": {
    "access_token": "sk-***",
    "refresh_token": "rt-***",
    "id_token": "ey***",
    "account_id": "acc-67890",
    "email": "test2@example.com",
    "plan_type": "self_serve_business_usage_based",
    "plan_type_raw": "self_serve_business_usage_based",
    "plan_supported": false,
    "expired": 1714123200.0
  },
  "bundle_unknown": {
    "access_token": "sk-***",
    "refresh_token": "rt-***",
    "id_token": "ey***",
    "account_id": "acc-99999",
    "email": "test3@example.com",
    "plan_type": "unknown",
    "plan_type_raw": "unknown",
    "plan_supported": false,
    "expired": 1714123200.0
  }
}
```

---

## 7. 不变量(Invariants)

- **I1**:任何对 plan_type 的字符串判定必须经过 `normalize_plan_type` 或 `is_supported_plan`,**禁止**直接 `bundle["plan_type"] == "team"`
- **I2**:`bundle["plan_type"]` 落盘后必为小写或 "unknown",`plan_type_raw` 保留原始字面量
- **I3**:`SUPPORTED_PLAN_TYPES` 修改必须经过测试评审(新增字面量代表新的 quota / seat 行为分支)
- **I4**:任何 unsupported plan_type 的账号都不能进入 STATUS_ACTIVE,只能在 STATUS_AUTH_INVALID / STATUS_PENDING / STATUS_STANDBY 中
- **I5**:cpa_sync 文件名推断与 OAuth bundle 提取的 plan_type 应保持一致(同一字面量集合)
- **I6**:`plan_type_unsupported` 字段名**不**新增到 accounts.json(改用 `plan_supported` 反向语义,默认 None=旧记录)

---

## 附录 A:已知不支持的字面量(运营观测)

| 字面量 | 来源 | OpenAI 定义 |
|---|---|---|
| `self_serve_business_usage_based` | 用户报告 #2/#6 | 自助商用按量计费,无 codex 配额 |
| `chatgpt_business_usage_based` | 推测 | 同上变体 |
| `enterprise` | OpenAI 公开文档 | 企业版,通常需要单独签约 |
| `edu` | OpenAI 公开文档 | 教育版 |
| `unknown` | JWT claim 缺失 | 无法判定,默认拒收 |

---

**文档结束。** 工程师据此可直接编写常量集 + 工具函数 + 单测,无需额外决策。
