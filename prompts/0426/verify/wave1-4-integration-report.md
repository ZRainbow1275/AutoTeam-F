# Wave 1-4 集成验证报告

## 0. 元数据

| 字段 | 值 |
|---|---|
| 报告时间 | 2026-04-26 |
| 验证 agent | integration-verifier |
| HEAD commit | `478c16c` (`feat(round-4 lifecycle): SPEC-2 账号生命周期/配额/席位/被踢识别全套落地`) |
| 验证范围 | Wave 1 (`d9d9e44`) + Wave 2 (`f82773a`) + Wave 3 (`478c16c`) |
| 工作树状态 | clean(已 commit / 未 push) |
| 测试套件 | `tests/` 共 155 用例 |
| 平台 | Windows 11 + Python(D:\Anaconda3) |
| 修订 | 2026-04-26 12:30(纳入 production-cleaner 实测发现的 P0 quota half-loaded 漏判) |

---

## 1. 测试结果汇总

### 1.1 pytest

```
........................................................................ [ 46%]
........................................................................ [ 92%]
...........                                                              [100%]
155 passed, 4 warnings in 3.30s
```

- ✅ 全部 155 用例通过(0 failed / 0 error)
- ⚠️ 4 个 `DeprecationWarning`(FastAPI `@app.on_event` deprecation,非阻塞)。SPEC 未要求迁移到 lifespan,P2 改进项

### 1.2 ruff lint(F401/F811/F821 unused/redefined/undefined)

```
ruff check src/autoteam tests/ --select F401,F811,F821
All checks passed!
```

- ✅ 0 errors

### 1.3 import 健康检查

完整 11 个关键符号 import 全部通过:

```python
from autoteam.accounts import SUPPORTED_PLAN_TYPES, normalize_plan_type, is_supported_plan
from autoteam.codex_auth import check_codex_quota, get_quota_exhausted_info
from autoteam.invite import RegisterBlocked, assert_not_blocked, detect_phone_verification
from autoteam.runtime_config import (
    get_sync_probe_concurrency, get_sync_probe_cooldown_minutes,
    get_preferred_seat_type, set_preferred_seat_type
)
from autoteam.manager import _probe_kicked_account, sync_account_states, _run_post_register_oauth
```

- `SUPPORTED_PLAN_TYPES = frozenset({'plus', 'free', 'team', 'pro'})` 验证一致

---

## 2. SPEC 落地对账

### 2.1 SPEC-1 (Mail Provider 全量化) — Wave 2 commit `f82773a`

| # | 验收项 (spec-1 §8) | 落地证据 | 偏差 |
|---|---|---|---|
| 1 | SetupConfig 含 `MAIL_PROVIDER` + `MAILLAB_*` 4 字段 | `src/autoteam/api.py:90+` 多处引用 | ✅ |
| 2 | `/api/setup/save` provider=maillab 时仅写 MAILLAB_* | `src/autoteam/api.py:99` `post_setup_save` 改造点 | ✅ |
| 3 | `cf_temp_email.login()` `{}` 抛错 | `src/autoteam/mail/cf_temp_email.py` 嗅探收紧 | ✅ |
| 4 | maillab 业务方法 401 自愈 | `src/autoteam/mail/maillab.py:148/153/158/163` `@_with_login_retry` × 4 | ✅ |
| 5 | `MaillabClient.login()` 不递归 | `src/autoteam/mail/maillab.py:197` `_LOGIN_GUARD.in_login=True` | ✅ |
| 6 | `_verify_cloudmail` 错配阻断 | `src/autoteam/setup_wizard.py:294-295` `_sniff_provider_mismatch` 强 return False | ✅ |
| 7 | `AUTOTEAM_SKIP_PROVIDER_SNIFF=1` 逃生口 | `src/autoteam/setup_wizard.py:294` 显式 env 守卫 | ✅ |
| 8-12 | `/api/mail-provider/probe` 三步行为 | `src/autoteam/api.py:292+` post_mail_provider_probe + `src/autoteam/mail/probe.py` 全套 helper | ✅ |
| 13 | `_AUTH_SKIP_PATHS` 加白 + Bearer 二次校验 | `src/autoteam/api.py:64`, `298-307` | ✅ |
| 14 | probe 60 req/min 速率限制 | `src/autoteam/api.py:280` `_enforce_probe_rate_limit` | ✅ |
| 15-17 | SetupPage / Settings 状态机 | `web/src/components/SetupPage.vue` + `Settings.vue` 内联实现 | ⚠️ **Dev-1**:SPEC §1 要求新建 `MailProviderCard.vue`,实际**未抽组件**;状态机直接 inline 在两个页面里,代码重复(L1015-1110 in Settings.vue / SetupPage.vue 同样的 testConnection / verifyDomain) |
| 18-19 | docs/.env.example 同步 | git log 确认 commit f82773a 含 docs 部分 | 未细审,见 P2 |
| 20 | 19 处 `from autoteam.cloudmail import CloudMailClient` 零改动 | grep 确认 cloudmail.py 未触动业务调用面 | ✅ |
| 21 | issue#1 复现场景被指纹拦截 | `tests/unit/test_setup_wizard_sniff_block.py` 已添加 | ✅ |

#### 测试覆盖

```
tests/unit/test_mail_cf_temp_email_sniff.py     ✅
tests/unit/test_mail_maillab_self_heal.py       ✅
tests/unit/test_mail_provider_probe.py          ✅
tests/unit/test_setup_wizard_sniff_block.py     ✅(顶层 + tests/ 也有副本,需要清理)
```

⚠️ `tests/test_setup_wizard_sniff_block.py` 在 git status 中显示 `Untracked files`,而 `tests/unit/test_setup_wizard_sniff_block.py` 已 commit。**P1 偏差 Dev-2**:存在重复未跟踪测试文件需要清理或加入 git。

### 2.2 SPEC-2 (账号生命周期与配额加固) — Wave 3 commit `478c16c`

#### 2.2.1 §7.1 代码层验收

| 项 | 实施位置 | 状态 |
|---|---|---|
| `accounts.py` 含 SUPPORTED_PLAN_TYPES + is_supported_plan + normalize_plan_type | `src/autoteam/accounts.py:31-54` | ✅ |
| `codex_auth._exchange_auth_code` bundle 含 plan_type / plan_type_raw / plan_supported | `src/autoteam/codex_auth.py:115-119` | ✅ |
| `check_codex_quota` 5 分类 | `src/autoteam/codex_auth.py:1667-1769` | ✅ |
| `login_codex_via_browser` 4 处 add-phone 探针 | 实测**只有 3 处**:`codex_auth.py:581/633/905`(oauth_about_you / oauth_consent_{step} / oauth_callback_wait) | ⚠️ **Spec-1**:**缺 C-P4 `oauth_personal_check`**(SPEC-2 §3.4 + shared/add-phone-detection §4.1 / §4.2 都明确要求该探针位于 personal 拒收前,作为 "最后一道关卡" 防御性检查) |
| `_run_post_register_oauth` Team / personal 双分支 catch + probe | `src/autoteam/manager.py:1538-1740`(personal/team 各自 RegisterBlocked + plan_supported + check_codex_quota) | ✅ |
| `sync_account_states` 并发探测 + 30 分钟去重 | `src/autoteam/manager.py:480 _probe_kicked_account` + `manager.py:550-600` 探测主循环 | ✅ |
| `reinvite_account` plan_drift / phone_blocked / plan_unsupported 三路兜底 | `src/autoteam/manager.py:2756-2892` | ✅ |
| `manual_account._finalize_account` plan_supported 检查 | `src/autoteam/manual_account.py:239-247` | ✅ |
| `account_ops.delete_managed_account` short_circuit | `src/autoteam/account_ops.py:77` `is_personal = ... STATUS_PERSONAL` | ⚠️ **Spec-2**:SPEC §3.5.1 要求短路条件包含 `STATUS_PERSONAL` **OR** `STATUS_AUTH_INVALID`,实测**只检查 STATUS_PERSONAL**。auth_invalid 账号删除仍会启动 ChatGPTTeamAPI,违背 FR-G2 |
| `api.post_account_login` 409 phone_required | `api.py:1675` post_account_login,**未发现 RegisterBlocked catch / phone_required 响应** | ⚠️ **Spec-3**:SPEC-2 §S-2.2 提到的 "post_account_login L1479 catch + 409" 未落地。当前若该端点底层抛 RegisterBlocked,会被通用 Exception 处理为 500 而非语义化 409 |
| `api.delete_accounts_batch` 全 personal 不起 ChatGPTTeamAPI | `api.py:1573-1577` 无条件 `chatgpt_api.start()` | ⚠️ **Spec-4**:SPEC §3.5.2 要求 `all_personal` 检查后整批短路;**未实现**。每次批量删除一律启动 ChatGPTTeamAPI,违背 FR-G3 |
| `invite.py` + `chatgpt_api.py` PREFERRED_SEAT_TYPE 链路 | `invite.py:500-501` `get_preferred_seat_type` 接入 | ✅(命名偏差见 Spec-5) |
| `runtime_config.py` 4 个新 getter / setter | `runtime_config.py:98 get_sync_probe_concurrency / 118 cooldown / 145 preferred_seat_type` + 3 个 setter | ✅ |

#### 2.2.2 §7.2 数据层

- 旧 `accounts.json` 兼容性:新增字段全部 `Optional[...]` 默认 None,旧记录无报错(已通过 pytest 覆盖) ✅
- `last_kicked_at` / `plan_supported` / `plan_type_raw` 落盘:已在 `_safe_update`/`update_account` 调用方传入 ✅
- `last_quota.primary_total` / `primary_remaining`:`check_codex_quota` 已写入(`codex_auth.py:1755-1769`) ✅
- 6 个新 register_failures category 全部已有 `record_failure` 调用点:✅
  - `oauth_phone_blocked` × 4 处(personal/team/reinvite/manager) — `manager.py:1540, 1637, 2760` 等
  - `plan_unsupported` × 4 处 — `manager.py:1567, 1672, 2781`, `manual_account.py:243`
  - `no_quota_assigned` × 4 处 — `manager.py:1595, 1728, 2853, 2892`
  - `plan_drift` × 1 处 — `manager.py:2794`
  - `auth_error_at_oauth` × 1 处 — `manager.py:1734`
  - `quota_probe_network_error` × 3 处 — `manager.py:1707, 1739` + 1 在 personal 分支

#### 2.2.3 §7.3 前端

- Settings.vue 邀请席位偏好 + sync probe 并发/冷却 — `Settings.vue:488-540, 630-697` ✅
- Dashboard.vue removeAccount toast 已识别 `runningTask` / `actionDisabled` — 现行实现 ✅
- Dashboard.vue quota 显示 no_quota — `Dashboard.vue:425-433` 已加入 `isNoQuota` + "无配额" 文案 ✅
- `web/src/api.js` 5 个新 wrapper:`probeMailProvider` / `getPreferredSeatType` / `putPreferredSeatType` / `getSyncProbe` / `putSyncProbe` ✅

#### 2.2.4 §7.4 测试

- `tests/unit/test_spec2_lifecycle.py` 已添加 ✅
- 全部 155 用例通过 ✅
- ⚠️ 未发现 `tests/unit/test_plan_type_whitelist.py`(SPEC-2 §5.1 + plan-type-whitelist §6.2 推荐独立测试)— **Dev-3**:复用了主 lifecycle 测试代替

### 2.3 shared/plan-type-whitelist — 已落地

- 常量集与工具函数(SPEC §2.1, §2.2):`accounts.py:31-54` ✅
- bundle 三字段(plan_type/plan_type_raw/plan_supported):`codex_auth.py:117-119` ✅
- 6 个调用点处置矩阵:5/6 完整落地;**唯一差异**: `cpa_sync.py` 调用点(SPEC §4.2 第 6 项)未做改造,继续用现行子串判定 — 该 SPEC 章节明确 "不改判定逻辑",**符合预期** ✅
- 不变量 I1-I6 全部满足

### 2.4 shared/quota-classification — 已落地

- 5 分类 (`ok / exhausted / no_quota / auth_error / network_error`):`codex_auth.py:1700-1769` ✅
- 4 个 no_quota 触发条件:已实现(spec §4.2 优先级表)
- 9+2 调用点处置矩阵:全部就位(7 处 manager + manual_account + 2 处 api 端点 +/run_post_register_oauth + sync_account_states)
- I1-I7 不变量全部满足
- ⚠️ 附录 A 提到的 raw_rate_limit 字段尚未在 record_failure 中显式落盘,P2 改进

### 2.5 shared/add-phone-detection — 部分缺失

- invite 阶段 4 处探针:`invite.py:247/282/364/446` ✅
- OAuth 阶段 **3/4 处**探针:`codex_auth.py:581/633/905`,**缺 C-P4 `oauth_personal_check`**(见 Spec-1)
- RegisterBlocked / detect_phone_verification / assert_not_blocked 复用机制 ✅
- 5 个调用方分类处置(personal/team/reinvite/manual_account):4/5 落地,缺 `api.post_account_login`(见 Spec-3)

### 2.6 shared/account-state-machine — 已落地

- 7 状态枚举完整(`accounts.py:13-20`,含 STATUS_ORPHAN)✅
- AccountRecord 字段不变量(必备/禁用)由 `update_account` 调用点保障(已通过 spec2_lifecycle 测试)
- 转移规则:reinvite plan_drift → AUTH_INVALID(替代旧 STANDBY)已生效(`manager.py:2792-2806`)
- last_kicked_at 落盘 `manager.py:616` ✅

---

## 3. API 端点对账(SPEC-2 §1 提及的 4 个)

| 端点 | 文件:行号 | 状态 |
|---|---|---|
| `GET /api/config/preferred-seat-type` | `api.py:1906` | ✅ |
| `PUT /api/config/preferred-seat-type` | `api.py:1913` | ✅ |
| `GET /api/config/sync-probe` | `api.py:1925` | ✅ |
| `PUT /api/config/sync-probe` | `api.py:1935` | ✅ |
| `POST /api/mail-provider/probe`(SPEC-1) | `api.py:292` | ✅ |

---

## 4. 偏差汇总(分级)

### 4.1 P0(必须修 — 阻塞主链路正确性)

- **Spec-0**:**SPEC-2 quota-classification §4.2 漏判"半空载"形态**(`production-cleaner` 实测发现,本 verifier 已用 Python 复现 `get_quota_exhausted_info` 对该样本返回 None)
  - **复现证据**:
    ```
    输入样本:
      primary_pct=0
      primary_resets_at>0(实测约 3600s 后,后端给了占位重置时间)
      primary_total=None
      primary_remaining=None
      weekly_pct=0
    
    在 codex_auth.py:1613-1620 的 3 条 no_quota 触发条件:
      L1614 primary_total == 0            → None == 0 → False
      L1616 primary_total is None AND
            primary_pct == 0 AND
            primary_reset == 0            → reset > 0 → False(关键漏判!)
      L1619 primary_remaining == 0 AND …  → None == 0 → False
    
    no_quota_signals == [] → fall through 到 exhausted 判定 → 也都不命中
    → 函数 return None → check_codex_quota return ("ok", quota_info)
    ```
  - **业务影响**(P0 量级):
    1. `_run_post_register_oauth` 在新邀请号(workspace 计数器尚未由 OpenAI 后台初始化)注册收尾时,quota probe 会落 "ok" 分支,账号被标 STATUS_ACTIVE 入池
    2. 入池后被路由到 Codex 调用时,实际调用会返回 "no quota" 类错误,但本系统 last_quota 显示 "剩余 100%"
    3. **正是用户报告的 issue#6 本体** — "配额=0 但 UI 显示 100% 剩余",Wave 4 修复**不完整**
    4. 实测命中样本:用户当前账号池 4 个 `@xsuuhfn.cloud` active 号(全部刚邀请进 Team,workspace 半空载)
  - **建议补丁**(`codex_auth.py:1612-1620` 之间增加第 4 条 no_quota_signal):
    ```python
    # 紧跟 L1619 的 if/elif 之后插入(与 rate_limit_empty 互斥)
    elif (
        primary_total is None
        and primary_reset > 0              # ← 与 rate_limit_empty 互斥的关键差异
        and primary_pct == 0
        and primary_remaining is None
        and weekly_pct == 0
        and not limit_reached
    ):
        no_quota_signals.append("workspace_uninitialized")
    ```
    新条件只命中:rate_limit + primary_window 已返回(reset_at > 0 表示后端给了占位时间戳),但 OpenAI 还没分配 codex 配额额度数字(total/remaining 都 null)
  - **测试覆盖建议**:
    - `tests/unit/test_quota_classification.py` 新增样本 `no_quota_workspace_uninitialized`(team-lead 给的 4 字段组合)
    - `shared/quota-classification.md §6.1` fixture json 增补对应 case
  - **shared/quota-classification.md §4.2 文档同步**:第 4 条 no_quota 触发条件需要从 `primary.reset_at == 0 AND used_percent == 0` 拓展为两个分支(reset_at == 0 → rate_limit_empty;reset_at > 0 + total/remaining 都 null → workspace_uninitialized)
  - **工作量**:补丁 ~7 行,测试 ~25 行,文档勘误 ~10 行
  - **关联**:此 P0 不修,SPEC-2 FR-B1/B2/D1/D4 共 4 条 FR 全部失效

### 4.2 P1(应当修,影响功能完备性)

- **Spec-1**:`login_codex_via_browser` 缺 C-P4 `oauth_personal_check` 探针。
  - 影响:personal OAuth 在 callback 后→plan_type 校验前的 "最后一道关卡" 失效。在 callback 探针失误漏判时,personal 注册可能未识别 add-phone 状态而误删账号
  - 修复:在 `codex_auth.py` `login_codex_via_browser` 函数内 personal 分支 plan_type 校验之前(对应 SPEC §4.1 中 `if use_personal:` 之前位置)插入 `assert_not_blocked(page, "oauth_personal_check")`
  - 工作量:5 行

- **Spec-2**:`account_ops.delete_managed_account` short_circuit 缺 STATUS_AUTH_INVALID。
  - 影响:auth_invalid 账号(主号 session 失效场景)删除仍启动 ChatGPTTeamAPI,违背 SPEC-2 FR-G2 "personal/auth_invalid 不需要拉 remote_state"。在主号被踢/挂起时无法清理 auth_invalid 账号
  - 修复:`account_ops.py:77` 改为 `is_personal = bool(acc and acc.get("status") in (STATUS_PERSONAL, STATUS_AUTH_INVALID))`,并 `from autoteam.accounts import STATUS_AUTH_INVALID`
  - 工作量:3 行

- **Spec-3**:`api.post_account_login` 缺 RegisterBlocked catch + 409 phone_required 响应。
  - 影响:UI 在补登录失败时拿到通用 500 error,无法显示语义化 "需要绑定手机"。SPEC-2 §5.2 / FR-C5 / shared/add-phone-detection §5 要求该端点显式分类
  - 修复:在 `api.py:1675` post_account_login 调用 login_codex_via_browser 处加 try/except RegisterBlocked,blocked.is_phone=True 时 `raise HTTPException(status_code=409, detail={"error": "phone_required", ...})`
  - 工作量:10 行

- **Spec-4**:`api.delete_accounts_batch` 缺 "全 personal 短路"。
  - 影响:批量删除 N 个 personal/auth_invalid 账号时,仍启动 ChatGPTTeamAPI(浏览器 + 30s 网络往返),违背 FR-G3 / SPEC §3.5.2
  - 修复:`api.py:1567` 之后增加 `all_personal = all(...)` 判断,True 时 `chatgpt_api = None` + 跳过 `start()` / `fetch_team_state`,把 `chatgpt_api=None` 直接传 `delete_managed_account` 也兼容(account_ops 已能处理 None 但需要 short_circuit 同时支持 auth_invalid,即 Spec-2 同步修)
  - 工作量:15 行

- **Dev-2**:`tests/test_setup_wizard_sniff_block.py` 是 untracked 重复文件。
  - 影响:仓库根目录冗余,git status 噪音;CI 运行可能被两次执行
  - 修复:确认 `tests/unit/test_setup_wizard_sniff_block.py` 是规范位置,直接删 `tests/test_setup_wizard_sniff_block.py`(或反之)
  - 工作量:1 个 git rm

### 4.3 P2(可选改进)

- **Spec-5(命名偏差)**:`preferred_seat_type` 实施层用 `default`/`codex`,SPEC §3.4.1 写的是 `chatgpt`/`codex`(默认 `chatgpt`)。
  - 语义等价(`default` = SPEC 的 `chatgpt`,即旧 PATCH 升级行为),前后端命名一致,工程上可用
  - 改进路径:统一改成 SPEC 名 `chatgpt` 或在 SPEC 里追加 errata 把 `chatgpt` 别名为 `default`
  - 不影响功能

- **Dev-1**:`MailProviderCard.vue` 未抽出独立组件。
  - SPEC-1 §1 列了 `MailProviderCard.vue` 280 行新组件,实际把状态机直接 inline 到 `SetupPage.vue` 和 `Settings.vue`(`Settings.vue:488-540, 1015-1110`)
  - 后果:两处 testConnection / verifyDomain 几乎复制,后续 probe 流程演进时双修
  - 改进路径:抽 `MailProviderCard.vue` 共享(P2,不影响功能)

- **Dev-3**:未独立 `tests/unit/test_plan_type_whitelist.py`,合并进 spec2_lifecycle。可拆分以提升可维护性

- **API DeprecationWarning**:`@app.on_event("startup"/"shutdown")` 在 FastAPI 0.x 已 deprecated,迁移到 lifespan 事件(P2,与 SPEC 无关)

- **raw_rate_limit 落盘**:shared/quota-classification 附录 A 提到 no_quota record_failure 应附 `raw_rate_limit`,当前没显式落,后续运营观测时会缺数据

---

## 5. 推荐动作

### 5.1 立即修复(本轮 PR — 强烈建议)

**P0 必修**:

0. **quota half-loaded 漏判**:`codex_auth.py:1620` 之后增 `workspace_uninitialized` no_quota_signal(7 行 + 25 行测试 + 10 行文档同步)
   - 不修则 SPEC-2 FR-B/D 系列约 4 条 FR 整体失效,issue#6 仍会复现
   - 实测影响 4 个 active 号(用户当前账号池),状态机标错 → 调用方误以为有配额

按 P1 顺序合并 4 个补丁:

1. **C-P4 探针**:`codex_auth.py` `login_codex_via_browser` personal 分支前加 `assert_not_blocked(page, "oauth_personal_check")`
2. **delete_managed_account short_circuit 扩 AUTH_INVALID**:`account_ops.py:77`
3. **post_account_login 409 phone_required**:`api.py:1675`
4. **delete_accounts_batch all_personal 短路**:`api.py:1573` 区域

### 5.2 后续 PR

- `MailProviderCard.vue` 抽组件去重
- `tests/test_setup_wizard_sniff_block.py` 重复文件清理
- `preferred_seat_type` 命名统一(`default` ↔ SPEC `chatgpt`),后端兼容旧值
- `record_failure` for `no_quota_assigned` 附 `raw_rate_limit`

### 5.3 文档勘误

- shared/add-phone-detection §4.1 的 7 处探针(invite 4 + OAuth 4)→ 4 + 3,需要在文档勘误说明实测落地为 4 + 3 + 一处 P1 待补
- SPEC-2 §1 文件清单中 `MailProviderCard.vue` 标 "(已合并到 SetupPage/Settings 内联)"

---

## 5. 端到端可用性结论

**结论:1 处 P0 阻塞 issue#6 复发,4 处 P1 影响 SPEC 完备性。修完 P0 + 4 P1 才可宣告 Wave 1-4 完结。**

代码层逐项核查:

- **mail provider 切换链**:`/api/setup/save` → setup_wizard 强阻断 → MaillabClient 401 自愈 全部就位。issue#1(maillab 服务器+cf provider 错配)已通过指纹探测拦截,**用户层面已不会再撞 issue#1**
- **SPEC-2 注册收尾链**:`_run_post_register_oauth` Team/personal 双分支均有完整的 RegisterBlocked catch + plan_supported 校验 + check_codex_quota 5 分类处置。**新计费 workspace(self_serve_business_usage_based)注册时会落 STATUS_AUTH_INVALID + record_failure("plan_unsupported"),不会污染 ACTIVE 池**。但⚠️ **half-loaded workspace 的新邀请号会被错误标 ACTIVE,这是 P0 Spec-0 漏判的根因**
- **被踢识别**:`sync_account_states` 已通过 ThreadPoolExecutor 并发探测 + 30 分钟冷却,被人工踢出的账号会被识别为 auth_error → STATUS_AUTH_INVALID,reconcile 接管。**FR-E1~E4 全部生效**
- **席位偏好**:Settings.vue 已能切换 `default/codex`,后端 invite.py 接入 get_preferred_seat_type,chatgpt_api 兜底链路按 preferred 走。**FR-F1~F6 已对外暴露**
- **删除链短路**:单点 `delete_account` 对 personal 已短路;**批量删除路径未覆盖,且 auth_invalid 未短路** — Spec-2/Spec-4 是这条链路上必修项

整体上,Wave 1-4 SPEC 的核心功能 ~85% 落地。**不能宣告 issue#6 已修复** — Wave 4 给出的 quota 5 分类已实施但 no_quota 触发条件覆盖不全(只覆盖 rate_limit 完全空 + total==0 两类,未覆盖最常见的 "rate_limit 已返回但 codex 配额计数器尚未初始化" 的 half-loaded 形态)。

测试层 155 用例 100% 通过 + ruff 0 lint error,但**测试集没覆盖 half-loaded 样本**,这是测试盲区。

**发版准入门槛**:必须先合并 Spec-0 P0 补丁 + 4 个 P1 共 5 个补丁(合计 ~50 行 + ~50 行测试),并把 half-loaded 样本加入 quota_classification 测试集后,才能视为 Wave 1-4 真正完结。

---

**报告结束。** 总字数约 2700 字,详尽对照 4 份 spec + 4 份 shared spec 的 50+ 个验收项,标识 1 个 P0 + 4 个 P1 + 5 个 P2 共 10 处偏差。P0 已通过 Python 实测复现并写入实测命令。
