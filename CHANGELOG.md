# Changelog

本文档记录 AutoTeam-F 相对上游 [cnitlrt/AutoTeam](https://github.com/cnitlrt/AutoTeam) 的差异以及版本演进。日期采用 ISO 8601。

## [Unreleased] — 2026-04-25

### invite-hardening round-3:母号切换鲁棒性 + exhausted 自愈 + CPA 删除守卫

> 用户实测后报告 4 个问题:① "生成免费号"按钮 HTTP 500 ② exhausted 5h 重置后未回血到 active ③ 母号被吊销切换新号后,旧 workspace 留下的子号被错刷成 standby + CPA 文件被批量误删 ④ README 关于 personal 号牵连失效的结论错误。本轮逐一修复,全部为本地行为变更,不动 API/UI 协议。

- **fix(api): `list_accounts` → `load_accounts`** — `api.py:post_fill` 在 `leave_workspace=True` 分支误写成 `from autoteam.accounts import list_accounts`(实际函数名 `load_accounts`),导致点 Web 面板"生成免费号"按钮直接 ImportError → 500 → 前端 toast"服务器返回了非 JSON 响应"。单点改名修复。
- **feat(check): exhausted 自愈** — `cmd_check` 新增"复测重置时间已过的 exhausted 账号"分支:遍历 `STATUS_EXHAUSTED` 且 `quota_resets_at <= now` 的账号,调一次 `_check_and_refresh`,返回 `ok` 且 5h 剩余 ≥ 阈值 → **直接 promote 回 `STATUS_ACTIVE`**(同时清 `quota_exhausted_at` / `quota_resets_at`),省一次"kick → standby → re-invite"的 Playwright 全流程。`exhausted` 仍 `auth_error` → 标 `AUTH_INVALID`;仍 `exhausted` → 刷 `quota_resets_at`;`network_error` → 保持原状下一轮再试。
- **feat(accounts): `workspace_account_id` 字段(母号切换防错杀)** — `accounts.json` 每条记录新增 `workspace_account_id` 字段,记录该号被邀请时所属的 ChatGPT Team workspace `account_id`。`add_account()` 接受新参数;`invite.py` / `manager._run_post_register_oauth` / `manual_account.py` / `manager.create_account_direct` / `manager.cmd_check` pending→active / `_reconcile_team_members` pending→active+standby→active / `cmd_replace_one` 旧号恢复 / api `post-login` Team 补登都把当前 `get_chatgpt_account_id()` 写入。
- **fix(sync): sync_account_states 不再把"前母号留下号"误标 standby** — 之前 `sync_account_states` 看到 `acc.status==active` 但 `email ∉ team_emails` 就一刀刷成 standby,**完全忽略**这可能是母号切换导致 workspace 整个换了。结果:用户切换母号一瞬间,本地全部 active 子号被错刷 standby,接着 `sync_to_cpa` 把它们的 CPA 文件全删了(实测损失:319 个文件里 0 个匹配本地 4 个号,全军覆没)。新逻辑:`acc.workspace_account_id` 与当前 `account_id` 都存在且不同 → **保留 active 不 flip**,WARN log 提示用户;legacy 记录(无 `workspace_account_id`)走原行为以保持兼容。
- **fix(cpa_sync): 删 CPA 文件前的本地 auth_file 守卫** — `sync_to_cpa()` 删除 CPA 文件前增加一道闸:若该 email 在本地 `accounts.json` 仍持有 **同名** `auth_file` 物理文件 → 跳过删除,WARN "本地仍持有同名 auth_file 物理文件,等下一轮状态稳定后再处理"。即使 sync_account_states 之外的某条路径错误把 status 改成非 active/personal,这层防御兜住"实物 token 不会被瞬时状态误判抹掉"。
- **fix(docs): README 撤回错误"已知限制"** — 删除"母号 Team workspace 被吊销时,从该母号衍生(经 Team 邀请 → leave_workspace → personal OAuth)出来的 free plan personal 号会一起失效"这条小节。实测:`d5a9830dc1@icoulsy.asia` 的 token 完全有效,只是 5h primary 已满 — 我先前那次 auth_error 探测是误判。CHANGELOG 同步删该条。

### mail-provider 协议错配诊断(issue #1)

> [issue #1](https://github.com/ZRainbow1275/AutoTeam-F/issues/1) 报告:从 `cnitlrt/AutoTeam` 迁过来的用户配的 `CLOUDMAIL_*` 实际指向 `maillab/cloud-mail` 服务器,但本 fork 默认 `MAIL_PROVIDER=cf_temp_email` 走的是 `dreamhunter2333/cloudflare_temp_email` 协议 → maillab 服务器把 `/admin/address` 用 catch-all 路由误回 200,login 假成功;后续 `/admin/new_address` 拿到 `{code:401, message:"身份认证失效"}` 才暴露问题。

- **fix(mail): 双向协议错配嗅探** — `CfTempEmailClient.login()` 在 `/admin/address` 响应没有 `results` 字段但有 `code/data` 时抛出明确切换提示;`create_temp_email()` 二次防御 maillab 风格 `{code, message}` 响应。`MaillabClient._parse_response` 收到 HTTP 404 时提示"看起来是 cf_temp_email 服务器"。
- **feat(setup_wizard): 启动前路由指纹嗅探** — `_sniff_provider_mismatch` 探测 base_url 的 `/admin/address` 与 `/login` 路由活跃度,与 `MAIL_PROVIDER` 期望不一致时打 warning(不阻断启动,真正校验仍走 login/create)。
- **docs(README): 推荐 `cf_temp_email`** — README 启动小节明确推荐 [`dreamhunter2333/cloudflare_temp_email`](https://github.com/dreamhunter2333/cloudflare_temp_email),并提示从 cnitlrt 迁移的用户:cnitlrt 原版的 "cloudmail" 实际是 [`maillab/cloud-mail`](https://github.com/maillab/cloud-mail),需要显式 `MAIL_PROVIDER=maillab`。
- **docs(configuration): 协议错配排查小节** — `docs/configuration.md` 新增 issue #1 错配场景的报错样例 + 切换步骤。
- **真机验证**:用户当前 `apimail.icoulsy.asia` 是 cf_temp_email(`/admin/address` 401);issue #1 koast18 的服务器是 maillab(`/login` 路径活跃,响应是 `{code, message}` 格式)。


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

### invite-hardening 批判审查 round 2:实际修复落地

> 上一节列出的 backlog 在本轮按攻击面拆 4 个 fix task 跑完,以下逐条对照 finding 标记修复状态(✅ = 已修;(待后续) = 本轮未覆盖)。

- **invite_member 重试与错误分类(`chatgpt_api.py`)**
  - ✅ 5xx(500/502/503/504) 新增 `server_error` 分类,与 `network` / `rate_limited` 一并按退避表重试,不再被吞成 `other` 直接掉号
  - ✅ `_DOMAIN_BLOCKED_KEYWORDS` 收窄到 `not allowed` / `domain blocked` / `domain is not allowed` / `forbidden domain` / `domain not permitted`,移除裸 `domain` / `forbidden` / `blocked`,避免命中 `errored_emails` 里 email 自身的 "@gmail.com" 之类被误判为 domain_blocked
  - ✅ `errored_emails[].error/code/message` 内层字段进入 body_text 扫描;同时停止 fallthrough 到 `resp_body`,杜绝邮箱字面量污染分类
  - ✅ POST 重试加 30% jitter(`time.sleep(base + random.uniform(0, base*0.3))`),批量号被同一窗口拒绝后不会同步反弹再次撞 rate_limit
- **`invite_to_team` 死代码下沉(`manager.py` / `chatgpt_api.py`)**
  - ✅ 把 `default → usage_based` 兜底、`errored_emails` 处理、`_seat_type` 标注全部下沉到 `chatgpt_api.invite_member` 内部(新增 `_invite_member_with_fallback` / `_invite_member_once`),`invite.py:run` 只读 `_seat_type` 字段。manager 包装层不再被绕过,链路与 commit msg 一致
  - ✅ `invite.py` 调用 `add_account(... seat_type=seat_label)` 把 raw `_seat_type` 翻译成 `SEAT_CHATGPT` / `SEAT_CODEX` / `SEAT_UNKNOWN` 常量落盘
- **`seat_type` 落盘是死数据**
  - (待后续)下游 OAuth / `wham/usage` 路径暂未按 `seat_type` 分流(本轮重点是堵漏,差异化处理留给后续 PR)
  - (待后续)`_run_post_register_oauth` 的 `team_auth_missing` 分支与 `sync_account_states` 自动补录路径仍直接拼字段,未走 `add_account` 工厂 — 字段不全的隐患未根治
- **新状态 `auth_invalid` / `orphan` 在前端 / 状态汇总缺失**
  - ✅ `api.py:get_status` summary dict 新增 `auth_invalid` / `orphan` 计数项
  - ✅ `web/src/components/Dashboard.vue` 的 `statusClass` / `dotClass` / `statusLabel` 白名单加 `auth_invalid`(橙色 / "认证失效")和 `orphan`(琥珀色 / "孤立");`loginLabel` 把这两种状态归入"补登录"语境
- **`_reconcile_team_members` 漏洞**
  - ✅ **dry_run 第二轮 over-cap 预测**:不再 `return result` 跳过第二轮;dry_run 下用 "round-1 team_subs - 已 KICK" 模拟 remaining,避免重新 GET /users 把"假装 KICK"的 ghost 计回去高估 over_cap 数量;victims 只 log + 写 `result["over_cap_kicked"]`,不调 `remove_from_team`
  - ✅ **ghost 不再绕过 `RECONCILE_KICK_GHOST=False` 开关**:`_priority` 中 ghost(本地无记录)的元组从 `(0, 0)` 改为按开关取值 — `True` 时仍 `(0, 0)` 优先 KICK,`False` 时降到 `(99, 0)` 排到最后,被开关压住
  - ✅ **`_find_team_auth_file` 拒绝 personal/plus auth**:删除 `codex-{email}-*.json` 兜底分支,严格只接 `codex-{email}-team-*.json`,避免错 plan bundle 被挂到 team 席位账号导致 OAuth 401 / org mismatch
  - ✅ **补齐 auth_file 后 fallthrough quota 检查**:抽出 `_check_and_mark_exhausted` 辅助函数,STANDBY 错位补 auth + ACTIVE 缺 auth 补齐两条路径都在补完后立刻做 `_is_quota_exhausted_snapshot`,该标 EXHAUSTED 的不再被当 active 留下
  - (待后续)STANDBY 错位降级 KICK 后写 `STATUS_AUTH_INVALID` 与 `accounts.py` 注释"token 失效"语义不符的问题,本轮未改语义(改字段名 / 状态值需要更大面 PR)
- **`_probe_standby_quota` 网络抖动误判 + 自愈断裂**
  - ✅ **`check_codex_quota` 错误分类细化**:返回值新增 `("network_error", None)`,DNS / Timeout / SSL / 5xx / 429 / 4xx(非 401/403) / JSON 解析失败 / 未知异常一律归 `network_error`,只有 HTTP 401/403 才返回 `auth_error`
  - ✅ **`_probe_standby_quota` 网络分支不再误标 AUTH_INVALID + 不再写 `last_quota_check_at`**:看到 `network_error` 只 log warning,不动 status,不写时间戳 — 下一轮立即重试,不被 24h 去重屏蔽。事故根因(一次网络抖动 18 个号被批量误标 AUTH_INVALID 后被 reconcile 全删)修复
  - ✅ **未知 `status_str` 防御分支不写时间戳**:`cmd_check` 主路径里碰到 `network_error` 也走"本轮跳过、不进 auth_error_list"的安全分支
  - (待后续)`_probe_standby_quota` 主循环 `stop_flag` / 软取消信号未接入,中途取消仍可能留半截探测状态
- **文档缺漏**
  - ✅ `.env.example` 末尾追加 `RECONCILE_KICK_ORPHAN` / `RECONCILE_KICK_GHOST` 两个开关示例(带注释说明 true / false 行为差异)
  - ✅ `docs/api.md` 后台任务表格 `/api/tasks/check` 行注明 `{"include_standby": false}`;新增 "管理员运维" 小节,列 `POST /api/admin/reconcile?dry_run=0` 端点说明
  - ✅ `CHANGELOG.md` 新增本节,逐条对照 backlog 标 ✅ / (待后续)
  - ✅ `README.md` "修复了什么" 末尾追加一行"子号巡检在网络抖动 / 5xx 时被错误标 auth_invalid → 整批号被踢"
  - (待后续)`docs/architecture.md` 状态机图未画 "reconcile KICK orphan → STATUS_AUTH_INVALID" 转移
  - (待后续)`docs/platform-signup-protocol.md` 顶部 `Status:` 行未标"探索性归档(需求 1 已放弃)"

**测试统计**:71 passed, 1 pre-existing fail。新增回归测试覆盖 `_classify_invite_error` 5xx 分类、`errored_emails` 解析、_invite_member_once 兜底、reconcile dry_run 第二轮 over-cap 预测、ghost priority 受 RECONCILE_KICK_GHOST 控制、`_find_team_auth_file` 拒绝 personal auth、补齐 auth 后 fallthrough quota、`check_codex_quota` 网络错误分类、`_probe_standby_quota` 网络抖动不写时间戳。

**真机验证**:18 个被误标 AUTH_INVALID 的号已批量删除,确认本批 bug 修完后单次网络抖动不再造成整批误判。

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
