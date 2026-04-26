# SPEC-2: 账号生命周期与配额加固 实施规范

## 0. 元数据

| 字段 | 内容 |
|---|---|
| 编号 | SPEC-2 |
| 名称 | 账号生命周期与配额加固 实施规范 |
| 主笔 | prd-lifecycle |
| 时间 | 2026-04-26 |
| 版本 | v1.4 (2026-04-26 Round 7 P2 follow-up — 命名归一化 + task["error"] 关键字契约 + 24h 去重接入) |
| 关联 PRD | [`../prd/prd-2-account-lifecycle.md`](../prd/prd-2-account-lifecycle.md) · [`../prd/prd-5-bug-fix-round.md`](../prd/prd-5-bug-fix-round.md) · [`../prd/prd-6-p2-followup.md`](../prd/prd-6-p2-followup.md) |
| 引用 shared spec | [`./shared/plan-type-whitelist.md`](./shared/plan-type-whitelist.md) · [`./shared/quota-classification.md`](./shared/quota-classification.md) · [`./shared/add-phone-detection.md`](./shared/add-phone-detection.md) · [`./shared/account-state-machine.md`](./shared/account-state-machine.md) |
| 覆盖 FR | A1~A5 / B1~B4 / C1~C5 / D1~D4 / E1~E4 / F1~F6 / G1~G4 / H1~H3 / **P0 / P1.1~P1.4(Round 6)** / **P2.1 / P2.5 / D6 / D7(Round 7)** |

---

## 1. 文件级修改清单

| 文件 | 预估改动行数 | 涉及函数/对象 |
|---|---|---|
| `src/autoteam/accounts.py` | +35 | 新增 `SUPPORTED_PLAN_TYPES` / `is_supported_plan` / `normalize_plan_type` / 扩 `add_account` 默认字段 |
| `src/autoteam/codex_auth.py` | ~80 | `_exchange_auth_code`(扩 bundle 字段)/ `login_codex_via_browser`(4 处探针)/ `check_codex_quota`(no_quota 分支)/ `get_quota_exhausted_info`(no_quota 短路) |
| `src/autoteam/manager.py` | ~250 | `_run_post_register_oauth`(quota probe + 5 类处置 + RegisterBlocked catch)/ `sync_account_states`(被踢识别 + 并发探测)/ `reinvite_account`(plan_drift / phone_blocked 兜底)/ `_check_pending_invites`(L1057 RegisterBlocked catch) |
| `src/autoteam/chatgpt_api.py` | ~30 | `_invite_member_once`(allow_patch_upgrade 参数)/ `_invite_member_with_fallback`(分支调整) |
| `src/autoteam/invite.py` | ~5 | `run` / `cmd_fill_team` 入口读 `PREFERRED_SEAT_TYPE`(L496) |
| `src/autoteam/runtime_config.py` | +30 | 新增 `get_preferred_seat_type` / `set_preferred_seat_type` / `get_sync_probe_concurrency` 等 |
| `src/autoteam/api.py` | ~50 | `/api/accounts/{email}/login`(409 phone_required)/ `delete_accounts_batch`(全 personal 短路) |
| `src/autoteam/account_ops.py` | ~25 | `delete_managed_account`(short_circuit 逻辑) |
| `src/autoteam/manual_account.py` | ~20 | `_finalize_account`(plan_supported 检查 + no_quota 处置) |
| `src/autoteam/register_failures.py` | +5 | docstring 扩 6 个新 category |
| `web/src/views/Settings.vue` | +60 | 邀请席位偏好下拉 / 探测并发 / 探测去重 |
| `web/src/components/Dashboard.vue` | ~40 | removeAccount toast / quota 显示识别 no_quota / status 文案 |
| `web/src/api.ts` | +20 | 解析 409 phone_required + 422 no_quota |

**总计**:13 个文件,预计 +650 行 / -120 行。

---

## 2. 引用的 shared specs

本主 spec 不重复定义以下契约,实施时请直接打开对应 shared:

| shared spec | 提供 |
|---|---|
| [`./shared/plan-type-whitelist.md`](./shared/plan-type-whitelist.md) | `SUPPORTED_PLAN_TYPES` 常量、`is_supported_plan` / `normalize_plan_type` 工具函数、bundle 字段扩展、6 调用点处置矩阵 |
| [`./shared/quota-classification.md`](./shared/quota-classification.md) | `check_codex_quota` 5 分类签名、QuotaSnapshot / QuotaExhaustedInfo 类型、9+2 调用点处置 |
| [`./shared/add-phone-detection.md`](./shared/add-phone-detection.md) | RegisterBlocked / assert_not_blocked 复用契约、4 探针接入点位置、5 调用方处置模板 |
| [`./shared/account-state-machine.md`](./shared/account-state-machine.md) | 7 状态完整状态机、AccountRecord Pydantic 模型、转移矩阵、不变量 |

---

## 3. 函数级修改详情

### 3.1 `_run_post_register_oauth` 加 quota probe 与 RegisterBlocked catch

**文件**:`src/autoteam/manager.py:1386-1486`
**FR**:D1~D4 + C3(Team / personal 调用点)

#### 3.1.1 改造前(精简版,见 §1463-1486)

```python
bundle = login_codex_via_browser(email, password, mail_client=mail_client)
if bundle:
    auth_file = save_auth_file(bundle)
    bundle_plan = (bundle.get("plan_type") or "").lower()
    seat_label = "chatgpt" if bundle_plan == "team" else "codex"
    update_account(email, status=STATUS_ACTIVE, seat_type=seat_label, auth_file=auth_file, ...)
    return email
update_account(email, status=STATUS_ACTIVE, ...)  # team_auth_missing
return email
```

#### 3.1.2 改造后(Team 分支完整 diff)

```python
# manager.py:1462 起,Team 分支完整改造
from autoteam.invite import RegisterBlocked
from autoteam.accounts import is_supported_plan
from autoteam.codex_auth import check_codex_quota, get_quota_exhausted_info
# (上述 import 应集中放到 manager.py 文件顶部)

try:
    bundle = login_codex_via_browser(email, password, mail_client=mail_client)
except RegisterBlocked as blocked:
    if blocked.is_phone:
        record_failure(
            email,
            category="oauth_phone_blocked",
            reason=f"OAuth 阶段触发 add-phone (step={blocked.step})",
            step=blocked.step,
            stage="run_post_register_oauth_team",
        )
        # Team 模式下账号已成功 invite,不能 delete_account(席位仍占着);标 AUTH_INVALID 让 reconcile 接管
        update_account(
            email,
            status=STATUS_AUTH_INVALID,
            workspace_account_id=get_chatgpt_account_id() or None,
        )
        _record_outcome("oauth_phone_blocked", reason="OAuth 阶段触发 add-phone")
        return None
    # is_duplicate 在 OAuth 阶段不应出现,记 exception 兜底
    record_failure(email, "exception", f"OAuth 意外 RegisterBlocked: {blocked.reason}")
    _record_outcome("oauth_failed", reason=f"unexpected RegisterBlocked: {blocked.reason}")
    return None

if not bundle:
    # 旧路径:bundle=None,team_auth_missing,保留 ACTIVE 等用户补登录
    update_account(email, status=STATUS_ACTIVE, workspace_account_id=get_chatgpt_account_id() or None)
    logger.warning("[注册] 账号已加入 Team 但 Codex 登录失败,需要补登录: %s", email)
    _record_outcome("team_auth_missing", reason="已入 Team 席位但 Codex OAuth 未返回 bundle,需要补登录")
    return email

auth_file = save_auth_file(bundle)
bundle_plan = bundle.get("plan_type", "unknown")  # 已被 _exchange_auth_code 归一化为小写
plan_supported = bundle.get("plan_supported", is_supported_plan(bundle_plan))

# 新增 FR-A4:plan_type 不支持 → AUTH_INVALID
if not plan_supported:
    record_failure(
        email,
        category="plan_unsupported",
        reason=f"OAuth bundle plan_type={bundle.get('plan_type_raw') or bundle_plan} 不在白名单",
        plan_type=bundle_plan,
        plan_type_raw=bundle.get("plan_type_raw"),
        stage="run_post_register_oauth_team",
    )
    update_account(
        email,
        status=STATUS_AUTH_INVALID,
        seat_type="codex",
        auth_file=auth_file,                   # 保留 auth_file 供调试
        plan_type_raw=bundle.get("plan_type_raw"),
        workspace_account_id=get_chatgpt_account_id() or None,
    )
    _record_outcome("plan_unsupported", plan=bundle_plan)
    return None

# FR-D1~D4:quota probe(对称 manual_account._finalize_account)
seat_label = "chatgpt" if bundle_plan == "team" else "codex"
access_token = bundle.get("access_token")
account_id = bundle.get("account_id")

update_fields = {
    "status": STATUS_ACTIVE,
    "seat_type": seat_label,
    "auth_file": auth_file,
    "last_active_at": time.time(),
    "workspace_account_id": get_chatgpt_account_id() or None,
    "plan_type_raw": bundle.get("plan_type_raw"),
}

if access_token:
    try:
        quota_status, quota_info = check_codex_quota(access_token, account_id=account_id)
    except Exception as exc:
        # FR-D4: probe 异常吞掉,降级 ACTIVE + 记录
        record_failure(email, "quota_probe_network_error", f"quota probe exception: {exc}",
                       stage="run_post_register_oauth_team")
        quota_status, quota_info = "network_error", None

    if quota_status == "ok" and isinstance(quota_info, dict):
        update_fields["last_quota"] = quota_info
    elif quota_status == "exhausted":
        snapshot = quota_info.get("quota_info") if isinstance(quota_info, dict) else None
        if snapshot:
            update_fields["last_quota"] = snapshot
        update_fields["status"] = STATUS_EXHAUSTED
        update_fields["quota_exhausted_at"] = time.time()
        update_fields["quota_resets_at"] = (
            quota_info.get("resets_at") if isinstance(quota_info, dict) else int(time.time() + 18000)
        )
    elif quota_status == "no_quota":
        snapshot = quota_info.get("quota_info") if isinstance(quota_info, dict) else None
        if snapshot:
            update_fields["last_quota"] = snapshot
        update_fields["status"] = STATUS_AUTH_INVALID
        record_failure(email, "no_quota_assigned",
                       "wham/usage 返回 no_quota(workspace 未分配 codex 配额)",
                       plan_type=bundle_plan, stage="run_post_register_oauth_team")
    elif quota_status == "auth_error":
        update_fields["status"] = STATUS_AUTH_INVALID
        record_failure(email, "auth_error_at_oauth",
                       "wham/usage 返回 401/403,token 失效",
                       stage="run_post_register_oauth_team")
    elif quota_status == "network_error":
        # 网络抖动:保留 ACTIVE,但记录一次失败便于运营观察
        record_failure(email, "quota_probe_network_error",
                       "wham/usage 网络异常,ACTIVE 状态由下轮 cmd_check 校准",
                       stage="run_post_register_oauth_team")

update_account(email, **update_fields)
_record_outcome("success" if update_fields["status"] == STATUS_ACTIVE else "quota_issue",
                plan=bundle_plan, status=update_fields["status"])
return email if update_fields["status"] == STATUS_ACTIVE else None
```

#### 3.1.3 personal 分支(L1431-1460)的对称改造

```python
# manager.py:1431 起
try:
    bundle = login_codex_via_browser(email, password, mail_client=mail_client, use_personal=True)
except RegisterBlocked as blocked:
    if blocked.is_phone:
        record_failure(
            email,
            category="oauth_phone_blocked",
            reason=f"personal OAuth 触发 add-phone (step={blocked.step})",
            step=blocked.step,
            stage="run_post_register_oauth_personal",
        )
        delete_account(email)  # personal 已 leave_workspace,本地无价值
        _record_outcome("oauth_phone_blocked", reason="personal OAuth 触发 add-phone")
        return None
    record_failure(email, "exception", f"personal OAuth RegisterBlocked: {blocked.reason}")
    delete_account(email)
    return None

if bundle:
    plan_supported = bundle.get("plan_supported", is_supported_plan(bundle.get("plan_type", "")))
    if not plan_supported:
        record_failure(
            email, "plan_unsupported",
            f"personal OAuth bundle plan_type={bundle.get('plan_type_raw')} 不在白名单",
            plan_type=bundle.get("plan_type"),
            plan_type_raw=bundle.get("plan_type_raw"),
            stage="run_post_register_oauth_personal",
        )
        delete_account(email)
        _record_outcome("plan_unsupported", plan=bundle.get("plan_type"))
        return None

    auth_file = save_auth_file(bundle)
    update_fields = {
        "status": STATUS_PERSONAL,
        "seat_type": "codex",
        "auth_file": auth_file,
        "last_active_at": time.time(),
        "plan_type_raw": bundle.get("plan_type_raw"),
    }

    # personal 分支也加 quota probe(对称),确认 free plan 真有 codex 配额
    access_token = bundle.get("access_token")
    if access_token:
        try:
            quota_status, quota_info = check_codex_quota(access_token, account_id=bundle.get("account_id"))
            if quota_status == "ok" and isinstance(quota_info, dict):
                update_fields["last_quota"] = quota_info
            elif quota_status == "no_quota":
                # personal 拿到无配额 → 保留 PERSONAL 但记一笔(用户可以决定删不删)
                record_failure(email, "no_quota_assigned",
                               "personal free plan 无 codex 配额",
                               stage="run_post_register_oauth_personal")
        except Exception:
            pass  # personal probe 失败不阻塞

    update_account(email, **update_fields)
    _record_outcome("success", plan=bundle.get("plan_type"))
    return email

# bundle is None — 旧路径保留
delete_account(email)
record_failure(email, "oauth_failed",
               "已退出 Team 但 personal Codex OAuth 登录未返回 bundle",
               stage="post_leave_workspace")
_record_outcome("oauth_failed", reason="personal Codex OAuth 未返回 bundle")
return None
```

### 3.2 `sync_account_states` 分支扩展(被踢识别 + 并发)

**文件**:`src/autoteam/manager.py:476-541`
**FR**:E1~E4

#### 3.2.1 改造点

```python
# manager.py:526-541 改造后
import concurrent.futures

def _probe_kicked_account(acc):
    """单账号探测:wham 401/403 → 被踢;否则返回 None."""
    auth_file = acc.get("auth_file")
    if not auth_file:
        return None  # 无 token 无法判定,降级 STANDBY
    try:
        bundle = load_auth_file(auth_file)  # 假设 codex_auth 提供
        access_token = bundle.get("access_token")
        if not access_token:
            return None
        status, _ = check_codex_quota(access_token)
        return status
    except Exception:
        return None


# 在 sync_account_states 函数体内,替换 L526-541 的现行分支
# 先收集需要探测的 acc,然后用 ThreadPoolExecutor 并发
need_probe = []
for acc in accounts:
    email = acc["email"].lower()
    in_team = email in team_emails

    if in_team and acc["status"] in (STATUS_STANDBY, STATUS_PENDING):
        acc["status"] = STATUS_ACTIVE
        if account_id:
            acc["workspace_account_id"] = account_id
        changed = True
    elif not in_team and acc["status"] == STATUS_ACTIVE:
        acc_ws = acc.get("workspace_account_id")
        if acc_ws and account_id and acc_ws != account_id:
            logger.warning("[同步] %s workspace 漂移,保留 active 不 flip", acc["email"])
            continue

        # FR-E3 探测去重:30 分钟内不重复探测
        last_check = acc.get("last_quota_check_at") or 0
        cooldown = get_runtime("sync_probe_cooldown_minutes", 30) * 60
        if time.time() - last_check < cooldown:
            acc["status"] = STATUS_STANDBY
            changed = True
            continue

        need_probe.append(acc)

# FR-E2 并发探测
concurrency = get_runtime("sync_probe_concurrency", 5)
if need_probe:
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        future_map = {ex.submit(_probe_kicked_account, acc): acc for acc in need_probe}
        for fut in concurrent.futures.as_completed(future_map, timeout=concurrency * 6):
            acc = future_map[fut]
            try:
                status_str = fut.result(timeout=5)
            except Exception:
                status_str = None
            now = time.time()
            acc["last_quota_check_at"] = now

            if status_str == "auth_error":
                acc["status"] = STATUS_AUTH_INVALID
                acc["last_kicked_at"] = now
                logger.warning("[同步] %s wham 401/403,判定被人工踢出 → AUTH_INVALID", acc["email"])
            elif status_str == "no_quota":
                acc["status"] = STATUS_AUTH_INVALID  # 不会自然恢复
                logger.warning("[同步] %s wham no_quota → AUTH_INVALID", acc["email"])
            else:
                # ok / exhausted / network_error / None → 保持自然待机语义
                acc["status"] = STATUS_STANDBY
            changed = True
```

#### 3.2.2 关键设计决策

- **并发上限 5**(可配):防止 N 个 active 号串行 wham 让 sync 周期超 30s(NFR-1)
- **单调用超时 5s** + **整体超时 30s**(`concurrency * 6` 上限)
- **去重 30 分钟**:同 email 不重复探测,避免同一 active 号在多次 sync 间被反复探测
- **任何探测异常吞掉**:保持 STATUS_STANDBY 旧行为,避免一次抖动批量误标 AUTH_INVALID
- **`load_auth_file` 工具函数**:codex_auth.py 中应已有(读 auth json),实施时核对;无则在该模块新增小工具

### 3.3 `reinvite_account` 兜底改造

**文件**:`src/autoteam/manager.py:2466-2585`
**FR**:H1~H3 + C3(reinvite 调用点)

#### 3.3.1 改造点(L2466 包 try/except + L2489 替换 STATUS_STANDBY → AUTH_INVALID)

```python
# manager.py:2466 起
try:
    bundle = login_codex_via_browser(email, password, mail_client=mail_client)
except RegisterBlocked as blocked:
    if blocked.is_phone:
        record_failure(
            email,
            category="oauth_phone_blocked",
            reason=f"reinvite OAuth 触发 add-phone (step={blocked.step})",
            step=blocked.step,
            stage="reinvite_account",
        )
        _cleanup_team_leftover("oauth_phone_blocked")
        update_account(
            email,
            status=STATUS_AUTH_INVALID,
            auth_file=None,
            quota_exhausted_at=None,
            quota_resets_at=None,
        )
        return False
    record_failure(email, "exception", f"reinvite RegisterBlocked: {blocked.reason}")
    _cleanup_team_leftover("exception")
    update_account(email, status=STATUS_AUTH_INVALID, auth_file=None)
    return False

# (现有 _cleanup_team_leftover 闭包定义保留)

if not bundle:
    logger.warning("[轮转] 旧账号 OAuth 登录失败,保持 standby: %s", email)
    _cleanup_team_leftover("no_bundle")
    update_account(email, status=STATUS_STANDBY)
    return False

# FR-H1 + plan_supported 检查:plan_supported=False → AUTH_INVALID;plan != team 也走 AUTH_INVALID
plan_type = (bundle.get("plan_type") or "").lower()
plan_supported = bundle.get("plan_supported", is_supported_plan(plan_type))

if not plan_supported:
    logger.warning("[轮转] 旧账号 plan=%s 不在白名单,推 AUTH_INVALID: %s",
                   bundle.get("plan_type_raw"), email)
    _cleanup_team_leftover(f"plan_unsupported_{plan_type or 'unknown'}")
    record_failure(
        email, "plan_unsupported",
        f"reinvite bundle plan_type={bundle.get('plan_type_raw')} 不在白名单",
        plan_type=plan_type, plan_type_raw=bundle.get("plan_type_raw"),
        stage="reinvite_account",
    )
    update_account(
        email,
        status=STATUS_AUTH_INVALID,
        auth_file=None,                # 清掉错误 plan 的 token
        plan_type_raw=bundle.get("plan_type_raw"),
        quota_exhausted_at=None,
        quota_resets_at=None,
    )
    return False

if plan_type != "team":
    logger.warning("[轮转] 旧账号 plan=%s,plan_drift,推 AUTH_INVALID: %s", plan_type, email)
    _cleanup_team_leftover(f"plan_drift_{plan_type or 'unknown'}")
    record_failure(
        email, "plan_drift",
        f"reinvite 拿到 plan={plan_type or 'unknown'} != team",
        plan_type=plan_type, stage="reinvite_account",
    )
    update_account(
        email,
        status=STATUS_AUTH_INVALID,    # 不再回 STANDBY
        auth_file=None,
        plan_type_raw=bundle.get("plan_type_raw"),
        quota_exhausted_at=None,
        quota_resets_at=None,
    )
    return False

# (剩余 quota verify 段保留 L2496-2595 现行实现,只在 fail_reason 分支补 no_quota 处置)
auth_file = save_auth_file(bundle)
...
# 在 quota verified 判断中增加:
elif status_str == "no_quota":
    fail_reason = "no_quota_assigned"
    logger.warning("[轮转] %s reinvite 后 wham no_quota,判定 token 风控", email)
    # 后续 quota_verified=False 分支已经会处理 _cleanup_team_leftover
```

#### 3.3.2 批判 R-7:不会误伤 personal 转化路径

**结论**:`reinvite_account` 仅由 `_rotate_round` / `_replace_single` 等场景从 STATUS_STANDBY 池选中调用。用户主动转 personal 的入口是 `cmd_fill_personal` → `_run_post_register_oauth(leave_workspace=True)`,**完全不进 reinvite_account**。因此 reinvite 拿到 free plan 永远是异常("Team workspace 同步异常 / token 漂移"),推 AUTH_INVALID 无误伤。

### 3.4 `PREFERRED_SEAT_TYPE` 在 invite_member / _invite_member_once 的应用

**文件**:`src/autoteam/invite.py:496` + `src/autoteam/chatgpt_api.py:1414-1511` + `runtime_config.py`
**FR**:F1~F6

#### 3.4.1 `runtime_config.py` 新增(Round 7 v1.4 命名归一化)

**命名规则**(v1.4 修订):
- 主名:`default`(默认值)— 行为=旧 PATCH 升级 ChatGPT 完整席位
- 别名:`chatgpt`(转移期接受)— setter 收到 `chatgpt` normalize 为 `default`,getter 永不返回 `chatgpt`
- 主名:`codex` — codex-only 席位,锁 usage_based,不升级

**理由**:实施层 `runtime_config.py:141-142` 已使用 `default`/`codex`(用户配置 `runtime_config.json` 已写 `"preferred_seat_type": "default"` 字面量),改实施会破坏向后兼容。Round 5 verify 揭示 SPEC v1.0~1.3 与实施不一致(SPEC 写 `chatgpt`),Round 7 P2.1 选择**改 SPEC**保持向后兼容,setter 加 `chatgpt → default` 转移期支持。

```python
# runtime_config.py(Round 7 v1.4 与实施对齐)

_PREFERRED_SEAT_TYPE_DEFAULT = "default"
_PREFERRED_SEAT_TYPE_VALID = frozenset({"default", "codex"})


def get_preferred_seat_type() -> str:
    """返回邀请席位偏好。'default'(默认/PATCH 升级 ChatGPT 完整席位)或 'codex'(锁 codex-only)。

    永不返回 'chatgpt'(它是 default 的转移期别名,见 set_preferred_seat_type)。
    """
    raw = (get("preferred_seat_type") or _PREFERRED_SEAT_TYPE_DEFAULT).strip().lower()
    return raw if raw in _PREFERRED_SEAT_TYPE_VALID else _PREFERRED_SEAT_TYPE_DEFAULT


def set_preferred_seat_type(value: str) -> str:
    """落盘邀请席位偏好。

    Round 7 v1.4:接受 'chatgpt' 作为 'default' 的转移期别名(向后兼容),
    setter 内部 normalize 为 'default' 后落盘。
    """
    cleaned = (str(value or "") or _PREFERRED_SEAT_TYPE_DEFAULT).strip().lower()
    # ★ Round 7 P2.1:chatgpt 是 default 的转移期别名
    if cleaned == "chatgpt":
        cleaned = "default"
    if cleaned not in _PREFERRED_SEAT_TYPE_VALID:
        cleaned = _PREFERRED_SEAT_TYPE_DEFAULT
    set_value("preferred_seat_type", cleaned)
    return cleaned


def get_runtime(key: str, default):
    """通用读取(供 sync_account_states 等使用)"""
    return get(key, default)
```

**转移期(deprecation timeline)**:
- 2026-04-26 起 setter 接受 `chatgpt` 别名(Round 7)
- ≥ 1 个 release 后(2026-07 视用户反馈)考虑移除别名
- 文档(本 SPEC + Settings.vue UI 文案)同步使用 `default` 主名

**单测**:
- `test_set_preferred_seat_type_accepts_chatgpt_alias` 验证 `set_preferred_seat_type("chatgpt") == "default"` + 落盘读回也是 `"default"`
- `test_get_preferred_seat_type_default` 验证 getter 永不返回 `chatgpt`

#### 3.4.2 `invite.py:496` 改造

```python
# invite.py:493-496 之间插入
from autoteam.runtime_config import get_preferred_seat_type

preferred = get_preferred_seat_type()
seat_type_param = "default" if preferred == "chatgpt" else "usage_based"
status, data = chatgpt.invite_member(email, seat_type=seat_type_param)
```

#### 3.4.3 `chatgpt_api.py:_invite_member_once` 加 `allow_patch_upgrade` 参数

```python
# chatgpt_api.py:1414
def _invite_member_once(self, email, seat_type, *, allow_patch_upgrade=True):
    ...
    # 现行 L1487-1506
    data["_seat_type"] = "usage_based"
    if seat_type == "usage_based":
        if not allow_patch_upgrade:
            # FR-F3:codex 偏好下不升级,直接保留 usage_based
            return status, data

        invites = data.get("account_invites", []) ...
        # 原 PATCH 升级链路保留
```

#### 3.4.4 `_invite_member_with_fallback` 调整(L1387)

```python
def _invite_member_with_fallback(self, email, seat_type, *, allow_fallback):
    preferred = get_preferred_seat_type()
    allow_patch_upgrade = (preferred == "chatgpt")

    status, data = self._invite_member_once(email, seat_type,
                                            allow_patch_upgrade=allow_patch_upgrade)
    # codex 偏好下,不再做 default → usage_based 兜底(直接 usage_based 入口)
    if preferred == "codex":
        return status, data

    # 现行 chatgpt 偏好兜底链路保留
```

#### 3.4.5 `login_codex_via_browser` 4 处 OAuth add-phone 探针(Round 6 强制要求)

**FR**:C1~C5、FR-P1.1(Round 6)

**位置**:`src/autoteam/codex_auth.py` `login_codex_via_browser` 函数体

**4 处探针接入点(完整清单)**:

| 探针 | step 名 | 插入位置 | Round 5 verify 落地状态 |
|---|---|---|---|
| C-P1 | `oauth_about_you` | `if "about-you" in page.url:` 之前(实测 codex_auth.py:581) | ✅ 已落地 |
| C-P2 | `oauth_consent_{step}` | `for step in range(10):` 内每轮第一行(实测 codex_auth.py:633) | ✅ 已落地 |
| C-P3 | `oauth_callback_wait` | `for _ in range(30):` 等 callback 之前(实测 codex_auth.py:905) | ✅ 已落地 |
| C-P4 | `oauth_personal_check` | **callback for-loop 后,`browser.close()` 之前,`_exchange_auth_code` 之前**(v1.3 实施对齐 — 不可放在 `if use_personal:` 之前,该位置 `browser.close()` 已执行,page 不可用) | ✅ **已落地**(`codex_auth.py:939`,Round 6 PRD-5 FR-P1.1) |

**实施代码(v1.3 与 codex_auth.py:939 对齐)**:

```python
# src/autoteam/codex_auth.py:929-949 区
# 位置:callback for-loop 后,browser.close() 之前,_exchange_auth_code 之前
# 不可放在 use_personal 分支前 — 该位置 browser 已 close,page 不可用

# (callback for-loop 结束)
if not auth_code:
    _screenshot(page, "codex_05_no_callback.png")
    logger.warning("[Codex] 未获取到 auth code,当前 URL: %s", page.url)

# ★ C-P4 探针(Round 6 PRD-5 FR-P1.1)
try:
    assert_not_blocked(page, "oauth_personal_check")
except Exception:
    # 命中 add-phone 抛 RegisterBlocked — 必须传播给上层,
    # 但要保证 browser 资源被释放
    try:
        browser.close()
    except Exception:
        pass
    raise

browser.close()

if not auth_code:
    return None

bundle = _exchange_auth_code(auth_code, code_verifier, fallback_email=email)
# personal 模式 plan_type 校验在 bundle 拿到后进行
if use_personal:
    plan = (bundle.get("plan_type") or "").lower()
    ...  # plan_type 校验
```

**调用方处置**:5 处 `login_codex_via_browser` 调用方(`manager.py:1057 / 1431 / 1463 / 2466` + `api.py:1675`)必须显式 `except RegisterBlocked`,详见 `shared/add-phone-detection.md §5.2 调用方分类处置矩阵`。

**验收**:`grep -rn "oauth_personal_check" src/autoteam/codex_auth.py` 必须命中 1 处(Round 6 实测 `:939` 已落地 ✅)。

### 3.4.6 quota check 24h 去重(Round 7 FR-D6)

**位置**:`codex_auth.py:check_codex_quota` 内 uninitialized_seat 分支

**契约**:`check_codex_quota` 在收到 wham 200 + uninitialized_seat 形态后,在调用 `cheap_codex_smoke` 之前先查 `_read_codex_smoke_cache(account_id)`:

- 24h cache 命中 → 直接转 5 分类返回(不调网络),`quota_info["smoke_cache_hit"] = True`
- 24h cache miss / 过期 → 调 `cheap_codex_smoke(access_token, account_id)`,落盘 `last_codex_smoke_at` + `last_smoke_result`,再转 5 分类

**accounts.json 字段扩**:

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `last_codex_smoke_at` | float \| null | null | 上次 cheap_codex_smoke 调用的 epoch seconds |
| `last_smoke_result` | str \| null | null | `"alive"` / `"auth_invalid"` / `"uncertain"` |

**详见**:`./shared/quota-classification.md §4.4`(完整工具函数代码 + I9 不变量)

**调用方透明**:9+2 调用方对 `check_codex_quota` 的 5 分类处置不变,24h 去重由 `check_codex_quota` 内部消化。

### 3.5 personal 删除链短路 fetch_team_state

**文件**:`src/autoteam/account_ops.py:40-162` + `src/autoteam/api.py:1306-1404`
**FR**:G1~G4

#### 3.5.1 `account_ops.delete_managed_account` 短路

**Round 6 PRD-5 FR-P1.2 强制要求**:short_circuit 条件**必须**同时覆盖 `STATUS_PERSONAL` 和 `STATUS_AUTH_INVALID`(Round 5 verify 实测 `account_ops.py:77` 仅判 STATUS_PERSONAL,违背 FR-G2)。

```python
# account_ops.py:72 起
from autoteam.accounts import STATUS_PERSONAL, STATUS_AUTH_INVALID

try:
    account_id = get_chatgpt_account_id()
    # ★Round 6 必修:短路条件必须同时含 STATUS_PERSONAL 和 STATUS_AUTH_INVALID
    short_circuit = (
        remove_remote
        and acc
        and acc.get("status") in (STATUS_PERSONAL, STATUS_AUTH_INVALID)
    )

    if remove_remote and not short_circuit:
        if remote_state is not None:
            members, invites = remote_state
        else:
            if chatgpt_api is None:
                from autoteam.chatgpt_api import ChatGPTTeamAPI
                own_chatgpt = ChatGPTTeamAPI()
                own_chatgpt.start()
                chatgpt_api = own_chatgpt
            members, invites = fetch_team_state(chatgpt_api)

        # 现有 member_matches / invite_matches 逻辑保留
        ...
    elif short_circuit:
        logger.info("[账号] %s 状态=%s,跳过 Team 远端同步,直接清本地",
                    email, acc.get("status"))
        members, invites = [], []

    # 后续 auth_file / cpa / local 删除链路与现行一致
```

**为什么 AUTH_INVALID 必须短路**:

- AUTH_INVALID 账号的 token 已失效 → wham/usage 401 → 调用 `fetch_team_state` 也很可能 401 拖累整个删除流程
- 主号 session 失效场景下,启动 ChatGPTTeamAPI 会卡死 30s
- 删除 AUTH_INVALID 不需要远端 KICK(因为 reconcile 已经 KICK 过或正在排队),只需清本地 records / auth_file
- 与 FR-G2(personal/auth_invalid 不需要拉 remote_state)契约一致

**验收**:`grep -A3 "short_circuit = " src/autoteam/account_ops.py` 必须显示 `STATUS_AUTH_INVALID` 字面量。

#### 3.5.2 `api.delete_accounts_batch` 全 personal 短路

**Round 6 PRD-5 FR-P1.4 强制要求**:批量删除路径必须先做 `all_personal` 检查,如果整批都是 personal/auth_invalid 则**整批跳过 ChatGPTTeamAPI 启动**(Round 5 verify 实测 `api.py:1573-1577` 无条件 `chatgpt_api.start()`,违背 FR-G3)。

```python
# api.py:1306-1404 之间(实施代码,Round 6 必修)
from autoteam.accounts import STATUS_PERSONAL, STATUS_AUTH_INVALID

def delete_accounts_batch(emails: list[str]):
    accounts = load_accounts()
    targets = [a for a in accounts if a["email"].lower() in {e.lower() for e in emails}]

    # ★Round 6 关键:bool(targets) 防空 list 误判 all=True
    all_personal = bool(targets) and all(
        a["status"] in (STATUS_PERSONAL, STATUS_AUTH_INVALID) for a in targets
    )

    chatgpt_api = None
    if not all_personal:
        # 至少一个号需要 Team 远端同步
        chatgpt_api = ChatGPTTeamAPI()
        chatgpt_api.start()

    try:
        results = []
        for acc in targets:
            try:
                cleanup = delete_managed_account(
                    acc["email"],
                    chatgpt_api=chatgpt_api,        # all_personal=True 时为 None
                    sync_cpa_after=False,
                )
                results.append({"email": acc["email"], "success": True, "cleanup": cleanup})
            except Exception as exc:
                results.append({"email": acc["email"], "success": False, "error": str(exc)})
        sync_to_cpa()
        return results
    finally:
        if chatgpt_api:
            chatgpt_api.stop()
```

**注意点**:

- `bool(targets) and all(...)`:空 targets 时 Python `all([])` 返回 True,会让短路路径误判成功 — 必须显式守卫
- `chatgpt_api=None` 传入 `delete_managed_account`:依赖 §3.5.1 short_circuit 同时支持 `STATUS_AUTH_INVALID`(否则 auth_invalid 进 `fetch_team_state` 路径会因 chatgpt_api=None 抛错)。**所以 §3.5.1 必须先于 / 同步 §3.5.2 落地**
- mixed 场景(2 personal + 1 active):`all_personal=False`,正常启动 ChatGPTTeamAPI,active 走完整删除链路,personal/auth_invalid 因 §3.5.1 short_circuit 跳过远端

**验收**:单测 mock `ChatGPTTeamAPI`,批量传 5 个 STATUS_AUTH_INVALID 邮箱,断言 `ChatGPTTeamAPI.__init__` 0 次调用、`ChatGPTTeamAPI.start()` 0 次调用。

#### 3.5.3 `api.post_account_login` 409 phone_required 详细契约(对应 Story Map S-2.2,Round 6 PRD-5 FR-P1.3 必修)

**位置**:`src/autoteam/api.py:1675`(`@app.post("/api/accounts/{email}/login")` 的 `post_account_login` 函数体)

**FR**:C5 + FR-P1.3(Round 6)

**端点契约**:

| 维度 | 规格 |
|---|---|
| Method / Path | `POST /api/accounts/{email}/login` |
| Auth | 需要管理员 Bearer(同其他 /api 端点) |
| Body | `{"password": "..."}` 或 `{}`(从 acc 读密码) |
| 成功响应 | 200 OK,body `{"email": ..., "status": ...}`(同现行) |
| 409 phone_required(★Round 6 新增) | body `{"detail": {"error": "phone_required", "step": "<C-P1..C-P4 step 名>", "reason": "<blocked.reason>"}}` |
| 500 oauth_failed | 通用 OAuth 失败兜底(非 phone) |
| 422 no_quota_assigned(SPEC §5 既有) | body `{"detail": {"error": "no_quota_assigned", "plan_type": ...}}` |

**实施代码**(完整版见 `shared/add-phone-detection.md §5.5`):

```python
@app.post("/api/accounts/{email}/login")
def post_account_login(email: str, ...):
    ...
    try:
        bundle = login_codex_via_browser(email, password, mail_client=mail_client,
                                         use_personal=use_personal)
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
        record_failure(email, "exception", f"补登录意外 RegisterBlocked: {blocked.reason}")
        raise HTTPException(status_code=500, detail={"error": "oauth_failed",
                                                      "reason": str(blocked)})
    ...
```

**前端解析**(`web/src/api.ts` 或 `web/src/api.js`,与仓库实际后缀一致):

**Round 7 v1.4 task["error"] 关键字契约**:

由于 `post_account_login` 是 `_start_task` 异步任务,`raise HTTPException(409, ...)` 在 `_run_task`(`api.py:488-516`)的 `except Exception as e: task["error"] = str(e)` 中被转录为字符串。`HTTPException.__str__` 输出形如:

```
"409: {'error': 'phone_required', 'step': 'oauth_consent_2', 'reason': 'add-phone url 命中'}"
```

前端 polling task status 时,需按以下子串关键字识别(顺序敏感,phone_required 优先):

| 关键字串 | 语义 | UI 友好提示 |
|---|---|---|
| `phone_required` | OAuth 撞 add-phone 探针 | "该账号需要绑定手机才能完成 OAuth" |
| `register_blocked` | OAuth 撞其他 RegisterBlocked(非 phone) | "该账号注册被阻断,请检查 OAuth 状态" |
| 其他 | 通用错误 | 默认 toast(通用错误信息) |

**前端 api.js 解析模板**(Round 7 FR-D7 落地):

```javascript
// web/src/api.js
async function pollTask(taskId) {
  const resp = await request('GET', `/tasks/${taskId}`)
  if (resp.error) {
    const errStr = String(resp.error)
    if (errStr.includes('phone_required')) {
      const e = new Error('该账号需要绑定手机才能完成 OAuth')
      e.code = 'phone_required'
      e.detail = errStr
      throw e
    }
    if (errStr.includes('register_blocked')) {
      const e = new Error('该账号注册被阻断,请检查 OAuth 状态')
      e.code = 'register_blocked'
      e.detail = errStr
      throw e
    }
    throw new Error(errStr)
  }
  return resp
}
```

**同步路径(直接 raise HTTPException 命中)**:

如果端点改为同步路径(非 _start_task 异步包装),HTTPException(409) 会直接产生 HTTP 响应:

```ts
if (resp.status === 409 && body.detail?.error === "phone_required") {
    showToast("该账号需要绑定手机才能继续", "error")
    // 可选:跳到运营回放页查看截图
}
```

**契约保证**:无论同步/异步路径,`phone_required` / `register_blocked` 字面量都会出现在响应/error 中,前端可统一按子串匹配解析。

**验收**:

- 单测 mock `login_codex_via_browser` 抛 `RegisterBlocked("oauth_consent_2", "...", is_phone=True)`,断言响应 status_code=409 + body detail error=phone_required + step=oauth_consent_2
- record_failure 必须先于 HTTPException 抛出(否则统计丢一条),用 mock 验证调用顺序
- 不可放行 500 路径(若 is_phone=True 走 500 视为 bug)
- **(Round 6 Q-3 决策 user 已确认)** 409 body 不带截图相对路径,保持端点 lean。前端如需截图自己 GET `/api/screenshots/...` 或从 `register_failures.json` 读 `step + url + screenshot_path`(`record_failure` 已记)

### 3.6 UI removeAccount toast 改造

**文件**:`web/src/components/Dashboard.vue:566-585`
**FR**:G4

#### 3.6.1 改造伪代码(原文 vue + ts)

```vue
<script setup lang="ts">
import { ref } from 'vue'
const message = ref<{ text: string; type: 'success' | 'error' | 'warning' } | null>(null)

async function removeAccount(email: string) {
  if (props.runningTask) {
    message.value = { text: '有任务在运行,请先停止后再删除', type: 'warning' }
    return
  }
  if (!adminReady.value && acc.status !== 'personal' && acc.status !== 'auth_invalid') {
    message.value = { text: '主号未就绪,无法删除该 Team 子号(personal/auth_invalid 不受限)', type: 'warning' }
    return
  }

  try {
    const resp = await api.deleteAccount(email)
    message.value = { text: `已删除 ${email}`, type: 'success' }
    emit('refresh')
  } catch (err: any) {
    if (err.status === 409) {
      message.value = { text: '操作冲突,请稍后重试', type: 'error' }
    } else if (err.status === 500) {
      message.value = { text: `删除失败:${err.body?.error || '服务器错误'}`, type: 'error' }
    } else {
      message.value = { text: `删除失败:${err.message}`, type: 'error' }
    }
  }
}
</script>
```

#### 3.6.2 quota 显示 no_quota 识别

```vue
<template>
  <span v-if="acc.last_quota?.primary_total === 0" class="quota-empty">
    无配额(联系管理员)
  </span>
  <span v-else-if="acc.last_quota">
    剩余 {{ 100 - acc.last_quota.primary_pct }}%
  </span>
</template>
```

---

## 4. 数据契约

### 4.1 `accounts.json` 新增字段

| 字段 | 类型 | 默认 | 来源 | 用途 |
|---|---|---|---|---|
| `plan_supported` | bool \| null | null | `_exchange_auth_code` 写入(经 `is_supported_plan`) | 标识当时 OAuth bundle 是否在白名单内;旧记录 null,不破坏兼容 |
| `plan_type_raw` | str \| null | null | `_exchange_auth_code` 写入(JWT 原始字面量) | 事后排查 |
| `last_kicked_at` | float \| null | null | `sync_account_states` 探测到 wham 401 时写入 | reconcile 历史回放 / UI 展示"X 分钟前被踢" |
| `last_quota.primary_total` | int \| null | null | `check_codex_quota` 解析 wham/usage 写入 | no_quota 识别;UI 显示"无配额" |
| `last_quota.primary_remaining` | int \| null | null | 同上 | UI 精确剩余值显示 |

**JSON Schema 片段**:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "additionalProperties": false,
  "required": ["email", "status", "created_at"],
  "properties": {
    "email": {"type": "string"},
    "password": {"type": "string"},
    "cloudmail_account_id": {"type": ["string", "null"]},
    "status": {"enum": ["active", "exhausted", "standby", "pending", "personal", "auth_invalid", "orphan"]},
    "seat_type": {"enum": ["chatgpt", "codex", "unknown"]},
    "workspace_account_id": {"type": ["string", "null"]},
    "auth_file": {"type": ["string", "null"]},
    "quota_exhausted_at": {"type": ["number", "null"]},
    "quota_resets_at": {"type": ["number", "null"]},
    "last_quota_check_at": {"type": ["number", "null"]},
    "last_quota": {
      "type": ["object", "null"],
      "properties": {
        "primary_pct": {"type": "integer", "minimum": 0, "maximum": 100},
        "primary_resets_at": {"type": "integer"},
        "primary_total": {"type": ["integer", "null"]},
        "primary_remaining": {"type": ["integer", "null"]},
        "weekly_pct": {"type": "integer", "minimum": 0, "maximum": 100},
        "weekly_resets_at": {"type": "integer"}
      }
    },
    "last_active_at": {"type": ["number", "null"]},
    "created_at": {"type": "number"},
    "plan_supported": {"type": ["boolean", "null"]},
    "plan_type_raw": {"type": ["string", "null"]},
    "last_kicked_at": {"type": ["number", "null"]}
  }
}
```

### 4.2 `runtime_config.json` PREFERRED_SEAT_TYPE 等

```json
{
  "register_domain": "...",
  "preferred_seat_type": "chatgpt",
  "sync_probe_concurrency": 5,
  "sync_probe_cooldown_minutes": 30,
  "quota_probe_threshold_pct": 10
}
```

| key | 类型 | 默认 | 取值 | UI 字段(Settings.vue) |
|---|---|---|---|---|
| `preferred_seat_type` | str | `"chatgpt"` | `chatgpt` / `codex` | "邀请席位偏好"(下拉) |
| `sync_probe_concurrency` | int | 5 | 1..20 | "同步探测并发数"(数字输入) |
| `sync_probe_cooldown_minutes` | int | 30 | 5..1440 | "探测去重分钟数"(数字输入) |
| `quota_probe_threshold_pct` | int | 10 | 0..100 | "Quota 阈值百分比"(已存在,新管理) |

### 4.3 完整 Pydantic 模型(实施期复制粘贴)

```python
# src/autoteam/_models.py(可新建,或挂在 accounts.py 末尾)
"""
内部 Pydantic 模型 — 给类型检查 / 测试断言 / OpenAPI schema 用。

落盘仍是 dict(load_accounts/save_accounts),Pydantic 模型不替换 dict。
仅在边界(api.py 入参 / out / 测试)用 model_validate / model_dump。
"""
from typing import Literal, Optional, Union
from pydantic import BaseModel, Field

AccountStatus = Literal["active", "exhausted", "standby", "pending", "personal", "auth_invalid", "orphan"]
SeatType = Literal["chatgpt", "codex", "unknown"]
QuotaStatus = Literal["ok", "exhausted", "no_quota", "auth_error", "network_error"]


class QuotaSnapshot(BaseModel):
    primary_pct: int = 0
    primary_resets_at: int = 0
    primary_total: Optional[int] = None
    primary_remaining: Optional[int] = None
    weekly_pct: int = 0
    weekly_resets_at: int = 0


class QuotaExhaustedInfo(BaseModel):
    window: Literal["primary", "weekly", "combined", "limit", "no_quota"]
    resets_at: int
    quota_info: QuotaSnapshot
    limit_reached: bool = False


class QuotaProbeResult(BaseModel):
    status: QuotaStatus
    info: Optional[Union[QuotaSnapshot, QuotaExhaustedInfo]] = None


class AccountRecord(BaseModel):
    email: str
    password: str = ""
    cloudmail_account_id: Optional[str] = None
    status: AccountStatus
    seat_type: SeatType = "unknown"
    workspace_account_id: Optional[str] = None
    auth_file: Optional[str] = None
    quota_exhausted_at: Optional[float] = None
    quota_resets_at: Optional[float] = None
    last_quota_check_at: Optional[float] = None
    last_quota: Optional[QuotaSnapshot] = None
    last_active_at: Optional[float] = None
    created_at: float
    plan_supported: Optional[bool] = None
    plan_type_raw: Optional[str] = None
    last_kicked_at: Optional[float] = None


class OAuthBundle(BaseModel):
    """codex_auth._exchange_auth_code 返回的 bundle 结构(实施期可作为类型注解)"""
    access_token: str
    refresh_token: str
    id_token: str
    account_id: str
    email: str
    plan_type: str            # 已归一化 .lower()
    plan_type_raw: str        # 原始字面量
    plan_supported: bool
    expired: float


class RegisterFailureRecord(BaseModel):
    """register_failures.json 单条记录结构"""
    timestamp: float
    email: str
    category: Literal[
        "phone_blocked", "duplicate_exhausted", "register_failed",
        "oauth_failed", "kick_failed", "team_oauth_failed", "exception",
        "oauth_phone_blocked",      # 新增 §5.2.5
        "plan_unsupported",         # 新增
        "no_quota_assigned",        # 新增
        "plan_drift",               # 新增
        "auth_error_at_oauth",      # 新增
        "quota_probe_network_error",# 新增
    ]
    reason: str
    stage: Optional[str] = None
    step: Optional[str] = None
    plan_type: Optional[str] = None
    plan_type_raw: Optional[str] = None
    url: Optional[str] = None
```

---

## 5. 测试用例

### 5.1 FR-A:plan_type 白名单(每 FR 至少 2 case)

| # | 用例 | 预期 |
|---|---|---|
| 5.1.1 | bundle.plan_type=`team` → `is_supported_plan` | True,plan_supported=True |
| 5.1.2 | bundle.plan_type=`self_serve_business_usage_based` | False;manager 调用方标 STATUS_AUTH_INVALID + record_failure("plan_unsupported") |

测试入口:`tests/unit/test_plan_type_whitelist.py`(参见 [./shared/plan-type-whitelist.md §6.2](./shared/plan-type-whitelist.md))

### 5.2 FR-B:wham/usage 5 分类

| # | 用例 | 预期 |
|---|---|---|
| 5.2.1 | wham 200 + `primary.limit==0` | status=`no_quota`,info.window=`no_quota` |
| 5.2.2 | wham 200 + `used_percent==100` | status=`exhausted`,info.window=`primary` |
| 5.2.3 | wham 401 | status=`auth_error`,info=None |
| 5.2.4 | wham 429 | status=`network_error`(不能误判 auth_error) |

测试入口:`tests/unit/test_quota_classification.py`(参见 [./shared/quota-classification.md §6.2](./shared/quota-classification.md))

### 5.3 FR-C:add-phone 探针

| # | 用例 | 预期 |
|---|---|---|
| 5.3.1 | OAuth callback 前 page.url=`/add-phone` | `assert_not_blocked` raise RegisterBlocked(is_phone=True) |
| 5.3.2 | consent 页含"phone"帮助链接但无 tel input | `detect_phone_verification` 返回 False(避免误报) |
| 5.3.3 | reinvite_account 抛 RegisterBlocked → STATUS_AUTH_INVALID + `_cleanup_team_leftover` 被调 | 验证 record_failure("oauth_phone_blocked", stage="reinvite_account") |

### 5.4 FR-D:`_run_post_register_oauth` quota probe

| # | 用例 | 预期 |
|---|---|---|
| 5.4.1 | bundle ok + wham `ok` | STATUS_ACTIVE,last_quota 有写入 |
| 5.4.2 | bundle ok + wham `no_quota` | STATUS_AUTH_INVALID,record_failure("no_quota_assigned") |
| 5.4.3 | bundle ok + plan_supported=False | STATUS_AUTH_INVALID,record_failure("plan_unsupported"),不进 ACTIVE |
| 5.4.4 | bundle 抛 RegisterBlocked(is_phone=True) | STATUS_AUTH_INVALID,record_failure("oauth_phone_blocked") |

### 5.5 FR-E:sync_account_states 区分被踢 / 待机

| # | 用例 | 预期 |
|---|---|---|
| 5.5.1 | active 不在 Team + wham 401 | STATUS_AUTH_INVALID,last_kicked_at 写入 |
| 5.5.2 | active 不在 Team + wham ok | STATUS_STANDBY(自然待机) |
| 5.5.3 | active 不在 Team + workspace_account_id 漂移 | 保留 ACTIVE(母号切换守卫) |
| 5.5.4 | 同 email 30 分钟内重复探测 | 第二次跳过(去重),保持上轮状态 |

### 5.6 FR-F:PREFERRED_SEAT_TYPE

| # | 用例 | 预期 |
|---|---|---|
| 5.6.1 | preferred=chatgpt → invite_member(seat_type="default") + PATCH 升级 | 行为不变(回归测试) |
| 5.6.2 | preferred=codex → invite_member(seat_type="usage_based") + 0 处 PATCH | 日志中无"修改邀请 seat_type" |

### 5.7 FR-G:personal 删除短路

| # | 用例 | 预期 |
|---|---|---|
| 5.7.1 | acc.status=personal + 主号 session 失效 → 单点删除 | 不抛 ChatGPTTeamAPI 启动错误,本地记录被清 |
| 5.7.2 | 批量删除 5 个 personal | 完全不起 ChatGPTTeamAPI |

### 5.8 FR-H:reinvite plan_drift / phone_blocked

| # | 用例 | 预期 |
|---|---|---|
| 5.8.1 | reinvite bundle.plan=`free` | STATUS_AUTH_INVALID + record_failure("plan_drift"),auth_file=None |
| 5.8.2 | reinvite RegisterBlocked(is_phone=True) | STATUS_AUTH_INVALID + record_failure("oauth_phone_blocked", stage="reinvite_account") |
| 5.8.3 | 标到 AUTH_INVALID 后下轮不再被 standby 池选中 | reinvite_account 不再被调用 |

### 5.9 集成测试

| # | 用例 | 预期 |
|---|---|---|
| 5.9.1 | mock 一个 self_serve_business_usage_based bundle 跑全链路 | 注册 → STATUS_AUTH_INVALID,UI 显示 "无配额(联系管理员)" |
| 5.9.2 | 模拟管理员手动从 ChatGPT 后台踢号 → 等下次 sync | 状态机正确转 STATUS_AUTH_INVALID,reconcile 自动 KICK + 留本地记录 |
| 5.9.3 | preferred_seat_type=codex 下注册新号 | seat_type=codex,_seat_type=usage_based,UI 显示 "Codex 席位" |

---

## 6. 实施顺序(Story Map → Sub-task 依赖图)

依赖图(箭头 → 表示"必须先于"):

```
Phase 0 (spec 落地)
├─ S-0.1 accounts.py 加白名单常量 + 工具函数
├─ S-0.2 runtime_config.py 加 PREFERRED_SEAT_TYPE 等
├─ S-0.3 register_failures.py docstring 扩 6 category
└─ S-0.4 _models.py 写 Pydantic 模型(可选,用于测试)
        │
        ▼
Phase 1 (核心实现)
├─ S-1.1 codex_auth._exchange_auth_code 写 bundle 字段     ◄── 依赖 S-0.1
├─ S-1.2 codex_auth.check_codex_quota 加 no_quota          ◄── 独立
├─ S-1.3 codex_auth.login_codex_via_browser 4 探针         ◄── 独立
├─ S-1.4 manager._run_post_register_oauth probe + catch    ◄── 依赖 S-1.1/1.2/1.3
├─ S-1.5 manager.sync_account_states 探测                  ◄── 依赖 S-1.2
├─ S-1.6 manager.reinvite_account 兜底                     ◄── 依赖 S-1.1/1.2/1.3
├─ S-1.7 manual_account._finalize_account 扩展             ◄── 依赖 S-0.1/S-1.2
└─ S-1.8 account_ops.delete_managed_account 短路           ◄── 独立
        │
        ▼
Phase 2 (调用方处置)
├─ S-2.1 manager._check_pending_invites L1057 catch        ◄── 依赖 S-1.3
├─ S-2.2 api.post_account_login L1479 catch + 409          ◄── 依赖 S-1.3
├─ S-2.3 9 处 check_codex_quota 调用方加 no_quota 分支     ◄── 依赖 S-1.2
└─ S-2.4 api.delete_accounts_batch 全 personal 短路        ◄── 依赖 S-1.8
        │
        ▼
Phase 3 (席位策略,可与 P1/P2 并行)
├─ S-3.1 invite.py L496 读 PREFERRED_SEAT_TYPE             ◄── 依赖 S-0.2
├─ S-3.2 chatgpt_api._invite_member_once 加参数            ◄── 依赖 S-3.1
└─ S-3.3 _invite_member_with_fallback 调整                 ◄── 依赖 S-3.2
        │
        ▼
Phase 4 (前端)
├─ S-4.1 Settings.vue 加 preferred_seat_type / 探测配置     ◄── 依赖 S-0.2
├─ S-4.2 Dashboard.vue removeAccount toast / quota 显示    ◄── 依赖 S-1.8
└─ S-4.3 api.ts 解析 409 phone_required + 422 no_quota     ◄── 依赖 S-2.2
        │
        ▼
Phase 5 (测试 + 文档,贯穿)
├─ S-5.1 单测覆盖 §5.1~5.8                                 ◄── 跟随各 sub-task
├─ S-5.2 集成测试 §5.9                                     ◄── 在 P1+P2 完成后
├─ S-5.3 CHANGELOG.md 增补                                 ◄── 在所有 sub-task 完成后
└─ S-5.4 docs/account-state-machine.md 等新文档            ◄── 同上
        │
        ▼
Phase 6 (灰度上线)
├─ S-6.1 默认 chatgpt 偏好上线,观察 register_failures
└─ S-6.2 1-2 周后根据 PATCH 失败率决议默认值切换
```

**关键串行链**:S-0.1 → S-1.1 → S-1.4 / S-1.6 / S-1.7;S-0.2 → S-3.x;S-1.3 → S-2.1 / S-2.2

**可并行链**:Phase 1 的 S-1.2 / S-1.3 / S-1.8 互不依赖;Phase 3 与 Phase 1/2 完全并行

---

## 7. 验收清单(完成检查表)

### 7.1 代码层

- [ ] `accounts.py` 含 `SUPPORTED_PLAN_TYPES` + `is_supported_plan` + `normalize_plan_type`
- [ ] `codex_auth._exchange_auth_code` bundle 含 `plan_type`(归一化)/ `plan_type_raw` / `plan_supported`
- [ ] `codex_auth.check_codex_quota` 5 分类,no_quota 触发条件 4 项已实现
- [ ] `codex_auth.login_codex_via_browser` 4 处 `assert_not_blocked` 接入(C-P1~C-P4)
- [ ] `manager._run_post_register_oauth` Team / personal 双分支 catch + probe
- [ ] `manager.sync_account_states` 并发探测被踢识别 + 30 分钟去重
- [ ] `manager.reinvite_account` plan_drift / phone_blocked / plan_unsupported 三路兜底
- [ ] `manual_account._finalize_account` plan_supported 检查
- [ ] `account_ops.delete_managed_account` short_circuit
- [ ] `api.post_account_login` 409 phone_required
- [ ] `api.delete_accounts_batch` 全 personal 不起 ChatGPTTeamAPI
- [ ] `invite.py` + `chatgpt_api.py` PREFERRED_SEAT_TYPE 链路
- [ ] `runtime_config.py` 4 个新 getter / setter

### 7.2 数据层

- [ ] 旧 `accounts.json` 记录(无新字段)读取无报错
- [ ] 新增 `last_kicked_at` / `plan_supported` / `plan_type_raw` 落盘
- [ ] `last_quota.primary_total` / `primary_remaining` 落盘
- [ ] `register_failures.json` 6 个新 category 计数

### 7.3 前端

- [ ] Settings.vue 邀请席位偏好下拉
- [ ] Dashboard.vue removeAccount 失败 toast 区分 actionDisabled / 409 / 500
- [ ] Dashboard.vue quota 显示 no_quota 时文案 "无配额(联系管理员)"

### 7.4 测试

- [ ] 全部单测通过(§5.1~5.8 各 ≥2 case)
- [ ] 集成测试通过(§5.9)
- [ ] 回归测试通过(invite / reinvite / sync / cmd_check)

### 7.5 文档

- [ ] CHANGELOG.md 列出 5 个新 category + 4 个 OAuth 探针接入点 + PREFERRED_SEAT_TYPE
- [ ] 4 份 shared spec 文件路径在 CHANGELOG / README 引用
- [ ] docs/oauth-add-phone-detection.md / quota-classification.md / account-state-machine.md 落地(或合入主 README)

### 7.6 灰度

- [ ] 默认 PREFERRED_SEAT_TYPE=chatgpt 行为不变
- [ ] 1-2 周后根据 register_failures 数据决议是否切默认值

---

**文档结束。** 工程师参照本 spec + 4 份 shared spec,无需查询 PRD 即可完成代码实施。

---

## 附录 A:修订记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-04-26 | 初版 PRD-2 同步落地;A~H 共 8 组 FR |
| v1.1 | 2026-04-26 Round 6 | 加 §3.4.5 OAuth 4 探针接入清单(强调 C-P4 必修);§3.5.1 short_circuit 强调 STATUS_AUTH_INVALID 必含;§3.5.2 加 `bool(targets)` 守卫与混合场景说明;新增 §3.5.3 `api.post_account_login` 409 phone_required 详细契约(对应 Story Map S-2.2)。关联 PRD-5 FR-P0 / P1.1 / P1.2 / P1.3 / P1.4 |
| v1.2 | 2026-04-26 Round 6 finalize | user 确认 Q-3 决策:§3.5.3 验收清单加 1 条 — 409 body 不带截图相对路径,前端自己拉 `/api/screenshots/...` 或读 `register_failures.json` 字段 |
| v1.3 | 2026-04-26 Round 6 quality-reviewer 终审 follow-up | §3.4.5 C-P4 探针位置描述对齐实施(`codex_auth.py:939`):由"`if use_personal:` 这行**之前**"修订为"callback for-loop 之后,`browser.close()` 之前,`_exchange_auth_code` 之前"。理由:`if use_personal:` 在 `browser.close()` 之后,page 已 close,assert_not_blocked 异常会被吞为 False。表格"插入位置"列 + 实施代码段全部对齐;落地状态由 ❌ 改为 ✅;同步规范 try/except 关 browser 后 raise 的资源释放要求。详见 `prompts/0426/verify/round6-review-report.md` §3.1 决策 1 审查 |
| v1.4 | 2026-04-26 Round 7 P2 follow-up | (1) §3.4.1 PREFERRED_SEAT_TYPE 命名归一化:主名 `default`(默认) / `codex`,加 `chatgpt` 转移期别名(setter 接受并 normalize 为 default,getter 永不返 chatgpt);(2) §3.4.6 加 quota check 24h 去重接入说明(Round 7 FR-D6,详见 quota-classification §4.4 + I9);(3) §3.5.3 加 task["error"] 关键字契约(phone_required / register_blocked 字符串子串匹配,前端 api.js 模板);(4) 引用方加 PRD-6;关联 `prompts/0426/prd/prd-6-p2-followup.md` §5.1 / §5.6 / §5.7 |
