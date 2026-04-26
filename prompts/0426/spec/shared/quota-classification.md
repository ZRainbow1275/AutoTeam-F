# Shared SPEC: wham/usage 配额分类

## 0. 元数据 + 引用方

| 字段 | 内容 |
|---|---|
| 名称 | Codex 配额查询(wham/usage)五分类规则 |
| 版本 | v1.3 (2026-04-26 Round 6 quality-reviewer 终审 follow-up) |
| 主题归属 | `check_codex_quota` 返回值契约 + 9 个调用方处置 + uninitialized_seat 二次验证 |
| 引用方 | PRD-2 / PRD-5 / spec-2-account-lifecycle.md / FR-B1~B4、FR-D1~D4、FR-E1、FR-P0 |
| 共因 | synthesis §1 共因 B、D;Issue#2 + Issue#6 + Round 5 verify 实测 |
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
┌──────────────────────────────────────────────────────────────┬──────────────────────┐
│ 触发条件(任一命中)                                            │ 分类                 │
├──────────────────────────────────────────────────────────────┼──────────────────────┤
│ rate_limit 字段缺失或为空                                     │ no_quota             │
│ rate_limit.primary_window 字段缺失或为空                      │ no_quota             │
│ primary.limit == 0(显式 0)                                  │ no_quota             │
│ primary.total == 0                                            │ no_quota             │
│ primary.reset_at == 0 AND used_percent == 0                   │ no_quota             │
│ primary.remaining == 0 AND total == 0                         │ no_quota             │
│ rate_limit.limit_reached == true                              │ exhausted            │
│ primary_pct >= 100                                            │ exhausted            │
│ weekly_pct >= 100                                             │ exhausted            │
│ I5: primary_total is None AND primary_remaining is None       │ ok + window=         │
│     AND primary_pct == 0 AND weekly_pct == 0                  │ "uninitialized_seat" │
│     AND primary_resets_at > 0 AND not limit_reached           │ + needs_codex_smoke  │
│ 上述都不命中                                                  │ ok                   │
└──────────────────────────────────────────────────────────────┴──────────────────────┘
```

**优先级总顺序**:exhausted(`limit_reached / pct>=100`) > no_quota(其余 6 条) > **uninitialized_seat**(I5) > ok

> **注**:I5 命中时 `check_codex_quota` 返回 `("ok", QuotaExhaustedInfo{window="uninitialized_seat", needs_codex_smoke=True, ...})`。它在分类层是 ok(因为不能贸然判 no_quota 错杀 fresh seat),但携带 `needs_codex_smoke` 信号要求上游做 cheap codex backend 二次验证才算最终判定。详见 §4.4。

### 4.3 `no_quota` 与 `exhausted` 的语义区分

| 维度 | exhausted | no_quota |
|---|---|---|
| 触发前提 | 配额本来有但已用完 | workspace 从未被分配配额 |
| primary_pct | 100 | 0(或缺失) |
| primary_total | > 0 | 0(或缺失) |
| 用户操作 | 等待 reset_at 后自然恢复 | 需要管理员介入(配额从来没有) |
| UI 文案 | "已耗尽,X 小时后恢复" | "无配额,联系管理员" |
| 是否 retry | 5h 后 retry | **不**自动 retry,直接终态 |

### 4.4 I5 — uninitialized_seat 形态识别(Round 6 新增)

**条件**(同 §4.2 优先级表 I5 行):

```python
primary_total is None
AND primary_remaining is None
AND primary_pct == 0
AND weekly_pct == 0
AND primary_resets_at > 0
AND not limit_reached
```

**语义**:OpenAI workspace 已分配 seat,但 fresh — 计数器尚未由后端初始化(`primary_total=null`),wham/usage 给了占位重置时间戳(`primary_resets_at>0`)但没填具体配额额度。**这是 Round 5 实测确认的 OpenAI lazy initialization 形态**:wham 在第一次实际消费 token 之前不写出 total/remaining。

**处置(强制)**:

- **不可**单凭 wham 判定。必须由上层调用方调一次 `cheap_codex_smoke(access_token, account_id)` 做最终验证:
  - HTTP 200 + 第一帧 `response.created` → **真 ok**,维持 STATUS_ACTIVE,落 `last_codex_smoke_at`
  - HTTP 401 / 403 / 429(quota 关键词)/ 4xx 含 quota → **STATUS_AUTH_INVALID** + record_failure("no_quota_assigned" 或 "auth_error_at_oauth")
  - HTTP 5xx / network → 保持原 status,等下轮(避免抖动误标)
- 24h 去重(`last_codex_smoke_at`)防 smoke 调用密集
- 不锁 5h、不直接 STATUS_AUTH_INVALID — 必须走 cheap_codex_smoke 二次验证

**真实样本(Round 5 production-cleaner 实测)**:

```json
{
  "primary_pct": 0, "primary_resets_at": 1777197556,
  "primary_total": null, "primary_remaining": null,
  "weekly_pct": 0, "weekly_resets_at": 1777784356
}
```

→ I5 命中 → cheap_codex_smoke 200 OK → 真活号,fresh seat 懒初始化。

**`cheap_codex_smoke` 函数契约**(实施在 `codex_auth.py`,与 `check_codex_quota` 同邻):

**Round 6 决策(2026-04-26 user 已确认 + v1.3 实施对齐)**:

| 维度 | 决策值(v1.3 与 codex_auth.py 实施对齐) |
|---|---|
| Endpoint | `POST https://chatgpt.com/backend-api/codex/responses` |
| Stream | `stream=true`(server 强制要求,non-stream 直接拒) |
| reasoning.effort | `"none"` |
| max_output_tokens | `1`(进一步降低 token 消耗,只触发 response.created 即足够) |
| Headers | `Authorization: Bearer <access_token>` + `Content-Type: application/json` + `Accept: text/event-stream` + `Chatgpt-Account-Id: <account_id>`(若有) |
| Read 策略 | `iter_lines(decode_unicode=True)` / 第一帧 `response.created` 立刻 close |
| Timeout | **15s**(v1.3 修订 — 与调用层 wham/usage 整体超时一致;OpenAI 后端 stream 第一帧偶尔慢,5s 易误判) |
| 去重粒度 | account 维度,落 `accounts.json.last_codex_smoke_at`(24h 内 cache 命中直接返回上次结果);**Round 6 实施推迟到 Round 7 落 manager 24h 去重 — 当前由 `check_codex_quota` 内部消化** |
| token 刷新场景 | 本轮按 account 维度(简洁优先);后续若发现误判可加 `last_smoke_token_id` 字段 |
| 返回签名 | `tuple[str, Optional[str]]` — `(result, reason)`,result ∈ {`alive`, `auth_invalid`, `uncertain`},reason 是失败原因(成功时 None) |
| 失败分类 | 200 + response.created → alive / 401, 403, 429 → auth_invalid / 4xx 含 `quota`/`no_quota`/`rate_limit`/`billing`/`exceeded` 关键词 → auth_invalid / 5xx, network, timeout → uncertain |
| 处置 | **由 `check_codex_quota` 内部消化**(v1.3 关键架构决策):smoke alive → ("ok", quota_info_with_smoke_verified)、auth_invalid → ("auth_error", None)、uncertain → ("network_error", None)。9+2 调用方对 5 分类的现有处置不变 |

```python
# 与 src/autoteam/codex_auth.py 实施对齐(v1.3)
import requests
from typing import Optional, Tuple

_CODEX_SMOKE_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
_CODEX_SMOKE_QUOTA_HINTS = ("quota", "no_quota", "rate_limit", "billing", "exceeded")


def cheap_codex_smoke(
    access_token: str,
    account_id: Optional[str] = None,
    *,
    timeout: float = 15.0,
) -> Tuple[str, Optional[str]]:
    """SPEC-2 shared/quota-classification §4.4 — uninitialized_seat 二次验证。

    对 codex backend 发一个最小推理请求(reasoning.effort=none + max_output_tokens=1 + stream),
    只读第一帧 SSE 立即关流,不消耗多余 token。

    返回 (result, detail):
      ("alive", None)             — HTTP 200 + 第一帧含 response.created → 真活号
      ("auth_invalid", reason)    — HTTP 401/403/429 / 4xx 含 quota 关键词 → token/seat 真失效
      ("uncertain", reason)       — HTTP 5xx / network / timeout / 解析异常 → 保留原状态等下轮

    24h 去重由调用方负责(读 acc.last_codex_smoke_at);Round 6 推迟到 Round 7。
    """
    if not access_token:
        return "auth_invalid", "empty_access_token"

    if not account_id:
        try:
            account_id = get_chatgpt_account_id()
        except Exception:
            account_id = None

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id

    payload = {
        "model": "gpt-5",
        "input": "ping",
        "max_output_tokens": 1,
        "stream": True,
        "reasoning": {"effort": "none"},
    }

    try:
        resp = requests.post(
            _CODEX_SMOKE_ENDPOINT,
            headers=headers,
            json=payload,
            stream=True,
            timeout=timeout,
        )
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.SSLError) as exc:
        return "uncertain", f"network:{type(exc).__name__}"
    except Exception as exc:
        return "uncertain", f"exception:{type(exc).__name__}"

    try:
        status_code = resp.status_code
        if status_code in (401, 403, 429):
            return "auth_invalid", f"http_{status_code}"

        if 500 <= status_code < 600:
            return "uncertain", f"http_{status_code}"

        if status_code != 200:
            # 4xx 非 401/403/429:body 含 quota 关键词视为 auth_invalid;否则 uncertain
            body_preview = ""
            try:
                body_preview = (resp.text or "").lower()[:1500]
            except Exception:
                pass
            if any(hint in body_preview for hint in _CODEX_SMOKE_QUOTA_HINTS):
                return "auth_invalid", f"http_{status_code}_quota_hint"
            return "uncertain", f"http_{status_code}"

        # HTTP 200 — 读 SSE 第一帧
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if "response.created" in line:
                    return "alive", None
        except Exception as exc:
            return "uncertain", f"stream:{type(exc).__name__}"

        return "uncertain", "no_response_created_frame"
    finally:
        try:
            resp.close()
        except Exception:
            pass
```

**v1.3 关键架构决策(实施落地后审查确认)**:

`check_codex_quota` 内部直接消化 smoke 结果,9+2 调用方**完全无感知**。Round 6 实施 codex_auth.py:1925-1944:

```python
# codex_auth.py 内 check_codex_quota 拿到 wham/usage 200 + 5 分类后,在返回前:
#   if status == "ok" and info["window"] == "uninitialized_seat":
#       smoke_result, smoke_reason = cheap_codex_smoke(access_token, account_id)
#       if smoke_result == "alive":
#           # 转返 ok,quota_info 注入 smoke_verified 字段供审计
#           quota_info["smoke_verified"] = True
#           quota_info["last_smoke_result"] = "alive"
#           return ("ok", quota_info)
#       elif smoke_result == "auth_invalid":
#           # 转返 auth_error,9+2 调用方按现有 auth_error 处置 → STATUS_AUTH_INVALID
#           return ("auth_error", None)
#       else:  # uncertain
#           # 转返 network_error,保留原状态等下轮
#           return ("network_error", None)
```

**为何选择内部消化而非调用方处置**:

- 降低调用方复杂度 — 9+2 调用方对 5 分类已有规范处置(§5.2 矩阵),smoke 仅是 5 分类的一种触发路径
- 满足 I8 不变量 — uninitialized_seat 形态绝不能在没有 smoke 验证时转 ACTIVE,内部消化保证不可绕过
- 透明性 — 调用方代码零改动,SPEC §5.2 处置矩阵不需要为每个调用点重复

**关于 24h 去重 + accounts.json 字段(Round 7 议题)**:

| 字段 | 类型 | 默认 | 含义 | Round 6 状态 |
|---|---|---|---|---|
| `last_codex_smoke_at` | float \| null | null | 上次 cheap_codex_smoke 调用的 epoch seconds | **未实施**(推迟到 Round 7;当前由 wham/usage 自身的 30s NFR-1 + `_probe_kicked_account` 30 分钟去重间接节流) |
| `last_smoke_result` | str \| null | null | `"alive"` / `"auth_invalid"` / `"uncertain"` | 同上 |
| `quota_info.smoke_verified` | bool | false | smoke 验证已通过的标记(注入 last_quota) | ✅ Round 6 已实施(`codex_auth.py:1932-1934`) |
| `quota_info.last_smoke_result` | str | - | smoke 结果字面量(注入 last_quota) | ✅ Round 6 已实施 |

**Round 7 待补**:在 `_run_post_register_oauth` / `_probe_kicked_account` 调用 `check_codex_quota` 前先读 `acc.last_codex_smoke_at`,24h 内 cache 命中跳过 smoke 调用。当前由 wham/usage 节流间接控密度,smoke 调用不爆。

**调用方接入点**(2 处实际接入,实施已落地):

| 文件:行号 | 上下文 | 接入逻辑(实施后) |
|---|---|---|
| `manager.py:_run_post_register_oauth` Team 分支 | 注册收尾 | 调 `check_codex_quota`,内部已消化 smoke;5 分类按现有 §5.2 处置 |
| `manager.py:_probe_kicked_account` | sync_account_states 探测 | 同上 |
| `manager.py:cmd_check` | 巡检 | 同上(已被现有 5 分类调用方处置覆盖,无需改) |

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
| `ok` + window=`uninitialized_seat`(I5,Round 6 新增) | **必须**调 cheap_codex_smoke 二次验证才能定 status。**v1.3 实施决策**:此逻辑由 `check_codex_quota` 内部消化 — smoke alive → 转返 ("ok", quota_info[smoke_verified=True]);auth_invalid → 转返 ("auth_error", None);uncertain → 转返 ("network_error", None)。9+2 调用方对 5 分类的现有处置不变,**调用方透明** | 写入 quota_info(含 `smoke_verified=True` + `last_smoke_result="alive"` 字段) | 由 smoke 结果决定:alive → ACTIVE / auth_invalid → AUTH_INVALID(经 auth_error 路径) / uncertain → 保留原状态(经 network_error 路径) |
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
- **I8**(Round 6 新增):**`uninitialized_seat` 形态在没有 cheap_codex_smoke 二次验证时,绝不能转 STATUS_ACTIVE 入池**。`_run_post_register_oauth` Team 分支 + `_probe_kicked_account` 必须在收到 `info.window=="uninitialized_seat"` 时显式调 `cheap_codex_smoke`,否则属于 bug。理由:wham 在 fresh seat 与"真无配额" workspace 上的形态可能完全相同,只能用 codex backend 实跑区分。

---

## 附录 A:wham/usage 真实 schema 待核实

PRD-2 Q-1 列出未决:`limit` / `total` / `remaining` 字段是否真实返回需要从用户实际抓包确认。**实施期约定**:
1. 先按本 spec 实现 4 个 no_quota 触发条件
2. 上线后通过 `register_failures.json` 的 `category="no_quota_assigned"` 计数,每条记录附 `raw_rate_limit` 字段以便回溯
3. 1-2 周后根据数据决议是否需要补充新的 no_quota 触发条件

---

**文档结束。** 工程师据此可直接编写 `check_codex_quota` 改造 + `get_quota_exhausted_info` 扩展 + 9+2 调用点的处置代码,无需额外决策。

---

## 附录 B:修订记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-04-26 | 初版,5 分类签名 + 9+2 调用点处置 |
| v1.1 | 2026-04-26 Round 6 | 加 I5 `uninitialized_seat` 形态识别(§4.2 + §4.4)+ I8 不变量(无 smoke 不入 ACTIVE)+ §5.2 处置矩阵新增 ok+window=uninitialized_seat 行;关联 PRD-5 / Round 5 verify 实测样本(`production-cleaner` cleanup-and-e2e-report.md §3.3)|
| v1.2 | 2026-04-26 Round 6 finalize | user 确认 3 决策:Q-1 endpoint=`POST /backend-api/codex/responses` + stream + reasoning.effort=none + 5s timeout;Q-2 24h 去重按 account 维度,加 `last_codex_smoke_at` + `last_smoke_result` 字段;Q-3 409 不带截图链接(由 add-phone-detection.md 落)。§4.4 cheap_codex_smoke 函数 + `_resolve_uninitialized_seat` 工具函数 finalize 完整可粘贴代码。|
| v1.3 | 2026-04-26 Round 6 quality-reviewer 终审 follow-up | (1) timeout `5s → 15s`(实施实际值,与 wham/usage 整体超时一致;OpenAI 后端 stream 第一帧偶尔慢,5s 易误判);(2) payload 与实施对齐:`{"model":"gpt-5","input":"ping","max_output_tokens":1,"stream":True,"reasoning":{"effort":"none"}}`(简化的 string input,不再用 messages 数组结构);(3) 返回签名改为 `Tuple[str, Optional[str]]` 二元组(result, reason),与实施一致;(4) 4xx body 关键词列表对齐为 `quota / no_quota / rate_limit / billing / exceeded`(实施实际 `_CODEX_SMOKE_QUOTA_HINTS`);(5) §5.2 处置矩阵 `ok+window=uninitialized_seat` 行加备注:**由 `check_codex_quota` 内部消化,调用方透明**(架构决策更优,smoke 结果转为 5 分类之一,9+2 调用方零改动);(6) `_resolve_uninitialized_seat` 工具函数从 SPEC 移除(实施已由 check_codex_quota 内部完成);(7) 24h 去重 + `last_codex_smoke_at`/`last_smoke_result` 字段标记为 Round 7 议题(当前由 wham/usage 节流间接控密度)。详见 `prompts/0426/verify/round6-review-report.md` §4.2 P1 + §3.2 决策 2 审查 |
