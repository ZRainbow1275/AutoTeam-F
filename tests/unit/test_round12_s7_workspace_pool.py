"""Round 12 S7 — multi team workspace pool unit tests.

Goals:
- 注册 / 切换 / 故障阈值 / 自动 promote
- 原子写 + .bak 回滚
- 单 workspace 模式 backwards-compat (admin_state.json seed)
- master_health 失败信号 → pool failover 集成
- admin_state 路由 (active workspace > state.json fallback)

Mock-only — 不触达真实 OpenAI workspace API。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from autoteam.workspace_pool import (
    SCHEMA_VERSION,
    STATUS_HEALTHY,
    STATUS_UNHEALTHY,
    STATUS_UNKNOWN,
    TIER_ACTIVE,
    TIER_COLD,
    TIER_WARM,
    WorkspacePool,
)

UUID_A = "00000000-0000-0000-0000-00000000aaaa"
UUID_B = "00000000-0000-0000-0000-00000000bbbb"
UUID_C = "00000000-0000-0000-0000-00000000cccc"


@pytest.fixture
def pool_path(tmp_path: Path) -> Path:
    return tmp_path / "workspaces.json"


@pytest.fixture(autouse=True)
def _isolate_admin_state(monkeypatch):
    """Default: seed from admin_state returns empty so tests don't pick up the
    real on-disk state.json. Tests that exercise the seed path override this
    explicitly via their own monkeypatch."""
    monkeypatch.setattr(
        "autoteam.admin_state.load_admin_state",
        lambda: {"email": "", "account_id": "", "session_token": ""},
    )


@pytest.fixture
def pool(pool_path: Path) -> WorkspacePool:
    return WorkspacePool(path=pool_path, fail_threshold=3)


# ---------------------------------------------------------------------------
# 1. register / get_active basics
# ---------------------------------------------------------------------------
def test_register_first_auto_promotes_to_active(pool: WorkspacePool):
    snap = pool.register("ws-a", "ad1@example.com", UUID_A, tier=TIER_WARM)
    # supplied tier was warm but pool was empty → auto-promote to active
    assert snap["tier"] == TIER_ACTIVE
    assert pool.get_active()["id"] == "ws-a"
    assert len(pool.list_all()) == 1


def test_register_second_keeps_warm(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    snap = pool.register("ws-b", "ad2@example.com", UUID_B, tier=TIER_WARM)
    assert snap["tier"] == TIER_WARM
    assert pool.get_active()["id"] == "ws-a"


def test_register_active_demotes_prior_active(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.register("ws-b", "ad2@example.com", UUID_B, tier=TIER_ACTIVE)
    # only one active at a time
    actives = [r for r in pool.list_all() if r["tier"] == TIER_ACTIVE]
    assert len(actives) == 1
    assert actives[0]["id"] == "ws-b"
    # previous active is demoted to cold
    a = pool.get("ws-a")
    assert a["tier"] == TIER_COLD


def test_register_duplicate_id_raises(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    with pytest.raises(ValueError):
        pool.register("ws-a", "ad1b@example.com", UUID_B)


def test_register_invalid_tier_raises(pool: WorkspacePool):
    with pytest.raises(ValueError):
        pool.register("ws-a", "ad1@example.com", UUID_A, tier="hot")


# ---------------------------------------------------------------------------
# 2. set_active / list_all
# ---------------------------------------------------------------------------
def test_set_active_swaps_and_demotes(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.register("ws-b", "ad2@example.com", UUID_B, tier=TIER_WARM)
    pool.set_active("ws-b")
    assert pool.get_active()["id"] == "ws-b"
    assert pool.get("ws-a")["tier"] == TIER_COLD
    assert pool.get("ws-b")["tier"] == TIER_ACTIVE


def test_set_active_unknown_id_raises(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    with pytest.raises(KeyError):
        pool.set_active("ws-zzz")


def test_set_active_idempotent_when_already_active(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    snap = pool.set_active("ws-a")
    assert snap["tier"] == TIER_ACTIVE


# ---------------------------------------------------------------------------
# 3. mark_unhealthy + failover
# ---------------------------------------------------------------------------
def test_mark_unhealthy_below_threshold_no_failover(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.register("ws-b", "ad2@example.com", UUID_B, tier=TIER_WARM)
    snap = pool.mark_unhealthy("ws-a", "subscription_cancelled")
    # only 1 fail (threshold=3) → no failover, ws-a still active
    assert snap["id"] == "ws-a"
    a = pool.get("ws-a")
    assert a["fail_count"] == 1
    assert a["status"] == STATUS_UNHEALTHY


def test_mark_unhealthy_at_threshold_triggers_failover(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.register("ws-b", "ad2@example.com", UUID_B, tier=TIER_WARM)
    pool.mark_unhealthy("ws-a", "auth_invalid")
    pool.mark_unhealthy("ws-a", "auth_invalid")
    snap = pool.mark_unhealthy("ws-a", "auth_invalid")
    # 3rd fail at threshold=3 → failover to ws-b
    assert snap is not None
    assert snap["id"] == "ws-b"
    assert pool.get_active()["id"] == "ws-b"
    a = pool.get("ws-a")
    assert a["tier"] == TIER_COLD


def test_mark_unhealthy_no_warm_no_failover_candidate(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    # only ws-a (active), no warm/cold candidate
    pool.set_fail_threshold(1)
    snap = pool.mark_unhealthy("ws-a", "auth_invalid")
    # no candidate → active becomes None
    assert snap is None
    assert pool.get_active() is None
    a = pool.get("ws-a")
    assert a["tier"] == TIER_COLD


def test_mark_unhealthy_force_failover_immediate(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.register("ws-b", "ad2@example.com", UUID_B, tier=TIER_WARM)
    snap = pool.mark_unhealthy("ws-a", "manual_force", force_failover=True)
    assert snap["id"] == "ws-b"


def test_failover_picks_oldest_warm_first(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.register("ws-b", "ad2@example.com", UUID_B, tier=TIER_WARM)
    pool.register("ws-c", "ad3@example.com", UUID_C, tier=TIER_WARM)
    # Set deterministic registered_at: ws-b oldest warm, ws-c younger
    raw = json.loads(pool.path.read_text(encoding="utf-8"))
    for row in raw["workspaces"]:
        if row["id"] == "ws-b":
            row["registered_at"] = 1000.0
        elif row["id"] == "ws-c":
            row["registered_at"] = 2000.0
    pool.path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    pool.set_fail_threshold(1)
    snap = pool.mark_unhealthy("ws-a", "auth_invalid")
    assert snap["id"] == "ws-b"  # oldest warm wins


def test_mark_healthy_resets_fail_count(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.mark_unhealthy("ws-a", "subscription_cancelled")
    pool.mark_unhealthy("ws-a", "subscription_cancelled")
    pool.mark_healthy("ws-a")
    a = pool.get("ws-a")
    assert a["fail_count"] == 0
    assert a["status"] == STATUS_HEALTHY


# ---------------------------------------------------------------------------
# 4. transition_log
# ---------------------------------------------------------------------------
def test_transition_log_register_promote_demote(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.register("ws-b", "ad2@example.com", UUID_B, tier=TIER_WARM)
    pool.set_active("ws-b")
    a = pool.get("ws-a")
    reasons_a = [e["reason"] for e in a["transition_log"]]
    assert "register" in reasons_a
    assert "demote_on_set_active" in reasons_a
    b = pool.get("ws-b")
    reasons_b = [e["reason"] for e in b["transition_log"]]
    assert "register" in reasons_b
    assert "set_active" in reasons_b


def test_transition_log_failover_writes_two_rows(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.register("ws-b", "ad2@example.com", UUID_B, tier=TIER_WARM)
    pool.set_fail_threshold(1)
    pool.mark_unhealthy("ws-a", "auth_invalid")
    a = pool.get("ws-a")
    b = pool.get("ws-b")
    assert any(e["reason"] == "failover_demote" for e in a["transition_log"])
    assert any(e["reason"] == "failover_promote" for e in b["transition_log"])


# ---------------------------------------------------------------------------
# 5. atomic write + .bak rollback (I3)
# ---------------------------------------------------------------------------
def test_atomic_write_replaces_file(pool: WorkspacePool):
    pool.register("ws-a", "ad1@example.com", UUID_A)
    raw = json.loads(pool.path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == SCHEMA_VERSION
    assert raw["active"] == "ws-a"
    # no leftover .tmp / .bak
    assert not pool.path.with_suffix(pool.path.suffix + ".tmp").exists()
    assert not pool.path.with_suffix(pool.path.suffix + ".bak").exists()


def test_atomic_write_rolls_back_on_os_replace_failure(pool_path: Path):
    pool = WorkspacePool(path=pool_path)
    pool.register("ws-a", "ad1@example.com", UUID_A)
    original = pool_path.read_text(encoding="utf-8")

    real_replace = os.replace

    def boom(src, dst):
        if str(dst) == str(pool_path):
            raise OSError("simulated mid-write failure")
        return real_replace(src, dst)

    with patch("autoteam.workspace_pool.os.replace", side_effect=boom):
        with pytest.raises(OSError):
            pool.register("ws-b", "ad2@example.com", UUID_B)

    # original content preserved (rollback restored .bak)
    assert pool_path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# 6. backwards-compat seed from admin_state.json (I4)
# ---------------------------------------------------------------------------
def test_seed_from_admin_state_when_pool_missing(monkeypatch, pool_path: Path):
    fake_state = {
        "email": "legacy_admin@example.com",
        "account_id": UUID_A,
        "session_token": "tok",
    }
    monkeypatch.setattr("autoteam.admin_state.load_admin_state", lambda: fake_state)
    pool = WorkspacePool(path=pool_path)
    active = pool.get_active()
    assert active is not None
    assert active["admin_email"] == "legacy_admin@example.com"
    assert active["account_id"] == UUID_A
    assert active["tier"] == TIER_ACTIVE


def test_seed_skipped_when_state_empty(monkeypatch, pool_path: Path):
    monkeypatch.setattr("autoteam.admin_state.load_admin_state", lambda: {"email": "", "account_id": ""})
    pool = WorkspacePool(path=pool_path)
    assert pool.get_active() is None
    assert pool.list_all() == []


# ---------------------------------------------------------------------------
# 7. admin_state routing
# ---------------------------------------------------------------------------
def test_admin_state_get_admin_email_routes_to_pool(monkeypatch, pool_path: Path):
    pool = WorkspacePool(path=pool_path)
    pool.register("ws-x", "pool_admin@example.com", UUID_A)
    monkeypatch.setattr("autoteam.workspace_pool.default_pool", pool)
    # state.json should NOT be consulted when pool active
    monkeypatch.setattr("autoteam.admin_state.load_admin_state",
                        lambda: {"email": "stale@example.com", "account_id": ""})
    from autoteam.admin_state import get_admin_email, get_chatgpt_account_id
    assert get_admin_email() == "pool_admin@example.com"
    assert get_chatgpt_account_id() == UUID_A


def test_admin_state_falls_back_to_state_json_when_pool_empty(monkeypatch, pool_path: Path):
    pool = WorkspacePool(path=pool_path)
    # empty pool (no register, and state seed returns empty)
    monkeypatch.setattr("autoteam.admin_state.load_admin_state",
                        lambda: {"email": "fallback@example.com", "account_id": UUID_B})
    monkeypatch.setattr("autoteam.workspace_pool.default_pool", pool)
    # also: pool.get_active will seed from state, so make seed return empty
    monkeypatch.setattr(
        WorkspacePool, "_seed_from_admin_state",
        lambda self: {"schema_version": SCHEMA_VERSION, "active": None, "workspaces": []},
    )
    from autoteam.admin_state import get_admin_email, get_chatgpt_account_id
    assert get_admin_email() == "fallback@example.com"
    assert get_chatgpt_account_id() == UUID_B


# ---------------------------------------------------------------------------
# 8. master_health pool integration
# ---------------------------------------------------------------------------
def test_apply_pool_health_signal_healthy_marks_healthy(monkeypatch, pool_path: Path):
    pool = WorkspacePool(path=pool_path)
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.mark_unhealthy("ws-a", "subscription_cancelled")  # fail_count=1
    from autoteam.master_health import apply_pool_health_signal
    snap = apply_pool_health_signal(True, "active", pool=pool)
    assert snap is not None
    a = pool.get("ws-a")
    assert a["fail_count"] == 0
    assert a["status"] == STATUS_HEALTHY


def test_apply_pool_health_signal_subscription_cancelled_drives_failover(pool_path: Path):
    pool = WorkspacePool(path=pool_path, fail_threshold=2)
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.register("ws-b", "ad2@example.com", UUID_B, tier=TIER_WARM)
    from autoteam.master_health import apply_pool_health_signal
    apply_pool_health_signal(False, "subscription_cancelled", pool=pool)
    snap = apply_pool_health_signal(False, "subscription_cancelled", pool=pool)
    # threshold=2 reached → failover to ws-b
    assert snap is not None
    assert snap["id"] == "ws-b"


def test_apply_pool_health_signal_network_error_no_failover(pool_path: Path):
    pool = WorkspacePool(path=pool_path, fail_threshold=1)
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.register("ws-b", "ad2@example.com", UUID_B, tier=TIER_WARM)
    from autoteam.master_health import apply_pool_health_signal
    snap = apply_pool_health_signal(False, "network_error", pool=pool)
    # network_error is transient — don't trip
    assert snap["id"] == "ws-a"
    a = pool.get("ws-a")
    assert a["fail_count"] == 0  # untouched


def test_apply_pool_health_signal_empty_pool_returns_none(pool_path: Path):
    pool = WorkspacePool(path=pool_path)
    from autoteam.master_health import apply_pool_health_signal
    assert apply_pool_health_signal(False, "auth_invalid", pool=pool) is None


# ---------------------------------------------------------------------------
# 9. event subscribers
# ---------------------------------------------------------------------------
def test_subscriber_receives_register_event(pool: WorkspacePool):
    events: list[dict] = []
    pool.subscribe(events.append)
    pool.register("ws-a", "ad1@example.com", UUID_A)
    types = [e["type"] for e in events]
    assert "registered" in types


def test_subscriber_receives_failover_promote_event(pool: WorkspacePool):
    events: list[dict] = []
    pool.subscribe(events.append)
    pool.register("ws-a", "ad1@example.com", UUID_A)
    pool.register("ws-b", "ad2@example.com", UUID_B, tier=TIER_WARM)
    pool.set_fail_threshold(1)
    pool.mark_unhealthy("ws-a", "auth_invalid")
    types = [e["type"] for e in events]
    assert "promoted" in types
    assert "marked_unhealthy" in types


def test_subscriber_exceptions_swallowed(pool: WorkspacePool):
    def bad(_e):
        raise RuntimeError("boom")
    pool.subscribe(bad)
    # must not raise
    pool.register("ws-a", "ad1@example.com", UUID_A)


# ---------------------------------------------------------------------------
# 10. concurrency safety (lock)
# ---------------------------------------------------------------------------
def test_concurrent_registers_no_duplicate_active(pool: WorkspacePool):
    barrier = threading.Barrier(5)
    errors: list[Exception] = []

    def worker(i):
        try:
            barrier.wait(timeout=2)
            pool.register(f"ws-{i}", f"ad{i}@example.com", f"00000000-0000-0000-0000-{i:012d}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    actives = [r for r in pool.list_all() if r["tier"] == TIER_ACTIVE]
    assert len(actives) == 1  # I1 — exactly one active


# ---------------------------------------------------------------------------
# 11. schema migration / corrupt-file resilience
# ---------------------------------------------------------------------------
def test_corrupt_json_treated_as_empty(pool_path: Path):
    pool_path.write_text("{not valid json", encoding="utf-8")
    pool = WorkspacePool(path=pool_path)
    assert pool.list_all() == []
    # subsequent register works (file is overwritten cleanly)
    pool.register("ws-a", "ad1@example.com", UUID_A)
    assert pool.get_active()["id"] == "ws-a"


def test_unknown_schema_version_discarded(pool_path: Path):
    pool_path.write_text(
        json.dumps({"schema_version": 999, "active": "ws-x", "workspaces": []}),
        encoding="utf-8",
    )
    pool = WorkspacePool(path=pool_path)
    assert pool.get_active() is None


def test_normalize_drops_invalid_tier(pool_path: Path):
    pool_path.write_text(
        json.dumps({
            "schema_version": SCHEMA_VERSION,
            "active": "ws-x",
            "workspaces": [{
                "id": "ws-x",
                "admin_email": "a@e.com",
                "account_id": UUID_A,
                "tier": "hot",  # invalid
                "status": "weird",  # invalid
                "fail_count": "not-an-int",
                "transition_log": "not-a-list",
            }],
        }),
        encoding="utf-8",
    )
    pool = WorkspacePool(path=pool_path)
    rows = pool.list_all()
    assert len(rows) == 1
    assert rows[0]["tier"] == TIER_WARM
    assert rows[0]["status"] == STATUS_UNKNOWN
    assert rows[0]["fail_count"] == 0
    assert rows[0]["transition_log"] == []


# ---------------------------------------------------------------------------
# 12. cpa_sync helper
# ---------------------------------------------------------------------------
def test_cpa_sync_get_active_sync_target_returns_pool_summary(monkeypatch, pool_path: Path):
    pool = WorkspacePool(path=pool_path)
    pool.register("ws-a", "cpa_admin@example.com", UUID_A)
    monkeypatch.setattr("autoteam.workspace_pool.default_pool", pool)
    from autoteam.cpa_sync import get_active_sync_target
    target = get_active_sync_target()
    assert target == {"id": "ws-a", "admin_email": "cpa_admin@example.com", "account_id": UUID_A}


def test_cpa_sync_get_active_sync_target_none_when_empty(monkeypatch, pool_path: Path):
    pool = WorkspacePool(path=pool_path)
    monkeypatch.setattr("autoteam.workspace_pool.default_pool", pool)
    monkeypatch.setattr(
        WorkspacePool, "_seed_from_admin_state",
        lambda self: {"schema_version": SCHEMA_VERSION, "active": None, "workspaces": []},
    )
    from autoteam.cpa_sync import get_active_sync_target
    assert get_active_sync_target() is None
