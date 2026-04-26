# Issue#3 list_accounts ImportError 全量扫荡

> 调研时间:2026-04-26
> 调研范围:整个 AutoTeam 仓库 + Dockerfile / docker-compose / docker-entrypoint
> 结论:**源代码已干净 — docker 镜像未重建是 100% 真因**

---

## A. 当前 accounts.py 真实导出

文件路径:`D:/Desktop/AutoTeam/src/autoteam/accounts.py`

**accounts.py 没有 `__all__`**,所以"导出"以模块顶层 `def`/常量为准:

| 类别 | 名称 |
|---|---|
| **状态常量** | `STATUS_ACTIVE` / `STATUS_EXHAUSTED` / `STATUS_STANDBY` / `STATUS_PENDING` / `STATUS_PERSONAL` / `STATUS_AUTH_INVALID` / `STATUS_ORPHAN` |
| **席位常量** | `SEAT_CHATGPT` / `SEAT_CODEX` / `SEAT_UNKNOWN` |
| **路径常量** | `PROJECT_ROOT` / `ACCOUNTS_FILE` |
| **私有辅助** | `_normalized_email` / `_is_main_account_email` |
| **核心 IO 函数** | `load_accounts` ← (line 36) |
| | `save_accounts` (line 45) |
| **业务函数** | `find_account` / `add_account` / `update_account` / `delete_account` |
| **筛选函数** | `get_active_accounts` / `get_personal_accounts` / `get_standby_accounts` / `get_next_reusable_account` |

**关键判定**:
- 模块**从未定义** `def list_accounts`
- 模块**从未定义** `list_accounts = load_accounts` 别名
- `accounts.py` 在 git 历史里也从未有过这个名字 — `list_accounts` 是 `api.py` 那处 typo 凭空引入的不存在符号

---

## B. 全项目 list_accounts 引用清单

> 命令:`grep -rn "list_accounts" D:/Desktop/AutoTeam`

### B.1 与"账号池(autoteam.accounts)"相关的引用 — **0 处**

```bash
$ grep -rn "from autoteam.accounts import.*list_accounts" D:/Desktop/AutoTeam
# (0 行,完全干净)

$ grep -rn "accounts\.list_accounts" D:/Desktop/AutoTeam/src
# (0 行,完全干净)
```

✅ commit cf2f7d3 之后,**autoteam.accounts 这一侧的 list_accounts 引用已清零**。

### B.2 残留的 list_accounts 引用全部归属"邮箱 provider 子系统",与 issue 无关

| 文件 | 行号 | 上下文 | 是否 bug |
|---|---|---|---|
| `docs/mail-provider-design.md` | 68, 308 | 设计文档:邮箱 provider 抽象 `def list_accounts(self, size: int = 200)` | ❌ 无关(文档) |
| `src/autoteam/mail/base.py` | 177 | 邮箱 provider 抽象基类的方法签名 `def list_accounts(self, size: int = 200) -> list[dict]` | ❌ 无关(`MailProvider` 类方法) |
| `src/autoteam/mail/cf_temp_email.py` | 190 | `CfTempEmailClient.list_accounts(self, size=200)` | ❌ 无关(provider 实现) |
| `src/autoteam/mail/maillab.py` | 219 | `MaillabClient.list_accounts(self, size: int = 200)` | ❌ 无关(provider 实现) |
| `src/autoteam/mail/maillab.py` | 309, 322 | `for row in self.list_accounts(size=500)` 内部调用 | ❌ 无关 |
| `tests/unit/test_maillab.py` | 216, 236, 240, 253, 258, 279 | 测试 maillab provider 的 `list_accounts` 翻页/截断行为 | ❌ 无关(测试) |
| `CHANGELOG.md` | 11, 134 | 历史记录条目(本 issue 修复说明 + 旧版 maillab.list_accounts 上限补丁) | ❌ 无关(文档) |
| `prompts/issues1.md` | 77, 78 | 用户报告的报错原文 | ❌ 无关(报告) |

**结论**:`list_accounts` 这个名字在仓库里**只剩"邮箱 provider 的方法名"** — 跟 `autoteam.accounts` 模块没有任何 import 关系,**不会被 Python 解释器拿来跟 accounts 模块对接**。

### B.3 commit cf2f7d3 之前的现场还原(供对照)

`api.py:1942` 历史代码:
```python
# 修复前(已是历史)
from autoteam.accounts import STATUS_ACTIVE, STATUS_EXHAUSTED, list_accounts
                                                              ^^^^^^^^^^^^^
                                                              # ← 这里就是 typo

in_team_local = sum(1 for a in list_accounts() if a.get("status") in ...)
```

`api.py:1944` 当前代码(已修):
```python
from autoteam.accounts import STATUS_ACTIVE, STATUS_EXHAUSTED, load_accounts
in_team_local = sum(1 for a in load_accounts() if a.get("status") in ...)
```

git log 里 `cf2f7d3 fix(round-3)` 第一条就是这条修复,文件指纹一致。

---

## C. Docker 镜像 / 挂载现状

### C.1 Dockerfile 关键事实

```dockerfile
# 第 23-25 行
COPY src/ src/
COPY web/ web/
```

**源代码是 `COPY` 进镜像的 — 不是 volume 挂载**。

### C.2 docker-compose.yml 挂载策略

```yaml
volumes:
  - ./data:/app/data    # 仅挂数据目录
```

**没有挂 `./src:/app/src`** — 也就是说源代码改动**必须 rebuild image** 才会进入容器。

### C.3 docker-entrypoint.sh 软链规则

入口脚本只把 `/app/data/{.env, accounts.json, state.json, auths/, screenshots/}` 软链到工作目录,**不动源码**:
```bash
for f in .env accounts.json state.json; do
    [ -f "/app/data/$f" ] || touch "/app/data/$f"
    rm -f "/app/$f"
    ln -s "/app/data/$f" "/app/$f"
done
```

`/app/src/autoteam/api.py` 是 `COPY` 时刻被烤进镜像的快照,不会随宿主机文件变动。

### C.4 推断:用户的 docker 镜像还是旧版

既然 commit cf2f7d3 已落 git,但 11:04 容器内还在报同样的 `ImportError: cannot import name 'list_accounts' from 'autoteam.accounts'`,只剩三种可能:

1. **(99%)镜像未重建** — 源码改了,`docker build` 没重跑
2. **(<1%)镜像已建但容器未替换** — 老 container 没 stop/restart
3. **(0%)有别处也写了 typo** — 已经 grep 全仓库扫干净,不存在

---

## D. 推荐修复方案

### 方案 A:仅 docker rebuild(对应 99% 真因)

```bash
cd D:/Desktop/AutoTeam

# 1. 确认源码确实是新版
git log --oneline src/autoteam/api.py | head -3
git diff cf2f7d3~1 cf2f7d3 -- src/autoteam/api.py | head -20

# 2. 停掉旧容器
docker compose down

# 3. 强制重建镜像(--no-cache 防意外缓存命中)
docker compose build --no-cache autoteam

# 4. 重新启动
docker compose up -d

# 5. 进容器验证
docker compose exec autoteam python -c "from autoteam.accounts import load_accounts; print('OK', len(load_accounts()))"

# 6. 也可以同时确认 typo 已不在镜像里
docker compose exec autoteam grep -n "list_accounts" /app/src/autoteam/api.py || echo "干净"
```

**预期**:第 5 步打印 `OK <n>`,第 6 步打印 `干净`。

### 方案 B:在 accounts.py 加 `list_accounts = load_accounts` 别名(防御层 / 不推荐)

```python
# src/autoteam/accounts.py 末尾追加
list_accounts = load_accounts  # 向后兼容,允许 typo 不炸
```

**评估**:
- 优点:即便有人重新写错也不会 ImportError
- 缺点:**掩盖 typo,长期维护更糟** — 团队不知道哪个名字才是规范的;静态分析工具看到两个绑定也会困惑
- **不推荐**:typo 应该让 lint / CI 抓住,而不是用别名兜底

### 方案 C:CI / pre-commit 防回归(强烈推荐组合)

虽然这条 issue 当前位点没残留 typo,但同类 typo 容易复发(`list_accounts` 写起来比 `load_accounts` 更顺手)。建议在 `ruff.toml` 或 pre-commit 增加一条 grep 守卫:

```bash
# scripts/lint-no-list-accounts.sh
#!/usr/bin/env bash
set -e
if grep -rn "from autoteam\.accounts import.*list_accounts\b" src tests; then
    echo "Found typo: list_accounts (use load_accounts)"
    exit 1
fi
if grep -rn "autoteam\.accounts\.list_accounts\b" src tests; then
    echo "Found typo: autoteam.accounts.list_accounts (use load_accounts)"
    exit 1
fi
echo "no list_accounts typo"
```

接到 pre-commit / CI:任何人再写错立刻拒绝 commit。

### 方案对比

| 方案 | 解决根因 | 落地成本 | 推荐度 |
|---|---|---|---|
| **A** docker rebuild | ✅ 完全解决当前 issue | 低(2 条命令) | ★★★★★ **必须做** |
| **B** 别名兜底 | ❌ 治标不治本 | 极低 | ★ 不推荐 |
| **C** lint 防回归 | ✅ 防未来同类 typo | 低(1 个 shell) | ★★★★ **推荐配套** |

**建议组合**:**A + C** — 立刻把镜像重建,同时埋一道 lint 防线。

---

## E. 防回归测试要点

### E.1 单元测试层

确认 api.py 里所有 `from autoteam.accounts import ...` 路径都不再误用 `list_accounts`:

```python
# tests/unit/test_no_list_accounts_typo.py
import re
from pathlib import Path

SRC = Path(__file__).parent.parent.parent / "src"

def test_no_list_accounts_in_accounts_namespace():
    """list_accounts 是 mail provider 的方法名,不应在 autoteam.accounts 命名空间出现"""
    pattern = re.compile(
        r"from\s+autoteam\.accounts\s+import[^#\n]*\blist_accounts\b"
        r"|autoteam\.accounts\.list_accounts\b"
    )
    bad = []
    for p in SRC.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                bad.append(f"{p}:{i}: {line.strip()}")
    assert not bad, "list_accounts typo:\n" + "\n".join(bad)
```

### E.2 集成测试层

加一条对"生成免费号"按钮(`POST /api/tasks/fill {leave_workspace: true}`)的最小冒烟:

```python
# tests/integration/test_post_fill_no_import_error.py
def test_post_fill_personal_does_not_raise_import_error(client, monkeypatch):
    # 桩掉 cmd_fill 避免真的开 Playwright,只校验 import 阶段不炸
    from autoteam import manager
    monkeypatch.setattr(manager, "cmd_fill", lambda **kw: [])
    monkeypatch.setattr(manager, "TEAM_SUB_ACCOUNT_HARD_CAP", 999)

    resp = client.post("/api/tasks/fill", json={"leave_workspace": True})
    # 关键:不能是 500(ImportError 会变 500)
    assert resp.status_code in (202, 409)
```

### E.3 部署测试层

`docker-entrypoint.sh` 启动末端加一行 self-check,把 typo 扼杀在容器启动期:

```bash
# 末尾追加
python -c "from autoteam.accounts import load_accounts" || {
    echo "FATAL: autoteam.accounts.load_accounts import failed"
    exit 1
}

exec uv run autoteam "$@"
```

如此一来:任何 typo 类 ImportError 在 `docker compose up` 时就**立刻 crash-loop**,不会再让用户在 Web 面板按按钮才发现。

### E.4 镜像版本指纹

建议在 Dockerfile 加一行 `LABEL org.opencontainers.image.revision=<git-sha>`,便于排查"当前镜像到底是哪个 commit":

```dockerfile
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA
ENV AUTOTEAM_GIT_SHA=$GIT_SHA
```

构建命令:
```bash
docker compose build --build-arg GIT_SHA=$(git rev-parse --short HEAD)
```

容器内可通过 `echo $AUTOTEAM_GIT_SHA` 立即知道是哪个 commit 烤的镜像 — 这次 issue#3 排错时就能直接看到镜像 SHA 是不是 cf2f7d3 之前的版本。

---

## 总结

| 维度 | 结论 |
|---|---|
| 源代码层是否还残留 typo? | ❌ 不残留(commit cf2f7d3 已修干净) |
| 是否需要在 accounts.py 加别名? | ❌ 不需要(掩盖 typo,反而坏事) |
| 真因? | **Docker 镜像未重建** — 用户 11:04 看到的容器还在跑旧 image |
| 立刻该做? | `docker compose down && docker compose build --no-cache && docker compose up -d` |
| 长期该做? | 加 lint 守卫 + entrypoint 启动期 self-check + 镜像 git-sha 标签 |
