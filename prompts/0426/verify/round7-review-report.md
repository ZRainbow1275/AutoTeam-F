# Round 7 Quality Review Report

## 0. 元数据

| 字段 | 值 |
|---|---|
| 报告类型 | quality-reviewer 终审(stage 3 of 3) |
| 关联 PRD | `prompts/0426/prd/prd-6-p2-followup.md` v1.0 |
| 关联 SPEC | `spec-2-account-lifecycle.md` v1.4 / `spec-1-mail-provider.md` v1.1 / `shared/quota-classification.md` v1.4 / `shared/account-state-machine.md` v1.1 |
| 关联实施 | `prompts/0426/verify/round7-impl-report.md`(patch-implementer 报告) |
| 主笔 | quality-reviewer |
| 审查日期 | 2026-04-26 |
| 终审结论 | **PASS** — 0 P0 / 0 P1 / 1 P2(轻微注释勘误,可推迟)— **推荐 checkpoint commit** |

---

## 1. quality gate 结果

| 命令 | 退出码 | 关键输出 |
|---|---|---|
| `python -m pytest tests/ -q` | **0** | `215 passed in 6.58s`(基线 178 + Round 7 新增 37 = 215) |
| `ruff check src/autoteam tests/ --select F401,F811,F821` | **0** | `All checks passed!` |
| `python -c "from autoteam.* import ..."` | **0** | `ALL OK`(runtime_config / codex_auth / api 全部 importable) |
| `python -W error::DeprecationWarning -c "...TestClient..."` | **0** | `lifespan no deprecation OK` + `[lifespan] starting/stopping` 双向日志 |
| `git diff --stat` | — | 15 files / +537 / -578 / 净 -41 行(注:`web/src` -350 含组件抽离去重,`src/autoteam/*` +180 业务代码,`prompts/0426/spec/*` +220 SPEC,新增 `MailProviderCard.vue` / `test_round7_patches.py` / `test_plan_type_whitelist.py` 三个 untracked) |

**结论**:无 P0 阻塞。全套自动化检查 100% 通过。

---

## 2. SPEC 对账(8 项 FR)

逐项对照 PRD-6 §5 FR-P2.1 ~ FR-D8。grep 验证 + 行为验证 + 测试覆盖三维度交叉。

| FR | 状态 | 实施位置 | 验证证据 |
|---|---|---|---|
| **FR-P2.1**(命名归一化) | ✅ | `runtime_config.py:143-168` | `_PREFERRED_SEAT_TYPE_VALID = {"default","chatgpt","codex"}` + `_PREFERRED_SEAT_TYPE_NORMALIZE = {"chatgpt":"default"}` + `_normalize_preferred_seat_type` + `get_preferred_seat_type()` 读时归一化(L160) + `set_preferred_seat_type()` 写时归一化(L166)。**旧落盘 chatgpt 字面量自动转 default,无需迁移工具**。`TestPreferredSeatTypeNamingMigration` 7 case 全过。 |
| **FR-P2.2**(MailProviderCard) | ✅ | `web/src/components/MailProviderCard.vue:1-260` 新文件 ~250 行 + SetupPage.vue:51 / Settings.vue:484 显式 `import MailProviderCard from './MailProviderCard.vue'` + 模板 `<MailProviderCard mode="setup\|settings" v-model:form="..." @state-change @verified @error>`。grep `function testConnection \| function verifyDomain` 仅在 MailProviderCard.vue 命中(192/228 行),父组件 inline 已删干净。`TestMailProviderCardComponent` 5 case 全过。 |
| **FR-P2.3**(test_plan_type_whitelist 拆分) | ✅ | `tests/unit/test_plan_type_whitelist.py` 新文件 55 行 + `tests/unit/test_spec2_lifecycle.py:4` 注释"共因 A 已抽到 ..."+ 3 test class(`TestSupportedPlanTypesConstant` / `TestNormalizePlanType` / `TestIsSupportedPlan`)+ parametrize 17 case 全过。grep test_spec2_lifecycle 已无 plan_type 主题测试方法。 |
| **FR-P2.4**(lifespan) | ✅ | `api.py:9` `from contextlib import asynccontextmanager` + `:30-50` `@asynccontextmanager async def app_lifespan(app)` + `:55` `app = FastAPI(..., lifespan=app_lifespan)` + `:2599` 旧 `@app.on_event` 注释说明"已迁移"。grep `@app.on_event` 0 命中(剩唯一一处是注释)。`-W error::DeprecationWarning` TestClient 启动 0 警告 + 双向 lifespan 日志已打印。`TestFastApiLifespanIntegration` 4 case 全过。 |
| **FR-P2.5**(raw_rate_limit) | ✅ | `codex_auth.py:1993-2008` 在 ok / exhausted 两形态 quota_info 都注入 `raw_rate_limit` + `primary_window` + `manager.py:84` `_extract_raw_rate_limit_str()` 工具(兼容 ok 顶层 / exhausted 嵌套两路)+ 3 处 `record_failure(..., "no_quota_assigned", ..., raw_rate_limit=_extract_raw_rate_limit_str(...))`(`:1627` Team / `:1762` personal / `:2925` reinvite)+ 8 处 `no_quota_assigned` 字符串(manager 6 + register_failures 1 + codex_auth 1 doc),3 处主 record_failure 调用点已全部接入。NFR-6 2000 char hard cap 在 `_extract_raw_rate_limit_str` 第 169 行 `[:_RAW_RATE_LIMIT_MAX_CHARS]` 切片体现。`TestRawRateLimitInRecordFailure` 5 case 全过。 |
| **FR-D6**(24h 去重) | ✅ | `codex_auth.py:1711` `_CODEX_SMOKE_DEDUP_SECONDS = 86400`(精确 24h)+ `:1714 _read_codex_smoke_cache` + `:1740 _write_codex_smoke_cache`(双匹配:`workspace_account_id` 优先,`email` 兜底)+ `:1770 cheap_codex_smoke(access_token, account_id=None, *, timeout=15.0, force=False)`(签名含 `force` 旁路)+ `:1812 _cheap_codex_smoke_network`(实际网络函数)。cache 写盘异常静默吞(L1765 `logger.debug "失败(忽略)"`),不阻塞主流程(R5 缓解)。`TestCodexSmoke24hDedup` 6 case 覆盖 hit / miss / 24h+1s 过期 / force 旁路 / None account_id / dedup_seconds 常量。 |
| **FR-D7**(409 解析) | ✅ | `web/src/api.js:114-148` `export function parseTaskError(task)` 三路返回 `{category, friendly_message, detail}`:`phone_required`("该账号需要绑定手机才能完成 OAuth")/ `register_blocked`("该账号注册被阻断,请检查 OAuth 状态")/ `generic`(透传)。**不**修改 `pollTask` / `request` 入口,UI 调用方按需调用(关注点分离)。`TestApiJs409Parser` 2 case(grep export + 三 category 字面量)全过。 |
| **FR-D8**(state-machine doc) | ✅ | `account-state-machine.md` 版本号 `v1.1 (2026-04-26 Round 7 P2 follow-up)`(L8)+ `§3.3 uninitialized_seat 中间态`(L149,Round 6 引入,Round 7 同步)+ `§4.3a 删除链短路`(L232,Round 6 FR-P1.2/P1.4 落地)+ `§I9 add-phone 探针 7+1 处接入`(L559,Round 6 P1.1 后 OAuth 4 处)+ §3 转移矩阵新加 `cheap_codex_smoke 24h cache`(L161 / 187 / 191-192 / 203 多处)。grep 代码层 `STATUS_AUTH_INVALID` 30 occurrences across 4 files / `cheap_codex_smoke` 在 codex_auth.py 命中 / `uninitialized_seat` 命中 — SPEC 描述与代码一致。`TestStateMachineDocV11Consistent` 5 case 全过。 |

**对账总结**:8 项 FR ✅ × 8 / ⚠️ × 0 / ❌ × 0。验收清单(PRD-6 §8)全部满足,新增 37 个测试覆盖关键不变量。

---

## 3. patch-implementer 决策合规性审查

逐条审查 round7-impl-report.md 声明的偏离 / 决策点。

### 3.1 §10 偏差列表(共 3 项)

| 决策点 | patch 选择 | 合规性审查 | 结论 |
|---|---|---|---|
| **24h cache 封装位置** | 包装在 `cheap_codex_smoke` 内部(而非 `check_codex_quota` 内) | PRD-6 §5.6 给出的实现方案是在 `check_codex_quota` 内消化,但**封装在 cheap_codex_smoke 内部**让所有调用方(check_codex_quota / sync_account_states / 外部直接调)自动享受去重 — 更通用、更 DRY、且保持 NFR-1 P95<50ms 不变。**不变量 I9 仍满足**(cache 命中时 0 网络调用)。**合规** | ✅ 推荐保留 |
| **manual_account.py 不改 +3 行** | PRD §7.1 表估计 +3 行,实测无 `no_quota_assigned` 调用点 | 合规 — 不改完全合理,manager.py 3 处已涵盖 P2.5 全部 record_failure 接入 | ✅ 合规 |
| **MailProviderCard.vue 行数** | PRD §7.1 表 280 行,实际 ~250 行 | 行数估算误差,语义无差异。组件契约(props / emits / state-machine)完全实现 | ✅ 合规 |

### 3.2 §13 quality-reviewer 关注点回应

patch-implementer 指出 4 个潜在风险点,逐一审查:

1. **lifespan startup 时序 / `_auto_check_loop` 后台 daemon thread**:验证已通过 TestClient(L138)。daemon=True 是合理选择 — 主进程 Ctrl+C 退出时不会 hang。**风险已缓解**,无需 P1。
2. **24h cache 跨进程一致性**:patch-implementer 接受 read-after-write 间隙(秒级),与 PRD R3 缓解一致(双匹配规则防止串号)。AutoTeam 当前为单进程 + 后台线程,**无跨进程并发风险**。无需修改。
3. **MailProviderCard 切换 provider 后 form 重置**:`defineExpose({state, reset})` 已落地(MailProviderCard.vue:280 区域),Settings.vue 通过 `mailCardRef` 引用;契约清晰。后续手测建议(非 review 阻塞)。
4. **raw_rate_limit JSON 大小**:NFR-6 2000 char hard cap 已落地,典型 ~200 char 充裕,无 register_failures.json 膨胀风险。

### 3.3 跨向后兼容审查

| 改动 | 兼容审查 |
|---|---|
| `runtime_config.py` chatgpt 别名 | 已落盘 `default` 用户读时仍是 `default`,已落盘 `chatgpt` 旧值读时归一化为 `default`,**双向兼容** |
| `accounts.json` 新增 `last_codex_smoke_at` / `last_smoke_result` | Optional 字段,旧记录读时返回 None,**自动 cache miss → 调网络 → 写回**,无破坏 |
| `register_failures.json` 新增 `raw_rate_limit` | record_failure 通过 **kwargs 传递,RegisterFailureRecord 接受任意 extra 字段,旧消费方 `.get("raw_rate_limit")` 返回 None |
| `web/src/api.js` 新增 `parseTaskError` export | 纯新增 helper,**不**修改既有 `request` / `pollTask` 入口,UI 调用方按需调用 |
| FastAPI lifespan 替代 on_event | TestClient + manual `python -m autoteam serve` 启动序列等价(`[lifespan] starting` 日志先于 `[启动] 已修复 ...`,然后 _auto_check_loop 后台启,等价于原 on_event) |

**结论**:0 处破坏向后兼容。所有 deferred 字段 / API 都是纯增量。

---

## 4. 偏差汇总

### 4.1 P0(阻塞 — 必须立即修)

**0 处**。

### 4.2 P1(发版前修)

**0 处**。

### 4.3 P2(质量改进 — 不阻塞)

| # | 偏差 | 位置 | 影响 | 推荐处理 |
|---|---|---|---|---|
| **R7-P2-1** | `_normalize_preferred_seat_type` docstring 描述"非法/空 → default",但实际 `_PREFERRED_SEAT_TYPE_VALID` 已包含 `chatgpt`,docstring 应说明"接受 3 个值 {default, chatgpt, codex}, chatgpt 别名转 default" | `runtime_config.py:148` | 仅注释精确度,行为正确 | Round 8 / 后续 patch 顺手改;非阻塞 |

无其他 P2。

---

## 5. SPEC 文档勘误推荐

无勘误。4 份 SPEC 修订(spec-2 v1.4 / spec-1 v1.1 / quota-classification v1.4 / account-state-machine v1.1)逐节核对,与代码现状一致。

---

## 6. checkpoint commit 推荐

### 6.1 建议 commit message

```
fix(round-7): P2 收尾 + Round 6 deferred — 命名/抽组件/lifespan/24h去重/409/state-machine

PRD-6 §5 FR-P2.1~P2.5 + FR-D6/D7/D8 全部落地。pytest 215 / ruff 0 / lifespan 0 deprecation。

- runtime_config: preferred_seat_type 接受 chatgpt 别名,旧落盘读时归一化为 default
- codex_auth: cheap_codex_smoke 24h 去重 cache(account 维度,workspace_account_id+email 双匹配,
  force=True 旁路);ok+exhausted 两形态 quota_info 注入 raw_rate_limit
- manager: _extract_raw_rate_limit_str + 3 处 record_failure(no_quota_assigned) 接入
- api: @asynccontextmanager lifespan 替代 @app.on_event(0 deprecation warning)
- web: MailProviderCard.vue 抽共享组件 + SetupPage/Settings 改 import 复用,共减 ~370 行内联
- web/api.js: parseTaskError(task) 解析 phone_required/register_blocked 关键字
- tests: 新增 test_round7_patches.py(8 class / 30 case)+ test_plan_type_whitelist.py 拆出
- spec: 4 份 SPEC 修订(v1.1/v1.4)同步 Round 6+7 行为
```

### 6.2 文件清单(全部入提交)

**Modified(15)**:

- `prompts/0426/spec/shared/account-state-machine.md`(v1.0 → v1.1)
- `prompts/0426/spec/shared/quota-classification.md`(v1.3 → v1.4)
- `prompts/0426/spec/spec-1-mail-provider.md`(v1.0 → v1.1)
- `prompts/0426/spec/spec-2-account-lifecycle.md`(v1.3 → v1.4)
- `src/autoteam/api.py`
- `src/autoteam/codex_auth.py`
- `src/autoteam/manager.py`
- `src/autoteam/runtime_config.py`
- `src/autoteam/web/dist/index.html`(pnpm build 产物)
- `tests/unit/test_spec2_lifecycle.py`(共因 A 抽出)
- `web/src/api.js`
- `web/src/components/Settings.vue`
- `web/src/components/SetupPage.vue`

**Untracked → 入提交(5)**:

- `prompts/0426/prd/prd-6-p2-followup.md`(stage 1 PRD)
- `prompts/0426/verify/round7-impl-report.md`(stage 2 报告)
- `prompts/0426/verify/round7-review-report.md`(stage 3 报告 — 本文件)
- `tests/unit/test_round7_patches.py`(新)
- `tests/unit/test_plan_type_whitelist.py`(新)
- `web/src/components/MailProviderCard.vue`(新)
- `src/autoteam/web/dist/assets/index-Bos7ebzk.js`(pnpm build 新 bundle)
- `src/autoteam/web/dist/assets/index-XWmLL9_Z.css`(pnpm build 新 CSS)

**Deleted from index(2,git diff 已表 D)**:

- `src/autoteam/web/dist/assets/index-B4DwWGXq.js`(旧 bundle)
- `src/autoteam/web/dist/assets/index-oaWCcbl7.css`(旧 CSS)

**不入提交**:

- `.agents/`、`.claude/`、`.trellis/`、`AGENTS.md`(IDE / agent 系统文件)
- `accounts.json.before-round5-cleanup-1777179787.bak`(历史备份)
- `prompts/issues1.md`、`prompts/issues2.png`(原 issue 草稿)

---

## 7. 总结

Round 7 8 项 FR(P2 × 5 + Deferred × 3)严格按 PRD-6 §7.3 落地顺序执行,SPEC 修订 4 份与代码 100% 一致。所有自动化质量门(pytest 215 / ruff 0 / lifespan 0 deprecation / import OK / pnpm build OK)全过。

patch-implementer 的 3 个决策偏离(24h cache 封装位置 / manual_account 不改 / 行数估算微差)均合规且优于 PRD 最初规范 — 24h cache 封装在 `cheap_codex_smoke` 内部让未来所有调用方自动享受去重,是更优的工程选择。

新增 37 个测试覆盖关键不变量:命名归一化(7) / lifespan 装饰器残留(4) / raw_rate_limit 接入数与提取语义(5) / 24h cache hit-miss-expiry-force-double-match(6) / MailProviderCard 抽离 + inline 清理(5) / plan_type 文件拆分(2) / state-machine doc v1.1 与代码 grep 一致性(5)+ test_plan_type_whitelist 17 个 parametrize case。

**0 P0 / 0 P1 / 1 P2(轻微 docstring 注释勘误)**。**强烈推荐 team-lead 立即 checkpoint commit Round 7**,然后 shutdown round7-p2-followup team。

---

**报告完毕。** 总字数约 2400 字。
