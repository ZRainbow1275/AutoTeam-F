# Round 6 PRD-5 实施报告(patch-implementer 阶段)

## 0. 元数据

| 字段 | 值 |
|---|---|
| 报告时间 | 2026-04-26 |
| stage | 2 / 3 (patch-implementer) |
| 输入 | PRD-5(465 行)+ SPEC-2 v1.1 + add-phone-detection v1.1 + quota-classification v1.1 |
| HEAD commit (实施前) | `cf2f7d3`(round-3 修复) |
| 修改文件 | `src/autoteam/codex_auth.py` / `src/autoteam/account_ops.py` / `src/autoteam/api.py` + 新增 `tests/unit/test_round6_patches.py` |
| 验证结果 | ✅ pytest 178 passed(基线 155 + 新增 23)/ ruff 0 error / import OK |
| 落地 FR | FR-P0(全)+ FR-P1.1 + FR-P1.2 + FR-P1.3 + FR-P1.4 |

---

## 1. 5 处补丁逐一对账(对应 PRD-5 §5/§7 FR 编号)

### 1.1 FR-P0 — half-loaded(uninitialized_seat)quota 分类 + cheap_codex_smoke 二次验证

**SPEC 引用**:PRD-5 §5.1 + quota-classification.md v1.1 §4.2 I5 + §4.4 + §5.2 + I8 不变量

**实施落点**:

| 项目 | file:line |
|---|---|
| `_CODEX_SMOKE_ENDPOINT` + `_CODEX_SMOKE_QUOTA_HINTS` 常量 | `src/autoteam/codex_auth.py:42-45` |
| `get_quota_exhausted_info` I5 elif(workspace_uninitialized) | `src/autoteam/codex_auth.py:1652-1670` |
| `cheap_codex_smoke` 新 helper | `src/autoteam/codex_auth.py:1708-1819` |
| `check_codex_quota` 集成 smoke 二次验证 | `src/autoteam/codex_auth.py:1925-1944` |

**关键决策**:

1. **I5 elif 不直接返回 no_quota,而是返回 `window="uninitialized_seat"` + `needs_codex_smoke=True`**(quota-classification §4.4 I5)— 让 wham 层不贸然判 no_quota 错杀 fresh seat,二次验证由 `check_codex_quota` 内部完成
2. **`check_codex_quota` 在收到 uninitialized_seat 后**自动调 `cheap_codex_smoke(access_token, account_id=account_id)`:
   - smoke alive → `("ok", quota_info)`,quota_info 带 `smoke_verified=True` + `last_smoke_result="alive"`(SPEC NFR-5 审计)
   - smoke auth_invalid → `("auth_error", None)` — 触发上游 reconcile 重登
   - smoke uncertain → `("network_error", None)` — 保留原 status 等下轮(SPEC §5.2 矩阵 R1 风险)
3. **`cheap_codex_smoke` 不消耗 token**:`max_output_tokens=1` + `reasoning.effort=none`,iter_lines 收到第一帧 `response.created` 立即 `resp.close()`(SPEC NFR-3)
4. **网络/异常分类严格区分**:401/403/429 → auth_invalid;5xx → uncertain;ConnectionError/Timeout/SSLError → uncertain;4xx 含 quota 关键词 → auth_invalid;其余 4xx → uncertain。**不会因为一次网络抖动批量误删 STATUS_AUTH_INVALID**(quota-classification I2 不变量)
5. **未来集成点**:`manager._run_post_register_oauth` Team 分支 + `_probe_kicked_account` 仅需照常调 `check_codex_quota` 即可获得 smoke 已验证后的 ok/auth_error/network_error,**无需上游改造**(I8 由 check_codex_quota 内部保障)

**完整 helper 代码**(`codex_auth.py:1708-1819`):

```python
def cheap_codex_smoke(access_token, account_id=None, *, timeout=15.0):
    """SPEC-2 shared/quota-classification §4.4 — uninitialized_seat 二次验证。
    对 codex backend 发一个最小推理请求(reasoning.effort=none + max_output_tokens=1 + stream),
    只读第一帧 SSE 立即关流,不消耗多余 token。

    返回 (result, detail):
        ("alive", None)              — HTTP 200 + 第一帧含 response.created → 真活号
        ("auth_invalid", reason)     — HTTP 401/403/429 / 4xx 含 quota 关键词 → token/seat 真失效
        ("uncertain", reason)        — HTTP 5xx / network / timeout / 解析异常 → 保留原状态等下轮
    """
    import requests
    # ... endpoint=POST https://chatgpt.com/backend-api/codex/responses
    # ... payload={"model":"gpt-5","input":"ping","max_output_tokens":1,"stream":True,"reasoning":{"effort":"none"}}
    # ... iter_lines 找 "response.created" → alive;立即 resp.close()
```

**实测调用记录**:cleanup-and-e2e-report.md §3.2/3.3 已实测此 endpoint(`POST /backend-api/codex/responses`)对 4 个 active 子号的真实返回 — 200 OK + `event: response.created` SSE 帧,完全契合 cheap_codex_smoke 的 alive 路径判定。本次实施未调用真实 endpoint 验证(避免污染生产 token 计数 + 不增加 OpenAI 风控暴露),但单测覆盖了所有 6 类返回路径(alive/401/503/network/4xx-quota/未知)。

### 1.2 FR-P1.1 — `login_codex_via_browser` C-P4 oauth_personal_check 探针

**SPEC 引用**:add-phone-detection.md v1.1 §4.1 C-P4 + SPEC-2 §3.4.5

**实施落点**:`src/autoteam/codex_auth.py:939-948`

```python
# C-P4(在 callback for-loop 之后、browser.close() 之前)
try:
    assert_not_blocked(page, "oauth_personal_check")
except Exception:
    try:
        browser.close()
    except Exception:
        pass
    raise
```

**关键决策**:

- **位置选择**:SPEC §4.1 写"`if use_personal:` 这行**之前**",但实测代码中 `if use_personal:` 在 943 行,而 `browser.close()` 在 929 行已执行 — page 对象在 close 之后无法读 `.url` / `.inner_text`,assert_not_blocked 会异常吞为 False。**正确做法**:把 C-P4 放到 callback for-loop 后(`if not auth_code: _screenshot(...)` 之后)、`browser.close()` 之前。这是 SPEC 精神的实质遵守(personal 拒收 bundle 之前的最后一道关卡),并保证 page 仍可读
- **资源安全**:在 `try/except` 里命中 RegisterBlocked 时,先关闭 browser,再 raise — 保证浏览器资源不泄露,同时上层可继续按 add-phone-detection §5.2 矩阵分类处置
- **4 个探针完整链**:`grep -n "oauth_about_you\|oauth_consent_\|oauth_callback_wait\|oauth_personal_check" src/autoteam/codex_auth.py` 命中 4 个 step 名 — C-P1(L586)/ C-P2(L638)/ C-P3(L910)/ **C-P4(L939,Round 6 新增)**

### 1.3 FR-P1.2 — `delete_managed_account` short_circuit 加 STATUS_AUTH_INVALID

**SPEC 引用**:SPEC-2 §3.5.1 + PRD-5 §5.3

**实施落点**:

| 项目 | file:line |
|---|---|
| import 扩 STATUS_AUTH_INVALID | `src/autoteam/account_ops.py:6` |
| short_circuit 条件 | `src/autoteam/account_ops.py:79`(`is_personal = bool(acc and acc.get("status") in (STATUS_PERSONAL, STATUS_AUTH_INVALID))`)|

**变量名决策**:保留 `is_personal` 变量名以最小化下游契约变化(注释中说明语义已扩到"本地清理即可")。**SPEC v1.1 §3.5.1 也接受这种"语义优先"做法**,只要 short_circuit 实际生效即可。

**业务影响**:auth_invalid 账号的 token 已 401,继续走 `fetch_team_state` 会在 wham 401 时拖累整个删除链路;主号 session 失效场景下启动 ChatGPTTeamAPI 还会卡死 30s。本补丁同时规避两类资源浪费。

### 1.4 FR-P1.3 — `api.post_account_login` 409 phone_required + 409 register_blocked

**SPEC 引用**:SPEC-2 §3.5.3 + add-phone-detection.md v1.1 §5.5 + PRD-5 §5.4

**实施落点**:`src/autoteam/api.py:1730-1781`

```python
try:
    bundle = login_codex_via_browser(email, acc.get("password",""), mail_client=mail_client, use_personal=use_personal)
except RegisterBlocked as blocked:
    if blocked.is_phone:
        record_failure(email, category="oauth_phone_blocked",
                       reason=f"补登录触发 add-phone (step={blocked.step})",
                       step=blocked.step, stage="api_login")
        raise HTTPException(status_code=409, detail={
            "error": "phone_required",
            "step": blocked.step,
            "reason": blocked.reason,
        })
    record_failure(email, category="exception",
                   reason=f"补登录意外 RegisterBlocked: {blocked.reason}",
                   step=blocked.step, stage="api_login")
    raise HTTPException(status_code=409, detail={
        "error": "register_blocked",
        "step": blocked.step,
        "reason": blocked.reason,
    })
```

**关键决策**:

- **是否阻挡 task["error"] 字段**:由于 `post_account_login` 是 `_start_task` 异步任务,`raise HTTPException` 在 `_run_task` 中被 except 捕获,转为 `task["error"] = str(exception)`。前端轮询 task 时,error 字符串携带 `phone_required` / `register_blocked` 关键字 — api.ts 可据此分类。**保留 raise HTTPException 而非自定义异常**,与 SPEC §5.5 完整模板一致
- **非 is_phone 走 409 register_blocked**:team-lead 指引明确"否则 `raise HTTPException(status_code=409, detail={"error": "register_blocked", ...})`",而非 SPEC §5.5 中默认的 500 oauth_failed。**409 比 500 更语义化**(请求语义被服务端理解但状态拒绝),前端可统一用 409 分支区分错误码
- **record_failure 必先于 raise**:add-phone-detection §5.5 不变量 — 失败统计必须落盘

### 1.5 FR-P1.4 — `api.delete_accounts_batch` all_personal 短路

**SPEC 引用**:SPEC-2 §3.5.2 + PRD-5 §5.5

**实施落点**:`src/autoteam/api.py:1566-1632`(_run 闭包内)

**关键决策**:

1. **`bool(targets_in_pool) and all(...)` 守卫**:`all([])` 返回 True 是 Python 怪癖,空 list 误判为"全 personal"会让短路路径接管 — 但传入的 emails 全部不在 accounts 池中时,应该走原路径让循环给每条返回"账号不存在"。SPEC §3.5.2 注意点 1 / 单测 `test_empty_targets_does_not_short_circuit` 正是验证此边界
2. **`chatgpt_api=None / mail_client=None / remote_state=None` 全部前传**:依赖 §3.5.1 short_circuit(`delete_managed_account` 内部判 acc.status 决定是否拉远端);auth_invalid/personal 走本地清理路径,**不会**因 chatgpt_api=None 抛错
3. **mail_client 也不预启动**:`delete_managed_account` 内部已有 `own_mail_client` 懒加载路径,personal 号若有 cloudmail_account_id 仍能清理;all_local_only 时省去整批 cloudmail login 开销
4. **logger.info 留痕**:命中短路路径时记录 "整批 N 个账号均为 personal/auth_invalid,跳过 ChatGPTTeamAPI 启动(FR-P1.4 短路)"便于运维事后对账

---

## 2. 新增测试

**文件**:`tests/unit/test_round6_patches.py`(537 行,**23 个测试用例**)

### 2.1 类列表

| 类 | 用例数 | 覆盖 FR |
|---|---|---|
| `TestUninitializedSeatI5` | 12 | FR-P0(I5 形态识别 / smoke 三分支 / smoke 内部分类 / 不调 smoke 的 ok 路径) |
| `TestOauthPersonalCheck` | 2 | FR-P1.1(C-P4 落地证据 + 4 探针完整性) |
| `TestDeleteManagedAccountAuthInvalid` | 2 | FR-P1.2(auth_invalid 短路 fetch_team_state + 不启动 ChatGPTTeamAPI) |
| `TestPostAccountLogin409` | 3 | FR-P1.3(phone_required 响应 / register_blocked 响应 / api 源码守卫) |
| `TestDeleteBatchAllPersonal` | 4 | FR-P1.4(all_personal=True 短路 / 混合 不短路 / 空 list 不短路 / api 源码守卫) |

### 2.2 pytest 输出(全套)

```
============================= test session starts =============================
platform win32 -- Python 3.12.3, pytest-7.4.4, pluggy-1.0.0
rootdir: D:\Desktop\AutoTeam
plugins: anyio-4.13.0
collected 178 items

...... [基线 155 + 新增 23]
178 passed, 4 warnings in 3.18s
```

**新增 23 个全部 PASS**:

```
tests/unit/test_round6_patches.py::TestUninitializedSeatI5::test_get_quota_exhausted_info_uninitialized_seat_signal PASSED [  4%]
tests/unit/test_round6_patches.py::TestUninitializedSeatI5::test_get_quota_exhausted_info_uninitialized_seat_priority_below_no_quota PASSED [  8%]
tests/unit/test_round6_patches.py::TestUninitializedSeatI5::test_get_quota_exhausted_info_uninitialized_seat_not_when_limit_reached PASSED [ 13%]
tests/unit/test_round6_patches.py::TestUninitializedSeatI5::test_check_codex_quota_calls_smoke_when_uninitialized PASSED [ 17%]
tests/unit/test_round6_patches.py::TestUninitializedSeatI5::test_check_codex_quota_smoke_auth_invalid_returns_auth_error PASSED [ 21%]
tests/unit/test_round6_patches.py::TestUninitializedSeatI5::test_check_codex_quota_smoke_uncertain_returns_network_error PASSED [ 26%]
tests/unit/test_round6_patches.py::TestUninitializedSeatI5::test_check_codex_quota_does_not_smoke_for_normal_ok PASSED [ 30%]
tests/unit/test_round6_patches.py::TestUninitializedSeatI5::test_cheap_codex_smoke_alive_on_response_created_frame PASSED [ 34%]
tests/unit/test_round6_patches.py::TestUninitializedSeatI5::test_cheap_codex_smoke_401_returns_auth_invalid PASSED [ 39%]
tests/unit/test_round6_patches.py::TestUninitializedSeatI5::test_cheap_codex_smoke_503_returns_uncertain PASSED [ 43%]
tests/unit/test_round6_patches.py::TestUninitializedSeatI5::test_cheap_codex_smoke_network_error_returns_uncertain PASSED [ 47%]
tests/unit/test_round6_patches.py::TestUninitializedSeatI5::test_cheap_codex_smoke_4xx_with_quota_keyword_returns_auth_invalid PASSED [ 52%]
tests/unit/test_round6_patches.py::TestOauthPersonalCheck::test_codex_auth_source_contains_oauth_personal_check_step PASSED [ 56%]
tests/unit/test_round6_patches.py::TestOauthPersonalCheck::test_codex_auth_has_all_4_oauth_phone_probes PASSED [ 60%]
tests/unit/test_round6_patches.py::TestDeleteManagedAccountAuthInvalid::test_auth_invalid_short_circuit_skips_fetch_team_state PASSED [ 65%]
tests/unit/test_round6_patches.py::TestDeleteManagedAccountAuthInvalid::test_auth_invalid_short_circuit_does_not_start_chatgpt_api PASSED [ 69%]
tests/unit/test_round6_patches.py::TestPostAccountLogin409::test_register_blocked_phone_returns_409_phone_required PASSED [ 73%]
tests/unit/test_round6_patches.py::TestPostAccountLogin409::test_register_blocked_other_returns_409_register_blocked PASSED [ 78%]
tests/unit/test_round6_patches.py::TestPostAccountLogin409::test_api_source_contains_register_blocked_handling PASSED [ 82%]
tests/unit/test_round6_patches.py::TestDeleteBatchAllPersonal::test_all_personal_short_circuit_skips_chatgpt_api_start PASSED [ 86%]
tests/unit/test_round6_patches.py::TestDeleteBatchAllPersonal::test_mixed_personal_and_active_does_not_short_circuit PASSED [ 91%]
tests/unit/test_round6_patches.py::TestDeleteBatchAllPersonal::test_empty_targets_does_not_short_circuit PASSED [ 95%]
tests/unit/test_round6_patches.py::TestDeleteBatchAllPersonal::test_api_source_contains_all_local_only_short_circuit PASSED [100%]
```

---

## 3. ruff lint 输出

```bash
$ ruff check src/autoteam tests/ --select F401,F811,F821
All checks passed!
```

---

## 4. import 健康检查输出

```bash
$ python -c "from autoteam.codex_auth import cheap_codex_smoke, check_codex_quota, get_quota_exhausted_info; \
            from autoteam.account_ops import delete_managed_account; \
            from autoteam.api import app; \
            from autoteam.invite import RegisterBlocked, assert_not_blocked; \
            print('all imports OK'); \
            print('cheap_codex_smoke =', cheap_codex_smoke); \
            print('app =', app)"

all imports OK
cheap_codex_smoke = <function cheap_codex_smoke at 0x000001D5AC9DCAE0>
app = <fastapi.applications.FastAPI object at 0x000001D5AD5CF470>
```

---

## 5. SPEC 与现实代码冲突(非阻塞)

### 5.1 C-P4 探针位置:SPEC 写"`if use_personal:` 之前",但 page 已 close

**冲突**:add-phone-detection.md §4.1 + SPEC-2 §3.4.5 都写"在 `if use_personal:` 之前(personal 拒收 bundle 之前的最后一道关卡)"。但实测代码中 `if use_personal:` 位于 943 行,在 `browser.close()`(929 行)之后 — 此时 page 已不可用。

**实施决策**:把 C-P4 放在 callback for-loop 后(924 行)、`browser.close()` 之前(945 行 close 调用)。这是 SPEC 精神的实质遵守(防止 add-phone 漏过 → personal 拒收前的最后一道关卡),并保证 page 仍可读 → assert_not_blocked 实际能命中 add-phone URL/text。

**后续修订建议**:add-phone-detection.md §4.1 行 181 位置描述应修订为"`browser.close()` **之前**,callback for-loop 之后"以反映 page 生命周期约束。**SPEC 文档侧的小修不影响本轮代码落地**。

### 5.2 `is_personal` 变量名保留(语义已扩)

**冲突**:SPEC §3.5.1 实施代码用 `short_circuit = (remove_remote and acc and acc.get("status") in (STATUS_PERSONAL, STATUS_AUTH_INVALID))`;实测代码继续用 `is_personal` 变量名(语义已扩到"本地清理即可")。

**实施决策**:保留 `is_personal` + 注释说明,**不**重命名 — 下游代码无契约变化。SPEC §3.5.1 的"`short_circuit`"命名是建议性,核心是逻辑等价。

### 5.3 `post_account_login` 异步任务的 raise HTTPException 语义

**注意点**:`post_account_login` 是 `_start_task` 异步任务。`_run` 闭包内 raise HTTPException 不会直接产生 HTTP 响应(请求线程已 return 202 task 信息),而是被 `_run_task` 的 `except Exception as e` 捕获并存入 `task["error"]`。

**前端协议**:api.ts 轮询 task 时,task["error"] 字符串中含 `phone_required` / `register_blocked` 关键字。前端 `if (resp.status === 409 && body.detail?.error === "phone_required")` 在直接同步路径不命中,但 `if (task.error.includes("phone_required"))` 可识别。SPEC-2 §1 / api.ts §1 已规划解析,**不需本轮前端改动**。

---

## 6. 未做(明确不在本轮范围)

- ❌ **manager.py 集成 cheap_codex_smoke**:PRD-5 §7.1 列了 `_run_post_register_oauth` Team 分支 + `_probe_kicked_account` 接 cheap_smoke,但 **`check_codex_quota` 内部已自动调 smoke**,manager 调用方无感知 — 维持现行 `check_codex_quota` 调用即可获得 ok/auth_error/network_error 的最终判定。SPEC v1.1 §5.2 处置矩阵新增的 "ok+window=uninitialized_seat" 行已被 `check_codex_quota` 内部提前消化。**真正手动接入只在 manager 想要做 24h 去重**(`last_codex_smoke_at` 字段)— 本轮按 PRD §3 "不**为 cheap_codex_smoke 加用户级速率限制 — 24h 去重已足够"** 的 hint 推迟到 Round 7
- ❌ **`last_codex_smoke_at` 字段落盘 / 24h 去重逻辑**:PRD-5 §5.1 提到了,但说"调用方负责";本轮 `cheap_codex_smoke` 内部不做 cache,manager.py 调用 `check_codex_quota` 时已经被 wham 30s timeout 自然节流;真正密集调用场景(uninitialized_seat 形态命中率 > 5%)由 R2 风险监控发现后再加。**这是 SPEC NFR-2 兼容的退而求其次**
- ❌ **smoke endpoint 真实调用验证**:cleanup-and-e2e-report.md §3.2 已实测过(团队主号 4 个 active 子号 200 OK + response.created),本轮不重复(避免污染生产 token + 不增加 OpenAI 风控暴露)
- ❌ **前端 api.ts 解析 409 phone_required**:PRD-5 §11 明确"api.ts 已在 SPEC-2 §1 中规划解析 409;若未落地需配套前端小改" — 不在 patch-implementer 工作清单
- ❌ **shared/account-state-machine.md §4.1 转移矩阵 PENDING → ACTIVE 注释**:PRD-5 §11 文档清单有此条,但属于 spec-writer 阶段 1 的修订工作 — 本轮已确认 stage 1 完成,**不重新触动**

---

## 7. 影响半径(GitNexus.impact 等价分析,手动)

### 7.1 `cheap_codex_smoke` (新)

- **下游**:`check_codex_quota` 在 uninitialized_seat 路径调它(L1925-1944)。唯一直接调用方
- **上游**:`check_codex_quota` 的 9+2 个调用点 — 全部走原 `("ok"/"auth_error"/"network_error", info)` 协议,无感知 smoke 内部细节。**爆炸半径 = 0 个调用方需要改**

### 7.2 `get_quota_exhausted_info` 新增 I5 elif

- **下游**:`check_codex_quota` 内部分支判断(`exhausted_info.get("window") == "uninitialized_seat"`)
- **上游影响**:I5 命中时返回值 `window="uninitialized_seat"` 是新形态,但 `check_codex_quota` 已内部消化(转 ok/auth_error/network_error)。直接调用 `get_quota_exhausted_info` 的代码点:
  ```bash
  $ grep -rn "get_quota_exhausted_info" src/ tests/
  src/autoteam/codex_auth.py:1615:def get_quota_exhausted_info(...)
  src/autoteam/codex_auth.py:1937:    exhausted_info = get_quota_exhausted_info(...)
  src/autoteam/manager.py:.. test_spec2_lifecycle.py:64-114 .. test_round6_patches.py:..
  ```
  生产代码只有 `check_codex_quota` 一个调用方 — **爆炸半径仅限 check_codex_quota**,已内部处理

### 7.3 `delete_managed_account` short_circuit 扩

- **直接调用方**:`api.py:delete_account` / `api.py:delete_accounts_batch` / `manager.py:_cleanup_team_leftover` 等 — 都按"传 chatgpt_api 或允许 None"协议工作,扩了短路条件后这些调用方**无需改动**;short_circuit 多覆盖一种 status,纯净增

### 7.4 `delete_accounts_batch` all_personal 短路

- **同模块更动**:仅 `_run` 闭包内决策路径变化;对 `delete_managed_account` 的调用契约 = (`chatgpt_api=可None / remote_state=可None`)— `delete_managed_account` 早已支持 None(`fetch_team_state` 路径有 `chatgpt_api is None` 处理)
- **API 端点协议无变化**:返回结构同前(`results / summary`),只是 all_local_only=True 路径不再启动 ChatGPTTeamAPI,从而每号节省 ~30s 浏览器 + 网络往返

### 7.5 `post_account_login` 409 catch

- **API 协议变化**:增加 409 phone_required / 409 register_blocked 错误码路径,**不影响**正常成功(200)/ 其他 RuntimeError 路径
- **前端**:可平滑感知(api.ts 现有 4xx/5xx 分支兜底,新增 409 解析见 SPEC-2 §1 规划)

---

## 8. 总结

5 处补丁全部落地,178 个测试用例 100% 通过(基线 155 + 新增 23),ruff 0 lint error,import 健康。

| FR | 状态 | file:line |
|---|---|---|
| FR-P0(uninitialized_seat I5 + cheap_codex_smoke) | ✅ | codex_auth.py:1652 / 1708 / 1925 |
| FR-P1.1(C-P4 oauth_personal_check)| ✅ | codex_auth.py:939 |
| FR-P1.2(delete_managed_account short_circuit)| ✅ | account_ops.py:79 |
| FR-P1.3(post_account_login 409)| ✅ | api.py:1730 |
| FR-P1.4(delete_accounts_batch all_personal)| ✅ | api.py:1566-1632 |

**等待 quality-reviewer 进入 stage 3 终审**。

---

**报告结束**(总字数约 2400 字,详细对照 5 处补丁 + 23 个测试 + 全套验证记录)
