# Changelog

本文档记录 AutoTeam-F 相对上游 [cnitlrt/AutoTeam](https://github.com/cnitlrt/AutoTeam) 的差异以及版本演进。日期采用 ISO 8601。

## [Unreleased] — 2026-04-25

### invite-hardening:邀请 / 巡检 / 对账三路加固

- **feat(invite): seat fallback 鲁棒性** — `chatgpt_api.invite_member` 新增 `_classify_invite_error`(rate_limited / network / domain_blocked / other) + POST `/invites` 退避重试 `[5s, 15s]`;`_update_invite_seat_type` 的 PATCH 加 1 次重试,全部失败时**保留 codex 席位**(`_seat_type="usage_based"`)而不是丢账号。响应 dict 现在一定包含 `_seat_type` ∈ {`chatgpt`, `usage_based`, `unknown`} 与 `_error_kind`,`invite.py` / `manual_account.py` / `manager._run_post_register_oauth` 都据此把席位类型落到 `accounts.json.seat_type`。
- **feat(check): `cmd_check --include-standby`** — `cmd_check(include_standby=False)` 默认行为不变;传 `True` 时调用新增的 `_probe_standby_quota` 遍历 standby 池,限速 `STANDBY_PROBE_INTERVAL_SEC=1.5s`、去重 `STANDBY_PROBE_DEDUP_SEC=86400s`(24h 内已探测过的跳过)。探到 401/403 → 标 `STATUS_AUTH_INVALID`,仍 exhausted → 刷新 `quota_exhausted_at/resets_at`,ok → 写回 `last_quota` + `last_quota_check_at`(不动 status)。CLI `autoteam check --include-standby`,API `POST /api/tasks/check` 接受 `{"include_standby": true}`。
- **feat(reconcile): 残废 / 错位 / 耗尽未抛弃 + dry-run** — `_reconcile_team_members` 从原先 3 类扩到 8 类分支,覆盖:
  - **残废**(workspace 有 active + 本地 `auth_file` 缺失)→ 先尝试从 `auths/codex-{email}-team-*.json` 兜底补齐;找不到则按 `RECONCILE_KICK_ORPHAN` 决定 KICK 或标 `STATUS_ORPHAN`
  - **错位**(workspace active + 本地 standby)→ 改回 active + 补齐 auth_file(找不到 auth 则降级残废路径)
  - **耗尽未抛弃**(active + `last_quota` 5h/周均 100%)→ 标 `STATUS_EXHAUSTED` + `quota_exhausted_at=now`,**不立即 kick**,让正常 rotate 流程走,避开 token_revoked 风控
  - **ghost**(workspace 有 + 本地完全无记录)→ 按 `RECONCILE_KICK_GHOST` 决定 KICK 或留给 `sync_account_states` 补录
  - `auth_invalid` / `exhausted` / `personal` → 同样 KICK
  - `orphan` → 已标记,跳过,等人工
- **feat(reconcile): dry-run 模式** — `cmd_reconcile(dry_run=True)` / `cmd_reconcile_dry_run()` 只诊断不动账户;CLI `autoteam reconcile [--dry-run]`,API `POST /api/admin/reconcile?dry_run=1`。`_reconcile_team_members` 返回结构化 dict(`kicked` / `orphan_kicked` / `orphan_marked` / `misaligned_fixed` / `exhausted_marked` / `ghost_kicked` / `ghost_seen` / `over_cap_kicked` / `flipped_to_active`),第二轮 over-cap kick 优先级改为 `orphan → auth_invalid → exhausted → personal → standby → 额度最低 active`。
- **新增字段 / 状态**:
  - `accounts.json.seat_type` ∈ `SEAT_CHATGPT` / `SEAT_CODEX` / `SEAT_UNKNOWN`,常量在 `autoteam.accounts`
  - `accounts.json.last_quota_check_at`(epoch 秒)— standby 探测去重依据
  - `STATUS_ORPHAN` — workspace 占席 + 本地 auth 丢失,等人工补登或 kick
  - `STATUS_AUTH_INVALID` — `auth_file` token 已不可用(401/403),待 reconcile 清理或重登
- **新增配置**:
  - `RECONCILE_KICK_ORPHAN`(默认 `true`)— 残废是否自动 KICK
  - `RECONCILE_KICK_GHOST`(默认 `true`)— ghost 是否自动 KICK
- **测试**:`tests/unit/test_invite_member_seat_fallback.py`(5)、`tests/unit/test_cmd_check_standby.py`(5)、`tests/unit/test_reconcile_anomalies.py`(5),全过;ruff 干净。

### invite-hardening 回归修复(真机对账后发现)

- **fix(reconcile): KICK orphan 成功后必须同步本地 `STATUS_AUTH_INVALID`** — `_reconcile_team_members` 第一轮把 workspace 残废账号 KICK 掉之后,**只动了 workspace 状态、没改 `accounts.json`**,下次 `cmd_fill` / `cmd_rotate` 仍按 `STATUS_ACTIVE` 计数,Team 席位计算飘移、出现"账号已被踢但本地仍占名额"的幽灵态。补丁:`manager.py:280-281`(STANDBY 错位降级路径)和 `manager.py:304-305`(直接残废路径)KICK 返回 `removed`/`already_absent`/`dry_run` 时,立刻 `_safe_update(email, status=STATUS_AUTH_INVALID)`。新增 `tests/unit/test_reconcile_anomalies.py::test_reconcile_orphan_kick_syncs_local_status_to_auth_invalid` 做回归保护。

### invite-hardening 批判性代码评审产出(2026-04-25,5-agent team review,findings only,补丁待后续 PR)

> 这一节记录 d6082ad + 上述回归修复合到 main 后,5 个 agent 各自负责一个攻击面跑批判审查得出的**待修问题清单**。本节代码未改动,只列入 backlog 供后续 PR 拆单解决。

- **invite_member 重试与错误分类(`chatgpt_api.py`)**
  - `_classify_invite_error` 把 5xx 归为 `other` → 不重试,OpenAI 网关短抖直接掉号(`chatgpt_api.py:1309-1340`)
  - `domain` / `forbidden` / `blocked` 关键词命中面太宽,可恢复错误被吞成 `domain_blocked` 不重试(`chatgpt_api.py:1338`)
  - `errored_emails` / `account_invites` 数组形态的内层 error 字段不被扫描(`chatgpt_api.py:1322-1334`)
  - 重试无 jitter,批量号同步反弹放大 rate_limit;`status==0` 网络分支总耗时可能 1–2 分钟卡死调用链
- **`invite_to_team` 是死代码**(`manager.py:1239-1268`)
  - `invite.py:479` 直接调 `chatgpt_api.invite_member` 绕过包装,`return_detail=True` / `seat_label` 转译 / `default→usage_based` 兜底**全部从未生效**;commit msg 宣称的链路与运行时不符
- **`seat_type` 落盘是死数据**
  - 全仓 grep 无任何模块读 `acc.get("seat_type")`,PATCH 失败保留 codex 席位的兜底对下游零影响 — 仍按 chatgpt 席位走 OAuth + 查 `wham/usage`
  - `_run_post_register_oauth` 的 `team_auth_missing` 分支(`manager.py:1364-1370`)+ `sync_account_states` 自动补录路径(`manager.py:479-491` / `509-521`)写新账号时跳过 `add_account` 工厂,字段不全
- **新状态 `auth_invalid` / `orphan` 在前端/状态汇总缺失**
  - `api.py:1529-1573` `/api/status` summary 硬编码 5 种旧状态,新状态不计数
  - `web/src/components/Dashboard.vue:381-403` `statusClass` / `dotClass` / `statusLabel` 白名单不包含新状态,UI 看到原始英文 + 灰色样式
- **`_reconcile_team_members` 漏洞**
  - **dry_run 严重低估真实 KICK 数**:跳过第二轮 over-cap,审批链路被绕过(`manager.py:344-346`)
  - **`_priority` 里 ghost 返回 `(0, 0)` 最先 kick,绕过 `RECONCILE_KICK_GHOST=False` 开关**(`manager.py:378-379`)
  - **`_find_team_auth_file` fallback** 接受 personal/plus plan 的 auth 挂到 team 席位账号,导致下次 API 401 / org mismatch(`manager.py:124-126`)
  - **补齐 auth_file 后 `continue` 跳过 `_is_quota_exhausted_snapshot`**:本应标 EXHAUSTED 的号当 active 留下,下次 fill 立即 429(`manager.py:269-272` / `295-298`)
  - STANDBY 错位降级 KICK 后打 `STATUS_AUTH_INVALID`,语义被拉宽到"auth 文件压根不存在",和 accounts.py:19 的"token 失效"注释不符,可能让暂时丢 auth 的号永久从 standby 池消失
- **`_probe_standby_quota` 网络抖动误判 + 自愈断裂**(`manager.py:1120-1122` + `codex_auth.py:1642-1656`)
  - `check_codex_quota` 把 DNS / timeout / SSL / 5xx / 429 一律返回 `auth_error` → standby 探测看到无条件标 `STATUS_AUTH_INVALID` + 写 `last_quota_check_at` → **24h 内不复验**;若该号之后 reinvite 回 Team,reconcile 立即 KICK,自愈链路断裂
  - 未知 `status_str` 防御分支也写 `last_quota_check_at`,异常被屏蔽 24h
  - 主循环无 `stop_flag` / 软取消信号,中途取消会留下半截探测状态
- **文档缺漏**
  - `.env.example` 漏列 `RECONCILE_KICK_ORPHAN` / `RECONCILE_KICK_GHOST` 两个开关示例
  - `docs/api.md` 未更新 `POST /api/admin/reconcile` 与 `POST /api/tasks/check {"include_standby": true}`
  - `docs/architecture.md` 状态机图未画 "reconcile KICK orphan → STATUS_AUTH_INVALID" 转移
  - `docs/platform-signup-protocol.md` 顶部 `Status:` 行未明确"探索性归档(需求 1 已放弃)"

> 评审范围:`d6082ad` + 本节回归补丁。共 5 个 reviewer 跑出 11 high / 13 medium / 2 low / 6 文档缺漏。补丁拆单到下个 PR,**这一节用于追溯,不构成代码改动**。

### 后续修复（基于代码评审 + 真机验证）

- **`maillab.list_emails` 漏传 `type=0`** — 上游 `service/email-service.js` 把空 `type` 翻成 `eq(email.type, NULL)`,所有 RECEIVE 类型(type=0)邮件被静默过滤,导致收件箱永远返回空。强制传 `type=0`。
- **`maillab.list_accounts` 服务端硬上限 30 条** — `account-service.js` 的 `list()` 把任何 `size>30` 截断到 30。改用游标(`lastSort` + `accountId`)循环翻页直到补满 `size`,避免请求 200 条只拿回 30 条造成轮转池误判。
- **删除 `mailCount` / `sendCount` 这两个永远为 None 的字段** — `entity/account.js` 没有这两列,前端读到的永远是 `null`,反而误导调用方。改取真实字段 `name` / `status` / `latestEmailTime`(后者经 `_parse_create_time` 转 epoch)。

### 新增 `maillab` 邮件后端 + provider 抽象层

- **新增 `MAIL_PROVIDER` 环境变量** — 在 `cf_temp_email`(默认,即 `dreamhunter2333/cloudflare_temp_email`)和 `maillab`(即 `maillab/cloud-mail`)之间切换。**业务调用方零改动**,旧的 `from autoteam.cloudmail import CloudMailClient` 仍然有效,工厂会按 provider dispatch。
- **拆分 `cloudmail.py`** → 新增 `src/autoteam/mail/` 包:
  - `base.py` — 定义 `MailProvider` ABC + `decode_jwt_payload` / `parse_mime` / `normalize_email_addr` 等公共辅助。
  - `cf_temp_email.py` — `dreamhunter2333/cloudflare_temp_email` 实现(`/admin/*` + `x-admin-auth` header + MIME 解析)。
  - `maillab.py` — `maillab/cloud-mail` 实现(`/login` + `/email/list` + 裸 JWT Authorization + 字段映射)。
  - `factory.py` — 单例工厂,按 `MAIL_PROVIDER` 实例化具体 provider。
- **`cloudmail.py` 退化为兼容 shim** — 不破坏导入路径,`CloudMailClient = get_mail_provider()` 即可。
- **新增 `MAILLAB_*` 配置** — `MAILLAB_API_URL` / `MAILLAB_USERNAME` / `MAILLAB_PASSWORD` / `MAILLAB_DOMAIN`(缺省回落 `CLOUDMAIL_DOMAIN`)。
- **`setup_wizard._verify_cloudmail` 按 provider 分支验证** — 启动时根据 `MAIL_PROVIDER` 选择不同的连通性检查脚本(登录 → 创建 → 删除测试邮箱)。

### Team 子号管理(此版本累计修复)

- **`token_revoked` 风控冷却 30 分钟** — OpenAI 对短时间高频 invite/kick 触发 token 失效,watchdog 加 30 分钟冷却阀,假恢复路径区分 `quota_low/exhausted` vs `auth_error/exception` 四类 fail_reason,只有前两类才上 5h 锁。
- **`cmd_check` 入口自动对账 + Team 子号硬上限 4** — 防止 baseline + 本批新号超过 5。
- **OAuth 失败必须 kick 残留账号** — 防止假 standby。
- **三层防止 standby 被误判恢复反复洗同一批耗尽账号**。
- **personal 模式拒收 team-plan 的 bundle** — 跳过 step-0 ChatGPT 预登录后,如果拿到 team-plan 的 token,kick + 等同步,防止污染 personal 池。

### 文档

- **README / `docs/getting-started.md` / `docs/configuration.md`** — 修正"支持 cloudmail"的歧义表述,明确两种 provider 的来源仓库与各自配置项。

### 测试

- 新增 `tests/unit/test_maillab.py`(16 个用例),覆盖字段映射、auth header、createTime 解析、type=0 防御、翻页边界、phantom 字段排除。

---

## 历史版本

完整 commit 历史参见 `git log`,以下列出与上游差异的重要节点:

| 日期       | Commit       | 说明                                                         |
| ---------- | ------------ | ------------------------------------------------------------ |
| 2026-04-25 | `860a4f0`    | refactor(mail): 拆分 cloudmail.py 为 mail provider 抽象层 + 双后端实现 |
| 2026-04-24 | `5a35372`    | fix(team-revoke): 区分 token 风控 vs quota 用完 + watchdog 冷却 |
| 2026-04-24 | `3c26e88`    | fix(team-shrink): 巡检加 watchdog + 假恢复必刷 last_quota    |
| 2026-04-24 | `3f13ba6`    | feat(fill-personal): 队列化拒绝,Team 满席时不再借位          |
| 2026-04-24 | `aeafda6`    | fix(reuse): 三层防止 standby 被误判恢复反复洗同一批耗尽账号  |
| 2026-04-24 | `f6e9a4a`    | feat(auto-replace): Team 子号失效立即 1 对 1 替换            |
| 2026-04-24 | `ceb9711`    | fix(reinvite): OAuth 失败必须 kick 残留账号,防止假 standby   |
| 2026-04-24 | `9c24a6f`    | feat(reconcile): cmd_check 入口自动对账 + Team 子号硬上限 4  |
| 2026-04-23 | `e760be9`    | fix(codex-oauth): personal 模式拒收 team-plan 的 bundle + kick 后等同步 |
| 2026-04-23 | `1963072`    | feat(check): 让 cmd_check 扫描 Personal 号的额度             |
| 2026-04-23 | `07ef29f`    | fix(fill-personal): 修复账号实际未被踢出 Team 的问题         |
| 2026-04-22 | `3df0958`    | feat: AutoTeam-F 首发 — fork of cnitlrt/AutoTeam,引入 Free-account pipeline |
