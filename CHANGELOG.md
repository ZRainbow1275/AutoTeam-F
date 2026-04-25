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
