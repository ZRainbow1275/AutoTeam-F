# S5 + S6 — 预测式替换 + 并发批量替换

## 父 PRD 引用

继承 `05-11-upstream-align-register-multimail-frontend-refresh/prd.md`
Q2 创新点 1（预测式替换）+ 创新点 2（并发批量替换）。基础设施依赖
round-12 已落地的 S1（`account_state.py` 状态机 / `update_account` 统一路由）
+ S3（`cmd_rotate` 双指标终止条件 + `ensure_account_mail` per-account
路由 + `_count_pool_active_accounts`）。

## Goal

1. **S5**：新增 `quota_predictor.py` —— 累积配额历史时序，基于线性回归
   预测每个 ACTIVE 子号"接近耗尽"的时刻；在 `cmd_rotate` 主循环里加
   "预测式抢先替换"分支：剩余时间 < `PREDICTIVE_LEAD_MIN` 分钟时主动
   `update_account(email, status=STANDBY)`，让 S3 既有 vacancy 兜底自动
   触发替换。
2. **S6**：把 `cmd_rotate` 的"复用 standby + 新建账号"两段串行循环改为
   `ThreadPoolExecutor` 并发；`ROTATE_CONCURRENCY` env 控制最大并发，
   默认 1（向后兼容）。每席位独立 try/except，失败聚合输出，互不阻塞。

## Scope

### S5 — 预测式抢先替换

新建 `src/autoteam/quota_predictor.py`：

- `class QuotaPredictor`
  - `__init__(history_path: Path | None = None)`
  - `record(email: str, p_remain: float, t_remain: float | None, ts: float | None = None) -> None`
    - 追加一行到 `quota_history.jsonl`：
      `{"email":"x","p_remain":50,"t_remain":12345,"ts":1736...}`
    - 文件不存在自动创建；t_remain 为 None 时写 null。
  - `load_history(email: str, *, max_points: int = 50) -> list[dict]`
    - 读全部历史，按 email 过滤，按 ts 升序排序，截尾保留最近
      `max_points` 个点（避免长跑期间历史无限增长）。
  - `predict_exhaust_time(email: str) -> float | None`
    - 历史 < 3 点 → 返回 None（不做预测）。
    - 用 `p_remain`（百分比）vs `ts` 做最小二乘线性拟合：
      `p_remain = a * ts + b`；
    - `slope a >= 0` → 配额在增长或不变 → 返回 None。
    - `slope a < 0` → 解 `0 = a * t_exhaust + b` → 返回 `t_exhaust`。
    - 任何异常（数值不稳定 / 全零） → 返回 None。
  - `should_preempt(email: str, lead_minutes: float, *, now: float | None = None) -> bool`
    - 计算 `t_exhaust = predict_exhaust_time(email)`；
    - 返回 `t_exhaust is not None and (t_exhaust - now) < lead_minutes * 60`。

- 配置（`config.py` 新增）：
  - `PREDICTIVE_ENABLED: bool` env `PREDICTIVE_ENABLED` 默认 `False`
    （安全默认 — 不预测，按 round-9~12 旧行为）。
  - `PREDICTIVE_LEAD_MIN: int` env `PREDICTIVE_LEAD_MIN` 默认 `15`。
  - `PREDICTIVE_HISTORY_FILE: Path` 默认 `PROJECT_ROOT / "quota_history.jsonl"`。

- `cmd_rotate` 接入（manager.py）：
  - 在 `[2/5] cmd_check()` 之后、`[3/5]` 移出 exhausted 之前，加新分支
    `[2.5/5] 预测式抢先替换`：
    - 仅当 `PREDICTIVE_ENABLED` 为 True 才执行；
    - 遍历所有 ACTIVE 非主号；
    - 调用 `predictor.should_preempt(email, PREDICTIVE_LEAD_MIN)`；
    - True → `update_account(email, status=STATUS_STANDBY,
      _reason="predictive_preempt")`，日志记录。
    - 不直接 remove_from_team —— 让 S3 双指标兜底 + 后续 [3/5]
      `STATUS_EXHAUSTED` 移出循环不触发（因为我们打的是 STANDBY 而非
      EXHAUSTED）；这里要走"先 STANDBY → vacancy 计算自动 + 1 →
      [5/5] 新号填补"流程，所以我们需要紧接着 remove_from_team。
    - 修正：`STATUS_STANDBY` 本身是"已移出 Team"语义，必须先 kick；
      所以分支调用顺序：
        a. `remove_from_team(chatgpt, email, return_status=True)`
        b. 成功 → `update_account(email, status=STATUS_STANDBY,
           _reason="predictive_preempt")` + 日志。

- `cmd_check` 钩子（manager.py）：每次 ChatGPT API quota 探测成功后
  调用 `predictor.record(email, p_remain, t_remain, ts=time.time())`。
  - 已有 `update_account(email, last_quota=quota_info)` 写入点 ~多处，
    我们在最具代表性的"标准成功路径"加一次 record，避免重复记录。
  - 若 cmd_check 路径复杂 → 至少在 `cmd_rotate` 的预测分支前先批量
    record 一次（用 acc.last_quota），最低代价保证测试可复现。

### S6 — 并发批量替换

- `config.py` 新增 `ROTATE_CONCURRENCY: int` env `ROTATE_CONCURRENCY`
  默认 `1`（向后兼容串行）。
- `manager.py` 改造 `cmd_rotate`：
  - **复用阶段**（standby 循环）：把单循环的"额度校验 + reinvite_account"
    抽成 `_reuse_one_standby(acc, chatgpt, mail_for_acc, threshold) -> dict`
    （返回 `{"email","result":"reused|skipped_quota|skipped_auto|failed",
    "error":str|None}`）；用 `ThreadPoolExecutor(max_workers=ROTATE_CONCURRENCY)`
    并行处理 standby_list（按 vacancies 截断）。
  - **新建阶段**（fill new accounts）：同样改造为
    `_create_one_new_account(chatgpt, mail_client) -> dict`，
    并发 `remaining` 个任务。
  - **线程安全保证**：
    - `update_account` / `load_accounts` 已通过 `default_machine.transition`
      内部 `threading.Lock` 串行写入 state_log；新增
      `_accounts_io_lock = threading.Lock()` 包裹 `load_accounts` +
      `save_accounts` 这对 read-modify-write（避免并发写覆盖）。
    - `chatgpt` 与 `mail_client` 不能并发访问 Playwright browser
      —— 我们在并发执行前**串行**完成所有 ChatGPT API 调用（reinvite
      / remove_from_team），仅把"邮件等待 + OTP 提取 + accounts.json
      写入"这种 IO bound 部分并发。
    - 修正实现：本任务**仅 IO bound 部分并发** —— 即"邮件 wait_email
      + OTP 解析"（每席位独立 mail 客户端，因 S3 `ensure_account_mail`
      已 per-acc 缓存）。`reinvite_account` 内部完整链路本来就含
      browser 调用 → 必须以"全 mail client 独立"为前提才能安全并发。
    - 决策：**S6 仅并发 standby 复用阶段 + 仅当 acc 有独立
      mail_provider 绑定时才并发**；新建阶段（创新号）保持串行，
      因 ChatGPT browser session 共享。
    - 失败聚合：每席位独立 try/except，结果列表用线程安全 `list.append`
      + lock；总结日志输出"成功 N / 失败 M / 跳过 K"。
  - **向后兼容**：`ROTATE_CONCURRENCY=1` 时退化为串行 for 循环
    （早期 return + 不开 ThreadPoolExecutor，避免单测里 mock 复杂）。

### 测试

`tests/unit/test_round12_s5_predictor.py`（≥ 6 case）：

1. `predict_exhaust_time` 历史 < 3 点 → None。
2. 线性递减 5 点（每 60 秒 -5%） → 预测耗尽时刻误差 < ±10%。
3. `slope >= 0`（配额持续上升或不变） → None。
4. `should_preempt` 在 lead window 内 → True；外 → False。
5. `record` 写文件后 `load_history` 能复现（roundtrip）。
6. `load_history(max_points=N)` 截尾正确（写 100 行只读最后 N）。

`tests/unit/test_round12_s6_concurrent.py`（≥ 6 case）：

7. `ROTATE_CONCURRENCY=1` → 行为与改造前一致：串行调用，无 executor
   open（用计数 mock 验证 `_reuse_one_standby` 调用顺序）。
8. `ROTATE_CONCURRENCY=3` + 5 个 standby 任务 → 并发完成
   （tasks 计入并发起始的最大 inflight ≥ 2，用 latch 验证）。
9. 单席位抛异常 → 不影响其他席位，聚合 result 包含 `failed` + `reused`。
10. `_accounts_io_lock` 在并发 update_account 下不丢写
    （线程并发触发 10 次 update_account → 最终 accounts.json 含 10 次更新）。
11. cancel_signal 在并发中能尽早结束（剩余 task 取消或快速返回）。
12. `_reuse_one_standby` 返回的 result 字面量稳定（用于 SSE 上报）。

合计新增 ≥ 12 单测，与原 658 累加。

## 必读引用

- 父 PRD（创新点 1 & 2 描述）
- `.trellis/tasks/05-11-s0-upstream-team-rotate-diff/research/upstream-diff.md`
- `.trellis/tasks/05-11-s3-cherry-pick-team-rotate/prd.md`
- `.upstream/manager.py`（上游 `cmd_rotate` baseline 行 2178-2520）
- `src/autoteam/account_state.py`（S1 状态机）
- `src/autoteam/accounts.py`（`update_account` 统一路由）
- `src/autoteam/manager.py`（S3 cherry-pick 后的 `cmd_rotate`）
- `src/autoteam/config.py`（env 配置范式）

## Acceptance Criteria

- [ ] `src/autoteam/quota_predictor.py` 落地（class 完整 + module-level
      `default_predictor`）
- [ ] `config.py` 新增 `PREDICTIVE_ENABLED` / `PREDICTIVE_LEAD_MIN` /
      `PREDICTIVE_HISTORY_FILE` / `ROTATE_CONCURRENCY`
- [ ] `manager.py:cmd_rotate` 接入预测分支（默认 disabled）
- [ ] `manager.py:cmd_rotate` standby 复用循环改造为
      `_reuse_one_standby` + ThreadPoolExecutor
- [ ] `accounts.py` 新增 `_accounts_io_lock` 包裹 load+save 原子段
- [ ] `tests/unit/test_round12_s5_predictor.py` ≥ 6 case 全绿
- [ ] `tests/unit/test_round12_s6_concurrent.py` ≥ 6 case 全绿
- [ ] `ruff check src/autoteam/ tests/unit/` 全绿
- [ ] `pytest tests/` ≥ 658 + 12 ≥ 670 passed（不退化）

## Definition of Done

- 所有验收点 ✓
- commit 信息：`feat(round-12 S5+S6): predictive preempt + concurrent rotate`
- git add 仅自己改的文件（**严禁 `-A`**）

## Out of Scope

- 不动 mail provider 模块（S2 已落地）
- 不动 `account_state.py` 状态机（S1 已落地）
- 不动 web/（F2+F3 已落地 SSE 进度推送）
- 不实现 multi-workspace（S7 顶层任务）
- 不改 S3 已 cherry-pick 的双指标终止条件
- 不并发"新建账号"阶段（browser 状态共享，风险过高）
- 不真实触发 rotate（无可用母/子账号）；全靠 mock + 静态分析

## Risk Notes

- ⚠ 预测算法选最简单的最小二乘 —— 配额非线性（5h 重置导致阶跃） 的
  场景预测会偏；为避免误触发，`PREDICTIVE_ENABLED` 默认 `False`，
  上线后由用户在前端 settings 主动开启。
- ⚠ 并发 standby 复用前提：每席位 mail provider 已 per-acc 绑定
  (S3 `ensure_account_mail`)；未绑定的旧 acc 仍走全局 `mail_client`
  —— 此时并发会争同一 client，必须 fallback 串行。
- ⚠ `ThreadPoolExecutor` 在 Windows + Playwright 子进程下不能跨 worker
  共享 browser；本任务**仅并发 mail wait_email/extract_code 这类 IO**，
  ChatGPT browser 调用保持主线程串行（在每个 worker 开始时主线程已
  完成 invite 调用）。
- ⚠ `quota_history.jsonl` 长期增长 —— 测试覆盖 `max_points` 截尾，
  但生产用户需手动定期清理；后续可加自动 rotate。
