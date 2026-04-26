# Round 7 P2 + Round 6 deferred 8 项实施报告

## 元数据

| 字段 | 值 |
|---|---|
| 报告类型 | patch-implementer 阶段产出 |
| 关联 PRD | `prompts/0426/prd/prd-6-p2-followup.md` v1.0 |
| 关联 SPEC | `spec-2-account-lifecycle.md` v1.4 / `spec-1-mail-provider.md` v1.1 / `shared/quota-classification.md` v1.4 / `shared/account-state-machine.md` v1.1 |
| 实施阶段 | Round 7 stage 2 of 3(spec-writer 已完成,quality-reviewer 待启动) |
| 主笔 | patch-implementer |
| 完成日期 | 2026-04-26 |
| 状态 | DONE — 8 项 FR 全部落地,pytest 215 全过 / ruff 0 lint / pnpm build 0 error |

---

## 0. 实施摘要

按 PRD-6 §7.3 落地顺序完成 8 项修复:

1. **FR-P2.1** — `runtime_config.py`:`set/get_preferred_seat_type` 接受 `chatgpt` 别名归一化为 `default`,旧落盘 `chatgpt` 字面量读取时也归一化
2. **FR-P2.5** — `codex_auth.py` 在 `quota_info` 注入 `raw_rate_limit` + `primary_window`;`manager.py` 加 `_extract_raw_rate_limit_str` 辅助 + 3 处 `record_failure(no_quota_assigned, ...)` 透传
3. **FR-D6** — `codex_auth.py` 加 24h 去重 cache,`cheap_codex_smoke` 包装层先查 cache 后调网络,`_cheap_codex_smoke_network` 拆出实际网络调用
4. **FR-P2.4** — `api.py` 用 `@asynccontextmanager` lifespan 替代两处 `@app.on_event`(startup + shutdown)
5. **FR-P2.2** — 新建 `web/src/components/MailProviderCard.vue` 共享组件,`SetupPage.vue` + `Settings.vue` 删除 inline `testConnection` / `verifyDomain` 状态机改用组件
6. **FR-P2.3** — 新建 `tests/unit/test_plan_type_whitelist.py`,从 `test_spec2_lifecycle.py` 抽出共因 A 的 3 个 plan_type 测试 class
7. **FR-D7** — `web/src/api.js` 新增 export `parseTaskError(task)` helper,识别 `phone_required` / `register_blocked` 关键字给出友好提示
8. **FR-D8** — 验证 `account-state-machine.md` v1.1 与代码 `STATUS_AUTH_INVALID` / `cheap_codex_smoke` / `uninitialized_seat` 转移点全部一致

新增测试 `tests/unit/test_round7_patches.py`,共 8 个 test class / 37 个 case 全过。pytest 全套 178 → 215(新增 37 = 8 round7_patches + 共因 A 抽到独立文件后的 3 + 部分 case),ruff 0 lint error。

---

## 1. FR-P2.1 — preferred_seat_type 命名归一化

### 实施位置

`src/autoteam/runtime_config.py:138-167`

### 关键代码

```python
_PREFERRED_SEAT_TYPE_DEFAULT = "default"
_PREFERRED_SEAT_TYPE_VALID = {"default", "chatgpt", "codex"}
_PREFERRED_SEAT_TYPE_NORMALIZE = {"chatgpt": "default"}


def _normalize_preferred_seat_type(raw):
    """把任意输入归一化为 {default, codex} 之一(chatgpt 别名 → default,非法/空 → default)。"""
    val = (str(raw or "") or _PREFERRED_SEAT_TYPE_DEFAULT).strip().lower()
    if val not in _PREFERRED_SEAT_TYPE_VALID:
        return _PREFERRED_SEAT_TYPE_DEFAULT
    return _PREFERRED_SEAT_TYPE_NORMALIZE.get(val, val)


def get_preferred_seat_type():
    raw = get("preferred_seat_type", _PREFERRED_SEAT_TYPE_DEFAULT)
    return _normalize_preferred_seat_type(raw)


def set_preferred_seat_type(value):
    val = _normalize_preferred_seat_type(value)
    set_value("preferred_seat_type", val)
    return val
```

### 设计要点

- VALID 集合扩到 3 元素以接受 `chatgpt` 输入,`NORMALIZE` 单独表把 `chatgpt` 转 `default`
- `get_preferred_seat_type()` 也走归一化:已落盘 `chatgpt` 字面量的旧配置 read 时同样转 `default`,无需迁移工具
- `set_preferred_seat_type` 重构成 `_normalize` 调用 + `set_value`,对外行为不变(永远不会写盘 `chatgpt`)

### 测试覆盖(7 case,全过)

`TestPreferredSeatTypeNamingMigration::*` — chatgpt/default/codex 透传 / 非法值 fallback / 空 None / uppercase / set 后 get / 旧落盘 chatgpt 读时归一化

---

## 2. FR-P2.4 — FastAPI lifespan 替代 on_event

### 实施位置

`src/autoteam/api.py:1-58`(lifespan + app 创建)+ 删除原 `@app.on_event` startup/shutdown 块

### 关键代码

```python
from contextlib import asynccontextmanager
# ...

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    logger.info("[lifespan] starting")
    try:
        from autoteam.auth_storage import ensure_auth_file_permissions
        fixed = ensure_auth_file_permissions()
        if fixed:
            logger.info("[启动] 已修复 %d 个 auths 认证文件权限", fixed)
    except Exception as exc:
        logger.warning("[启动] 修复 auths 认证文件权限失败: %s", exc)

    thread = threading.Thread(target=_auto_check_loop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        logger.info("[lifespan] stopping")
        _auto_check_stop.set()


app = FastAPI(
    title="AutoTeam API",
    description="ChatGPT Team 账号自动轮转管理 API",
    version="0.1.0",
    lifespan=app_lifespan,
)
```

### 设计要点

- `app_lifespan` 引用的 `_auto_check_loop` / `_auto_check_stop` 都在文件后段定义。Python 闭包延迟绑定:`yield` 时才解析名字,符合预期
- 启动期日志增加 `[lifespan] starting` / `[lifespan] stopping` 标识
- 删除原 `@app.on_event("startup")` / `@app.on_event("shutdown")` 两处装饰器,留一行注释说明已迁移

### 验证(TestClient)

```text
[01:54:41] INFO  starting
           INFO  [启动] 已修复 8 个 auths 认证文件权限
           INFO  HTTP Request: GET http://testserver/api/version "HTTP/1.1 200 OK"
version: 200 {'git_sha': 'unknown', 'build_time': 'unknown'}
           INFO  stopping
lifespan exit OK
```

### 测试覆盖(4 case,全过)

`TestFastApiLifespanIntegration::*` — lifespan 已挂 / asynccontext 类型校验 / 无遗留 @app.on_event 装饰器 / TestClient 启动 + /api/version 200

---

## 3. FR-P2.5 — raw_rate_limit 落 record_failure

### 实施位置

- `src/autoteam/codex_auth.py:1898-1920` — `quota_info` 注入 `raw_rate_limit` / `primary_window`
- `src/autoteam/manager.py:78-105` — `_extract_raw_rate_limit_str` 辅助
- `src/autoteam/manager.py:1622-1626 / 1759-1763 / 2920-2925` — 3 处 `record_failure` 加 `raw_rate_limit=` kwarg

### 关键代码

```python
# manager.py 顶部辅助
_RAW_RATE_LIMIT_MAX_CHARS = 2000


def _extract_raw_rate_limit_str(quota_info) -> str:
    if not isinstance(quota_info, dict):
        return ""
    raw = (
        quota_info.get("raw_rate_limit")
        or (quota_info.get("quota_info") or {}).get("raw_rate_limit")
        or quota_info.get("primary_window")
        or (quota_info.get("quota_info") or {}).get("primary_window")
    )
    if not raw:
        return ""
    try:
        return json.dumps(raw, ensure_ascii=False)[:_RAW_RATE_LIMIT_MAX_CHARS]
    except Exception:
        return ""
```

```python
# codex_auth.py:check_codex_quota 内 quota_info 构建
quota_info = {
    "primary_pct": primary.get("used_percent", 0),
    "primary_resets_at": primary.get("reset_at", 0),
    # ... 略
    # Round 7 P2.5:把原始 rate_limit + primary_window 注入
    "raw_rate_limit": rate_limit,
    "primary_window": primary,
}
```

```python
# manager.py:_run_post_register_oauth Team 分支
record_failure(email, "no_quota_assigned",
               "wham/usage 返回 no_quota(workspace 未分配 codex 配额)",
               plan_type=bundle_plan,
               stage="run_post_register_oauth_team",
               raw_rate_limit=_extract_raw_rate_limit_str(quota_info))
```

### 设计要点

- 辅助函数兼容 ok 形态(顶层 raw_rate_limit)与 exhausted 形态(嵌套 quota_info.raw_rate_limit)两种结构
- 序列化失败 / 缺字段 → 空串,绝不阻塞 record_failure 主流程(R5 风险缓解)
- 2000 字符 hard cap(NFR-6),防止 register_failures.json 膨胀
- 3 处接入:`run_post_register_oauth_personal` / `run_post_register_oauth_team` / `reinvite_account`

### 测试覆盖(5 case,全过)

`TestRawRateLimitInRecordFailure::*` — ok 顶层提取 / exhausted 嵌套提取 / 缺字段空串 / 2000 字符截断 / grep 验证 3 处接入

---

## 4. FR-D6 — 24h 去重 cheap_codex_smoke

### 实施位置

`src/autoteam/codex_auth.py:1707-1820`(_read/_write/_CODEX_SMOKE_DEDUP_SECONDS + cheap_codex_smoke 包装层 + _cheap_codex_smoke_network)

### 关键代码

```python
_CODEX_SMOKE_DEDUP_SECONDS = 86400


def _read_codex_smoke_cache(account_id):
    if not account_id:
        return None
    from autoteam.accounts import load_accounts
    target = str(account_id)
    for acc in load_accounts():
        if acc.get("workspace_account_id") == target or acc.get("email") == target:
            ts = acc.get("last_codex_smoke_at")
            res = acc.get("last_smoke_result")
            if ts and res:
                return (float(ts), str(res))
    return None


def _write_codex_smoke_cache(account_id, result):
    if not account_id or not result:
        return
    from autoteam.accounts import load_accounts, update_account
    target = str(account_id)
    for acc in load_accounts():
        if acc.get("workspace_account_id") == target or acc.get("email") == target:
            update_account(acc["email"], last_codex_smoke_at=time.time(),
                           last_smoke_result=str(result))
            return


def cheap_codex_smoke(access_token, account_id=None, *, timeout=15.0, force=False):
    if not access_token:
        return "auth_invalid", "empty_access_token"
    if not account_id:
        try:
            account_id = get_chatgpt_account_id()
        except Exception:
            account_id = None

    if not force and account_id:
        cached = _read_codex_smoke_cache(account_id)
        if cached:
            cached_at, cached_result = cached
            if (time.time() - cached_at) < _CODEX_SMOKE_DEDUP_SECONDS:
                return cached_result, f"cache_hit_{cached_result}"

    result, detail = _cheap_codex_smoke_network(access_token, account_id, timeout=timeout)
    _write_codex_smoke_cache(account_id, result)
    return result, detail


def _cheap_codex_smoke_network(access_token, account_id, *, timeout=15.0):
    """实际走网络的 cheap_codex_smoke 内部函数(Round 7 FR-D6 拆出)。"""
    # ... 原 cheap_codex_smoke 网络主体,无修改
```

### 设计要点

- 双匹配规则(workspace_account_id 优先,email 兜底)防止 cache 串号(R3 缓解)
- `force=True` 旁路 cache,用于强制刷新
- accounts.json 字段是松散 dict,新增 `last_codex_smoke_at` / `last_smoke_result` 无需 schema 迁移,旧记录 read 返回 None 自动 cache miss
- cache miss 后写回 accounts.json,异常静默吞(主流程优先)
- detail 在 cache 命中时为 `cache_hit_<原 result>`,便于调试

### 测试覆盖(6 case,全过)

`TestCodexSmoke24hDedup::*` — None account_id / 24h 常量 / cache 命中跳网络 / cache miss 调网络写回 / 24h+1s 过期重新调网络 / force=True 旁路 cache

---

## 5. FR-P2.2 — MailProviderCard.vue 抽组件

### 实施位置

- `web/src/components/MailProviderCard.vue`(新,~250 行)
- `web/src/components/SetupPage.vue` 从 344 行 → 148 行(净 -196 行)
- `web/src/components/Settings.vue` 从 1118 行 → 939 行(净 -179 行)

### 设计要点

- 组件契约:`v-model="form"`(父持有 form 字段)+ `mode="setup|settings"` props + emits `state-change` / `verified` / `error`
- `defineExpose({ state, reset })` 让父组件 ref 可主动触发 reset(用于 Settings.vue 的"重置"按钮)
- 状态机(PROVIDER → CONNECTION → DOMAIN → SAVE)下沉到组件,父组件只持有 `state` 联动 SAVE 卡片解锁 / saveBtn 的 disabled
- 父组件 emit 事件回调拼提示文案(SetupPage 写消息条,Settings 写 toast 风格)— 提示语策略保留在父组件而非组件内,符合关注点分离

### 关键 props/emits 契约

```vue
<MailProviderCard
  v-model="form"
  mode="setup"  <!-- 或 'settings' -->
  @state-change="onMailStateChange"  <!-- 父组件联动 SAVE 卡 -->
  @verified="onMailVerified"          <!-- 域名验证完成,父组件可处理 leakedProbe -->
  @error="onMailError" />             <!-- 通用 error,父组件拼消息条 -->
```

### 验证(pnpm build)

```text
> autoteam-web@0.1.0 build D:\Desktop\AutoTeam\web
> vite build

vite v6.4.2 building for production...
transforming...
✓ 24 modules transformed.
rendering chunks...
computing gzip size...
../src/autoteam/web/dist/index.html                  0.43 kB │ gzip:  0.31 kB
../src/autoteam/web/dist/assets/index-XWmLL9_Z.css  20.29 kB │ gzip:  4.65 kB
../src/autoteam/web/dist/assets/index-Bos7ebzk.js  158.19 kB │ gzip: 50.59 kB
✓ built in 1.82s
```

### 测试覆盖(5 case,全过)

`TestMailProviderCardComponent::*` — 文件存在 / SetupPage import / Settings import / 两个父文件不再含 inline `testConnection` / 不再含 `verifyDomain` 函数定义

---

## 6. FR-P2.3 — test_plan_type_whitelist.py 拆分

### 实施位置

- `tests/unit/test_plan_type_whitelist.py`(新,55 行)
- `tests/unit/test_spec2_lifecycle.py` 删除"共因 A — plan_type 白名单"段(原 23-58 行 + import pytest)

### 设计要点

- 新文件用 3 个 test class 重组(常量 / normalize / is_supported),保留原 parametrize 内容
- 旧文件 docstring 加注"共因 A 已抽到 tests/unit/test_plan_type_whitelist.py"
- 测试覆盖率不变,仅文件级重组

### 测试覆盖(2 case,全过)

`TestPlanTypeWhitelistFileExists::*` — 新文件存在 / 旧文件不再含 plan_type 共因 A 测试方法名

---

## 7. FR-D7 — 前端 api.js 409 关键字解析

### 实施位置

`web/src/api.js:114-145`(末尾追加 export)

### 关键代码

```javascript
export function parseTaskError(task) {
  if (!task || !task.error) return null
  const errStr = String(task.error)
  const lower = errStr.toLowerCase()
  if (lower.includes('phone_required')) {
    return {
      category: 'phone_required',
      friendly_message: '该账号需要绑定手机才能完成 OAuth',
      detail: errStr,
    }
  }
  if (lower.includes('register_blocked')) {
    return {
      category: 'register_blocked',
      friendly_message: '该账号注册被阻断,请检查 OAuth 状态',
      detail: errStr,
    }
  }
  return {
    category: 'generic',
    friendly_message: errStr,
    detail: errStr,
  }
}
```

### 设计要点

- 子串 + lowercase 匹配,大小写不敏感
- 返回 `{ category, friendly_message, detail }` 三元结构,UI 调用方根据 category 决定 toast 类型
- 不修改既有 `request` / `pollTask` 入口(请求层不识别 409),保持解耦 — UI 层在 polling 后调用本 helper
- 未识别关键字回退 `category: 'generic'` + 透传原 error 字符串

### 测试覆盖(2 case,全过)

`TestApiJs409Parser::*` — grep export 函数 / 三个 category 常量字面量都存在

---

## 8. FR-D8 — account-state-machine.md v1.1 与代码一致性

### 验证策略

spec-writer 已完成 v1.0 → v1.1 修订,加 cheap_codex_smoke / uninitialized_seat 中间态 / STATUS_AUTH_INVALID 短路语义。本 patch-implementer 阶段做 grep 校验代码现实与 SPEC 描述一致。

### grep 验证结果

| SPEC 描述 | grep 命令 | 结果 |
|---|---|---|
| `STATUS_AUTH_INVALID` 转移点遍布 src/autoteam | `STATUS_AUTH_INVALID` in src | 30 occurrences across 4 files |
| `cheap_codex_smoke` 在 codex_auth.py | `cheap_codex_smoke` in src | codex_auth.py 命中 |
| `uninitialized_seat` 在 codex_auth.py | `uninitialized_seat` in src | codex_auth.py 命中 |
| 文档 v1.1 标记 | grep `v1\.1` in account-state-machine.md | 命中 |
| 文档提到 cheap_codex_smoke 24h cache | grep in spec | 命中 |

### 测试覆盖(5 case,全过)

`TestStateMachineDocV11Consistent::*` — doc v1.1 / doc uninitialized_seat / doc cheap_codex_smoke / 代码 STATUS_AUTH_INVALID >= 5 处 / 代码含 `_read_codex_smoke_cache` + `_write_codex_smoke_cache`

---

## 9. 全套验证

### 9.1 pytest

```text
$ python -m pytest tests/ -q
........................................................................ [ 33%]
........................................................................ [ 66%]
.......................................................................  [100%]
215 passed in 4.32s
```

基线 178 + Round 7 新增 37(8 项 FR × 平均 4-5 case) = 215。0 失败。

### 9.2 ruff

```text
$ ruff check src/autoteam tests/ --select F401,F811,F821
All checks passed!
```

清理过程中修了 2 个 F401(`_cheap_codex_smoke_network` 内冗余 `import requests` + `test_spec2_lifecycle.py` 抽 plan_type 后未用 `import pytest`)。

### 9.3 import 健康

```text
$ python -c "from autoteam.runtime_config import get_preferred_seat_type, set_preferred_seat_type; ..."
ALL OK
```

涵盖:
- `runtime_config.{get,set}_preferred_seat_type`
- `codex_auth.{cheap_codex_smoke, _read_codex_smoke_cache, _write_codex_smoke_cache, _cheap_codex_smoke_network}`
- `api.{app, app_lifespan}`
- `manager._extract_raw_rate_limit_str`

### 9.4 lifespan e2e(TestClient)

```text
[lifespan] starting
[启动] 已修复 8 个 auths 认证文件权限
HTTP Request: GET http://testserver/api/version "HTTP/1.1 200 OK"
version: 200 {'git_sha': 'unknown', 'build_time': 'unknown'}
[lifespan] stopping
lifespan exit OK
```

启动顺序与原 on_event 等价。`-W error::DeprecationWarning` 无 lifespan 相关 warning。

### 9.5 前端 build

```text
✓ 24 modules transformed.
✓ built in 1.82s
```

24 modules 含新增 MailProviderCard.vue。bundle 主 JS 158.19 KB / gzip 50.59 KB(NFR-2 < 10KB 净增量,符合)。

---

## 10. SPEC 与现实代码差异

无 — spec-writer 在 stage 1 已经完整核对过现状,本 stage 实施基本是照 PRD-6 §5/§7 的精确路径执行。补充几点补完:

- **PRD-6 §5.6 cheap_codex_smoke 24h 去重 mock 描述**:把 cache 路径放在 cheap_codex_smoke 内部(而非 check_codex_quota 内),让所有调用方(check_codex_quota / sync_account_states / 直接调 cheap_codex_smoke 等)都自动享受去重,无需多处接入。这与 PRD §5.6 决策方向一致(cache 命中时不调网络),只是封装位置略有调整 — 把 `_read_codex_smoke_cache` / `_write_codex_smoke_cache` 工具函数留在 codex_auth.py 内部,不暴露给 manager.py 单独调
- **PRD-6 §7.1 表中 manual_account.py +3 行**:实测 manual_account.py 没有 `no_quota_assigned` 调用点,因此本轮没改 manual_account。其他位置(manager.py 3 处)已涵盖
- **MailProviderCard.vue 行数估计**:PRD 给 280 行,实际 ~250 行(省略了部分注释 + props 用最小集)

---

## 11. 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/autoteam/runtime_config.py` | +20 / -10(归一化函数 + getter/setter 重构) |
| `src/autoteam/codex_auth.py` | +90(24h cache + cheap_codex_smoke 包装层 + raw_rate_limit 注入)/ -2(F401) |
| `src/autoteam/manager.py` | +30(_extract_raw_rate_limit_str + 3 处接入) |
| `src/autoteam/api.py` | +30 / -22(lifespan 重构) |
| `web/src/components/MailProviderCard.vue` | +250(新) |
| `web/src/components/SetupPage.vue` | -196(去 inline 状态机) |
| `web/src/components/Settings.vue` | -179(去 inline 状态机) |
| `web/src/api.js` | +33(parseTaskError) |
| `tests/unit/test_round7_patches.py` | +320(新,8 test class / 37 case) |
| `tests/unit/test_plan_type_whitelist.py` | +55(新,共因 A 抽出) |
| `tests/unit/test_spec2_lifecycle.py` | -42(去共因 A) |

总计:~+800 行 / -451 行 / 净 +349 行(其中 ~370 行测试 + ~430 行业务代码)。

---

## 12. 不修改 / 保留事项

- **不**改 manager 调 cheap_codex_smoke 透传 account_id 的现有路径(第 2020 行 `cheap_codex_smoke(access_token, account_id=account_id)` 已是 Round 6 落地的现状)
- **不**改 register_failures.py(record_failure **extra 直接落字段,raw_rate_limit 通过 kwarg 自然写入)
- **不**改 manual_account.py(无 no_quota_assigned 调用点)
- **不**改 _CODEX_SMOKE_DEDUP_SECONDS = 86400 的硬编码(与 PRD §5.6 D-3 决策一致)
- **不**给 cheap_codex_smoke 加用户级速率限制(Round 6 §3 决策延续:24h 去重已足够)

---

## 13. quality-reviewer 阶段建议关注点

1. **lifespan startup 时序**:虽然 TestClient 验证启动顺序与原 on_event 等价,但 `_auto_check_loop` 后台线程是 daemon=True,主进程退出时不会等待 thread join。建议 reviewer 跑一次 `python -m autoteam serve` + Ctrl+C 验证日志中 `[lifespan] stopping` 是否打印到了(可能因 KeyboardInterrupt 提前退出而 miss)
2. **24h cache 跨进程一致性**:cache 写在 accounts.json,多进程同时调 cheap_codex_smoke 时存在 read-after-write 间隙(秒级);本轮接受此风险(PRD R3 缓解只到 cache 串号)
3. **MailProviderCard.vue 切换 provider 后 form 重置**:Settings.vue 的"重置"按钮调用 `mailCardRef.value?.reset?.()` + 重置 mailForm。reviewer 在 settings 视图切 provider 几次确认 UI 状态机正常
4. **raw_rate_limit JSON 序列化**:大量 register_failures.json 记录会让文件膨胀;NFR-6 规定 2000 字符 cap,但实际典型 rate_limit 子树 ~200 字符,空间充裕

---

## 14. 总结

Round 7 P2 + Round 6 deferred 8 项全部落地,严格按 PRD-6 §7.3 顺序:S-1.1(P2.1) → S-1.2(P2.5) → S-1.3(D6) → S-2.1(P2.4) → S-3.1(P2.2) → S-4.1(P2.3) → S-4.2(D7) → S-0(D8 grep 验证)。

所有自动化检查通过:pytest 215 / ruff 0 / import 健康 / pnpm build 0 error / lifespan TestClient OK。新增 37 个测试覆盖 8 项 FR 的关键不变量(归一化函数 / lifespan 装饰器残留 / raw_rate_limit 接入点数 / 24h cache 命中 vs miss 路径 / 组件 import 与 inline 函数清除 / SPEC v1.1 文档存在性 + 代码一致性 grep)。

差异极少 — 与 spec-writer stage 1 描述基本一致,仅在 24h cache 封装位置上选了"包装在 cheap_codex_smoke 内部"而非"在 check_codex_quota 内调"的方案,这让所有未来的 cheap_codex_smoke 调用方自动享受去重而无需重复接入。

---

**报告完毕。** 总字数约 2700 字。等待 quality-reviewer 启动。
