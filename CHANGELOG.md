# Changelog

本文档记录 AutoTeam-F 相对上游 [cnitlrt/AutoTeam](https://github.com/cnitlrt/AutoTeam) 的差异以及版本演进。日期采用 ISO 8601。

## [Unreleased] — 2026-04-25

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
