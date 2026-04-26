# PRD-6: Round 7 P2 + Round 6 Deferred 收尾

## 0. 元数据

| 字段 | 值 |
|---|---|
| PRD 编号 | PRD-6 |
| 主题 | Round 5 verify P2 + Round 6 review deferred 8 项收尾 |
| 关联 verify | `prompts/0426/verify/wave1-4-integration-report.md` §4.3(P2 × 5)+ `prompts/0426/verify/round6-review-report.md` §5(deferred × 3)+ `prompts/0426/verify/round6-impl-report.md`(实施细节) |
| 关联 SPEC | `spec/spec-1-mail-provider.md` v1.0 + `spec/spec-2-account-lifecycle.md` v1.3 + `spec/shared/quota-classification.md` v1.3 + `spec/shared/account-state-machine.md` v1.0 |
| 关联 issue | issue#1(MailProviderCard 抽组件回声)+ issue#6(deferred 路径补完) |
| 主笔 | spec-writer (Round 7,stage 1 of 3) |
| 创建日期 | 2026-04-26 |
| 状态 | DRAFT(待 patch-implementer 落地) |
| 修复粒度 | P2 × 5(命名 / 抽组件 / 测试拆分 / lifespan 迁移 / record_failure 字段)+ Deferred × 3(24h 去重 / 前端 409 解析 / 状态机文档) |
| 优先级 | 全部 P2 — 不阻塞发版,作为质量与可维护性收尾 |

---

## 1. 背景

Round 6 的 quality-reviewer 终审(`round6-review-report.md`)报告 0 P0 / 1 P1(已落 v1.3 SPEC)/ 2 P2。叠加 Round 5 `wave1-4-integration-report.md` §4.3 列出的 5 处 P2 偏差,以及 Round 6 实施时明确推迟到 Round 7 的 3 项 deferred 议题,共 8 件事在本轮收尾。

### 1.1 P2 — Round 5 verify 留下 5 项

1. **`preferred_seat_type` 命名偏差**(Spec-5):SPEC §3.4.1 写 `chatgpt`/`codex`(默认 `chatgpt`),实施落 `default`/`codex`(默认 `default`,见 `runtime_config.py:141-142`)。语义等价但命名不一致,导致文档与代码读者会困惑。前端/UI/runtime_config.json 都已使用 `default`,**改实施会破坏向后兼容**(老用户的 runtime_config.json 已写 `"preferred_seat_type": "default"` 字面量)。**推荐:改 SPEC,加 `chatgpt` 别名转移期支持**

2. **`MailProviderCard.vue` 未抽组件**(Dev-1):SPEC-1 §1 列了 280 行新组件,实施却把状态机直接 inline 到 `web/src/components/SetupPage.vue` + `web/src/components/Settings.vue` 各一份,代码重复 ~80 行(testConnection / verifyDomain 几乎雷同)。后续 probe 流程演进时双修,长期维护成本上升

3. **`tests/unit/test_plan_type_whitelist.py` 未拆分**(Dev-3):shared/plan-type-whitelist.md §6.2 推荐独立测试文件,实施合并到 `test_spec2_lifecycle.py`(共享 fixture)。文件尺寸增大、测试焦点混杂,可读性差

4. **FastAPI `@app.on_event` deprecation**:`api.py` 多处(实测 `:2519` startup + 至少 1 处 shutdown)用 `@app.on_event("startup"/"shutdown")`,FastAPI 0.10x 已 deprecated。当前 4 个 `DeprecationWarning` 在 pytest 输出常态化,迁移到 `@asynccontextmanager` lifespan handler 是现代写法

5. **`raw_rate_limit` 落 record_failure**:`shared/quota-classification.md` 附录 A 提到 `no_quota_assigned` 的 `record_failure` 应附 `raw_rate_limit`(wham/usage 原始 `rate_limit` 字段),便于事后排查 OpenAI 协议变化。当前未显式落,后期诊断麻烦

### 1.2 Deferred — Round 6 推迟 3 项

6. **manager 24h 去重 + `last_codex_smoke_at` / `last_smoke_result` 字段**:Round 6 patch-implementer 把 cheap_codex_smoke 内部消化在 `check_codex_quota` 中(决策 2),但**未实现 24h 去重 cache**。当前由 wham/usage 整体 30s timeout + `_probe_kicked_account` 30 分钟去重间接节流,但严格意义上 fresh seat 命中率超预期时(R2 风险)smoke 调用密度仍可能爆。Round 7 议题:落 accounts.json 字段 + manager 在调 cheap_codex_smoke 前先读 cache

7. **前端 `web/src/api.js` 解析 409 phone_required / register_blocked**:Round 6 patch-implementer 决策 4 把 `task["error"]` 字符串化携带关键字(`phone_required` / `register_blocked`),但前端 `api.js` 在 `polling task status` 时未明确解析,UI 仍以通用 error 提示。需要前端按字符串匹配关键字给出友好提示("此号需要绑定手机才能完成 OAuth"等)

8. **`shared/account-state-machine.md` 转移矩阵小注**:Round 6 落地了 C-P4 探针(P1.1)+ uninitialized_seat 中间状态(P0)+ STATUS_AUTH_INVALID 短路(P1.2/P1.4),但 `account-state-machine.md` v1.0 §3 / §4 / §6 都未同步注释。文档与实施脱节

### 1.3 不修的代价

- **不修 P2.1 命名偏差**:文档与代码长期不一致 → 新工程师上手时困惑、潜在的"按文档改实施"可能反向引入回归
- **不修 P2.2 抽组件**:每次 probe 流程改 都要同步两个 .vue 文件,长期 ~10% 双修概率引入两侧不一致 bug
- **不修 P2.3 测试拆分**:lifecycle 测试文件 ~600 行,新增 case 时焦点漂移
- **不修 P2.4 lifespan**:FastAPI 1.0 移除 `on_event` 时一次性升级压力大;现在 deprecation warning 也增加 noise,影响 CI 信号
- **不修 P2.5 raw_rate_limit**:OpenAI 协议变化时缺乏一手数据排查
- **不修 D6 24h 去重**:R2 风险触发(命中率 > 5%)时 smoke 调用每分钟超阈
- **不修 D7 前端 409 解析**:UX 损失,但不阻塞业务
- **不修 D8 状态机文档**:维护文档失效,后续 patch 拿过时 SPEC 容易撞错

---

## 2. 目标

| # | 目标 | 衡量 |
|---|---|---|
| G1 | `preferred_seat_type` SPEC 与实施命名达成一致 | spec-2 §3.4.1 接受 `default`/`chatgpt`(别名) /`codex`,set_preferred_seat_type 接收 `chatgpt` 后 normalize 为 `default` |
| G2 | `MailProviderCard.vue` 抽出共享组件 | `web/src/components/MailProviderCard.vue` 存在;SetupPage.vue + Settings.vue 各 import 复用 |
| G3 | `test_plan_type_whitelist.py` 独立文件 | tests/unit/test_plan_type_whitelist.py 存在,test_spec2_lifecycle.py 不再含 plan_type 专项 case |
| G4 | FastAPI on_event 全部迁移到 lifespan | `pytest` 输出 0 个 `DeprecationWarning`(原 4 个) |
| G5 | `no_quota_assigned` record_failure 附 `raw_rate_limit` | grep `record_failure.*raw_rate_limit` 命中至少 1 处 |
| G6 | manager 24h 去重 cache 落地 | accounts.json 含 `last_codex_smoke_at` 字段;check_codex_quota 24h 内 cache 命中不调网络 |
| G7 | 前端 409 关键字解析 | api.js 解析 task["error"] 含 `phone_required` 时给出 UI toast |
| G8 | account-state-machine.md 同步 Round 6 转移与短路语义 | v1.1 §3 矩阵加 cheap_codex_smoke / STATUS_AUTH_INVALID 短路 / uninitialized_seat 中间态 |
| G9 | 不引入回归 | pytest 全 178 + 新增 ~25 用例全绿,ruff 0 lint error |

---

## 3. 非目标(明确不做)

- **不**深度重构 `web/src/components/SetupPage.vue`(只抽 testConnection / verifyDomain 状态机到 MailProviderCard.vue,SetupPage 其余 layout 保留)
- **不**改测试覆盖率目标(P2.3 拆分纯文件级,case 内容不变)
- **不**为 cheap_codex_smoke 加用户级速率限制 — 24h 去重已足够(Round 6 §3 决策延续)
- **不**改 `preferred_seat_type` 实施层 enum 值(`default`/`codex` 保留,只在 SPEC 加 `chatgpt` 别名 + setter 接受 `chatgpt` 转 `default`,转移期支持)
- **不**对前端做架构重构(api.js 仅追加 409 解析逻辑,不改全局 error 处理框架)
- **不**改 24h 去重粒度(account 维度,Round 6 Q-2 决策延续)

---

## 4. 用户故事

### 4.1 运维

> 作为运维,我用工程师 Round 5/6 SPEC 作为对账依据,但发现 SPEC §3.4.1 写 `chatgpt` 而代码注释 / runtime_config.json 都是 `default`,我需要在 SPEC 与代码之间反复对照才能确认是同一个设置。希望 SPEC 加 `chatgpt` 作为兼容别名 + 注明实施实际值,统一交流语言。

> 作为运维,使用 24h 去重后,新邀请的批量号(50 个)在初始化期内的 fresh seat 形态不会触发 50 次 cheap_codex_smoke,而是只首轮调一次,后续 24h 内直接读 cache。

### 4.2 前端 UX

> 作为前端用户,在 Settings 页点"补登录",撞 add-phone 时希望 UI 直接弹出"该账号需要绑定手机才能继续 OAuth",而不是通用错误。希望 api.js 解析 task["error"] 关键字并给出具体提示。

### 4.3 开发者

> 作为开发者,看到 SetupPage 和 Settings 各有一份近 100 行的 testConnection/verifyDomain 代码,想改 probe 流程时纠结要不要双修。希望 MailProviderCard.vue 抽出来,改一处即生效。

> 作为开发者,跑 pytest 看到 4 个 `DeprecationWarning` 信号噪音,在 CI 里很难一眼分辨真问题。希望 lifespan 迁移把 deprecation 清零。

---

## 5. 功能需求

### 5.1 FR-P2.1: `preferred_seat_type` 命名统一(改 SPEC)

**问题**:实施层 `runtime_config.py:141-142`:
```python
_PREFERRED_SEAT_TYPE_DEFAULT = "default"
_PREFERRED_SEAT_TYPE_VALID = {"default", "codex"}
```

SPEC `spec-2 §3.4.1` v1.3 写:
```python
_VALID_SEAT_TYPES = frozenset({"chatgpt", "codex"})
def get_preferred_seat_type() -> str:
    raw = (get("preferred_seat_type") or "chatgpt").strip().lower()
```

**规范**(改 SPEC,实施保持 `default`/`codex`):

`spec-2 §3.4.1` 改为接受 3 个值的归一化集合:
- `default` — 主名,默认值,行为=旧 PATCH 升级 ChatGPT 完整席位
- `chatgpt` — 别名,转移期接受;set_preferred_seat_type 收到 `chatgpt` normalize 为 `default`
- `codex` — codex-only 席位,锁 usage_based,不升级

**实施期约束**(轻量改 setter):

```python
# runtime_config.py 改 set_preferred_seat_type(已存在,只补 alias 接受逻辑)
def set_preferred_seat_type(value):
    val = (str(value or "") or _PREFERRED_SEAT_TYPE_DEFAULT).strip().lower()
    # ★Round 7:接受 chatgpt 作为 default 的转移期别名
    if val == "chatgpt":
        val = "default"
    if val not in _PREFERRED_SEAT_TYPE_VALID:
        val = _PREFERRED_SEAT_TYPE_DEFAULT
    set_value("preferred_seat_type", val)
    return val
```

**SPEC §3.4.1** 同步描述:`get_preferred_seat_type()` 返回值永远是 `{default, codex}` 之一(不返回 chatgpt);setter 内部把 `chatgpt` 视为 `default` 别名。

**验收**:
- `set_preferred_seat_type("chatgpt")` 返回 `"default"` 且写盘后读回也是 `"default"`
- 新增 unit test:`test_set_preferred_seat_type_accepts_chatgpt_alias`
- spec-2 §3.4.1 修订记录注明:命名归一化 / 别名转移期 / 默认值 default

### 5.2 FR-P2.2: MailProviderCard.vue 抽共享组件

**位置**:`web/src/components/MailProviderCard.vue`(新)

**设计目标**:

把 SetupPage.vue + Settings.vue 各自的 testConnection / verifyDomain 状态机抽到一个共享组件,接收 props 控制差异(`mode: 'setup' | 'settings'`),emit 事件回传父组件(`@verified`、`@error`)。

**组件契约**(Vue 3 SFC):

```vue
<script setup lang="ts">
import { reactive, ref } from 'vue'
import { probeMailProvider } from '../api'

interface Props {
  mode?: 'setup' | 'settings'   // setup: 控制后续 SAVE step 解锁;settings: 直接保存
  initial?: {
    provider?: 'cf_temp_email' | 'maillab'
    base_url?: string
    username?: string
    password?: string
    admin_password?: string
    domain?: string
  }
}

const props = withDefaults(defineProps<Props>(), { mode: 'setup' })
const emit = defineEmits<{
  verified: [payload: { provider: string; domain: string; baseUrl: string }]
  error: [errorCode: string, message: string]
}>()

const form = reactive({ ...props.initial })
const state = ref<'PROVIDER' | 'CONNECTION' | 'DOMAIN' | 'DONE'>('PROVIDER')
const domainList = ref<string[]>([])
const lastError = ref<{ error_code: string; message: string; hint?: string } | null>(null)

async function testConnection() {
  // 复用原 SetupPage.vue / Settings.vue 的 fingerprint + credentials 调用
  // ...
}

async function verifyDomain() {
  // 复用原 domain_ownership 调用
  // ...
}
</script>

<template>
  <div class="mail-provider-card">
    <!-- 4 步 wizard layout -->
  </div>
</template>
```

**改造**:
- SetupPage.vue: 删除内联 testConnection / verifyDomain,改用 `<MailProviderCard mode="setup" :initial="config" @verified="onVerified" />`
- Settings.vue: 同上,`mode="settings"`

**验收**:
- `web/src/components/MailProviderCard.vue` 文件存在,~280 行(SPEC-1 §1 预估)
- SetupPage.vue 中 `testConnection` / `verifyDomain` 函数定义被删除
- Settings.vue 同上
- `grep -rn "probeMailProvider" web/src` 应只在 MailProviderCard.vue + api.js 中出现
- 已有 .vue 测试 / 集成测试无回归

### 5.3 FR-P2.3: test_plan_type_whitelist.py 拆分

**位置**:`tests/unit/test_plan_type_whitelist.py`(新)

**改造步骤**:
1. 从 `tests/unit/test_spec2_lifecycle.py` 抽出所有 plan_type 相关测试 case(grep 关键字 `plan_type` / `is_supported_plan` / `normalize_plan_type` / `SUPPORTED_PLAN_TYPES`)
2. 移到新文件 `test_plan_type_whitelist.py`,共享 fixture(可复制或在 conftest.py 提取)
3. 验证 pytest 全套通过、case 数量不变

**新文件骨架**:

```python
# tests/unit/test_plan_type_whitelist.py
"""SPEC-2 + shared/plan-type-whitelist 专项测试。

从 test_spec2_lifecycle.py 抽出(Round 7 P2.3)。
"""
import pytest
from autoteam.accounts import (
    SUPPORTED_PLAN_TYPES,
    is_supported_plan,
    normalize_plan_type,
)


class TestSupportedPlanTypes:
    def test_supported_set_immutable(self):
        ...

class TestNormalizePlanType:
    def test_uppercase_normalized(self):
        ...

class TestIsSupportedPlan:
    def test_team_supported(self):
        ...
```

**验收**:
- 新文件存在,case 数与 test_spec2_lifecycle.py 抽出后总数不变
- `pytest tests/unit/test_plan_type_whitelist.py` 单独跑通
- pytest 全套测试通过

### 5.4 FR-P2.4: FastAPI on_event → lifespan 迁移

**位置**:`src/autoteam/api.py` 多处 `@app.on_event` 装饰器

**改造**:

把现有 startup / shutdown handler 合并到 `@asynccontextmanager` lifespan,FastAPI 在 `app = FastAPI(lifespan=...)` 中传入。

**改前**(实测 api.py:2519 区域):
```python
@app.on_event("startup")
async def _startup_warmup():
    # 现有 startup 逻辑
    ...

@app.on_event("shutdown")
async def _shutdown_cleanup():
    # 现有 shutdown 逻辑
    ...

app = FastAPI()
```

**改后**:
```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    # === startup ===
    logger.info("[lifespan] starting")
    # 把 _startup_warmup 函数体迁过来
    ...
    yield
    # === shutdown ===
    logger.info("[lifespan] stopping")
    # 把 _shutdown_cleanup 函数体迁过来
    ...

app = FastAPI(lifespan=app_lifespan)
# 删除 @app.on_event 装饰器
```

**注意**:
- `app = FastAPI()` 调用顺序很关键 — `app_lifespan` 必须在 `app = FastAPI(...)` 之前 def,但 lifespan 内部对 app 的引用是延迟绑定,符合 Python 闭包语义,不会有循环引用
- 若 startup 内部依赖 module-level 全局变量,迁移后行为等价;若依赖 app.state 或 app.dependency_overrides,需要手动测试一遍
- TestClient 自动适配 lifespan(无需改 conftest.py)

**验收**:
- `pytest -W error::DeprecationWarning` 不再触发 `@app.on_event` deprecation(原 4 个 warning 清零)
- e2e: `python -m autoteam serve` 启动 + Ctrl+C 停止,日志含 `[lifespan] starting` / `[lifespan] stopping`

### 5.5 FR-P2.5: raw_rate_limit 落 record_failure

**位置**:`codex_auth.py:check_codex_quota` 内 no_quota 分支

**改造**:

`record_failure(category="no_quota_assigned", ...)` 调用点附 `raw_rate_limit` 字段,把 wham/usage 返回的 `body.get("rate_limit")` 原始 dict 序列化成 JSON 字符串后落进 register_failures.json 的 record。

**改前**(假设 manager.py 现有):
```python
record_failure(
    email,
    category="no_quota_assigned",
    reason="wham/usage 返回 no_quota",
    plan_type=bundle_plan,
    stage="run_post_register_oauth_team",
)
```

**改后**:
```python
import json as _json

raw_rate_limit_str = ""
if isinstance(quota_info, dict) and "raw_rate_limit" in quota_info:
    try:
        raw_rate_limit_str = _json.dumps(quota_info["raw_rate_limit"], ensure_ascii=False)[:2000]
    except Exception:
        raw_rate_limit_str = ""

record_failure(
    email,
    category="no_quota_assigned",
    reason="wham/usage 返回 no_quota",
    plan_type=bundle_plan,
    stage="run_post_register_oauth_team",
    raw_rate_limit=raw_rate_limit_str,        # ★ Round 7 新增
)
```

**协同改造**:
- `check_codex_quota` 在返回 `no_quota` / `exhausted` 的 `QuotaExhaustedInfo` 中额外注入 `raw_rate_limit` 字段(原始 wham 响应的 `rate_limit` 子树)
- `register_failures.py` 的 RegisterFailureRecord 模型加 `raw_rate_limit: Optional[str]` 字段(可缺失,旧记录兼容)

**验收**:
- 调用点 grep `record_failure.*raw_rate_limit` 命中至少 4 处(quota-classification §5.1 列出的 9+2 调用点中所有 no_quota 分支)
- register_failures.json 单条 no_quota_assigned 记录含 `raw_rate_limit` 字段 + 内容是合法 JSON 字符串

### 5.6 FR-D6: manager 24h 去重 cheap_codex_smoke

**位置**:`codex_auth.py:check_codex_quota` 内 uninitialized_seat 分支 + `accounts.py` 字段扩

**accounts.json 字段扩**:

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `last_codex_smoke_at` | float \| null | null | 上次 cheap_codex_smoke 调用的 epoch seconds |
| `last_smoke_result` | str \| null | null | `"alive"` / `"auth_invalid"` / `"uncertain"` |

**24h 去重契约**(check_codex_quota 内部消化,与 Round 6 决策 2 兼容):

```python
# codex_auth.py:check_codex_quota 内 uninitialized_seat 分支(Round 7 改造)
import time

_CODEX_SMOKE_DEDUP_SECONDS = 86400  # 24h


def check_codex_quota(access_token, account_id=None):
    ...
    # wham/usage 200 + uninitialized_seat 形态命中
    if status == "ok" and quota_info.get("window") == "uninitialized_seat":
        # ★ Round 7 24h 去重 cache lookup
        cached = _read_codex_smoke_cache(account_id)
        if cached:
            cached_at, cached_result = cached
            if time.time() - cached_at < _CODEX_SMOKE_DEDUP_SECONDS:
                # cache 命中,直接转 5 分类(不调网络)
                if cached_result == "alive":
                    quota_info["smoke_verified"] = True
                    quota_info["last_smoke_result"] = "alive"
                    quota_info["smoke_cache_hit"] = True
                    return ("ok", quota_info)
                elif cached_result == "auth_invalid":
                    return ("auth_error", None)
                else:  # uncertain
                    return ("network_error", None)

        # cache miss,调 cheap_codex_smoke
        smoke_result, smoke_reason = cheap_codex_smoke(access_token, account_id)
        _write_codex_smoke_cache(account_id, smoke_result)
        # ... 原有 alive/auth_invalid/uncertain 分支转 5 分类
```

**新工具函数**(`codex_auth.py` 末尾):

```python
def _read_codex_smoke_cache(account_id):
    """从 accounts.json 读 last_codex_smoke_at + last_smoke_result。
    返回 (epoch_at, result) 或 None。
    """
    if not account_id:
        return None
    from autoteam.accounts import load_accounts
    for acc in load_accounts():
        if acc.get("workspace_account_id") == account_id or acc.get("email") == account_id:
            ts = acc.get("last_codex_smoke_at")
            res = acc.get("last_smoke_result")
            if ts and res:
                return (float(ts), str(res))
    return None


def _write_codex_smoke_cache(account_id, result):
    """落盘 last_codex_smoke_at + last_smoke_result,用于 24h 去重。"""
    if not account_id:
        return
    from autoteam.accounts import load_accounts, update_account
    for acc in load_accounts():
        if acc.get("workspace_account_id") == account_id or acc.get("email") == account_id:
            update_account(
                acc["email"],
                last_codex_smoke_at=time.time(),
                last_smoke_result=result,
            )
            return
```

**注意**:Round 6 已有 `quota_info.smoke_verified` + `quota_info.last_smoke_result` 字段(注入 last_quota,不落 accounts.json 顶层),Round 7 需要把这两个字段额外**也**写入 accounts.json 顶层(`last_codex_smoke_at` / `last_smoke_result`)。共存语义:
- `acc.last_quota.smoke_verified` — 最近一次 wham 探测的快照(可以是 24h 之前的旧 quota_info)
- `acc.last_codex_smoke_at` / `acc.last_smoke_result` — 最近一次实际 smoke 调用的时间戳与结果(用于 24h 去重)

**验收**:
- accounts.json 含 `last_codex_smoke_at` / `last_smoke_result` 字段(可缺失)
- 24h 内对同 account_id 重复调 check_codex_quota,实际 cheap_codex_smoke 网络调用数 = 1
- 24h+1s 时间戳被绕过,cheap_codex_smoke 网络调用数 = 2
- 不变量 I9(quota-classification.md):cheap_codex_smoke 在 24h cache 命中时不调用网络

### 5.7 FR-D7: 前端 api.js 409 关键字解析

**位置**:`web/src/api.js`(项目使用 .js 而非 .ts,以仓库实际为准)

**改造**:

`api.js` 在 polling task status 时,如果 `task.error` 字符串含 `phone_required` / `register_blocked` 关键字,自动 throw 一个带 code 的 Error,UI 调用方可解析并给出友好 toast。

**改前**(假设现有 pollTask):
```javascript
async function pollTask(taskId) {
  const resp = await request('GET', `/tasks/${taskId}`)
  if (resp.error) {
    throw new Error(resp.error)
  }
  return resp
}
```

**改后**:
```javascript
async function pollTask(taskId) {
  const resp = await request('GET', `/tasks/${taskId}`)
  if (resp.error) {
    const errStr = String(resp.error)
    // ★ Round 7:解析 Round 6 P1.3 注入的关键字串
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

**UI 调用方改造**(可选,Settings.vue 等):
```javascript
try {
  await api.pollTask(taskId)
} catch (e) {
  if (e.code === 'phone_required') {
    showToast(e.message, 'warning')
  } else {
    showToast(`操作失败: ${e.message}`, 'error')
  }
}
```

**验收**:
- api.js pollTask / pollTaskStatus 分支含 `phone_required` 字符串匹配
- 单测(若有 web 测试 setup)mock task["error"] = "409: {'error': 'phone_required', ...}",验证 throw error 的 code === 'phone_required'

### 5.8 FR-D8: account-state-machine.md 转移矩阵小注

**位置**:`spec/shared/account-state-machine.md`(本 PRD-6 任务 2 同步修订)

**修订要点**:

1. **§3 状态转移矩阵**:加 STATUS_ACTIVE → STATUS_AUTH_INVALID 的触发条件 `cheap_codex_smoke 返回 auth_invalid`(Round 6 FR-P0)
2. **§3 转移规则**:加 `uninitialized_seat 中间状态`(从 wham 半空载到 cheap_codex_smoke 验证完成期间,概念上是 STATUS_ACTIVE 的子态,不落盘 status,只在 last_quota.window=uninitialized_seat 标识)
3. **§4 删除短路语义**:加 STATUS_AUTH_INVALID 与 STATUS_PERSONAL 的等价处置(Round 6 FR-P1.2 / P1.4)
4. **§6 不变量**:加 add-phone 探针 7 处接入要求(invite 4 + OAuth 3,Round 6 实施实际 — invite 4 + OAuth C-P1/C-P2/C-P3/C-P4 共 4 处,7 处可写为"4 + 4=8 处"或保留 7 处对应 SPEC v1.0 假设)

**修订记录**:加 v1.1 — 2026-04-26 Round 7 P2 follow-up,同步 Round 6 落地的 cheap_codex_smoke + uninitialized_seat 中间态 + STATUS_AUTH_INVALID 短路语义 + 7 处探针接入。

(详细修订内容见任务 2 SPEC 修订)

---

## 6. 非功能需求

| # | NFR | 衡量 |
|---|---|---|
| NFR-1 | 24h 去重不破坏 sync_account_states 30s 周期上限 | 命中 cache 时 0 网络调用,P95 < 50ms |
| NFR-2 | MailProviderCard.vue 共享组件不增加首屏加载体积 | bundle size 净变化 < 10KB(Vue chunk 复用) |
| NFR-3 | lifespan 迁移不破坏 startup 时序 | startup 日志顺序与原 on_event 等价 |
| NFR-4 | 旧 accounts.json 兼容 | last_codex_smoke_at / last_smoke_result 字段 Optional,默认 None |
| NFR-5 | 前端 409 解析不影响其他 task error 处理 | 非 phone_required / register_blocked 字符串走默认 throw 分支 |
| NFR-6 | record_failure raw_rate_limit 字段长度上限 2000 字符 | 超长截断防止 register_failures.json 膨胀 |

---

## 7. 技术方案

### 7.1 代码改动总览

| 文件 | 行数估计 | 函数 / 改动 |
|---|---|---|
| `src/autoteam/runtime_config.py` | +3 | `set_preferred_seat_type` 接受 `chatgpt` 别名 → normalize 为 `default` |
| `web/src/components/MailProviderCard.vue` | +280(新) | 共享组件,4 步 wizard 状态机 |
| `web/src/components/SetupPage.vue` | -90 / +20 | 删除 inline testConnection/verifyDomain,改用 `<MailProviderCard>` |
| `web/src/components/Settings.vue` | -90 / +20 | 同上 |
| `tests/unit/test_plan_type_whitelist.py` | +120(新) | 从 test_spec2_lifecycle.py 抽出 plan_type case |
| `tests/unit/test_spec2_lifecycle.py` | -120 | 移除 plan_type case |
| `src/autoteam/api.py` | +30 / -8 | `app_lifespan` asynccontextmanager,删除 `@app.on_event` |
| `src/autoteam/codex_auth.py` | +35 | `_read_codex_smoke_cache` / `_write_codex_smoke_cache` + check_codex_quota 24h 去重接入 + raw_rate_limit 注入 quota_info |
| `src/autoteam/manager.py` | +10 | `record_failure(no_quota_assigned, raw_rate_limit=...)` 4 处接入(_run_post_register_oauth Team/personal + reinvite_account + sync_account_states) |
| `src/autoteam/manual_account.py` | +3 | record_failure(no_quota_assigned, raw_rate_limit=...) 接入 |
| `src/autoteam/register_failures.py` | +2 | docstring 加 `raw_rate_limit` 字段说明 |
| `web/src/api.js` | +20 | pollTask 解析 phone_required / register_blocked |
| `tests/unit/test_round7_followup.py` | +200(新) | 测试 P2.1 alias / P2.4 lifespan / D6 24h cache / D7 (若可) |
| `prompts/0426/spec/shared/account-state-machine.md` | +60(v1.0 → v1.1) | §3 / §4 / §6 加注 |
| `prompts/0426/spec/shared/quota-classification.md` | ~30(v1.3 → v1.4) | §4.4 24h 去重契约从 "Round 7 议题" 改 "已实施" + I9 |
| `prompts/0426/spec/spec-2-account-lifecycle.md` | ~40(v1.3 → v1.4) | §3.4.1 命名归一化 + §S-2.2 task["error"] 关键字契约 |
| `prompts/0426/spec/spec-1-mail-provider.md` | ~10(v1.0 → v1.1) | §1 文件清单显式说明 MailProviderCard.vue Round 7 抽出 |

总计:~700 行代码 / ~200 行测试 / ~140 行 SPEC 修订。

### 7.2 关键决策表

| 决策点 | 决策值 | 影响位置 |
|---|---|---|
| **D-1 命名归一化策略** | 改 SPEC,实施保留 default;setter 接受 chatgpt 别名转 default | spec-2 §3.4.1 + runtime_config.py |
| **D-2 MailProviderCard 抽组件 mode 区分** | 单组件 + `mode: 'setup' \| 'settings'` props,而非两个独立组件 | MailProviderCard.vue + 父组件传 mode |
| **D-3 24h 去重粒度** | account 维度(Round 6 Q-2 决策延续)— `workspace_account_id` 优先,无则 email | check_codex_quota 内部 + accounts.json |
| **D-4 24h cache 命中时 quota_info 标记** | 加 `smoke_cache_hit: True` 字段,与 `smoke_verified` 共存 | quota_info dict |
| **D-5 lifespan 迁移时序** | 一次性迁所有 on_event;不分阶段 | api.py |
| **D-6 raw_rate_limit 截断** | 2000 字符 hard cap,序列化失败时空串(不阻塞 record_failure) | record_failure 调用点 |
| **D-7 前端 409 解析方式** | 字符串子串匹配,不解析 JSON 结构 | api.js |
| **D-8 状态机 uninitialized_seat 定位** | 概念上是 STATUS_ACTIVE 的"待验证"子态,不新增 status 枚举,只在 last_quota.window=uninitialized_seat 标识 | account-state-machine.md §3 |

### 7.3 落地顺序(patch-implementer 阶段)

1. **S-1**(P2.1):runtime_config.py setter 接受 chatgpt alias + 单测
2. **S-2**(P2.5):codex_auth.py check_codex_quota / get_quota_exhausted_info 注入 raw_rate_limit + 4 处 record_failure 调用点
3. **S-3**(D6):codex_auth.py 24h 去重 cache + accounts.json 字段
4. **S-4**(P2.4):api.py lifespan 迁移
5. **S-5**(P2.2):MailProviderCard.vue 抽组件 + 双父组件改造
6. **S-6**(P2.3):测试文件拆分
7. **S-7**(D7):web/src/api.js pollTask 关键字解析
8. **S-8**(D8):account-state-machine.md v1.1 修订(本 PRD spec-writer 阶段)
9. **S-9**:全套 pytest + ruff + 前端 lint + e2e

**关键串行链**:S-2 → S-3(D6 依赖 raw_rate_limit 字段已落地)→ S-9
**可并行链**:S-1 / S-4 / S-5 / S-6 / S-7 / S-8 互不依赖,可并行

---

## 8. 验收标准

- [ ] `set_preferred_seat_type("chatgpt")` 返回 `"default"`
- [ ] `tests/unit/test_round7_followup.py::test_set_preferred_seat_type_accepts_chatgpt_alias` 通过
- [ ] `web/src/components/MailProviderCard.vue` 存在
- [ ] SetupPage.vue + Settings.vue 不再含 inline testConnection / verifyDomain
- [ ] `tests/unit/test_plan_type_whitelist.py` 存在,case 数与 test_spec2_lifecycle.py 抽出后一致
- [ ] `pytest -W error::DeprecationWarning` 0 个 on_event warning
- [ ] api.py 不含 `@app.on_event`,改为 `lifespan=app_lifespan`
- [ ] `record_failure(no_quota_assigned, raw_rate_limit=...)` 至少 4 处
- [ ] register_failures.json 单条 no_quota_assigned 记录含 raw_rate_limit JSON 字符串
- [ ] accounts.json 字段加 last_codex_smoke_at / last_smoke_result(Optional)
- [ ] 24h 内 check_codex_quota 命中 cache 不调网络
- [ ] api.js pollTask 解析 phone_required / register_blocked 关键字
- [ ] account-state-machine.md v1.1 §3 矩阵加 cheap_codex_smoke 转移
- [ ] quota-classification.md v1.4 §4.4 24h 去重已实施 + I9
- [ ] spec-2 v1.4 §3.4.1 命名归一化 + §S-2.2 task["error"] 关键字契约
- [ ] spec-1 v1.1 §1 显式说明 MailProviderCard.vue
- [ ] pytest 全套(原 178 + 新增 ~25)0 失败
- [ ] ruff 0 lint error
- [ ] 4 份 SPEC 文档同步修订(v1.1 / v1.4)

---

## 9. 测试计划

### 9.1 单元

| 测试模块 | 关键 case |
|---|---|
| `test_round7_followup.py` | `test_set_preferred_seat_type_accepts_chatgpt_alias`(P2.1) |
| `test_round7_followup.py` | `test_set_preferred_seat_type_default_unchanged`(P2.1 兼容) |
| `test_round7_followup.py` | `test_record_failure_no_quota_includes_raw_rate_limit`(P2.5) |
| `test_round7_followup.py` | `test_check_codex_quota_24h_cache_hit_no_network_call`(D6) |
| `test_round7_followup.py` | `test_check_codex_quota_24h_cache_miss_calls_smoke`(D6) |
| `test_round7_followup.py` | `test_lifespan_replaces_on_event`(P2.4 — assert no DeprecationWarning) |
| `test_plan_type_whitelist.py` | 从 test_spec2_lifecycle 抽出的所有 case(P2.3) |

### 9.2 集成

- `pytest -W error::DeprecationWarning` 全套通过(P2.4 验证)
- 模拟 24h 去重场景:同 account_id 在 23.9h 间隔内调 check_codex_quota,断言 cheap_codex_smoke 实际 mock 调用 1 次

### 9.3 前端

- 启动 dev server,Settings 页切换 mail provider,验证 MailProviderCard.vue 共享组件正常工作(P2.2)
- mock task error = "409: {'error': 'phone_required', ...}" → UI toast "该账号需要绑定手机才能完成 OAuth"(D7)

### 9.4 回归

- 跑全套 pytest(原 178 + 新增 ~25)
- ruff F401/F811/F821 0 lint error
- web 端 npm test / npm run build 通过

---

## 10. 灰度 / 回滚

### 10.1 灰度

- **P2.1 命名归一化**:setter 接受 chatgpt 别名是新行为,无破坏。SPEC 文档化转移期 ≥ 1 个 release;3 月后(2026-07)考虑移除别名(取决于用户反馈)
- **P2.2 抽组件**:vue 组件改造,无 SSR / 持久化影响。验证 Settings.vue 与 SetupPage.vue 双视图后 commit
- **P2.3 测试拆分**:纯文件级,case 内容不变
- **P2.4 lifespan**:破坏性较小;FastAPI lifespan 是 0.95+ 标准,仓库已隐式依赖 0.10x。**回滚:还原 on_event 即可**
- **P2.5 raw_rate_limit**:register_failures.json 字段扩展,旧消费方读 .get("raw_rate_limit") 返回 None,无破坏
- **D6 24h 去重**:accounts.json 加字段。旧版本忽略 last_codex_smoke_at,无破坏
- **D7 前端 409 解析**:api.js 追加 if 分支,默认走 throw error,不影响其他 task
- **D8 状态机文档**:纯 SPEC 修订

### 10.2 回滚

- 全部 P2 + Deferred 都可回滚到 commit `b5a697b`(Round 6 完结点)
- 无数据迁移
- 24h 去重 cache 字段 旧版本会忽略

---

## 11. 文档影响清单

- [x] PRD-6(本文档,v1.0)
- [x] `spec/shared/quota-classification.md` v1.3 → v1.4(§4.4 24h 去重已实施 + I9 不变量;附录 B 修订记录)
- [x] `spec/shared/account-state-machine.md` v1.0 → v1.1(§3 矩阵加 cheap_codex_smoke / uninitialized_seat / AUTH_INVALID 短路;§4 删除短路语义;§6 探针 7 处不变量;附录 A 修订记录)
- [x] `spec/spec-2-account-lifecycle.md` v1.3 → v1.4(§3.4.1 命名归一化;§S-2.2 task["error"] 关键字契约;§3.5 24h 去重接入说明;附录 A 修订记录)
- [x] `spec/spec-1-mail-provider.md` v1.0 → v1.1(§1 文件清单加 MailProviderCard.vue Round 7 显式说明;附录 修订记录)

---

## 12. 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | MailProviderCard.vue 抽组件后,Settings/Setup 双视图行为细微差异破坏现有用户配置流程 | 按 mode props 区分 + e2e 双视图测试 |
| R2 | lifespan 迁移破坏 startup 时序(某个 globals 初始化顺序依赖) | 单测 + 启动日志对比 |
| R3 | 24h 去重 cache 错读其他 account_id 的字段 | _read_codex_smoke_cache 双匹配 workspace_account_id + email |
| R4 | preferred_seat_type 别名期 chatgpt 用户配置不被识别 | setter normalize 在 set_value 之前;getter 永不返 chatgpt |
| R5 | raw_rate_limit JSON 序列化失败破坏 record_failure 主流程 | try/except 吞 + 空串 fallback |
| R6 | 前端 api.js 关键字匹配过于宽松误报 | 子串匹配限定 task.error 字段,不 match 其他 string |

---

## 13. 未决问题(spec-writer 阶段 1 idle 前提示给 team-lead)

- **无**(8 项均已在 §5 / §7.2 决策表给出明确实施路径)

---

## 14. Story Map

```
Phase 0(SPEC 修订,本 spec-writer 阶段)
├─ S-0.1 修 quota-classification.md v1.3 → v1.4(§4.4 + I9)
├─ S-0.2 修 account-state-machine.md v1.0 → v1.1(§3 + §4 + §6)
├─ S-0.3 修 spec-2-account-lifecycle.md v1.3 → v1.4(§3.4.1 + §S-2.2)
├─ S-0.4 修 spec-1-mail-provider.md v1.0 → v1.1(§1)
└─ S-0.5 写 PRD-6(本文件)

Phase 1(P2.1 / P2.5 / D6 — patch-implementer)
├─ S-1.1 runtime_config.py setter chatgpt alias
├─ S-1.2 codex_auth.py raw_rate_limit 注入 + 4 处接入
└─ S-1.3 codex_auth.py 24h 去重 cache + accounts.json 字段

Phase 2(P2.4 — patch-implementer)
└─ S-2.1 api.py lifespan 迁移

Phase 3(P2.2 — patch-implementer)
└─ S-3.1 MailProviderCard.vue + 双父组件改造

Phase 4(P2.3 / D7 — patch-implementer)
├─ S-4.1 测试文件拆分 test_plan_type_whitelist.py
└─ S-4.2 web/src/api.js 409 关键字解析

Phase 5(quality-reviewer 阶段)
├─ S-5.1 全套 pytest + ruff
├─ S-5.2 npm test / build
└─ S-5.3 e2e fill --target 1(若 user 授权)
```

**关键串行链**:S-0(SPEC 修订)→ S-1.2(raw_rate_limit)→ S-1.3(24h 去重)→ S-5.1
**可并行链**:S-1.1 / S-2 / S-3 / S-4 互不依赖,patch-implementer 可全程并行

---

**文档结束。** 总字数约 3300 字。
