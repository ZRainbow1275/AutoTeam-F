"""Round 7 P2 + Round 6 deferred 8 项修复的单元测试。

对应 PRD-6 §5/§7 的 8 项 FR:
  - FR-P2.1 preferred_seat_type chatgpt 别名归一化
  - FR-P2.4 FastAPI lifespan 替代 @app.on_event
  - FR-P2.5 raw_rate_limit 落 record_failure
  - FR-D6   24h 去重 cheap_codex_smoke
  - FR-D7   web/src/api.js parseTaskError 关键字解析(grep + JS 字符串验证)
  - FR-P2.2 web/src/components/MailProviderCard.vue 抽组件(grep + 文件存在)
  - FR-P2.3 test_plan_type_whitelist.py 文件拆分(grep)
  - FR-D8   account-state-machine.md v1.1 与代码 STATUS_AUTH_INVALID/cheap_codex_smoke 一致(grep)
"""

import json
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# FR-P2.1 — preferred_seat_type chatgpt 别名归一化
# ---------------------------------------------------------------------------


class TestPreferredSeatTypeNamingMigration:
    def test_chatgpt_alias_normalized_to_default(self):
        from autoteam.runtime_config import _normalize_preferred_seat_type
        assert _normalize_preferred_seat_type("chatgpt") == "default"

    def test_default_value_passthrough(self):
        from autoteam.runtime_config import _normalize_preferred_seat_type
        assert _normalize_preferred_seat_type("default") == "default"

    def test_codex_value_passthrough(self):
        from autoteam.runtime_config import _normalize_preferred_seat_type
        assert _normalize_preferred_seat_type("codex") == "codex"

    def test_invalid_value_falls_back_to_default(self):
        from autoteam.runtime_config import _normalize_preferred_seat_type
        assert _normalize_preferred_seat_type("garbage") == "default"

    def test_empty_and_none_fall_back(self):
        from autoteam.runtime_config import _normalize_preferred_seat_type
        assert _normalize_preferred_seat_type("") == "default"
        assert _normalize_preferred_seat_type(None) == "default"

    def test_uppercase_chatgpt_normalized(self):
        from autoteam.runtime_config import _normalize_preferred_seat_type
        assert _normalize_preferred_seat_type("CHATGPT") == "default"
        assert _normalize_preferred_seat_type("  ChatGPT  ") == "default"

    def test_set_then_get_chatgpt_returns_default(self, tmp_path, monkeypatch):
        # 用 tmp_path 隔离 runtime_config.json,避免污染真实配置
        import autoteam.runtime_config as rc
        fake_file = tmp_path / "runtime_config.json"
        monkeypatch.setattr(rc, "RUNTIME_CONFIG_FILE", fake_file)
        rc.set_preferred_seat_type("chatgpt")
        assert rc.get_preferred_seat_type() == "default"

    def test_legacy_chatgpt_on_disk_normalized_on_read(self, tmp_path, monkeypatch):
        # 模拟旧版本落盘了 chatgpt 字面量,读时也得归一化为 default
        import autoteam.runtime_config as rc
        fake_file = tmp_path / "runtime_config.json"
        fake_file.write_text(json.dumps({"preferred_seat_type": "chatgpt"}), encoding="utf-8")
        monkeypatch.setattr(rc, "RUNTIME_CONFIG_FILE", fake_file)
        assert rc.get_preferred_seat_type() == "default"


# ---------------------------------------------------------------------------
# FR-P2.4 — FastAPI lifespan 替代 @app.on_event
# ---------------------------------------------------------------------------


class TestFastApiLifespanIntegration:
    def test_app_has_lifespan_attached(self):
        from autoteam.api import app
        # FastAPI 内部把 lifespan 包到 router.lifespan_context
        assert getattr(app.router, "lifespan_context", None) is not None

    def test_app_lifespan_is_asynccontext(self):
        import inspect
        from autoteam.api import app_lifespan
        # asynccontextmanager 装饰器返回的对象有 __wrapped__,且原函数是 async generator
        wrapped = getattr(app_lifespan, "__wrapped__", None)
        assert wrapped is not None
        assert inspect.isasyncgenfunction(wrapped)

    def test_no_legacy_on_event_decorators(self):
        import re
        api_path = PROJECT_ROOT / "src" / "autoteam" / "api.py"
        content = api_path.read_text(encoding="utf-8")
        # 只匹配真正的装饰器调用(行首可能有缩进 + @app.on_event("...")),不命中注释
        decorator_lines = re.findall(r'^[ \t]*@app\.on_event\(', content, re.MULTILINE)
        assert decorator_lines == [], (
            f"FR-P2.4 要求清除所有 @app.on_event 装饰器,改用 lifespan,残留: {decorator_lines}"
        )

    def test_startup_via_testclient(self):
        from fastapi.testclient import TestClient
        from autoteam.api import app
        client = TestClient(app)
        with client:
            r = client.get("/api/version")
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# FR-P2.5 — raw_rate_limit 落 record_failure
# ---------------------------------------------------------------------------


class TestRawRateLimitInRecordFailure:
    def test_extract_from_ok_quota_info(self):
        from autoteam.manager import _extract_raw_rate_limit_str
        ok_qi = {
            "primary_pct": 0,
            "raw_rate_limit": {"primary_window": {"limit": 100}, "limit_reached": False},
        }
        s = _extract_raw_rate_limit_str(ok_qi)
        assert s and "primary_window" in s
        # 是合法 JSON
        parsed = json.loads(s)
        assert parsed["primary_window"]["limit"] == 100

    def test_extract_from_exhausted_info_nested(self):
        from autoteam.manager import _extract_raw_rate_limit_str
        ex = {
            "window": "primary",
            "quota_info": {"primary_pct": 100, "raw_rate_limit": {"foo": "bar"}},
        }
        s = _extract_raw_rate_limit_str(ex)
        assert json.loads(s) == {"foo": "bar"}

    def test_extract_returns_empty_on_missing(self):
        from autoteam.manager import _extract_raw_rate_limit_str
        assert _extract_raw_rate_limit_str({}) == ""
        assert _extract_raw_rate_limit_str(None) == ""

    def test_extract_truncates_to_2000_chars(self):
        from autoteam.manager import _extract_raw_rate_limit_str
        big = {"raw_rate_limit": {"k": "x" * 5000}}
        out = _extract_raw_rate_limit_str(big)
        assert 0 < len(out) <= 2000

    def test_no_quota_call_sites_pass_raw_rate_limit(self):
        # grep manager.py 确认 3 处 record_failure(no_quota_assigned, ...) 均带 raw_rate_limit kwarg
        manager_path = PROJECT_ROOT / "src" / "autoteam" / "manager.py"
        content = manager_path.read_text(encoding="utf-8")
        # 3 处:run_post_register_oauth_personal / run_post_register_oauth_team / reinvite_account
        assert content.count("raw_rate_limit=_extract_raw_rate_limit_str(") >= 3


# ---------------------------------------------------------------------------
# FR-D6 — 24h 去重 cheap_codex_smoke
# ---------------------------------------------------------------------------


class TestCodexSmoke24hDedup:
    def test_cache_helpers_handle_none_account_id(self):
        from autoteam.codex_auth import _read_codex_smoke_cache, _write_codex_smoke_cache
        assert _read_codex_smoke_cache(None) is None
        # write 不应抛异常
        _write_codex_smoke_cache(None, "alive")

    def test_dedup_seconds_is_24h(self):
        from autoteam.codex_auth import _CODEX_SMOKE_DEDUP_SECONDS
        assert _CODEX_SMOKE_DEDUP_SECONDS == 86400

    def test_cache_hit_skips_network(self):
        # mock _read_codex_smoke_cache 返回新鲜 cache → cheap_codex_smoke 不应调网络
        import autoteam.codex_auth as ca
        import time as _time

        fresh = (_time.time() - 60, "alive")
        with patch.object(ca, "_read_codex_smoke_cache", return_value=fresh) as m_read, \
             patch.object(ca, "_cheap_codex_smoke_network") as m_net:
            result, detail = ca.cheap_codex_smoke("tok_x", account_id="acc_test")
        assert result == "alive"
        assert detail == "cache_hit_alive"
        m_read.assert_called_once_with("acc_test")
        m_net.assert_not_called()

    def test_cache_miss_calls_network_and_writes_back(self):
        import autoteam.codex_auth as ca
        with patch.object(ca, "_read_codex_smoke_cache", return_value=None) as m_read, \
             patch.object(ca, "_cheap_codex_smoke_network", return_value=("alive", None)) as m_net, \
             patch.object(ca, "_write_codex_smoke_cache") as m_write:
            result, detail = ca.cheap_codex_smoke("tok_y", account_id="acc_miss")
        assert result == "alive"
        assert detail is None
        m_read.assert_called_once()
        m_net.assert_called_once()
        m_write.assert_called_once_with("acc_miss", "alive")

    def test_cache_expiry_after_24h_calls_network(self):
        import autoteam.codex_auth as ca
        import time as _time

        stale = (_time.time() - 86401, "alive")  # 24h+1s 前
        with patch.object(ca, "_read_codex_smoke_cache", return_value=stale), \
             patch.object(ca, "_cheap_codex_smoke_network", return_value=("alive", None)) as m_net:
            result, _ = ca.cheap_codex_smoke("tok_z", account_id="acc_stale")
        assert result == "alive"
        m_net.assert_called_once()

    def test_force_bypasses_cache(self):
        import autoteam.codex_auth as ca
        import time as _time

        fresh = (_time.time() - 60, "alive")
        with patch.object(ca, "_read_codex_smoke_cache", return_value=fresh) as m_read, \
             patch.object(ca, "_cheap_codex_smoke_network", return_value=("alive", None)) as m_net:
            result, _ = ca.cheap_codex_smoke("tok_f", account_id="acc_force", force=True)
        assert result == "alive"
        # force=True 跳过 cache,直接走网络
        m_read.assert_not_called()
        m_net.assert_called_once()


# ---------------------------------------------------------------------------
# FR-D7 — 前端 api.js parseTaskError 关键字解析
# ---------------------------------------------------------------------------


class TestApiJs409Parser:
    """JS 端单测在仓库无 jest 时改成 grep 验证 + 简单字符串验证。"""

    def test_api_js_exports_parse_task_error(self):
        api_js = PROJECT_ROOT / "web" / "src" / "api.js"
        content = api_js.read_text(encoding="utf-8")
        assert "export function parseTaskError" in content
        assert "phone_required" in content
        assert "register_blocked" in content

    def test_api_js_returns_category_object(self):
        api_js = PROJECT_ROOT / "web" / "src" / "api.js"
        content = api_js.read_text(encoding="utf-8")
        assert "category: 'phone_required'" in content
        assert "category: 'register_blocked'" in content
        assert "category: 'generic'" in content


# ---------------------------------------------------------------------------
# FR-P2.2 — MailProviderCard.vue 抽组件
# ---------------------------------------------------------------------------


class TestMailProviderCardComponent:
    def test_component_file_exists(self):
        comp = PROJECT_ROOT / "web" / "src" / "components" / "MailProviderCard.vue"
        assert comp.is_file()

    def test_setuppage_imports_mailprovidercard(self):
        sp = PROJECT_ROOT / "web" / "src" / "components" / "SetupPage.vue"
        content = sp.read_text(encoding="utf-8")
        assert "import MailProviderCard from './MailProviderCard.vue'" in content
        assert "<MailProviderCard" in content

    def test_settings_imports_mailprovidercard(self):
        s = PROJECT_ROOT / "web" / "src" / "components" / "Settings.vue"
        content = s.read_text(encoding="utf-8")
        assert "import MailProviderCard from './MailProviderCard.vue'" in content
        assert "<MailProviderCard" in content

    def test_setuppage_no_inline_test_connection(self):
        sp = PROJECT_ROOT / "web" / "src" / "components" / "SetupPage.vue"
        content = sp.read_text(encoding="utf-8")
        # inline testConnection / verifyDomain 函数已删除(方法名不再出现)
        assert "async function testConnection" not in content
        assert "async function verifyDomain" not in content

    def test_settings_no_inline_test_connection(self):
        s = PROJECT_ROOT / "web" / "src" / "components" / "Settings.vue"
        content = s.read_text(encoding="utf-8")
        assert "async function mailTestConnection" not in content
        assert "async function mailVerifyDomain" not in content


# ---------------------------------------------------------------------------
# FR-P2.3 — test_plan_type_whitelist.py 拆分
# ---------------------------------------------------------------------------


class TestPlanTypeWhitelistFileExists:
    def test_dedicated_test_file_present(self):
        f = PROJECT_ROOT / "tests" / "unit" / "test_plan_type_whitelist.py"
        assert f.is_file()

    def test_lifecycle_test_no_longer_holds_plan_type_whitelist(self):
        f = PROJECT_ROOT / "tests" / "unit" / "test_spec2_lifecycle.py"
        content = f.read_text(encoding="utf-8")
        # 共因 A 区已删除,关键 case 名/parametrize 不应再出现
        assert "test_supported_plan_types_constant_is_frozenset_with_4_entries" not in content
        assert "test_normalize_plan_type" not in content
        assert "test_is_supported_plan_whitelist_only" not in content


# ---------------------------------------------------------------------------
# FR-D8 — account-state-machine.md v1.1 与现实代码一致
# ---------------------------------------------------------------------------


class TestStateMachineDocV11Consistent:
    def test_doc_marks_v1_1(self):
        doc = PROJECT_ROOT / "prompts" / "0426" / "spec" / "shared" / "account-state-machine.md"
        content = doc.read_text(encoding="utf-8")
        assert "v1.1" in content

    def test_doc_mentions_uninitialized_seat_intermediate(self):
        doc = PROJECT_ROOT / "prompts" / "0426" / "spec" / "shared" / "account-state-machine.md"
        content = doc.read_text(encoding="utf-8")
        assert "uninitialized_seat" in content

    def test_doc_mentions_cheap_codex_smoke_24h_cache(self):
        doc = PROJECT_ROOT / "prompts" / "0426" / "spec" / "shared" / "account-state-machine.md"
        content = doc.read_text(encoding="utf-8")
        assert "cheap_codex_smoke" in content

    def test_code_has_status_auth_invalid_transitions(self):
        # SPEC v1.1 §3 表格里每个 STATUS_AUTH_INVALID 转移都应在代码里能 grep 到
        manager_py = PROJECT_ROOT / "src" / "autoteam" / "manager.py"
        content = manager_py.read_text(encoding="utf-8")
        assert content.count("STATUS_AUTH_INVALID") >= 5

    def test_code_has_cheap_codex_smoke_call(self):
        codex_auth = PROJECT_ROOT / "src" / "autoteam" / "codex_auth.py"
        content = codex_auth.read_text(encoding="utf-8")
        assert "cheap_codex_smoke" in content
        assert "_read_codex_smoke_cache" in content
        assert "_write_codex_smoke_cache" in content
