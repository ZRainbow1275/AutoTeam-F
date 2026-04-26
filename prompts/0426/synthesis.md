# 调研综合分析(Synthesis)

> 时间:2026-04-26
> 输入:`prompts/0426/research/issue-{1..6}-*.md` 共 6 份调研报告
> 输出:跨 issue 共因识别、PRD 切分方案、依赖图、优先级

---

## 1. 跨 Issue 共因(实施时必须统一)

| 共因点 | 涉及 Issue | 文件:行号 | 一句话 |
|---|---|---|---|
| **A. plan_type 白名单** | #2 + #6 | `codex_auth.py:111`,`manager.py:1463/2489`,`manual_account.py:233` | `self_serve_business_usage_based` 等新值无识别,通通走 codex 但实际无配额 |
| **B. wham/usage 识别 quota=0** | #2 + #6 | `codex_auth.py:1582-1700` | `used_percent=0 + total=0` 当 ok 处理,UI 显示"100% 剩余"实际 429 |
| **C. login_codex_via_browser 接 add-phone 探针** | #4 + #6 | `codex_auth.py:250-932` | invite.py 的 `detect_phone_verification` 没复用,撞 add-phone 静默 30s 超时 |
| **D. _run_post_register_oauth 加 quota probe** | #6 | `manager.py:1463-1486` | 注册收尾不验配额,假入池 |
| **E. sync_account_states 区分"被踢" vs "待机"** | #6 | `manager.py:526-541` | 都被无脑标 STANDBY,被踢号反复死循环 |

**实施纪律**:A/B/C/D/E 必须由同一 PR 处理,否则会出现"修了 D 没修 B,quota probe 仍然不识别 quota=0"这类半截修复。

---

## 2. PRD 切分方案(4 + 1)

### PRD-1 — Mail Provider 全量化(Issue#1 独立)
- **范围**:SetupConfig 扩字段、cf_temp_email 嗅探收紧、`/api/mail-provider/probe` 新端点、前端归属验证 4 步流、6 个文档改写
- **不依赖其他 PRD**,可单独实施
- **风险**:UI 变动大、跨域代理设计需小心

### PRD-2 — 账号生命周期与配额(Issue#2 + #4 + #6 合并)
- **范围**:plan_type 白名单 + wham/usage no_quota 分类 + add-phone 探针 + _run_post_register_oauth probe + sync_account_states 区分被踢 + reinvite 兜底
- **PREFERRED_SEAT_TYPE 配置**(独属 #2)+ **personal 删除解耦**(独属 #2)
- **关键**:5 个共因点必须同 PR,否则半截
- **风险**:扩散到 manager/codex_auth/account_ops/api,需充分回归测试

### PRD-3 — Docker 镜像守卫(Issue#3 收尾)
- **范围**:entrypoint self-check + lint 守卫 + 镜像 git-sha 标签 + docs/docker.md 增加 rebuild SOP
- **代码层 cf2f7d3 已修**,这是防回归 + 用户操作指引
- **风险**:低

### PRD-4 — Playwright 一致性(Issue#5 待定)
- **状态**:研究反转 — 全项目 0 处实际误用
- **行动**:需向用户索取具体报错堆栈或样本;在拿到样本前,**仅落地"可选优化"**(顶层 import 统一 + asyncio 运行时 guard + AST 静态守卫单测)
- **风险**:用户可能描述的是其他问题,不要凭空重构

### 增量 — UI 删除 toast / OAuth 模式选择(可选,后置)
- 来自 #2 + #6 的 D.2 可选项,P1
- 单独 PR,不阻塞主线

---

## 3. 实施依赖图

```
PRD-1 (Mail Provider) ────────────┐
                                  ├─→ 集成测试(各自独立可并行)
PRD-2 (Lifecycle) ────────────────┤
                                  │
PRD-3 (Docker Guard) ─────────────┤
                                  │
PRD-4 (Playwright) [aw user] ─────┘
```

PRD-1 / PRD-2 / PRD-3 三个互不依赖,可并行实施。
PRD-4 阻塞在用户答复。

---

## 4. 优先级

| Rank | PRD | 价值 | 风险 |
|---|---|---|---|
| **P0** | PRD-2 (生命周期) | 高 — 解决 3 个 issue 的核心 bug,直接改善可用率 | 高 — 跨多文件 |
| **P0** | PRD-1 (Mail Provider) | 高 — 401 阻塞用户 setup,且补齐缺失功能 | 中 — UI 改动 |
| **P1** | PRD-3 (Docker) | 中 — 防回归 + 用户操作友好 | 低 |
| **P2** | PRD-4 (Playwright) | 不确定 — 待用户澄清 | 低 |

---

## 5. 共享 spec(横切关注点)

以下抽象应在 spec 阶段抽出独立文档,供多个 PRD 引用:

- `spec/shared/plan-type-whitelist.md`:`SUPPORTED_PLAN_TYPES` 常量、判定函数、所有调用点
- `spec/shared/quota-classification.md`:wham/usage 4+1 分类(ok/exhausted/no_quota/auth_error/network_error)、no_quota 触发条件、上游处理
- `spec/shared/add-phone-detection.md`:`detect_phone_verification` 复用契约、OAuth 模式定制探针、错误归类
- `spec/shared/account-state-machine.md`:7 状态(active/exhausted/standby/pending/personal/auth_invalid/orphan)的转换规则、被踢识别规则

---

## 6. 测试矩阵

| 测试类型 | 范围 | 工具 |
|---|---|---|
| 单元测试 | 共因函数 (plan_type/quota/add-phone) | pytest |
| 集成测试 | _run_post_register_oauth 全链路 | pytest + httpx mock |
| 回归测试 | invite/reinvite/sync_account_states 现有路径 | 既有测试套件 |
| E2E | mail provider 切换 + 归属验证 | playwright |
| 手测 | docker rebuild 流程、UI 删除 toast、OAuth 模式选择 | 用户参与 |

---

## 7. 已知未决问题(送 PRD 阶段决议)

1. **wham/usage schema** — `limit/total` 字段是否真存在?需要从用户 `auths/codex-*.json` 实测样本
2. **`self_serve_business_usage_based` 字面量** — 用户报告的字面值需 grep 用户实际 bundle 确认
3. **PATCH 升级失败率** — 改默认席位策略前应有数据
4. **add-phone 探针误报** — OAuth 域(auth.openai.com)的 detect 规则可能需要定制
5. **PRD-4 用户澄清** — 需要给出具体 async/sync 报错样本
