"""Multi team workspace pool — Round 12 S7。

维持 K 个 team workspace 互为冷备:任意时刻仅 1 个 active(主热路径用),
其余作 warm/cold 备份;主 workspace 连续探测失败超阈值时秒切。

不变量(I1~I7,详见 .trellis/tasks/05-11-s7-multi-workspace-pool/prd.md):

- I1: workspaces[*].tier == "active" 至多 1 个
- I2: mark_unhealthy 命中 active 且 fail_count >= threshold → 自动 promote warm/cold
- I3: 写盘原子(snapshot .bak → tmp → os.replace),失败回滚
- I4: 单 workspace 模式向后兼容 — workspaces.json 不存在时 seed 自 state.json
- I5: register / set_active / mark_unhealthy / promote 均追加 transition_log
- I6: 与 default_machine 风格一致(lock 内修改、lock 外发布事件)
- I7: 健康检查 hot-path(`mark_unhealthy` / `mark_healthy`)在收到非法
       workspace_id 时**抛 KeyError**;`register` 在重复 / 非法 tier 时
       **抛 ValueError**. 这与 master_health.py M-I1 的语义对齐 ——
       输入验证错误抛(快失败,便于调用方排查),probe / 重试 hot-path
       通过 `apply_pool_health_signal()` 用 broad try/except 包裹 raise,
       不会让异常蔓延到顶层流程. 永远不会因为正常运行时状态(读盘失败 /
       schema 漂移 / 并发写覆盖)抛异常.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_POOL_FILE = PROJECT_ROOT / "workspaces.json"
POOL_FILE_MODE = 0o666

SCHEMA_VERSION = 1

TIER_ACTIVE = "active"
TIER_WARM = "warm"
TIER_COLD = "cold"
_VALID_TIERS = (TIER_ACTIVE, TIER_WARM, TIER_COLD)

STATUS_HEALTHY = "healthy"
STATUS_UNHEALTHY = "unhealthy"
STATUS_UNKNOWN = "unknown"
_VALID_STATUSES = (STATUS_HEALTHY, STATUS_UNHEALTHY, STATUS_UNKNOWN)


def _default_fail_threshold() -> int:
    raw = os.environ.get("MASTER_HEALTH_FAIL_THRESHOLD", "3")
    try:
        v = int(raw)
        return v if v > 0 else 3
    except Exception:
        return 3


PoolSubscriber = Callable[[dict], None]


class WorkspacePool:
    """Persistent multi-workspace pool with auto failover.

    The on-disk schema is documented in prd.md §3. The instance owns a
    threading lock — all reads / writes serialize on it, mirroring
    :class:`autoteam.account_state.StateMachine`.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        fail_threshold: int | None = None,
    ) -> None:
        self._path: Path = Path(path) if path else DEFAULT_POOL_FILE
        self._lock = threading.Lock()
        self._subscribers: list[PoolSubscriber] = []
        self._fail_threshold = fail_threshold if fail_threshold is not None else _default_fail_threshold()

    # ---------------------------------------------------------------- config
    @property
    def path(self) -> Path:
        return self._path

    @property
    def fail_threshold(self) -> int:
        return self._fail_threshold

    def set_fail_threshold(self, threshold: int) -> None:
        if not isinstance(threshold, int) or threshold <= 0:
            raise ValueError("fail_threshold must be a positive int")
        self._fail_threshold = threshold

    # ----------------------------------------------------------- event bus
    def subscribe(self, callback: PoolSubscriber) -> PoolSubscriber:
        if not callable(callback):
            raise TypeError("subscribe expects a callable")
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)
        return callback

    def unsubscribe(self, callback: PoolSubscriber) -> bool:
        with self._lock:
            try:
                self._subscribers.remove(callback)
                return True
            except ValueError:
                return False

    def _publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(event)
            except Exception:
                logger.exception("workspace_pool subscriber %r raised on event %s", cb, event)

    # ---------------------------------------------------------------- I/O
    def _load_raw(self) -> dict:
        """Load on-disk pool. Auto-seed from state.json on missing file (I4)."""
        if not self._path.exists():
            return self._seed_from_admin_state()
        try:
            text = self._path.read_text(encoding="utf-8").strip()
        except Exception as exc:
            logger.warning("[workspace_pool] read failed: %s — treating as empty", exc)
            return _empty_doc()
        if not text:
            return _empty_doc()
        try:
            data = json.loads(text)
        except Exception as exc:
            logger.warning("[workspace_pool] json parse failed: %s — treating as empty", exc)
            return _empty_doc()
        if not isinstance(data, dict):
            return _empty_doc()
        if data.get("schema_version") != SCHEMA_VERSION:
            # forward-incompatible / legacy → discard, will re-seed on next register
            logger.warning(
                "[workspace_pool] schema_version mismatch (got %r, expect %d) — discard",
                data.get("schema_version"),
                SCHEMA_VERSION,
            )
            return _empty_doc()
        ws = data.get("workspaces")
        if not isinstance(ws, list):
            ws = []
        # normalize each row
        norm_ws: list[dict] = []
        for row in ws:
            n = _normalize_row(row)
            if n is not None:
                norm_ws.append(n)
        active = data.get("active") or None
        return {
            "schema_version": SCHEMA_VERSION,
            "active": active if isinstance(active, str) else None,
            "workspaces": norm_ws,
        }

    def _seed_from_admin_state(self) -> dict:
        """I4 — single-workspace backwards compat.

        On first read with no workspaces.json, attempt to seed a single
        workspace from state.json (admin_email + account_id). Empty state
        produces an empty doc (no workspaces). Never raises.
        """
        try:
            from autoteam.admin_state import load_admin_state  # late import to break cycle
            st = load_admin_state() or {}
        except Exception as exc:
            logger.warning("[workspace_pool] seed: cannot read admin_state: %s", exc)
            return _empty_doc()
        admin_email = (st.get("email") or "").strip()
        account_id = (st.get("account_id") or "").strip()
        if not admin_email or not account_id:
            # nothing to seed — fresh install
            return _empty_doc()
        ws_id = f"ws-{account_id}"
        now = time.time()
        row = {
            "id": ws_id,
            "admin_email": admin_email,
            "account_id": account_id,
            "tier": TIER_ACTIVE,
            "status": STATUS_UNKNOWN,
            "fail_count": 0,
            "last_check_ts": None,
            "registered_at": now,
            "transition_log": [
                {"ts": now, "from": None, "to": TIER_ACTIVE, "reason": "seed_from_admin_state"},
            ],
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "active": ws_id,
            "workspaces": [row],
        }

    def _save_raw(self, doc: dict) -> None:
        """I3 — atomic write with .bak rollback."""
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        bak = path.with_suffix(path.suffix + ".bak")
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")
        had_snapshot = False
        if path.exists():
            try:
                shutil.copy2(path, bak)
                had_snapshot = True
            except OSError as exc:
                logger.warning("[workspace_pool] snapshot failed %s -> %s: %s", path, bak, exc)
        try:
            with open(tmp, "wb") as fp:
                fp.write(payload)
                fp.flush()
                try:
                    os.fsync(fp.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except OSError:
            logger.exception("[workspace_pool] write failed for %s, attempting rollback", path)
            if had_snapshot and bak.exists():
                try:
                    os.replace(bak, path)
                except OSError:
                    logger.exception("[workspace_pool] rollback failed for %s", path)
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise
        else:
            try:
                os.chmod(path, POOL_FILE_MODE)
            except OSError:
                pass
            if had_snapshot and bak.exists():
                try:
                    bak.unlink()
                except OSError:
                    logger.warning("[workspace_pool] cannot delete .bak %s", bak)

    # ---------------------------------------------------------- public API

    def list_all(self) -> list[dict]:
        """Return all workspace rows (deep copy, safe to mutate)."""
        with self._lock:
            doc = self._load_raw()
        return [dict(row) | {"transition_log": list(row.get("transition_log") or [])}
                for row in doc["workspaces"]]

    def get(self, workspace_id: str) -> dict | None:
        """Return one workspace by id (deep copy) or None."""
        if not workspace_id:
            return None
        with self._lock:
            doc = self._load_raw()
        for row in doc["workspaces"]:
            if row.get("id") == workspace_id:
                return dict(row) | {"transition_log": list(row.get("transition_log") or [])}
        return None

    def get_active(self) -> dict | None:
        """Return the current active workspace (or None if pool empty)."""
        with self._lock:
            doc = self._load_raw()
        active_id = doc.get("active")
        if not active_id:
            return None
        for row in doc["workspaces"]:
            if row.get("id") == active_id and row.get("tier") == TIER_ACTIVE:
                return dict(row) | {"transition_log": list(row.get("transition_log") or [])}
        return None

    def register(
        self,
        workspace_id: str,
        admin_email: str,
        account_id: str,
        tier: str = TIER_WARM,
    ) -> dict:
        """Register a new workspace. If id exists, raise ValueError.

        - tier=active is allowed but will demote any prior active to cold
          (I1 — at most one active).
        - First registration with no active in pool auto-promotes (regardless
          of supplied tier) so single-workspace fresh installs work.
        """
        if not workspace_id or not isinstance(workspace_id, str):
            raise ValueError("workspace_id required")
        if not admin_email or not isinstance(admin_email, str):
            raise ValueError("admin_email required")
        if not account_id or not isinstance(account_id, str):
            raise ValueError("account_id required")
        if tier not in _VALID_TIERS:
            raise ValueError(f"tier must be one of {_VALID_TIERS!r}")

        events: list[dict] = []
        with self._lock:
            doc = self._load_raw()
            for row in doc["workspaces"]:
                if row.get("id") == workspace_id:
                    raise ValueError(f"workspace_id {workspace_id!r} already registered")
            now = time.time()
            target_tier = tier
            # auto-promote if pool is empty / has no active
            if doc.get("active") is None and not any(r.get("tier") == TIER_ACTIVE for r in doc["workspaces"]):
                target_tier = TIER_ACTIVE

            new_row = {
                "id": workspace_id,
                "admin_email": admin_email,
                "account_id": account_id,
                "tier": target_tier,
                "status": STATUS_UNKNOWN,
                "fail_count": 0,
                "last_check_ts": None,
                "registered_at": now,
                "transition_log": [
                    {"ts": now, "from": None, "to": target_tier, "reason": "register"},
                ],
            }

            if target_tier == TIER_ACTIVE:
                # I1 — demote any other active row
                prior_active_id = doc.get("active")
                for row in doc["workspaces"]:
                    if row.get("tier") == TIER_ACTIVE:
                        _append_transition(row, TIER_ACTIVE, TIER_COLD, "demote_on_register_active", now)
                        row["tier"] = TIER_COLD
                doc["active"] = workspace_id
                if prior_active_id and prior_active_id != workspace_id:
                    events.append({
                        "type": "demoted",
                        "workspace_id": prior_active_id,
                        "reason": "register_active",
                        "ts": now,
                    })

            doc["workspaces"].append(new_row)
            self._save_raw(doc)
            events.append({
                "type": "registered",
                "workspace_id": workspace_id,
                "tier": target_tier,
                "ts": now,
            })
            snapshot = dict(new_row) | {"transition_log": list(new_row["transition_log"])}

        for ev in events:
            self._publish(ev)
        return snapshot

    def set_active(self, workspace_id: str) -> dict:
        """Promote ``workspace_id`` to active; demote any prior active to cold."""
        if not workspace_id:
            raise ValueError("workspace_id required")
        events: list[dict] = []
        with self._lock:
            doc = self._load_raw()
            target = None
            for row in doc["workspaces"]:
                if row.get("id") == workspace_id:
                    target = row
                    break
            if target is None:
                raise KeyError(f"workspace_id {workspace_id!r} not found")
            now = time.time()
            prior_active_id = doc.get("active")
            if target.get("tier") == TIER_ACTIVE and prior_active_id == workspace_id:
                # already active — short-circuit but still touch last_check_ts? no, idempotent no-op
                snapshot = dict(target) | {"transition_log": list(target.get("transition_log") or [])}
                return snapshot
            for row in doc["workspaces"]:
                if row is target:
                    continue
                if row.get("tier") == TIER_ACTIVE:
                    _append_transition(row, TIER_ACTIVE, TIER_COLD, "demote_on_set_active", now)
                    row["tier"] = TIER_COLD
                    events.append({
                        "type": "demoted",
                        "workspace_id": row.get("id"),
                        "reason": "set_active",
                        "ts": now,
                    })
            _append_transition(target, target.get("tier"), TIER_ACTIVE, "set_active", now)
            target["tier"] = TIER_ACTIVE
            target["status"] = STATUS_UNKNOWN  # reset; next probe sets healthy/unhealthy
            target["fail_count"] = 0
            doc["active"] = workspace_id
            self._save_raw(doc)
            events.append({
                "type": "promoted",
                "workspace_id": workspace_id,
                "reason": "set_active",
                "ts": now,
            })
            snapshot = dict(target) | {"transition_log": list(target["transition_log"])}

        for ev in events:
            self._publish(ev)
        return snapshot

    def mark_unhealthy(
        self,
        workspace_id: str,
        reason: str,
        *,
        force_failover: bool = False,
    ) -> dict | None:
        """Mark a workspace unhealthy. If active and fail_count >= threshold,
        auto-promote a warm/cold healthy candidate (I2).

        Returns the (possibly newly-promoted) active workspace snapshot, or
        None if the pool ended up with no active candidate.
        """
        if not workspace_id:
            raise ValueError("workspace_id required")
        events: list[dict] = []
        with self._lock:
            doc = self._load_raw()
            target = None
            for row in doc["workspaces"]:
                if row.get("id") == workspace_id:
                    target = row
                    break
            if target is None:
                raise KeyError(f"workspace_id {workspace_id!r} not found")

            now = time.time()
            prev_status = target.get("status")
            target["fail_count"] = int(target.get("fail_count") or 0) + 1
            target["status"] = STATUS_UNHEALTHY
            target["last_check_ts"] = now
            _append_transition(
                target,
                prev_status,
                STATUS_UNHEALTHY,
                f"mark_unhealthy:{reason}",
                now,
                kind="status",
                extra={"fail_count": target["fail_count"]},
            )
            events.append({
                "type": "marked_unhealthy",
                "workspace_id": workspace_id,
                "reason": reason,
                "fail_count": target["fail_count"],
                "ts": now,
            })

            should_failover = (
                target.get("tier") == TIER_ACTIVE
                and (force_failover or target["fail_count"] >= self._fail_threshold)
            )
            promoted_id: str | None = None
            if should_failover:
                # demote current active → cold
                _append_transition(target, TIER_ACTIVE, TIER_COLD, "failover_demote", now)
                target["tier"] = TIER_COLD
                if doc.get("active") == workspace_id:
                    doc["active"] = None
                # pick promotion candidate
                candidate = _pick_failover_candidate(doc["workspaces"])
                if candidate is not None:
                    _append_transition(
                        candidate,
                        candidate.get("tier"),
                        TIER_ACTIVE,
                        "failover_promote",
                        now,
                    )
                    candidate["tier"] = TIER_ACTIVE
                    candidate["status"] = STATUS_UNKNOWN
                    candidate["fail_count"] = 0
                    doc["active"] = candidate.get("id")
                    promoted_id = candidate.get("id")
                    events.append({
                        "type": "promoted",
                        "workspace_id": promoted_id,
                        "reason": "failover",
                        "ts": now,
                    })
                else:
                    events.append({
                        "type": "no_failover_candidate",
                        "workspace_id": workspace_id,
                        "ts": now,
                    })

            self._save_raw(doc)
            new_active = self._active_row(doc)
            snapshot = (
                dict(new_active) | {"transition_log": list(new_active.get("transition_log") or [])}
                if new_active else None
            )

        for ev in events:
            self._publish(ev)
        return snapshot

    def mark_healthy(self, workspace_id: str) -> dict | None:
        """Reset fail_count to 0 + status=healthy on a successful probe.

        Idempotent. Useful for the master_health success path so that
        intermittent failures don't accumulate to threshold.
        """
        if not workspace_id:
            raise ValueError("workspace_id required")
        events: list[dict] = []
        with self._lock:
            doc = self._load_raw()
            target = None
            for row in doc["workspaces"]:
                if row.get("id") == workspace_id:
                    target = row
                    break
            if target is None:
                raise KeyError(f"workspace_id {workspace_id!r} not found")
            now = time.time()
            prev_status = target.get("status")
            prev_fail = int(target.get("fail_count") or 0)
            target["fail_count"] = 0
            target["status"] = STATUS_HEALTHY
            target["last_check_ts"] = now
            if prev_status != STATUS_HEALTHY or prev_fail != 0:
                _append_transition(
                    target,
                    prev_status,
                    STATUS_HEALTHY,
                    "mark_healthy",
                    now,
                    kind="status",
                    extra={"prev_fail_count": prev_fail},
                )
                events.append({
                    "type": "marked_healthy",
                    "workspace_id": workspace_id,
                    "ts": now,
                })
            self._save_raw(doc)
            snapshot = dict(target) | {"transition_log": list(target.get("transition_log") or [])}
        for ev in events:
            self._publish(ev)
        return snapshot

    # --------------------------------------------------------------- helpers
    def _active_row(self, doc: dict) -> dict | None:
        active_id = doc.get("active")
        if not active_id:
            return None
        for row in doc["workspaces"]:
            if row.get("id") == active_id:
                return row
        return None

    def reset(self) -> None:
        """Test helper — wipe the pool file. Production code should not call."""
        with self._lock:
            try:
                if self._path.exists():
                    self._path.unlink()
            except OSError as exc:
                logger.warning("[workspace_pool] reset failed: %s", exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _empty_doc() -> dict:
    return {"schema_version": SCHEMA_VERSION, "active": None, "workspaces": []}


def _normalize_row(row: Any) -> dict | None:
    if not isinstance(row, dict):
        return None
    rid = row.get("id")
    if not isinstance(rid, str) or not rid:
        return None
    tier = row.get("tier")
    if tier not in _VALID_TIERS:
        tier = TIER_WARM
    status = row.get("status")
    if status not in _VALID_STATUSES:
        status = STATUS_UNKNOWN
    log = row.get("transition_log")
    if not isinstance(log, list):
        log = []
    fail_count = row.get("fail_count")
    try:
        fail_count = int(fail_count or 0)
    except (TypeError, ValueError):
        fail_count = 0
    return {
        "id": rid,
        "admin_email": str(row.get("admin_email") or ""),
        "account_id": str(row.get("account_id") or ""),
        "tier": tier,
        "status": status,
        "fail_count": fail_count,
        "last_check_ts": row.get("last_check_ts"),
        "registered_at": row.get("registered_at"),
        "transition_log": list(log),
    }


def _append_transition(
    row: dict,
    from_value: Any,
    to_value: Any,
    reason: str,
    ts: float,
    *,
    kind: str = "tier",
    extra: dict | None = None,
) -> None:
    entry: dict = {
        "ts": ts,
        "kind": kind,
        "from": from_value,
        "to": to_value,
        "reason": reason,
    }
    if extra:
        entry["extra"] = extra
    log = row.get("transition_log")
    if not isinstance(log, list):
        log = []
        row["transition_log"] = log
    log.append(entry)


def _pick_failover_candidate(workspaces: list[dict]) -> dict | None:
    """Pick best workspace to promote to active.

    Order:
      1. tier=warm AND status != unhealthy, sorted by registered_at asc (oldest first)
      2. tier=cold AND status == healthy, sorted by registered_at asc
      3. tier=warm regardless of status (last resort), oldest first
      4. None
    """
    warm_healthy = [w for w in workspaces if w.get("tier") == TIER_WARM and w.get("status") != STATUS_UNHEALTHY]
    if warm_healthy:
        warm_healthy.sort(key=lambda w: w.get("registered_at") or 0)
        return warm_healthy[0]
    cold_healthy = [w for w in workspaces if w.get("tier") == TIER_COLD and w.get("status") == STATUS_HEALTHY]
    if cold_healthy:
        cold_healthy.sort(key=lambda w: w.get("registered_at") or 0)
        return cold_healthy[0]
    warm_any = [w for w in workspaces if w.get("tier") == TIER_WARM]
    if warm_any:
        warm_any.sort(key=lambda w: w.get("registered_at") or 0)
        return warm_any[0]
    return None


# ---------------------------------------------------------------------------
# Module-level default pool
# ---------------------------------------------------------------------------
default_pool = WorkspacePool()


def get_default_pool() -> WorkspacePool:
    return default_pool


__all__ = [
    "DEFAULT_POOL_FILE",
    "SCHEMA_VERSION",
    "STATUS_HEALTHY",
    "STATUS_UNHEALTHY",
    "STATUS_UNKNOWN",
    "TIER_ACTIVE",
    "TIER_COLD",
    "TIER_WARM",
    "WorkspacePool",
    "default_pool",
    "get_default_pool",
]
