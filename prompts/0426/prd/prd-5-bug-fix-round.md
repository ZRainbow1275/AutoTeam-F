# PRD-5: Round 6 Bug Fix Round (P0 + 4 P1)

## 0. 元数据

| 字段 | 值 |
|---|---|
| PRD 编号 | PRD-5 |
| 主题 | Round 5 verify 揭示的 5 处 SPEC 偏差修复 |
| 关联 verify | `prompts/0426/verify/wave1-4-integration-report.md`(2700 字) + `prompts/0426/verify/cleanup-and-e2e-report.md`(1900 字) |
| 关联 SPEC | `spec/spec-2-account-lifecycle.md` + `spec/shared/quota-classification.md` + `spec/shared/add-phone-detection.md` + `spec/shared/account-state-machine.md` |
| 关联 issue | issue#6(残余:half-loaded workspace 漏判) |
| 主笔 | spec-writer (Round 6) |
| 创建日期 | 2026-04-26 |
| 状态 | DRAFT(待 patch-implementer 落地) |
| 修复粒度 | P0 × 1(quota half-loaded)+ P1 × 4(C-P4 探针 / delete short_circuit / 409 phone_required / 批量 all_personal 短路) |
| 优先级 | P0 阻塞发版,P1 全部一轮内合并 |

---

## 1. 背景

Round 5 的两组验证(`integration-verifier` 全量 SPEC 对账 + `production-cleaner` 4 个 active 号 cheap codex probe 实测)联合揭示:Wave 1-4 落地约 85%,但**关键的 issue#6 修复并不彻底**,且 SPEC-2 §3.4 / §3.5 / §S-2.2 三段共 4 处契约未在代码侧落地。

### 1.1 P0 — half-loaded workspace 漏判(issue#6 残余)

`production-cleaner` 实测,4 个新邀请 fresh seat 的 wham/usage 真实返回是:

```json
{"primary_pct": 0, "primary_resets_at": 1777197556,
 "primary_total": null, "primary_remaining": null,
 "weekly_pct": 0, "weekly_resets_at": 1777784356}
```

而当前 `codex_auth.py:get_quota_exhausted_info` 的 3 条 no_quota 判据:

| 触发条件 | 实际值 | 命中 |
|---|---|---|
| `primary_total == 0` | None | ✗ |
| `primary_total is None AND primary_pct == 0 AND primary_reset == 0 AND not limit_reached` | reset > 0 | ✗ |
| `primary_remaining == 0 AND primary_total in (0, None) AND primary_pct == 0` | remaining = None | ✗ |

三条都漏判 → fall through 到 ok 分支 → 账号被错误标 STATUS_ACTIVE。**这是 issue#6 第二个表面相同(UI 显示 100% 剩余)但根因不同的失效路径**。`production-cleaner` 用 cheap codex probe(`POST /backend-api/codex/responses` + stream + 立即关流)实测确认:这 4 个号是真活的(200 OK + `response.created` 事件),OpenAI wham/usage 在 fresh seat 上做了懒初始化,`primary_total` 在第一次消费 token 之前不写出。

但**未来某次** OpenAI API 协议微调可能把"workspace 真无配额"也用同形态(`primary_total=null + reset>0`)返回。当前实现既不能区分二者,也没有兜底验证手段,issue#6 等价场景必然复发。

### 1.2 P1 × 4 — SPEC 已写但代码未落地

`integration-verifier` 全文 SPEC 对账后明确 4 处偏差:

1. **`login_codex_via_browser` 缺 C-P4 `oauth_personal_check` 探针**
   - SPEC-2 §3.4 + `shared/add-phone-detection §4.1 / §4.2` 都明确要求 OAuth 4 处探针,实际只落地 3 处(`codex_auth.py:581/633/905`),personal 拒收 bundle 之前的"最后一道关卡"缺失
2. **`account_ops.delete_managed_account` short_circuit 缺 STATUS_AUTH_INVALID**
   - SPEC-2 §3.5.1 / FR-G2 要求短路条件 `status in (STATUS_PERSONAL, STATUS_AUTH_INVALID)`,实际只判 STATUS_PERSONAL。auth_invalid 账号删除仍启动 ChatGPTTeamAPI(浏览器 + 30s 网络),浪费资源且在主号失效场景下直接卡死
3. **`api.post_account_login` 缺 RegisterBlocked catch + 409 phone_required**
   - SPEC-2 §S-2.2 + `shared/add-phone-detection §5` 第 5 行要求该端点显式分类,实际是裸 try/except 把所有失败转 500;UI 拿不到语义化"需要绑定手机"提示
4. **`api.delete_accounts_batch` 缺 all_personal 短路**
   - SPEC-2 §3.5.2 / FR-G3 要求批量删除全 personal/auth_invalid 时整批跳过 ChatGPTTeamAPI;实际无条件 `chatgpt_api.start()`,违背 G2 单点短路精神

### 1.3 不修的代价

- **不修 P0**:用户报告的 issue#6 等价复发概率随 OpenAI 协议变化非零,且影响面大(整池 active 号一夜全标错状态);更直接的影响是:任何一次 cheap probe 网络抖动 + half-loaded 形态共现时,我们没法区分"真活但懒初始化"和"真无配额",只能盲目相信 wham
- **不修 P1.1**:某次 about-you / consent / callback 三处探针漏过的"边角 add-phone"会让 personal 模式继续运行到 plan_type 校验拒收,留下噪声日志,但可能引发后续误删 — 这条偏差防御性强、修复廉价(5 行)
- **不修 P1.2 / P1.4**:auth_invalid + 批量删除场景每次都拉 30s 浏览器,在主号 session 失效时连删 5 个号要 150s,且过程中可能触发更多 401
- **不修 P1.3**:UI/UX 损失,但不阻塞业务

---

## 2. 目标

| # | 目标 | 衡量 |
|---|---|---|
| G1 | half-loaded workspace 不会再被错标 ACTIVE | quota-classification 加 I5 + 测试样本 `no_quota_workspace_uninitialized` 命中 |
| G2 | uninitialized_seat 通过 cheap codex smoke 二次验证消歧,200 → ok / 4xx → AUTH_INVALID / 5xx → 保 ACTIVE | smoke 函数有 24h 去重 |
| G3 | OAuth personal 拒收前必经过 C-P4 探针 | grep `oauth_personal_check` 命中 1 处 |
| G4 | auth_invalid 单点 / 批量删除一律不启动 ChatGPTTeamAPI | 两处单测断言 `chatgpt_api is None` 路径 |
| G5 | post_account_login 撞 add-phone 时返回 409 phone_required | 集成测试断言响应体 `{"error": "phone_required", ...}` |
| G6 | 不引入回归 | pytest 全 155 用例 + 新增 ~40 用例全绿,ruff 0 lint error |

---

## 3. 非目标(明确不做)

- **不**重构现有 `_run_post_register_oauth` / `reinvite_account` / `sync_account_states` 业务逻辑(Round 4 已落地,本轮只补 4 处缺失契约)
- **不**改写 `check_codex_quota` 的 5 分类签名(签名稳定;只在 `get_quota_exhausted_info` 里加 1 个 elif 分支 + 上游加 cheap smoke 二次验证)
- **不**修 P2 列表中的命名偏差(`preferred_seat_type` 的 `default` vs `chatgpt`)/ `MailProviderCard.vue` 抽组件 / `tests/test_setup_wizard_sniff_block.py` 重复文件清理 — 这些进 Round 7 或更后
- **不**对主号路径(`login_codex_via_session` / `MainCodexSyncFlow`)加探针 — 主号都已绑手机
- **不**为 cheap_codex_smoke 加用户级速率限制 — 24h 去重 + 单调用 timeout 15s 已足够

---

## 4. 用户故事

### 4.1 运维

> 作为运维,我新邀请一批账号进 Team 后立即跑 fill 命令。OpenAI 此时 wham 返回 `primary_total=null`,过去会被错标 ACTIVE,被路由调用就报"no quota"。我希望系统能自己用 cheap probe 二次验证,而不是让我一个个手测。

### 4.2 开发者

> 作为开发者删除 30 个 auth_invalid 账号,过去要等 30s × 30 = 900s。我希望批量删除路径检测全是 personal/auth_invalid 时整批短路,几秒内完成。

### 4.3 终端用户

> 作为前端用户,在 Settings 页点"补登录",撞 add-phone 时希望看到"该账号需要绑定手机才能继续",而不是"500 服务器错误"。

---

## 5. 功能需求

### 5.1 FR-P0:half-loaded(uninitialized_seat)quota 分类

**规范**:

`get_quota_exhausted_info` 在现有 3 条 no_quota 判据后加第 4 条 elif,识别 fresh seat 懒初始化:

```python
# codex_auth.py:1620 之后(no_quota_signals 末尾追加)
elif (
    primary_total is None
    and primary_remaining is None
    and primary_pct == 0
    and weekly_pct == 0
    and primary_reset > 0
    and not limit_reached
):
    no_quota_signals.append("workspace_uninitialized")
```

**关键**:此条件不直接判 `no_quota`,而是返回新 window 形态 `"uninitialized_seat"`,**同时**在 `QuotaExhaustedInfo` 加 `needs_codex_smoke: True` 字段,**不**走"锁 5h"也**不**走"AUTH_INVALID"路径,而是**强制上游**调用方调一次 `cheap_codex_smoke(access_token, account_id)` 做最终判定。

**cheap_codex_smoke 实现要点**(新增到 `codex_auth.py`,与 `check_codex_quota` 同邻):

- endpoint:`POST https://chatgpt.com/backend-api/codex/responses`
- payload 最小化(`reasoning.effort: "none"` + stream + `instructions=""` + `input=[{"text":"ok"}]`)
- header:`Authorization: Bearer <access_token>` + `Chatgpt-Account-Id: <account_id>`
- 返回值:`Literal["alive", "auth_invalid", "uncertain"]`
  - HTTP 200 + 第一帧 `response.created` → alive
  - HTTP 401 / 403 / 429(quota 关键词)/ 4xx 含 quota → auth_invalid
  - HTTP 5xx / network → uncertain
- timeout 15s
- **必须**只读第一帧 SSE 然后 close 连接(不消耗 token)
- 24h 去重:account 维度落 `last_codex_smoke_at`,在 24h 内的 cache 命中直接返回上次结果

**上游调用方**(`_run_post_register_oauth` / `_probe_kicked_account` / `cmd_check`):

- 收到 `quota_status="ok"` 但 `info.window=="uninitialized_seat"` 时,调用 `cheap_codex_smoke`
  - alive → 真 ok,写 `last_codex_smoke_at` + 维持 STATUS_ACTIVE
  - auth_invalid → STATUS_AUTH_INVALID + record_failure("no_quota_assigned" 或 "auth_error_at_oauth")
  - uncertain → 保留原 status,等下轮(避免抖动误标)

### 5.2 FR-P1.1:`login_codex_via_browser` C-P4 oauth_personal_check 探针

**位置**:`codex_auth.py` `login_codex_via_browser` 函数内,personal 拒收 bundle 之前(对应 SPEC §4.1 中"`if use_personal:` 分支后、`leave_workspace()` 调用前")。

**实施**:

```python
# codex_auth.py 在 personal 模式 plan_type 校验前
assert_not_blocked(page, "oauth_personal_check")  # ★新增 C-P4
if use_personal:
    plan = (bundle.get("plan_type") or "").lower()
    ...
```

**验收**:`grep -rn "oauth_personal_check" src/autoteam/codex_auth.py` 至少 1 命中。

### 5.3 FR-P1.2:`delete_managed_account` auth_invalid 短路

**位置**:`account_ops.py:77`

**实施**:

```python
# account_ops.py 顶部 import
from autoteam.accounts import STATUS_PERSONAL, STATUS_AUTH_INVALID

# 改造 short_circuit 判断
short_circuit = (
    remove_remote
    and acc
    and acc.get("status") in (STATUS_PERSONAL, STATUS_AUTH_INVALID)
)
```

**验收**:单测 `test_delete_managed_account_skips_team_api_for_auth_invalid` 验证 `chatgpt_api is None` 时不抛错且本地清理完成。

### 5.4 FR-P1.3:`api.post_account_login` 409 phone_required

**位置**:`api.py:1675`(`post_account_login` 调用 `login_codex_via_browser` 处)

**实施**:

```python
from autoteam.invite import RegisterBlocked
from fastapi import HTTPException

try:
    bundle = login_codex_via_browser(email, password, mail_client=mail_client)
except RegisterBlocked as blocked:
    if blocked.is_phone:
        record_failure(
            email,
            category="oauth_phone_blocked",
            reason=f"补登录触发 add-phone (step={blocked.step})",
            step=blocked.step, stage="api_login",
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "phone_required",
                "step": blocked.step,
                "reason": blocked.reason,
            },
        )
    raise HTTPException(status_code=500, detail={"error": "oauth_failed", "reason": str(blocked)})
```

**验收**:集成测试 mock `login_codex_via_browser` 抛 `RegisterBlocked(is_phone=True)`,断言响应 `status_code == 409 and body["error"] == "phone_required"`。

### 5.5 FR-P1.4:`api.delete_accounts_batch` all_personal 短路

**位置**:`api.py:1573` 区域

**实施**(伪代码):

```python
from autoteam.accounts import STATUS_PERSONAL, STATUS_AUTH_INVALID

def delete_accounts_batch(emails: list[str]):
    accounts = load_accounts()
    targets = [a for a in accounts if a["email"].lower() in {e.lower() for e in emails}]

    all_personal = bool(targets) and all(
        a["status"] in (STATUS_PERSONAL, STATUS_AUTH_INVALID) for a in targets
    )

    chatgpt_api = None
    if not all_personal:
        chatgpt_api = ChatGPTTeamAPI()
        chatgpt_api.start()

    try:
        results = []
        for acc in targets:
            cleanup = delete_managed_account(
                acc["email"], chatgpt_api=chatgpt_api, sync_cpa_after=False,
            )
            results.append({"email": acc["email"], "success": True, "cleanup": cleanup})
        sync_to_cpa()
        return results
    finally:
        if chatgpt_api:
            chatgpt_api.stop()
```

**验收**:单测 mock `ChatGPTTeamAPI`,批量传 5 个 personal/auth_invalid 邮箱,断言 `ChatGPTTeamAPI.start()` 0 次调用。

---

## 6. 非功能需求

| # | NFR | 衡量 |
|---|---|---|
| NFR-1 | cheap_codex_smoke 单调用 P95 < 5s | 实测落 last_codex_smoke_at 时记录 elapsed |
| NFR-2 | uninitialized_seat 处置不破坏 sync_account_states 30s 周期上限 | 24h 去重 + 整体并发 5 已存在,无需改 |
| NFR-3 | 不触发额外 token 消耗 | 只读第一帧 SSE 立即关流 |
| NFR-4 | 旧 accounts.json 兼容 | `last_codex_smoke_at` 字段 Optional,旧记录默认 None |
| NFR-5 | 审计可追溯 | record_failure 与 cheap_codex_smoke 命中记录写 `last_smoke_result` 字段(alive/auth_invalid/uncertain) |

---

## 7. 技术方案

### 7.0 Finalize 决策(2026-04-26 user 已确认)

| 决策点 | 决策值 | 影响位置 |
|---|---|---|
| **Q-1 cheap_codex_smoke endpoint** | `POST https://chatgpt.com/backend-api/codex/responses` + `stream=true` + `reasoning.effort="none"` + `store=false` + 5s timeout + 只读第一帧 `event: response.created` SSE 立即 close | `codex_auth.py` 新函数 + `quota-classification.md §4.4` |
| **Q-2 24h 去重粒度** | account 维度,新增字段 `last_codex_smoke_at`(epoch float)+ `last_smoke_result`(`alive` / `auth_invalid` / `uncertain`)写入 accounts.json;24h 内 cache 命中直接返回上次结果 | `accounts.json` Schema + `manager.py:_resolve_uninitialized_seat` 工具 |
| **Q-3 409 phone_required body** | 不带截图相对路径,保持端点 lean。前端如需截图自己 GET `/api/screenshots/...` 或从 `register_failures.json` 读 `step + url + screenshot_path` | `api.py:post_account_login` |
| 失败分类(codex/responses) | 200 + response.created → alive / 401, 403, 429, 4xx 含 quota|no_seat|billing|no_subscription 关键词 → auth_invalid / 5xx, network, timeout, 200 但未读到 response.created → uncertain | 同 Q-1 |
| 处置 | alive → 维持 STATUS_ACTIVE / auth_invalid → STATUS_AUTH_INVALID + record_failure("no_quota_assigned") / uncertain → 保留原状态(不更新) | 同 Q-2 |

### 7.1 代码改动总览

| 文件 | 行数估计 | 函数 |
|---|---|---|
| `src/autoteam/codex_auth.py` | +25(no_quota_signal `workspace_uninitialized`)+ 90(`cheap_codex_smoke` + 常量) | `get_quota_exhausted_info`(L1620 区)+ `cheap_codex_smoke`(新)+ `_CODEX_SMOKE_ENDPOINT` / `_CODEX_SMOKE_TIMEOUT` / `_CODEX_SMOKE_DEDUP_SECONDS` 常量 |
| `src/autoteam/manager.py` | +50(`_resolve_uninitialized_seat` 工具函数 + 2 处接入:`_run_post_register_oauth` Team 分支 + `_probe_kicked_account`) | 现有函数小改 + 新工具 |
| `src/autoteam/accounts.py` | +2(JSON Schema 加 `last_codex_smoke_at` + `last_smoke_result` 字段) | AccountRecord(Pydantic 可选)|
| `src/autoteam/account_ops.py` | +3(short_circuit 加 `STATUS_AUTH_INVALID`) | `delete_managed_account` |
| `src/autoteam/api.py` | +12(post_account_login try/except + 409)+ 18(delete_accounts_batch all_personal 短路) | 两个端点 |
| `src/autoteam/codex_auth.py` 顶部 | +2 | import & 常量 |
| `tests/unit/test_quota_classification.py` | +40 | 新增 `no_quota_workspace_uninitialized` + `cheap_codex_smoke_alive` + `_4xx_quota` + `_5xx_uncertain` + `_24h_dedup_cache_hit` 等 5 case |
| `tests/unit/test_oauth_phone_detection.py` | +10 | C-P4 接入位测试 |
| `tests/unit/test_account_ops.py` | +25 | auth_invalid short_circuit + 批量 all_personal |
| `tests/unit/test_api_login.py` | +20 | 409 phone_required |

总计:~235 行代码 + ~95 行测试。

### 7.2 落地顺序

1. **S-1**(P0):`codex_auth.py` 加 `cheap_codex_smoke` + `get_quota_exhausted_info` 的 4th elif → 配套测试
2. **S-2**(P0):manager.py `_run_post_register_oauth` Team 分支 + `_probe_kicked_account` 接 cheap_smoke
3. **S-3**(P1):account_ops `STATUS_AUTH_INVALID` 短路 + 单测
4. **S-4**(P1):api `post_account_login` 409 phone_required + 单测
5. **S-5**(P1):api `delete_accounts_batch` all_personal + 单测
6. **S-6**(P1):codex_auth `login_codex_via_browser` C-P4 探针 + 单测
7. **S-7**:全套 pytest + ruff 通过

### 7.3 关键验证测试

```python
# tests/unit/test_quota_classification.py 新增 fixture
"no_quota_workspace_uninitialized": {
    "status_code": 200,
    "body": {
        "rate_limit": {
            "primary_window": {"used_percent": 0, "reset_at": 1777197556,
                                "limit": None, "remaining": None},
            "secondary_window": {"used_percent": 0, "reset_at": 1777784356}
        }
    }
}

def test_uninitialized_seat_does_not_become_active_without_smoke():
    """workspace_uninitialized 形态必须返回 needs_codex_smoke=True"""
    ...

def test_cheap_codex_smoke_alive_keeps_active():
    """smoke 200 OK → 维持 ACTIVE"""
    ...

def test_cheap_codex_smoke_4xx_marks_auth_invalid():
    """smoke 401/403/429 → STATUS_AUTH_INVALID"""
    ...
```

---

## 8. 验收标准

- [ ] `get_quota_exhausted_info` 加 4th no_quota_signal `workspace_uninitialized`
- [ ] `cheap_codex_smoke` 新函数实现 + 24h 去重
- [ ] `_run_post_register_oauth` Team 分支接 cheap_smoke
- [ ] `_probe_kicked_account` 接 cheap_smoke(uninitialized_seat 路径)
- [ ] `login_codex_via_browser` C-P4 `oauth_personal_check` 探针接入
- [ ] `delete_managed_account` short_circuit 含 `STATUS_AUTH_INVALID`
- [ ] `post_account_login` catch RegisterBlocked → HTTP 409 phone_required
- [ ] `delete_accounts_batch` all_personal 短路
- [ ] tests/unit/ 新增 ~85 行测试全部通过
- [ ] pytest 全套(原 155 + 新 ~40)0 失败
- [ ] ruff 0 lint error
- [ ] 3 份 SPEC 文档同步修订

---

## 9. 测试计划

### 9.1 单元

| 测试模块 | 关键 case |
|---|---|
| `test_quota_classification.py` | `no_quota_workspace_uninitialized` → window=`uninitialized_seat`,`needs_codex_smoke=True` |
| `test_quota_classification.py` | smoke alive 后维持 ok |
| `test_quota_classification.py` | smoke 401 后 AUTH_INVALID |
| `test_quota_classification.py` | smoke 5xx 后保留原 status |
| `test_oauth_phone_detection.py` | C-P4 命中 `oauth_personal_check` step |
| `test_account_ops.py` | auth_invalid 单点删除 chatgpt_api=None 路径 |
| `test_api_login.py` | 409 phone_required 响应体 |
| `test_api_login.py` | 批量 all_personal 0 次 ChatGPTTeamAPI.start |

### 9.2 集成

- 模拟 fresh seat 注册:bundle ok + wham 返回 uninitialized_seat → cheap_smoke alive → STATUS_ACTIVE
- 模拟管理员真无配额:wham uninitialized_seat → smoke 401 → STATUS_AUTH_INVALID

### 9.3 回归

- 跑全套 pytest(155 + 新增 ~40)
- 跑 `python -m autoteam fill --target 1` 单号最小 e2e(若用户授权)

---

## 10. 灰度 / 回滚

### 10.1 灰度

- 默认开启(无 feature flag);P0 必须修
- 上线后 24h 内监控 `last_codex_smoke_result` 分布(应以 alive 为主,若 auth_invalid 比例 > 5% 立即排查 OpenAI API 协议变化)

### 10.2 回滚

- 回滚到 commit `478c16c` 即可;无数据迁移
- 新增字段 `last_codex_smoke_at` / `last_smoke_result` 旧版本会忽略,无破坏

---

## 11. 文档影响清单

- [x] PRD-5(本文档,v1.0 → v1.1 含 §7.0 finalize 决策表 + §13 Q-1/Q-2/Q-3 已决策)
- [x] `spec/shared/quota-classification.md` v1.0 → v1.2(§4.2 加 I5、§4.4 finalize cheap_codex_smoke 完整代码、§6 加 I8、§5.2 处置矩阵新增 uninitialized_seat 行、附录 B 修订记录)
- [x] `spec/shared/add-phone-detection.md` v1.0 → v1.1(§4.1 强调 C-P4 必修、§5.2 第 5 行 409 详化、§5.5 全新 post_account_login 完整模板、§6 加 2 个测试 case、附录 B 修订记录)
- [x] `spec/spec-2-account-lifecycle.md` v1.0 → v1.1(§3.4.5 全新 OAuth 4 探针清单、§3.5.1 short_circuit 扩 AUTH_INVALID、§3.5.2 加 bool(targets) 守卫、§3.5.3 全新 post_account_login 409 详细契约、附录 A 修订记录)
- [ ] `prompts/0426/spec/shared/account-state-machine.md` §4.1 转移矩阵第 1 行 PENDING → ACTIVE 加注"经 cheap_codex_smoke 验证后"(可由 patch-implementer 顺手或 Round 7 补)

---

## 12. 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | cheap_codex_smoke endpoint 协议被 OpenAI 变更 | 24h 去重 + uncertain 分支保留原状态;协议变更时 alive 比例下降会被监控发现 |
| R2 | uninitialized_seat 形态命中率超预期高 → smoke 调用密集 | 24h 去重已挡;若每分钟 > 1 调用则触发统计告警 |
| R3 | C-P4 探针误报(personal 真合法的 phone 帮助文字) | 复用 `assert_not_blocked` 严格规则(URL 强信号 + 文本+tel input 组合),与 C-P1~C-P3 同语义 |
| R4 | 409 phone_required 响应体破坏前端旧解析 | api.ts 已在 SPEC-2 §1 中规划解析 409;若未落地需配套前端小改 |
| R5 | all_personal 短路逻辑漏判 mixed 场景(2 personal + 1 active) | `bool(targets) and all(...)` 严格判全等,空 list 不短路 |

---

## 13. 已决策(Round 6 finalize 2026-04-26 user 确认)

### Q-1:cheap_codex_smoke endpoint(✅ 已决策)

**决策**:`POST https://chatgpt.com/backend-api/codex/responses` + `stream=true` + `reasoning.effort="none"` + `store=false` + 5s timeout。

**理由**:`production-cleaner` Round 5 实测 4 个 fresh seat 都 200 alive、3 秒内拿到 `response.created` SSE 帧 + `resp_id`。该 endpoint 直接覆盖"codex seat 配额是否真分配",比 `/me` 类纯 metadata endpoint 信号更强(后者只能验 token 有效性)。

**实现要点**:开 stream → `iter_lines()` 读到第一帧 `event: response.created` 立即 close → 5s timeout → 200+frame=alive / 401/403/429/4xx_quota=auth_invalid / 5xx/network/timeout=uncertain。完整代码见 `quota-classification.md §4.4`。

### Q-2:24h 去重粒度(✅ 已决策)

**决策**:account 维度。新增字段 `last_codex_smoke_at`(epoch float)+ `last_smoke_result`(`"alive"` / `"auth_invalid"` / `"uncertain"`)写 accounts.json,24h cache 命中直接返回上次结果。

**理由**:简洁优先。token 刷新场景(access_token 被 refresh_token 替换后严格说应重新 smoke)在 Round 7 视实际误判数据决议是否补 `last_smoke_token_id` 字段。

**配置**:`_CODEX_SMOKE_DEDUP_SECONDS = 86400`(可选后续做 runtime_config getter)。

### Q-3:409 phone_required body(✅ 已决策)

**决策**:不带截图相对路径,保持端点 lean。响应 body 仅含 `{"detail": {"error": "phone_required", "step": <C-Px step 名>, "reason": <blocked.reason>}}`。

**理由**:前端如需截图,自己 GET `/api/screenshots/...` 或从 `register_failures.json` 取已落盘的 `step + url + screenshot_path`(`record_failure` 已记)。避免每个端点 contract 扩散到截图字段。

---

## 14. Story Map

```
Phase 0(SPEC 修订,本 spec-writer 阶段)
├─ S-0.1 修 quota-classification.md §4.2 + I5
├─ S-0.2 修 add-phone-detection.md §4.1 C-P4 + §5
├─ S-0.3 修 spec-2-account-lifecycle.md §3.4.5 / §3.5 / §S-2.2
└─ S-0.4 写 PRD-5(本文件)

Phase 1(P0 — quota half-loaded,patch-implementer)
├─ S-1.1 codex_auth.get_quota_exhausted_info 加 4th elif
├─ S-1.2 codex_auth.cheap_codex_smoke 新函数
├─ S-1.3 manager._run_post_register_oauth Team 分支接 smoke
├─ S-1.4 manager._probe_kicked_account 接 smoke
└─ S-1.5 单测 4 case

Phase 2(P1.1 — C-P4 探针)
└─ S-2.1 codex_auth.login_codex_via_browser 加 oauth_personal_check

Phase 3(P1.2 — delete short_circuit)
└─ S-3.1 account_ops.py 短路扩 AUTH_INVALID + 单测

Phase 4(P1.3 — post_account_login 409)
└─ S-4.1 api.py 端点 try/except + 单测

Phase 5(P1.4 — delete_accounts_batch 短路)
└─ S-5.1 api.py 端点 all_personal + 单测

Phase 6(quality-reviewer 阶段)
├─ S-6.1 全套 pytest 验证
├─ S-6.2 ruff lint 0 error
└─ S-6.3 e2e fill --target 1(若 user 授权)
```

**关键串行链**:S-0 → S-1 → S-2..5(P1 4 个补丁可在 S-1 完成后并行)→ S-6

---

**文档结束。** 总字数约 2700 字。
